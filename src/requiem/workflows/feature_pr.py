"""feature_pr workflow — gate trunk readiness, then open the ``feature/<root>``
→ ``main`` integration PR.

This is the trunk→main half of v0 non-negotiable #7 (merge-group topology) on
the **live (Hermes) delivery path**, per ADR-0006 Option D and ADR-0018. The
fan-out executor (``kanban_executor.py``, ADR-0014) delivers each implementable
leaf onto an ``impl/<root>-<item>`` branch; the *integration trunk* is
``feature/<root>``. This workflow is the final gate: it verifies every expected
leaf PR is merged into the trunk, then opens (or reuses) the trunk → ``main``
PR and hands off — it never merges anything itself.

Shape (mirrors ``plan_pr.py``'s open-and-handoff)::

    start
      → verify_readiness   (script · every expected leaf PR head/base/merged?)
      → verify_dispositions (script · every in-scope item's requirement satisfied?)
      → open_pr            (script · feature/<root> → main, idempotent)
      → link_pr            (script · twig backlink, best-effort)
      → end_success

Per ADR-0018 the readiness contract is **explicit, not a fuzzy search**: the
driver hands this workflow the authoritative expected-leaf set together with
each leaf's PR number (produced by the requiem-owned leaf-PR-open step). We
read each PR with ``gh.pr_view`` — reliable for *merged* state, which a default
``gh pr list`` (open-only) cannot report. A leaf is trunk-integrated iff its PR
has ``head == impl/<root>-<item>``, ``base == feature/<root>``, and
``merged == True``. Anything else fails closed to a human (ADR-0018 invariant
2: "delivered task" ≠ "merged into trunk").

Design invariants (ADR-0018):

* **Trunk-before-fan-out.** This workflow does *not* create ``feature/<root>``
  — it is bootstrapped earlier (driver, pre-dispatch). Here we only verify the
  leaf PRs landed on it and open the trunk→main PR.
* **No self-merge on the final feature PR.** This workflow never merges the
  ``feature/<root>`` → base PR; review→merge of that final trunk PR is owned by
  ``pr_lifecycle`` / the human, exactly like ``plan_pr``. This invariant is
  deliberately scoped to the final feature PR — leaf PR self-merge is owned by
  ``leaf_lifecycle.py``.
* **Fail closed on a partial set.** If any expected leaf PR is missing,
  wrong-head, wrong-base, or unmerged, we escalate to a human rather than
  opening an integration PR over an incomplete trunk.
* **Idempotent.** Re-running reuses an open trunk→main PR (head+base match); a
  stale wrong-base PR escalates instead of being silently adopted.
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
from requiem.clients.twig import TwigClientError
from requiem.dsl import AgentRegistry, VerbRegistry, Workflow, WorkflowBuilder
from requiem.kernel import Engine
from requiem.outcomes import NeedsHuman, PermanentFailure, Success
from requiem.toolbelt import FakeFileClient, RealGitClient, Toolbelt

# ADR-0024 step 4: a tuple covering both platforms' typed client errors.
# Lets the workflow except-clauses stay platform-agnostic.
_REPO_CLIENT_ERRORS: tuple[type[Exception], ...] = (
    GhClientError, AdoClientError,
)

# ---- error kinds --------------------------------------------------------

EK_NO_LEAVES = "feature_pr.no_leaves"

# ---- gates --------------------------------------------------------------

GATE_TRUNK_NOT_READY = "trunk_not_ready"
GATE_PR_WRONG_BASE = "pr_exists_wrong_base"
GATE_DISPOSITIONS_UNSATISFIED = "requirement_dispositions_unsatisfied"


# ---- public dataclasses -------------------------------------------------


@dataclass(frozen=True, slots=True)
class LeafPr:
    """An expected implementable leaf and the PR opened for it (if any).

    ``pr_number`` is ``None`` when no leaf PR has been opened yet — that leaf is
    not trunk-integrated, so the trunk is not ready.
    """

    leaf_id: str
    pr_number: int | None = None


@dataclass(frozen=True, slots=True)
class ItemDisposition:
    """The requirement-disposition of one in-scope work item (ADR-0006 P10).

    INV-DRIVER-GATES-FEATURE-MERGE: the feature→main merge waits not only on
    every leaf-impl PR landing on the trunk (``verify_readiness``) but also on
    every in-scope item's *requirement disposition* being satisfied. ``state``
    is the item's tracker state (e.g. ADO ``Done``/``Closed``/``Active``);
    ``satisfied`` is requiem's boolean disposition for it. The driver sources
    these from the committed plan / Twig item states; the gate only evaluates
    what it is given, fail-closed.
    """

    item_id: str
    state: str = ""
    satisfied: bool = False


@dataclass(slots=True)
class FeaturePrInputs:
    """Everything the feature_pr workflow needs, stamped once at start_run."""

    root_item_id: int
    repo: str
    leaves: tuple[LeafPr, ...]
    base_branch: str = "main"
    dry_run: bool = True
    # Requirement-disposition gate (ADR-0006 INV-DRIVER-GATES-FEATURE-MERGE).
    # Empty ⇒ the gate is a no-op pass (a caller that does not supply
    # dispositions keeps the pre-gate behaviour); non-empty ⇒ every entry must
    # be ``satisfied`` or the feature PR is fail-closed to a human.
    dispositions: tuple[ItemDisposition, ...] = ()

    @property
    def trunk_branch(self) -> str:
        return branch_model.feature_trunk(self.root_item_id)

    def impl_branch_for(self, leaf_id: str) -> str:
        return branch_model.impl_branch(self.root_item_id, leaf_id)


@dataclass(frozen=True, slots=True)
class FeaturePrResult:
    """Programmatic projection of a feature_pr run."""

    root_item_id: int
    verdict: Literal["opened", "previewed", "needs_human", "failed"]
    trunk_branch: str
    base_branch: str
    pr_number: int | None
    pr_url: str | None
    leaves_total: int
    leaves_ready: int
    reused_existing: bool
    dry_run: bool
    dispositions_total: int = 0
    dispositions_satisfied: int = 0


# ---- in-memory fakes (CLI demo + tests duck-type these) -----------------


@dataclass
class _DemoGhClient:
    """In-memory GhClient stand-in (demo / unit tests).

    ``by_number`` maps a PR number to a fully-typed ``GhPullRequest`` so
    ``pr_view`` can report merged state deterministically.
    """

    next_pr_number: int = 9000
    by_number: dict[int, GhPullRequest] = field(default_factory=dict)
    open_prs: list[GhPullRequest] = field(default_factory=list)
    created: list[dict[str, Any]] = field(default_factory=list)
    raise_on_view: Exception | None = None
    raise_on_search: Exception | None = None
    raise_on_create: Exception | None = None

    async def pr_view(self, repo: str, number: int) -> GhPullRequest:
        if self.raise_on_view is not None:
            raise self.raise_on_view
        try:
            return self.by_number[number]
        except KeyError as e:
            from requiem.clients.gh import GhUnknownError

            raise GhUnknownError(
                f"no such PR #{number}", exit_code=1, stderr="not found", argv=(),
            ) from e

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
        self.open_prs.append(pr)  # so a re-run's pr_search finds it
        return pr


@dataclass
class _DemoTwigClient:
    raise_on_comment: Exception | None = None
    comments: list[tuple[int, str]] = field(default_factory=list)

    async def comment_async(self, item_id: int, message: str) -> None:
        if self.raise_on_comment is not None:
            raise self.raise_on_comment
        self.comments.append((item_id, message))


# ---- verb registry ------------------------------------------------------


def build_verb_registry(inputs: FeaturePrInputs) -> VerbRegistry:
    verbs = VerbRegistry()
    trunk = inputs.trunk_branch

    def _require_repo_platform(ctx):
        # ADR-0024: prefer toolbelt.repo; fall back to toolbelt.gh.
        repo_client = ctx.toolbelt.repo or ctx.toolbelt.gh
        if repo_client is None:
            return PermanentFailure(
                error_kind="toolbelt.missing_client",
                message=(
                    "feature_pr workflow requires a RepoPlatform "
                    "(set toolbelt.repo, or toolbelt.gh for back-compat)"
                ),
            )
        return repo_client

    # ---- start --------------------------------------------------------

    @verbs.register("start_run")
    def _start(ctx):
        return Success(value={
            "intent": "feature_pr",
            "root_item_id": inputs.root_item_id,
            "repo": inputs.repo,
            "trunk_branch": trunk,
            "base_branch": inputs.base_branch,
            "leaves_total": len(inputs.leaves),
            "dry_run": inputs.dry_run,
        })

    # ---- verify_readiness (runs fully even in dry-run) ----------------

    @verbs.register("verify_readiness")
    async def _verify(ctx):
        if not inputs.leaves:
            return PermanentFailure(
                error_kind=EK_NO_LEAVES,
                message=(
                    "feature_pr has no expected leaves — refusing to open an "
                    "integration PR over an empty trunk"
                ),
                details={"root_item_id": inputs.root_item_id, "trunk": trunk},
            )
        repo_client = _require_repo_platform(ctx)
        if isinstance(repo_client, PermanentFailure):
            return repo_client

        ready: list[str] = []
        not_ready: list[dict[str, Any]] = []
        for leaf in inputs.leaves:
            want_head = inputs.impl_branch_for(leaf.leaf_id)
            if leaf.pr_number is None:
                not_ready.append({"leaf_id": leaf.leaf_id, "reason": "no_pr",
                                  "expected_head": want_head})
                continue
            try:
                pr = await repo_client.pr_view(inputs.repo, leaf.pr_number)
            except _REPO_CLIENT_ERRORS as e:
                not_ready.append({"leaf_id": leaf.leaf_id, "reason": "view_failed",
                                  "pr_number": leaf.pr_number, "error": str(e)})
                continue
            if pr.head != want_head:
                not_ready.append({"leaf_id": leaf.leaf_id, "reason": "head_mismatch",
                                  "pr_number": leaf.pr_number, "expected_head": want_head,
                                  "actual_head": pr.head})
            elif pr.base != trunk:
                not_ready.append({"leaf_id": leaf.leaf_id, "reason": "wrong_base",
                                  "pr_number": leaf.pr_number, "expected_base": trunk,
                                  "actual_base": pr.base})
            elif not pr.merged:
                not_ready.append({"leaf_id": leaf.leaf_id, "reason": "unmerged",
                                  "pr_number": leaf.pr_number})
            else:
                ready.append(leaf.leaf_id)

        if not_ready:
            reasons = ", ".join(f"{n['leaf_id']}:{n['reason']}" for n in not_ready)
            return NeedsHuman(
                gate=GATE_TRUNK_NOT_READY,
                prompt=(
                    f"{len(ready)}/{len(inputs.leaves)} leaf PR(s) integrated into "
                    f"{trunk!r}; {len(not_ready)} not ready ({reasons}). Merge the "
                    f"laggard leaf PR(s) into the trunk and re-run."
                ),
                options=("abort", "override"),
                context={"trunk": trunk, "ready": len(ready),
                         "total": len(inputs.leaves), "not_ready": not_ready},
            )
        return Success(value={
            "trunk_branch": trunk,
            "base_branch": inputs.base_branch,
            "leaves_total": len(inputs.leaves),
            "leaves_ready": len(ready),
            "ready_leaves": ready,
            "dry_run": inputs.dry_run,
        })

    # ---- verify_dispositions (ADR-0006 INV-DRIVER-GATES-FEATURE-MERGE) ----
    #
    # Trunk-readiness ("every leaf PR merged") is necessary but not sufficient:
    # the feature→main merge also waits on every in-scope item's *requirement
    # disposition* being satisfied (ADR-0006 P10 — "PR-state + requirement
    # disposition is the canonical signal", INV-NO-CORRUPT-FORWARD makes it a
    # hard refuse-if-unsatisfied check, not a best-effort merge). The gate
    # evaluates the disposition set the driver supplies; an empty set is a
    # deliberate no-op pass (a caller that doesn't track dispositions keeps the
    # pre-gate behaviour). Runs fully in dry-run (read-only).

    @verbs.register("verify_dispositions")
    async def _verify_dispositions(ctx):
        dispositions = inputs.dispositions
        total = len(dispositions)
        if total == 0:
            # No disposition set supplied — nothing to gate on (pass-through).
            return Success(value={
                "dispositions_total": 0,
                "dispositions_satisfied": 0,
                "dry_run": inputs.dry_run,
            })
        satisfied = [d for d in dispositions if d.satisfied]
        unsatisfied = [d for d in dispositions if not d.satisfied]
        if unsatisfied:
            detail = ", ".join(
                f"{d.item_id}:{d.state or 'unknown'}" for d in unsatisfied
            )
            return NeedsHuman(
                gate=GATE_DISPOSITIONS_UNSATISFIED,
                prompt=(
                    f"{len(satisfied)}/{total} in-scope item disposition(s) "
                    f"satisfied; {len(unsatisfied)} unsatisfied ({detail}). "
                    "The feature→base merge is gated until every in-scope item's "
                    "requirement disposition is satisfied (ADR-0006 "
                    "INV-DRIVER-GATES-FEATURE-MERGE). Resolve the item(s) and "
                    "re-run, or abort."
                ),
                options=("abort", "override"),
                context={
                    "dispositions_total": total,
                    "dispositions_satisfied": len(satisfied),
                    "unsatisfied": [
                        {"item_id": d.item_id, "state": d.state}
                        for d in unsatisfied
                    ],
                },
            )
        return Success(value={
            "dispositions_total": total,
            "dispositions_satisfied": len(satisfied),
            "dry_run": inputs.dry_run,
        })

    # ---- open_pr (trunk → main, idempotent) ---------------------------

    @verbs.register("open_pr")
    async def _open_pr(ctx):
        if inputs.dry_run:
            return Success(value={"pr_number": None, "pr_url": None, "dry_run": True})
        repo_client = _require_repo_platform(ctx)
        if isinstance(repo_client, PermanentFailure):
            return repo_client
        verify = ctx.completed["verify_readiness"]["value"]
        n_leaves = verify.get("leaves_ready", 0)
        title = f"Integrate AB#{inputs.root_item_id} ({trunk} → {inputs.base_branch})"
        body = (
            f"Integration PR for **AB#{inputs.root_item_id}**: merges the "
            f"`{trunk}` trunk into `{inputs.base_branch}`.\n\n"
            f"All {n_leaves} implementable leaf PR(s) are merged into the trunk. "
            f"Approve to land the run; the `pr_lifecycle` (review→merge) workflow "
            f"takes over from here.\n"
        )
        try:
            existing = await repo_client.find_open_pr_for_branch(
                inputs.repo, head=trunk, limit=5,
            )
        except _REPO_CLIENT_ERRORS as e:
            return PermanentFailure(
                error_kind="pr.search_failed",
                message=f"repo client pr search failed: {e}",
                details={"error": str(e)},
            )
        for pr in existing:
            if pr.head != trunk:
                continue
            if pr.base != inputs.base_branch:
                return NeedsHuman(
                    gate=GATE_PR_WRONG_BASE,
                    prompt=(
                        f"An open PR #{pr.number} already exists from {trunk!r} "
                        f"but targets {pr.base!r}, not the expected "
                        f"{inputs.base_branch!r}."
                    ),
                    options=("abort", "reuse_anyway"),
                    context={"pr_number": pr.number, "pr_base": pr.base,
                             "expected_base": inputs.base_branch},
                )
            return Success(
                value={"pr_number": pr.number, "pr_url": pr.url, "title": pr.title,
                       "reused_existing": True},
                inspected_artifacts=(f"gh:pr:{inputs.repo}#{pr.number}",),
            )
        try:
            pr = await repo_client.pr_create(
                inputs.repo, title=title, body=body, head=trunk, base=inputs.base_branch,
            )
        except _REPO_CLIENT_ERRORS as e:
            return PermanentFailure(
                error_kind="pr.create_failed",
                message=f"repo client pr create failed: {e}",
                details={"error": str(e)},
            )
        return Success(
            value={"pr_number": pr.number, "pr_url": pr.url, "title": pr.title,
                   "reused_existing": False},
            inspected_artifacts=(f"gh:pr:{inputs.repo}#{pr.number}",),
        )

    # ---- link_pr (best-effort ADO backlink) ---------------------------

    @verbs.register("link_pr")
    async def _link_pr(ctx):
        if inputs.dry_run:
            return Success(value={"linked": False, "dry_run": True})
        pr = ctx.completed.get("open_pr", {}).get("value", {})
        url = pr.get("pr_url")
        if not url:
            return Success(value={"linked": False, "reason": "no PR url to link"})
        twig = ctx.toolbelt.twig
        if twig is None:
            return Success(value={"linked": False, "reason": "no twig client"})
        try:
            await twig.comment_async(
                inputs.root_item_id,
                f"Feature integration PR opened by Requiem feature_pr workflow: {url}",
            )
        except TwigClientError as e:
            return PermanentFailure(
                error_kind="pr.link_failed",
                message=f"twig comment failed: {e}",
                details={"pr_url": url, "error": str(e)},
            )
        return Success(value={"linked": True, "pr_url": url})

    return verbs


# ---- workflow topology --------------------------------------------------


def build_workflow() -> Workflow:
    return (
        WorkflowBuilder("feature-pr", module="requiem.workflows.feature_pr", version="0.1")
            .entry("start")
            .script("start", verb="start_run")
                .edge("start", on="success", to="verify_readiness")
            .script("verify_readiness", verb="verify_readiness")
                .edge("verify_readiness", on="success", to="verify_dispositions")
                .edge("verify_readiness", on="needs_human", to="end_human")
                .edge("verify_readiness", on="permanent_failure", to="end_failed")
            .script("verify_dispositions", verb="verify_dispositions")
                .edge("verify_dispositions", on="success", to="open_pr")
                .edge("verify_dispositions", on="needs_human", to="end_human")
                .edge("verify_dispositions", on="permanent_failure", to="end_failed")
            .script("open_pr", verb="open_pr")
                .edge("open_pr", on="success", to="link_pr")
                .edge("open_pr", on="needs_human", to="end_human")
                .edge("open_pr", on="permanent_failure", to="end_failed")
            .script("link_pr", verb="link_pr")
                # best-effort: a failed backlink still hands off (PR is open).
                .edge("link_pr", on="success", to="end_success")
                .edge("link_pr", on="permanent_failure", to="end_success")
            .terminate("end_success", disposition="completed")
            .terminate("end_failed", disposition="failed")
            .terminate("end_human", disposition="needs_human")
            .humanize({
                "start": "Starting feature integration PR",
                "verify_readiness": "Verified trunk readiness",
                "verify_dispositions": "Verified requirement dispositions",
                "open_pr": "Opened feature integration PR",
                "link_pr": "Linked PR to work item",
                "end_success": "Feature PR ready for review",
                "end_failed": "Feature PR failed",
                "end_human": "Needs human decision",
            })
            .build()
    )


# ---- engine construction ------------------------------------------------


def _demo_inputs_and_toolbelt(
    log_dir: Path, root_item_id: int,
) -> tuple[FeaturePrInputs, Toolbelt]:
    """A canned, ready-to-integrate 2-leaf scenario for the zero-arg demo."""
    repo = "Owner/Repo"
    leaf_ids = ["1", "2"]
    by_number: dict[int, GhPullRequest] = {}
    leaves: list[LeafPr] = []
    for i, lid in enumerate(leaf_ids):
        n = 100 + i
        head = branch_model.impl_branch(root_item_id, lid)
        by_number[n] = GhPullRequest(
            number=n, title=f"Leaf {lid}", state="MERGED", merged=True, merged_at=None,
            head=head, base=branch_model.feature_trunk(root_item_id),
            url=f"https://github.com/{repo}/pull/{n}", raw={},
        )
        leaves.append(LeafPr(leaf_id=lid, pr_number=n))
    inputs = FeaturePrInputs(
        root_item_id=root_item_id, repo=repo, leaves=tuple(leaves), dry_run=True,
    )
    toolbelt = Toolbelt(
        git=RealGitClient(),
        files=FakeFileClient({}),
        gh=_DemoGhClient(by_number=by_number),  # type: ignore[arg-type]
        fs=None,
        twig=_DemoTwigClient(),  # type: ignore[arg-type]
    )
    return inputs, toolbelt


def build_engine(
    log_dir: Path,
    *,
    inputs: FeaturePrInputs | None = None,
    toolbelt: Toolbelt | None = None,
    gate_handler=None,
) -> Engine:
    """Build an Engine for ``feature-pr``.

    Zero-arg (``build_engine(log_dir)``) ships a canned, dry-run,
    side-effect-free demo: a 2-leaf root whose leaf PRs are merged into the
    trunk, with in-memory gh/twig fakes.

    Environment overrides (read once here, only when ``inputs`` is not given):

    * ``REQUIEM_FEATURE_PR_ROOT`` — root work item id
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    if inputs is None:
        env_root = os.environ.get("REQUIEM_FEATURE_PR_ROOT")
        root_item_id = int(env_root) if env_root else 4242
        inputs, demo_toolbelt = _demo_inputs_and_toolbelt(log_dir, root_item_id)
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

    feature_pr gates (trunk not ready, existing PR on the wrong base) are always
    recoverable by a human re-running after merging the laggard leaves, so the
    safe default is to abort rather than open an integration PR over an
    incomplete trunk.
    """
    return "abort" if "abort" in options else options[-1]


_default_gate_handler.__requiem_auto__ = True  # type: ignore[attr-defined]


# ---- result projection --------------------------------------------------


def feature_pr_result(completed: dict, final_node: str) -> FeaturePrResult:
    start = (completed.get("start") or {}).get("value") or {}
    verify = (completed.get("verify_readiness") or {}).get("value") or {}
    pr = (completed.get("open_pr") or {}).get("value") or {}
    dry_run = bool(start.get("dry_run"))
    if final_node == "end_success":
        verdict: Literal["opened", "previewed", "needs_human", "failed"] = (
            "previewed" if dry_run else "opened"
        )
    elif final_node == "end_human":
        verdict = "needs_human"
    else:
        verdict = "failed"
    pr_number = pr.get("pr_number")
    disp = (completed.get("verify_dispositions") or {}).get("value") or {}
    return FeaturePrResult(
        root_item_id=int(start.get("root_item_id") or 0),
        verdict=verdict,
        trunk_branch=str(start.get("trunk_branch") or ""),
        base_branch=str(start.get("base_branch") or ""),
        pr_number=int(pr_number) if pr_number else None,
        pr_url=pr.get("pr_url"),
        leaves_total=int(start.get("leaves_total") or 0),
        leaves_ready=int(verify.get("leaves_ready") or 0),
        reused_existing=bool(pr.get("reused_existing")),
        dry_run=dry_run,
        dispositions_total=int(disp.get("dispositions_total") or 0),
        dispositions_satisfied=int(disp.get("dispositions_satisfied") or 0),
    )


def verdict_card(completed: dict) -> str | None:
    start = (completed.get("start") or {}).get("value")
    if not start:
        return None
    pr = (completed.get("open_pr") or {}).get("value") or {}
    dry = start.get("dry_run")
    trunk = start.get("trunk_branch")
    if dry:
        head = "  ◐ Dry run (preview)"
        tail = f"would open integration PR {trunk} → {start.get('base_branch')}"
    elif pr.get("pr_url"):
        verb = "reused" if pr.get("reused_existing") else "opened"
        head = "  ✓ Feature PR ready"
        tail = f"{verb} #{pr.get('pr_number')} — {pr.get('pr_url')}"
    else:
        head = "  ⚠ Feature PR not opened"
        tail = "see verdict"
    return f"{head}\n  AB#{start.get('root_item_id')} — {tail}"


# ---- __main__ -----------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Open the feature/<root> → main integration PR.")
    p.add_argument("--root", type=int, default=None, help="root work item id (demo)")
    p.add_argument("--run-id", default="feature-pr")
    p.add_argument("--log-dir", type=Path, default=Path("runs"))
    return p


async def _amain(argv: list[str]) -> int:
    args = _build_arg_parser().parse_args(argv)
    if args.root is not None:
        os.environ.setdefault("REQUIEM_FEATURE_PR_ROOT", str(args.root))
    engine = build_engine(args.log_dir)
    result = await engine.run(args.run_id)
    completed = {}
    try:
        from requiem.workflows.planning import completed_from_log

        completed = completed_from_log(engine.log_path(args.run_id))
    except Exception:  # pragma: no cover — best-effort verdict card
        pass
    card = verdict_card(completed)
    if card:
        print(card)
    return 0 if type(result).__name__ == "Completed" else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
