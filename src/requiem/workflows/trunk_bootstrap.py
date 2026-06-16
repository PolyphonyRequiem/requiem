"""trunk_bootstrap workflow — idempotently ensure the ``feature/<root>``
integration trunk exists *before* the fan-out executor delivers leaves.

This is ADR-0018 build-sequence **step 1** for v0 non-negotiable #7. On the live
Hermes path each implementable leaf is delivered onto an ``impl/<root>-<item>``
branch and requiem itself opens the ``impl/<root>-<item>`` → ``feature/<root>``
leaf PRs (``leaf_pr.py``, step 2) and the ``feature/<root>`` → ``main``
integration PR (``feature_pr.py``, step 3). All of that presupposes the trunk
exists. Creating it is the job here.

Why remote, not local git
-------------------------
The toolbelt git client (``toolbelt.GitClient``) is **read-only** (a single
``show`` method) — requiem has no local branch-create/checkout primitive. Rather
than introduce a whole git-mutation client + working-tree management just to cut
one branch, ADR-0018 (ratified 2026-06-07) makes the driver bootstrap the trunk
**remotely** via the GitHub refs API, confined to the narrow
``gh.branch_sha`` / ``gh.ensure_branch_ref`` capability. No working tree, fully
idempotent, fits the "driver owns trunk topology" ownership split.

Shape (mirrors ``leaf_pr.py`` / ``feature_pr.py``)::

    start
      → ensure_trunk   (script · GET base sha → ensure feature/<root> ref)
      → end_success

Design invariants (ADR-0018):

* **Never move an existing trunk.** ``ensure_branch_ref`` creates the ref only
  when absent; an already-present ``feature/<root>`` is left exactly as-is, so a
  re-run never rewinds a trunk that earlier leaves have advanced.
* **Fail closed on a missing base.** If the base branch (default ``main``)
  doesn't exist we cannot pick a source SHA — that is a typed PermanentFailure,
  not a silent "create off nothing".
* **Dry-run is read-only.** A dry-run still GETs the base SHA and probes whether
  the trunk exists (to report "would create" vs "already exists"), but performs
  no ref mutation.

.. note::

   **Pending live validation.** The *contract* here (idempotent create, no
   force-move, fail-closed on a missing base) is unit-tested against a fake refs
   store, but the live behaviour this enables — and the worktree-from-HEAD drift
   wrinkle in ADR-0018's "Open refinement" (a leaf branch cut from old ``main``
   conflicting with a trunk later leaves have advanced) — cannot be exercised by
   unit fakes. Driver wiring (build-sequence step 4) stays gated on a live
   Hermes loop.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from requiem import branch_model
from requiem.clients.azuredevops import AdoClientError, AdoNotFoundError
from requiem.clients.gh import GhClientError, GhNotFoundError
from requiem.dsl import AgentRegistry, VerbRegistry, Workflow, WorkflowBuilder
from requiem.kernel import Engine
from requiem.outcomes import PermanentFailure, Success
from requiem.toolbelt import FakeFileClient, RealGitClient, Toolbelt

# ADR-0024 step 4: the trunk-topology workflows are platform-neutral via
# the RepoPlatform Protocol; the only place that still differs is the
# error taxonomy (GhClient raises Gh* errors, AdoClient raises Ado* errors).
# Tuples that the workflow except-clauses unify over so we don't have to
# special-case one platform.
_REPO_NOT_FOUND_ERRORS: tuple[type[Exception], ...] = (
    GhNotFoundError, AdoNotFoundError,
)
_REPO_CLIENT_ERRORS: tuple[type[Exception], ...] = (
    GhClientError, AdoClientError,
)

# ---- error kinds --------------------------------------------------------

EK_BASE_MISSING = "trunk_bootstrap.base_missing"


# ---- public dataclasses -------------------------------------------------


@dataclass(slots=True)
class TrunkBootstrapInputs:
    """Everything trunk_bootstrap needs, stamped once at start_run."""

    root_item_id: int
    repo: str
    base_branch: str = "main"
    dry_run: bool = True

    @property
    def trunk_branch(self) -> str:
        return branch_model.feature_trunk(self.root_item_id)


@dataclass(frozen=True, slots=True)
class TrunkBootstrapResult:
    """Programmatic projection of a trunk_bootstrap run."""

    root_item_id: int
    verdict: Literal["created", "exists", "previewed", "failed"]
    trunk_branch: str
    base_branch: str
    base_sha: str | None
    dry_run: bool


# ---- in-memory fake (CLI demo + tests duck-type this) -------------------


@dataclass
class _DemoGhClient:
    """In-memory GhClient stand-in: an addressable refs store.

    ``refs`` maps a branch name to the SHA it points at. ``branch_sha`` /
    ``ensure_branch_ref`` mirror the real client's async signatures.
    """

    refs: dict[str, str] = field(default_factory=lambda: {"main": "basesha000"})
    created: list[str] = field(default_factory=list)
    raise_on_sha: Exception | None = None

    async def branch_sha(self, repo: str, branch: str) -> str:
        if self.raise_on_sha is not None:
            raise self.raise_on_sha
        try:
            return self.refs[branch]
        except KeyError as e:
            raise GhNotFoundError(
                f"no such branch {branch}", exit_code=1, stderr="404", argv=(),
            ) from e

    async def ensure_branch_ref(self, repo: str, branch: str, source_sha: str) -> bool:
        if branch in self.refs:
            return False
        self.refs[branch] = source_sha
        self.created.append(branch)
        return True


# ---- verb registry ------------------------------------------------------


def build_verb_registry(inputs: TrunkBootstrapInputs) -> VerbRegistry:
    verbs = VerbRegistry()
    trunk = inputs.trunk_branch

    def _require_repo_platform(ctx):
        # ADR-0024: workflows prefer the platform-neutral toolbelt.repo;
        # fall back to toolbelt.gh so older callers that haven't migrated
        # keep working (GhClient IS a RepoPlatform).
        repo_client = ctx.toolbelt.repo or ctx.toolbelt.gh
        if repo_client is None:
            return PermanentFailure(
                error_kind="toolbelt.missing_client",
                message=(
                    "trunk_bootstrap workflow requires a RepoPlatform "
                    "(set toolbelt.repo, or toolbelt.gh for back-compat)"
                ),
            )
        return repo_client

    @verbs.register("start_run")
    def _start(ctx):
        return Success(value={
            "intent": "trunk_bootstrap",
            "root_item_id": inputs.root_item_id,
            "repo": inputs.repo,
            "trunk_branch": trunk,
            "base_branch": inputs.base_branch,
            "dry_run": inputs.dry_run,
        })

    @verbs.register("ensure_trunk")
    async def _ensure(ctx):
        repo_client = _require_repo_platform(ctx)
        if isinstance(repo_client, PermanentFailure):
            return repo_client

        # The base SHA is needed in every mode: it is the source for a real
        # create and the "would create from" report in dry-run. A missing base
        # is fail-closed — there is nothing to branch from.
        try:
            base_sha = await repo_client.branch_sha(inputs.repo, inputs.base_branch)
        except _REPO_NOT_FOUND_ERRORS as e:
            return PermanentFailure(
                error_kind=EK_BASE_MISSING,
                message=(
                    f"base branch {inputs.base_branch!r} not found — cannot "
                    f"bootstrap {trunk!r} off a base that does not exist"
                ),
                details={"base_branch": inputs.base_branch, "error": str(e)},
            )
        except _REPO_CLIENT_ERRORS as e:
            return PermanentFailure(
                error_kind="repo.base_sha_failed",
                message=f"repo client (base sha) failed: {e}",
                details={"base_branch": inputs.base_branch, "error": str(e)},
            )

        if inputs.dry_run:
            # Read-only probe: does the trunk already exist?
            try:
                await repo_client.branch_sha(inputs.repo, trunk)
                exists = True
            except _REPO_NOT_FOUND_ERRORS:
                exists = False
            except _REPO_CLIENT_ERRORS as e:
                return PermanentFailure(
                    error_kind="repo.trunk_probe_failed",
                    message=f"repo client (trunk probe) failed: {e}",
                    details={"trunk": trunk, "error": str(e)},
                )
            return Success(value={
                "trunk_branch": trunk, "base_branch": inputs.base_branch,
                "base_sha": base_sha, "created": False, "exists": exists,
                "dry_run": True,
            })

        try:
            created = await repo_client.ensure_branch_ref(inputs.repo, trunk, base_sha)
        except _REPO_CLIENT_ERRORS as e:
            return PermanentFailure(
                error_kind="repo.ensure_ref_failed",
                message=f"repo client (ensure ref) failed: {e}",
                details={"trunk": trunk, "base_sha": base_sha, "error": str(e)},
            )
        return Success(
            value={
                "trunk_branch": trunk, "base_branch": inputs.base_branch,
                "base_sha": base_sha, "created": created, "exists": not created,
                "dry_run": False,
            },
            inspected_artifacts=(f"git:ref:{inputs.repo}#{trunk}",),
        )

    return verbs


# ---- workflow topology --------------------------------------------------


def build_workflow() -> Workflow:
    return (
        WorkflowBuilder(
            "trunk-bootstrap", module="requiem.workflows.trunk_bootstrap", version="0.1",
        )
            .entry("start")
            .script("start", verb="start_run")
                .edge("start", on="success", to="ensure_trunk")
            .script("ensure_trunk", verb="ensure_trunk")
                .edge("ensure_trunk", on="success", to="end_success")
                .edge("ensure_trunk", on="permanent_failure", to="end_failed")
            .terminate("end_success", disposition="completed")
            .terminate("end_failed", disposition="failed")
            .humanize({
                "start": "Starting trunk bootstrap",
                "ensure_trunk": "Ensured integration trunk",
                "end_success": "Trunk ready for fan-out",
                "end_failed": "Trunk bootstrap failed",
            })
            .build()
    )


# ---- engine construction ------------------------------------------------


def _demo_inputs_and_toolbelt(
    root_item_id: int,
) -> tuple[TrunkBootstrapInputs, Toolbelt]:
    inputs = TrunkBootstrapInputs(
        root_item_id=root_item_id, repo="Owner/Repo", dry_run=True,
    )
    toolbelt = Toolbelt(
        git=RealGitClient(),
        files=FakeFileClient({}),
        gh=_DemoGhClient(),  # type: ignore[arg-type]
        fs=None,
        twig=None,
    )
    return inputs, toolbelt


def build_engine(
    log_dir: Path,
    *,
    inputs: TrunkBootstrapInputs | None = None,
    toolbelt: Toolbelt | None = None,
    gate_handler=None,
) -> Engine:
    """Build an Engine for ``trunk-bootstrap``.

    Zero-arg (``build_engine(log_dir)``) ships a canned, dry-run,
    side-effect-free demo against an in-memory refs store.

    Environment overrides (read once here, only when ``inputs`` is not given):

    * ``REQUIEM_TRUNK_BOOTSTRAP_ROOT`` — root work item id
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    if inputs is None:
        env_root = os.environ.get("REQUIEM_TRUNK_BOOTSTRAP_ROOT")
        root_item_id = int(env_root) if env_root else 4242
        inputs, demo_toolbelt = _demo_inputs_and_toolbelt(root_item_id)
        toolbelt = toolbelt or demo_toolbelt

    return Engine(
        workflow=build_workflow(),
        verbs=build_verb_registry(inputs),
        agents=AgentRegistry(),
        provider=None,
        toolbelt=toolbelt or Toolbelt.real(),
        log_dir=log_dir,
        gate_handler=gate_handler,
    )


# ---- result projection --------------------------------------------------


def trunk_bootstrap_result(completed: dict, final_node: str) -> TrunkBootstrapResult:
    start = (completed.get("start") or {}).get("value") or {}
    ensure = (completed.get("ensure_trunk") or {}).get("value") or {}
    dry_run = bool(start.get("dry_run"))
    if final_node == "end_success":
        if dry_run:
            verdict: Literal["created", "exists", "previewed", "failed"] = "previewed"
        elif ensure.get("created"):
            verdict = "created"
        else:
            verdict = "exists"
    else:
        verdict = "failed"
    base_sha = ensure.get("base_sha")
    return TrunkBootstrapResult(
        root_item_id=int(start.get("root_item_id") or 0),
        verdict=verdict,
        trunk_branch=str(start.get("trunk_branch") or ""),
        base_branch=str(start.get("base_branch") or ""),
        base_sha=str(base_sha) if base_sha else None,
        dry_run=dry_run,
    )


def verdict_card(completed: dict) -> str | None:
    start = (completed.get("start") or {}).get("value")
    if not start:
        return None
    ensure = (completed.get("ensure_trunk") or {}).get("value") or {}
    trunk = start.get("trunk_branch")
    if start.get("dry_run"):
        state = "already exists" if ensure.get("exists") else "would create"
        head = "  ◐ Dry run (preview)"
        tail = f"{trunk} — {state}"
    elif ensure.get("created"):
        head = "  ✓ Trunk created"
        tail = f"{trunk} @ {str(ensure.get('base_sha'))[:8]}"
    else:
        head = "  ✓ Trunk ready"
        tail = f"{trunk} already existed"
    return f"{head}\n  AB#{start.get('root_item_id')} — {tail}"


# ---- __main__ -----------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Ensure the feature/<root> integration trunk exists."
    )
    p.add_argument("--root", type=int, default=None, help="root work item id (demo)")
    p.add_argument("--run-id", default="trunk-bootstrap")
    p.add_argument("--log-dir", type=Path, default=Path("runs"))
    return p


async def _amain(argv: list[str]) -> int:
    args = _build_arg_parser().parse_args(argv)
    if args.root is not None:
        os.environ.setdefault("REQUIEM_TRUNK_BOOTSTRAP_ROOT", str(args.root))
    engine = build_engine(args.log_dir)
    result = await engine.run(args.run_id)
    completed = {}
    try:
        from requiem.workflows.planning import completed_from_log

        completed = completed_from_log(engine.log_path(args.run_id))
    except Exception:  # pragma: no cover - best-effort card
        pass
    card = verdict_card(completed)
    if card:
        print(card)
    return 0 if getattr(result, "final_node", "") == "end_success" else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv if argv is not None else sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
