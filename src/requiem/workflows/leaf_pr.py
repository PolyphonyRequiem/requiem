"""leaf_pr workflow — open (or reuse) each ``impl/<root>-<item>`` →
``feature/<root>`` leaf PR, idempotently, and emit the
``{leaf_id: pr_number}`` map the ``feature_pr`` gate consumes.

This is the *requiem-owns-topology* half of v0 non-negotiable #7 on the live
(Hermes) delivery path (ADR-0018 Option C, build-sequence step 2). The fan-out
executor (``kanban_executor.py``, ADR-0014) delivers each implementable leaf
onto an ``impl/<root>-<item>`` branch but **cannot** target a PR base —
``hermes kanban create`` has ``--branch`` only, no ``--base`` (ADR-0018). So
requiem opens the leaf PRs itself: ``head = impl/<root>-<item>``,
``base = feature/<root>``. The numbers it returns are exactly what
``feature_pr`` reads back (via ``gh.pr_view``) to gate trunk readiness — hence
this module re-exports ``feature_pr.LeafPr`` as its output element, making the
hand-off type-explicit.

Shape (mirrors ``feature_pr.py``'s open-and-handoff)::

    start
      → open_leaf_prs   (script · per leaf: reuse-open-or-create, fail closed)
      → end_success

Design invariants (ADR-0018):

* **Narrow mutation only.** This workflow opens leaf PRs with the
  already-approved ``gh pr create`` surface — it does *not* create branches or
  the trunk. ``feature/<root>`` is bootstrapped earlier (a separate, ratified
  step); if it is absent, ``gh pr create`` fails and we surface that to a human
  rather than papering over missing topology.
* **Fail closed.** An existing leaf PR on the *wrong base*, an *ambiguous* set
  of open PRs for one head, or a ``gh`` create/search failure escalates to a
  human (or a typed PermanentFailure) — we never half-open a partial leaf set
  and report success.
* **Idempotent (pre-merge window).** A re-run reuses an already-open leaf PR
  whose head+base match. Once a leaf PR has *merged*, the authoritative number
  lives in the driver's event log (which persists this map), not in a re-search
  — a default ``gh pr list`` is open-only and cannot see merged PRs. So this
  workflow's reuse path is the pre-merge window; the driver owns post-merge
  re-derivation, consistent with how requiem persists every decision.
* **Dry-run previews.** ``verify`` (search/reuse) always runs; only
  ``gh pr create`` is gated on ``dry_run``. A not-yet-opened leaf in dry-run is
  reported as "would_open" with ``pr_number is None``.

.. note::

   **Pending live Hermes validation.** This workflow's *contract* (idempotent
   reuse, fail-closed, correct head/base) is unit-tested, but the live behaviour
   it presupposes — that the Hermes worker has pushed ``impl/<root>-<item>`` to
   the remote, and that a leaf branch cut from repo HEAD still merges cleanly
   into a trunk that earlier leaves have advanced (the drift wrinkle in
   ADR-0018's "Open refinement") — cannot be exercised by unit fakes. Treat
   driver wiring (build-sequence step 4) as gated on a live loop.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from requiem import branch_model
from requiem.clients.azuredevops import AdoClientError
from requiem.clients.gh import GhClientError, GhPullRequest
from requiem.dsl import AgentRegistry, VerbRegistry, Workflow, WorkflowBuilder
from requiem.kernel import Engine
from requiem.outcomes import NeedsHuman, PermanentFailure, Success
from requiem.toolbelt import FakeFileClient, RealGitClient, Toolbelt
from requiem.workflows.feature_pr import LeafPr  # the hand-off element type

# ADR-0024 step 4: a tuple covering both platforms' typed client errors.
# Lets the workflow except-clauses stay platform-agnostic.
_REPO_CLIENT_ERRORS: tuple[type[Exception], ...] = (
    GhClientError, AdoClientError,
)

# ---- error kinds --------------------------------------------------------

EK_NO_LEAVES = "leaf_pr.no_leaves"

# ---- gates --------------------------------------------------------------

GATE_LEAF_PR_CONFLICT = "leaf_pr_conflict"


# ---- public dataclasses -------------------------------------------------


@dataclass(slots=True)
class LeafPrInputs:
    """Everything the leaf_pr workflow needs, stamped once at start_run.

    ``leaf_ids`` are the *delivered* implementable leaves (bare ids); for each
    we form ``head = impl/<root>-<item>`` and ``base = feature/<root>`` via
    :mod:`requiem.branch_model`.
    """

    root_item_id: int
    repo: str
    leaf_ids: tuple[str, ...]
    dry_run: bool = True

    @property
    def trunk_branch(self) -> str:
        return branch_model.feature_trunk(self.root_item_id)

    def impl_branch_for(self, leaf_id: str) -> str:
        return branch_model.impl_branch(self.root_item_id, leaf_id)


@dataclass(frozen=True, slots=True)
class LeafPrResult:
    """Programmatic projection of a leaf_pr run.

    ``leaves`` is the ``{leaf_id: pr_number}`` map (as ``LeafPr`` elements) that
    ``feature_pr`` consumes; ``pr_number is None`` for any leaf not opened (a
    dry-run "would open", or a leaf that hit a conflict).
    """

    root_item_id: int
    verdict: Literal["opened", "previewed", "needs_human", "failed"]
    trunk_branch: str
    leaves: tuple[LeafPr, ...]
    leaves_total: int
    opened: int
    reused: int
    dry_run: bool


# ---- in-memory fakes (CLI demo + tests duck-type these) -----------------


@dataclass
class _DemoGhClient:
    """In-memory GhClient stand-in (demo / unit tests)."""

    next_pr_number: int = 9000
    open_prs: list[GhPullRequest] = field(default_factory=list)
    created: list[dict[str, Any]] = field(default_factory=list)
    raise_on_search: Exception | None = None
    raise_on_create: Exception | None = None

    async def pr_search(self, repo: str, query: str, limit: int = 30):
        if self.raise_on_search is not None:
            raise self.raise_on_search
        return list(self.open_prs)

    async def find_open_pr_for_branch(
        self, repo: str, *, head: str, limit: int = 30
    ):
        # ADR-0024 step 4 RepoPlatform method — filter open_prs by head.
        if self.raise_on_search is not None:
            raise self.raise_on_search
        return [pr for pr in self.open_prs if pr.head == head][:limit]

    async def pr_create(self, repo: str, *, title: str, body: str, head: str, base: str):
        if self.raise_on_create is not None:
            raise self.raise_on_create
        n = self.next_pr_number
        self.next_pr_number += 1
        url = f"https://github.com/{repo}/pull/{n}"
        pr = GhPullRequest(
            number=n, title=title, state="OPEN", merged=False, merged_at=None,
            head=head, base=base, url=url, raw={"number": n, "url": url},
        )
        self.created.append({"title": title, "head": head, "base": base, "url": url})
        self.open_prs.append(pr)  # so a re-run's pr_search reuses it
        return pr


# ---- verb registry ------------------------------------------------------


def build_verb_registry(inputs: LeafPrInputs) -> VerbRegistry:
    verbs = VerbRegistry()
    trunk = inputs.trunk_branch

    def _require_repo_platform(ctx):
        # ADR-0024: prefer toolbelt.repo; fall back to toolbelt.gh.
        repo_client = ctx.toolbelt.repo or ctx.toolbelt.gh
        if repo_client is None:
            return PermanentFailure(
                error_kind="toolbelt.missing_client",
                message=(
                    "leaf_pr workflow requires a RepoPlatform "
                    "(set toolbelt.repo, or toolbelt.gh for back-compat)"
                ),
            )
        return repo_client

    # ---- start --------------------------------------------------------

    @verbs.register("start_run")
    def _start(ctx):
        return Success(value={
            "intent": "leaf_pr",
            "root_item_id": inputs.root_item_id,
            "repo": inputs.repo,
            "trunk_branch": trunk,
            "leaves_total": len(inputs.leaf_ids),
            "dry_run": inputs.dry_run,
        })

    # ---- open_leaf_prs (reuse-open-or-create; runs search in dry-run) -

    @verbs.register("open_leaf_prs")
    async def _open(ctx):
        if not inputs.leaf_ids:
            return PermanentFailure(
                error_kind=EK_NO_LEAVES,
                message=(
                    "leaf_pr has no delivered leaves — nothing to open onto the "
                    "integration trunk"
                ),
                details={"root_item_id": inputs.root_item_id, "trunk": trunk},
            )
        repo_client = _require_repo_platform(ctx)
        if isinstance(repo_client, PermanentFailure):
            return repo_client

        # Pass 1 — classify each leaf against the open-PR set (read-only). We
        # gather every conflict first so a single bad leaf fails the whole set
        # closed *before* we open anything new (no half-applied topology).
        to_create: list[str] = []
        reused: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        for leaf_id in inputs.leaf_ids:
            want_head = inputs.impl_branch_for(leaf_id)
            try:
                results = await repo_client.find_open_pr_for_branch(
                    inputs.repo, head=want_head, limit=5,
                )
            except _REPO_CLIENT_ERRORS as e:
                return PermanentFailure(
                    error_kind="pr.search_failed",
                    message=f"repo client pr search failed for {want_head}: {e}",
                    details={"leaf_id": leaf_id, "head": want_head, "error": str(e)},
                )
            matches = [pr for pr in results if pr.head == want_head]
            if len(matches) > 1:
                conflicts.append({
                    "leaf_id": leaf_id, "reason": "ambiguous",
                    "head": want_head, "pr_numbers": [pr.number for pr in matches],
                })
            elif len(matches) == 1:
                pr = matches[0]
                if pr.base != trunk:
                    conflicts.append({
                        "leaf_id": leaf_id, "reason": "wrong_base", "head": want_head,
                        "pr_number": pr.number, "actual_base": pr.base,
                        "expected_base": trunk,
                    })
                else:
                    reused.append({"leaf_id": leaf_id, "pr_number": pr.number,
                                   "pr_url": pr.url})
            else:
                to_create.append(leaf_id)

        if conflicts:
            summary = ", ".join(f"{c['leaf_id']}:{c['reason']}" for c in conflicts)
            return NeedsHuman(
                gate=GATE_LEAF_PR_CONFLICT,
                prompt=(
                    f"{len(conflicts)} leaf PR(s) cannot be opened/reused safely "
                    f"({summary}). Resolve the conflicting PR(s) — wrong base or "
                    f"duplicate open PRs for one branch — and re-run."
                ),
                options=("abort", "override"),
                context={"trunk": trunk, "conflicts": conflicts,
                         "reused": reused, "to_create": to_create},
            )

        # Pass 2 — open the leaves that have no open PR yet (skipped in dry-run).
        opened: list[dict[str, Any]] = []
        if not inputs.dry_run:
            for leaf_id in to_create:
                want_head = inputs.impl_branch_for(leaf_id)
                title = f"Leaf {leaf_id} → {trunk} (AB#{inputs.root_item_id})"
                body = (
                    f"Implementation PR for leaf `{leaf_id}` of "
                    f"**AB#{inputs.root_item_id}**: merges `{want_head}` into the "
                    f"`{trunk}` integration trunk.\n"
                )
                try:
                    pr = await repo_client.pr_create(
                        inputs.repo, title=title, body=body, head=want_head, base=trunk,
                    )
                except _REPO_CLIENT_ERRORS as e:
                    return PermanentFailure(
                        error_kind="pr.create_failed",
                        message=f"repo client pr create failed for {want_head}: {e}",
                        details={"leaf_id": leaf_id, "head": want_head, "base": trunk,
                                 "error": str(e)},
                    )
                opened.append({"leaf_id": leaf_id, "pr_number": pr.number,
                               "pr_url": pr.url})

        # Assemble the {leaf_id: pr_number} map across reused + opened + (dry-run
        # would-open). Order follows the input leaf order for stable receipts.
        by_leaf: dict[str, dict[str, Any]] = {}
        for r in reused:
            by_leaf[r["leaf_id"]] = {"pr_number": r["pr_number"], "action": "reused"}
        for o in opened:
            by_leaf[o["leaf_id"]] = {"pr_number": o["pr_number"], "action": "opened"}
        for leaf_id in to_create:
            by_leaf.setdefault(leaf_id, {"pr_number": None, "action": "would_open"})

        leaves = [
            {"leaf_id": lid, "pr_number": by_leaf[lid]["pr_number"],
             "action": by_leaf[lid]["action"]}
            for lid in inputs.leaf_ids
        ]
        artifacts = tuple(
            f"gh:pr:{inputs.repo}#{e['pr_number']}"
            for e in leaves if e["pr_number"] is not None
        )
        return Success(
            value={
                "trunk_branch": trunk,
                "leaves_total": len(inputs.leaf_ids),
                "opened": len(opened),
                "reused": len(reused),
                "leaves": leaves,
                "dry_run": inputs.dry_run,
            },
            inspected_artifacts=artifacts,
        )

    return verbs


# ---- workflow topology --------------------------------------------------


def build_workflow() -> Workflow:
    return (
        WorkflowBuilder("leaf-pr", module="requiem.workflows.leaf_pr", version="0.1")
            .entry("start")
            .script("start", verb="start_run")
                .edge("start", on="success", to="open_leaf_prs")
            .script("open_leaf_prs", verb="open_leaf_prs")
                .edge("open_leaf_prs", on="success", to="end_success")
                .edge("open_leaf_prs", on="needs_human", to="end_human")
                .edge("open_leaf_prs", on="permanent_failure", to="end_failed")
            .terminate("end_success", disposition="completed")
            .terminate("end_failed", disposition="failed")
            .terminate("end_human", disposition="failed")
            .humanize({
                "start": "Starting leaf integration PRs",
                "open_leaf_prs": "Opened leaf integration PRs",
                "end_success": "Leaf PRs ready for the trunk",
                "end_failed": "Leaf PRs failed",
                "end_human": "Needs human decision",
            })
            .build()
    )


# ---- engine construction ------------------------------------------------


def _demo_inputs_and_toolbelt(root_item_id: int) -> tuple[LeafPrInputs, Toolbelt]:
    """A canned, dry-run, side-effect-free 2-leaf scenario for the zero-arg demo."""
    repo = "Owner/Repo"
    inputs = LeafPrInputs(
        root_item_id=root_item_id, repo=repo, leaf_ids=("1", "2"), dry_run=True,
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
    inputs: LeafPrInputs | None = None,
    toolbelt: Toolbelt | None = None,
    gate_handler=None,
) -> Engine:
    """Build an Engine for ``leaf-pr``.

    Zero-arg (``build_engine(log_dir)``) ships a canned, dry-run,
    side-effect-free demo: a 2-leaf root with an in-memory gh fake.

    Environment overrides (read once here, only when ``inputs`` is not given):

    * ``REQUIEM_LEAF_PR_ROOT`` — root work item id
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    if inputs is None:
        env_root = os.environ.get("REQUIEM_LEAF_PR_ROOT")
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
        gate_handler=gate_handler or _default_gate_handler,
    )


def _default_gate_handler(node_id: str, prompt: str, options: tuple[str, ...]) -> str:
    """Auto-abort gate handler for non-interactive (CLI / demo) runs.

    A leaf_pr conflict (wrong base / ambiguous open PRs) is always recoverable
    by a human resolving the offending PR and re-running, so the safe default is
    to abort rather than override and open over a known-bad set.
    """
    return "abort" if "abort" in options else options[-1]


_default_gate_handler.__requiem_auto__ = True  # type: ignore[attr-defined]


# ---- result projection --------------------------------------------------


def leaf_pr_result(completed: dict, final_node: str) -> LeafPrResult:
    start = (completed.get("start") or {}).get("value") or {}
    opened = (completed.get("open_leaf_prs") or {}).get("value") or {}
    dry_run = bool(start.get("dry_run"))
    if final_node == "end_success":
        verdict: Literal["opened", "previewed", "needs_human", "failed"] = (
            "previewed" if dry_run else "opened"
        )
    elif final_node == "end_human":
        verdict = "needs_human"
    else:
        verdict = "failed"
    leaves = tuple(
        LeafPr(leaf_id=str(e.get("leaf_id")), pr_number=e.get("pr_number"))
        for e in (opened.get("leaves") or [])
    )
    return LeafPrResult(
        root_item_id=int(start.get("root_item_id") or 0),
        verdict=verdict,
        trunk_branch=str(start.get("trunk_branch") or ""),
        leaves=leaves,
        leaves_total=int(start.get("leaves_total") or 0),
        opened=int(opened.get("opened") or 0),
        reused=int(opened.get("reused") or 0),
        dry_run=dry_run,
    )


def verdict_card(completed: dict) -> str | None:
    start = (completed.get("start") or {}).get("value")
    if not start:
        return None
    opened = (completed.get("open_leaf_prs") or {}).get("value") or {}
    dry = start.get("dry_run")
    trunk = start.get("trunk_branch")
    total = start.get("leaves_total")
    if dry:
        head = "  ◐ Dry run (preview)"
        tail = f"would open {total} leaf PR(s) → {trunk}"
    else:
        head = "  ✓ Leaf PRs ready"
        tail = (
            f"{opened.get('opened', 0)} opened, {opened.get('reused', 0)} reused "
            f"→ {trunk}"
        )
    return f"{head}\n  AB#{start.get('root_item_id')} — {tail}"


# ---- __main__ -----------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Open the impl/<root>-<item> → feature/<root> leaf PRs."
    )
    p.add_argument("--root", type=int, default=None, help="root work item id (demo)")
    p.add_argument("--run-id", default="leaf-pr")
    p.add_argument("--log-dir", type=Path, default=Path("runs"))
    return p


async def _amain(argv: list[str]) -> int:
    args = _build_arg_parser().parse_args(argv)
    if args.root is not None:
        os.environ.setdefault("REQUIEM_LEAF_PR_ROOT", str(args.root))
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
