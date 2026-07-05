"""requiem.workflows.fanout — the IN-PROCESS fan-out orchestrator (ADR-0021).

Parity #4's "missing core": walk a committed plan tree's implementable leaves and
dispatch each one into ``implementation`` — but **in-process** (single requiem
process, INV-SINGLE-PROCESS) rather than handing them to an external Hermes worker
the way ``kanban_executor`` (ADR-0014) does. The two are siblings: a fleet
deployment uses ``kanban_executor``; a single-process deployment uses this.

Shape (sequential v0)::

    start
      → resolve_leaves   (script · plan_tree.load_committed_leaves → ResolvedLeaf[]
                           or explicit inline leaves for tests)
      → dispatch_leaves  (script · for each not-yet-done leaf, build + run an
                           ``implementation`` engine in-process; roll up outcomes)
          ├─ every leaf landed a green PR        → end_success
          ├─ ≥1 leaf surrendered (needs_human)   → end_needs_human
          └─ ≥1 leaf hard-failed                 → end_failed

Why a script-verb loop and not a DSL ``subworkflow`` node: the DSL sub-workflow
node is *static* (one module + one sub_run_id), so it can't dispatch N dynamic
leaves. ``kanban_executor`` already established the resolve-then-loop idiom; we
mirror it, constructing an ``implementation`` engine per leaf inside the verb. The
ADR-0020 child-seam means each in-process child inherits this orchestrator's real
provider/toolbelt instead of silently faking (B1); ADR-0006 ``root`` makes each
leaf branch ``impl/<root>-<leaf>`` (B3); a leaf that surrenders terminates
``needs_human`` and rolls up to ``end_needs_human`` (B2).

Idempotent re-entry (iterate-until-stable): before dispatching a leaf we check
whether its child run already reached a terminal disposition (its own
``*.events.jsonl``). A re-run skips finished leaves — the loop is resume-driven,
leaning on the authoritative event log rather than a new loop primitive. Each
child writes ``fanout-<root>__leaf-<real_id>.events.jsonl``
(INV-SUBWORKFLOW-LOG-ISOLATION).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from requiem.dsl import AgentRegistry, VerbRegistry, Workflow, WorkflowBuilder
from requiem.kernel import Completed, Engine, Failed, Suspended
from requiem.outcomes import PermanentFailure, Success
from requiem.plan_tree import PlanArtifactError, load_committed_leaves
from requiem.toolbelt import Toolbelt
from requiem.workflows import implementation as impl_mod

MODULE = "requiem.workflows.fanout"


# ---- value objects ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FanoutLeaf:
    """One implementable leaf to dispatch in-process.

    A thin, JSON-able projection of ``plan_tree.ResolvedLeaf`` (or an explicit
    inline leaf for tests). ``real_id`` is the leaf's ADO identity and the
    ``item`` half of its ``impl/<root>-<item>`` branch.
    """

    real_id: int
    title: str
    body: str = ""


@dataclass(frozen=True, slots=True)
class FanoutInputs:
    """Everything the in-process fan-out needs, stamped once at start_run."""

    root_item_id: int
    repo: str
    repo_path: Path
    log_dir: Path
    base_branch: str = "main"
    dry_run: bool = True
    # Parallel dispatch with per-leaf git worktree isolation (ADR-0022, #5).
    # When False (default) leaves run sequentially over the shared repo. When
    # True, each leaf gets its own worktree and leaves run concurrently.
    parallel: bool = False
    max_parallel: int = 4
    # Explicit leaves (tests / inline) take priority; otherwise resolve from the
    # committed plan artifacts.
    leaves: tuple[FanoutLeaf, ...] = ()
    plan_tree_path: Path | None = None
    committed_path: Path | None = None
    # ADR-0030 §1 (context-pack inputs): when present, each leaf's
    # dispatch first commits a `.requiem/AGENTS.md` slice synthesised
    # from these. None on either is tolerated — the pack still builds
    # (process_config defaults to no policy; doctrine to empty), which
    # keeps the dogfood loop unblocked while config bootstrap catches up.
    process_config: Any | None = None  # ProcessConfig — avoiding circular import
    doctrine: Any | None = None        # Doctrine

    def child_run_id(self, real_id: int) -> str:
        return f"fanout-{self.root_item_id}__leaf-{real_id}"

    def worktree_path(self, real_id: int) -> Path:
        """Per-leaf worktree dir (parallel mode). Sibling of the repo so it isn't
        nested inside the main working tree."""
        return self.repo_path.parent / f".requiem-wt-{self.root_item_id}-{real_id}"


@dataclass(frozen=True, slots=True)
class LeafOutcome:
    """The roll-up record for one dispatched leaf."""

    real_id: int
    disposition: str            # completed | needs_human | failed | error
    final_node: str
    child_run_id: str
    skipped: bool = False       # already terminal on a prior run (idempotent)
    # Populated when the leaf's own `implementation` child opened a PR (its
    # `create_pr` verb). None when the leaf never reached create_pr (needs_human
    # / failed) or when running in dry_run. Lets the caller (end_to_end's
    # in-process dispatch) drive leaf_lifecycle per landed leaf, mirroring the
    # kanban backend's separate leaf_pr leg.
    pr_number: int | None = None
    branch_name: str = ""


@dataclass(frozen=True, slots=True)
class FanoutResult:
    """What a caller can pluck out of a finished fan-out run."""

    root_item_id: int
    verdict: Literal["all_landed", "needs_human", "failed", "no_leaves", "previewed"]
    leaves_total: int
    leaves_landed: int
    leaves_needs_human: int
    leaves_failed: int
    dry_run: bool
    outcomes: tuple[LeafOutcome, ...] = field(default_factory=tuple)


# ---- helpers ------------------------------------------------------------


def _leaf_to_dict(leaf: FanoutLeaf) -> dict[str, Any]:
    return {"real_id": leaf.real_id, "title": leaf.title, "body": leaf.body}


def _leaf_from_dict(d: dict[str, Any]) -> FanoutLeaf:
    return FanoutLeaf(
        real_id=int(d["real_id"]),
        title=str(d.get("title", "")),
        body=str(d.get("body", "")),
    )


def _build_leaf_context_pack(leaf: FanoutLeaf, inputs: FanoutInputs) -> Any | None:
    """ADR-0030 §1: synthesise the per-leaf context pack for the fanout
    dispatch path. Returns the rendered :class:`ContextPack` so the
    implementation workflow's ``commit_context_pack`` verb can land it
    on the leaf branch before invoke_coder runs.

    Defensive: any failure to build the pack returns ``None`` so the
    leaf still gets its baseline coder prompt (the legacy pre-ADR-0030
    behaviour). The pack is a context-engineering improvement, not a
    blocking dependency — a missing process_config or a doctrine read
    error must NOT torch a leaf's whole implementation run.
    """
    try:
        from requiem.context_pack import (
            ContextPackLeaf,
            build_context_pack,
        )
    except ImportError:
        return None
    try:
        leaf_proj = ContextPackLeaf.from_mapping({
            "leaf_id": str(leaf.real_id),
            "real_id": leaf.real_id,
            "title": leaf.title,
            "body": leaf.body,
            # The planner's prose is in body for FanoutLeaf; mirror as
            # rationale so AGENTS.md's "Why this leaf exists" section
            # is populated for v0. Future enhancement: thread the
            # full planner output through FanoutLeaf so rationale and
            # body are separable.
            "rationale": leaf.body,
        })
        return build_context_pack(
            leaf=leaf_proj,
            plan_payload={"item_id": leaf.real_id, "title": leaf.title},
            process_config=inputs.process_config,
            doctrine=inputs.doctrine,
        )
    except Exception:  # noqa: BLE001 — defensive: never break dispatch
        return None


def _terminal_disposition(log_path: Path) -> str | None:
    """Read a child run's terminal disposition from its log, or None if the run
    never finished (no ``run_completed``)."""
    if not log_path.exists():
        return None
    from requiem.persistence import CorruptLogError, replay
    disp: str | None = None
    try:
        for ev in replay(log_path):
            if ev.get("kind") == "run_completed":
                disp = (ev.get("payload") or {}).get("terminal")
    except CorruptLogError:
        return None
    return disp


def _leaf_pr_info(log_path: Path) -> tuple[int | None, str]:
    """Read a landed leaf's PR number + branch name from its own durable log.

    The in-process ``implementation`` child opens its own PR (``create_pr``
    verb) rather than deferring to a separate leaf_pr leg (unlike the kanban
    backend). This is how the caller (``end_to_end._dispatch_in_process``)
    recovers the PR number needed to drive ``leaf_lifecycle`` per leaf —
    mirroring the kanban path's ``leaf_pr`` → ``leaf_lifecycle`` handoff.
    Returns ``(None, "")`` for a leaf that never reached ``create_pr`` (a
    needs_human/failed leaf, or a dry_run preview) or whose log is missing.
    """
    if not log_path.exists():
        return None, ""
    from requiem.persistence import CorruptLogError, replay
    pr_number: int | None = None
    branch_name = ""
    try:
        for ev in replay(log_path):
            if ev.get("kind") != "verb_completed":
                continue
            node_id = ev.get("node_id")
            outcome = (ev.get("payload") or {}).get("outcome") or {}
            value = outcome.get("value") or {}
            if node_id == "create_branch":
                branch_name = str(value.get("branch_name") or branch_name)
            elif node_id == "create_pr":
                pr_number = value.get("pr_number")
    except CorruptLogError:
        return None, ""
    return pr_number, branch_name


@dataclass
class _LeafTwig:
    """Per-leaf twig adapter: serves THIS leaf's resolved plan to the child's
    ``fetch_plan`` instead of re-fetching from ADO.

    The plan is already known — ``plan_tree`` resolved the leaf's title/body from
    the planner's proposal. So the in-process fan-out hands the child the plan it
    already holds rather than requiring live ADO twig access (which a
    single-process deployment may not have). ``comment_async`` is a no-op sink
    (the child's best-effort PR backlink); a real deployment swaps in a real twig.
    """

    real_id: int
    title: str
    body: str
    comments: list[tuple[int, str]] = field(default_factory=list)

    async def show_async(self, item_id: int):
        from requiem.clients.twig import TwigItem
        return TwigItem(
            id=item_id,
            title=self.title,
            state="Active",
            area_path="Fanout",
            work_item_type="Task",
            parent_id=None,
            raw={"id": item_id, "title": self.title, "description": self.body},
        )

    async def comment_async(self, item_id: int, message: str) -> None:
        self.comments.append((item_id, message))


# ---- verb registry ------------------------------------------------------


def build_verb_registry(inputs: FanoutInputs) -> VerbRegistry:
    verbs = VerbRegistry()

    @verbs.register("resolve_leaves")
    async def _resolve_leaves(ctx):
        # Priority 1 — explicit inline leaves (tests / the driver's inline path).
        if inputs.leaves:
            leaves = list(inputs.leaves)
        elif inputs.plan_tree_path is not None and inputs.committed_path is not None:
            try:
                resolved = load_committed_leaves(
                    Path(inputs.plan_tree_path), Path(inputs.committed_path)
                )
            except PlanArtifactError as e:
                # Fail closed: never dispatch on an unfaithful enumeration.
                return PermanentFailure(
                    error_kind=f"fanout.plan.{e.kind}",
                    message=f"could not resolve implementable leaves: {e}",
                )
            leaves = [
                FanoutLeaf(real_id=r.real_id, title=r.title, body=r.body)
                for r in resolved
            ]
        else:
            return PermanentFailure(
                error_kind="fanout.no_leaf_source",
                message="fanout needs either inline `leaves` or both "
                        "`plan_tree_path` and `committed_path`.",
            )
        if not leaves:
            return PermanentFailure(
                error_kind="fanout.no_leaves",
                message="no implementable leaves to dispatch.",
            )
        return Success(value={"leaves": [_leaf_to_dict(leaf) for leaf in leaves]})

    async def _dispatch_one(leaf: FanoutLeaf, toolbelt: Toolbelt) -> LeafOutcome:
        """Build + run one leaf's implementation engine, returning its outcome.

        In parallel mode the leaf runs in its own git worktree (ADR-0022) so
        concurrent leaves don't clobber each other's checkout/index. The branch
        is created *with* the worktree (`-b impl/<root>-<leaf>`), so
        `implementation.create_branch` finds it already current.
        """
        from dataclasses import replace as _dc_replace

        from requiem.clients.fs import FilesystemClient, FsGitError

        run_id = inputs.child_run_id(leaf.real_id)
        log_path = inputs.log_dir / f"{run_id}.events.jsonl"

        # Idempotent re-entry: skip a leaf whose child run already reached a
        # terminal disposition on a prior orchestrator run.
        prior = _terminal_disposition(log_path)
        if prior is not None:
            prior_pr, prior_branch = _leaf_pr_info(log_path)
            return LeafOutcome(
                real_id=leaf.real_id, disposition=prior,
                final_node="(prior run)", child_run_id=run_id, skipped=True,
                pr_number=prior_pr, branch_name=prior_branch,
            )

        repo_path = inputs.repo_path
        worktree: Path | None = None
        if inputs.parallel:
            worktree = inputs.worktree_path(leaf.real_id)
            branch = f"impl/{inputs.root_item_id}-{leaf.real_id}"
            # Reuse an existing worktree dir on re-entry (a second `worktree add`
            # on the same path errors). Otherwise create it on the leaf branch.
            if not worktree.exists():
                main_fs = toolbelt.fs or FilesystemClient(repo_path)
                try:
                    await main_fs.git_worktree_add(
                        worktree, branch=branch, from_ref=inputs.base_branch,
                    )
                except FsGitError as e:
                    return LeafOutcome(
                        real_id=leaf.real_id, disposition="failed",
                        final_node=f"worktree_add_failed:{e}",
                        child_run_id=run_id,
                    )
            repo_path = worktree

        child_inputs = impl_mod.ImplementationInputs(
            item_id=leaf.real_id,
            repo=inputs.repo,
            repo_path=repo_path,
            base_branch=inputs.base_branch,
            dry_run=inputs.dry_run,
            root=inputs.root_item_id,   # ADR-0006 (B3): impl/<root>-<leaf>
            context_pack=_build_leaf_context_pack(leaf, inputs),
            # ADR-0030 §2: thread the operator's ProcessConfig into
            # every per-leaf implementation engine so the coder agent's
            # role="implementer" tag actually picks up the
            # `models.implementer` routing block. Without this, every
            # leaf falls back to the provider's default model (the run
            # #28 gap that this commit closes).
            process_config=inputs.process_config,
        )
        # Per-leaf toolbelt: serve THIS leaf's already-resolved plan via a
        # _LeafTwig (no live ADO re-fetch) and bind fs to the leaf's working
        # tree (the worktree in parallel mode, else the shared repo). We start
        # from the orchestrator's toolbelt (so gh/git are the real,
        # seam-propagated clients) and swap twig + fs for the leaf.
        leaf_twig = _LeafTwig(real_id=leaf.real_id, title=leaf.title, body=leaf.body)
        leaf_toolbelt = _dc_replace(
            toolbelt,
            twig=leaf_twig,  # type: ignore[arg-type]
            fs=FilesystemClient(repo_path),
        )
        # The ADR-0020 seam (installed by THIS orchestrator's build_engine, and
        # safe under asyncio.gather — a parent-set contextvar propagates into
        # gathered children) lets the child inherit our real provider. The
        # per-leaf toolbelt is passed explicitly so a leaf is never built over a
        # silent fake.
        child = impl_mod.build_engine(
            inputs.log_dir, inputs=child_inputs, toolbelt=leaf_toolbelt,
        )
        result = await child.run(run_id)

        if isinstance(result, Completed):
            disp, final = result.disposition, result.final_node
        elif isinstance(result, Suspended):
            disp, final = "needs_human", result.node_id
        elif isinstance(result, Failed):
            disp, final = "failed", result.node_id
        else:  # defensive
            disp, final = "error", "(unknown)"

        # Best-effort worktree cleanup on a landed leaf; leave it on disk for a
        # surrendered/failed leaf so the human can inspect the working tree
        # (mirrors implementation's "branch left on disk" handoff contract).
        if worktree is not None and disp == "completed":
            try:
                main_fs = toolbelt.fs or FilesystemClient(inputs.repo_path)
                await main_fs.git_worktree_remove(worktree, force=True)
            except FsGitError:
                pass

        pr_number, branch_name = _leaf_pr_info(log_path)
        return LeafOutcome(
            real_id=leaf.real_id, disposition=disp,
            final_node=final, child_run_id=run_id,
            pr_number=pr_number, branch_name=branch_name,
        )

    @verbs.register("dispatch_leaves")
    async def _dispatch_leaves(ctx):
        resolved = (ctx.completed.get("resolve_leaves") or {}).get("value") or {}
        leaves = [_leaf_from_dict(d) for d in resolved.get("leaves", [])]
        toolbelt: Toolbelt = ctx.toolbelt

        if inputs.parallel:
            # Bounded concurrency: at most `max_parallel` leaves in flight.
            import asyncio

            # GC first: prune stale worktree admin entries from a prior crashed
            # run so a `git worktree add` on a reused path doesn't collide
            # (ADR-0022). Best-effort — never block dispatch on cleanup.
            from requiem.clients.fs import FilesystemClient, FsGitError
            try:
                main_fs = toolbelt.fs or FilesystemClient(inputs.repo_path)
                await main_fs.git_worktree_prune()
            except FsGitError:
                pass

            sem = asyncio.Semaphore(max(1, inputs.max_parallel))

            async def _guarded(leaf):
                async with sem:
                    return await _dispatch_one(leaf, toolbelt)

            outcomes = list(await asyncio.gather(*(_guarded(leaf) for leaf in leaves)))
        else:
            outcomes = []
            for leaf in leaves:
                outcomes.append(await _dispatch_one(leaf, toolbelt))

        landed = sum(1 for o in outcomes if o.disposition == "completed")
        needs_human = sum(1 for o in outcomes if o.disposition == "needs_human")
        failed = sum(1 for o in outcomes
                     if o.disposition not in ("completed", "needs_human"))
        return Success(value={
            "leaves_total": len(outcomes),
            "leaves_landed": landed,
            "leaves_needs_human": needs_human,
            "leaves_failed": failed,
            "outcomes": [
                {
                    "real_id": o.real_id, "disposition": o.disposition,
                    "final_node": o.final_node, "child_run_id": o.child_run_id,
                    "skipped": o.skipped,
                    "pr_number": o.pr_number, "branch_name": o.branch_name,
                }
                for o in outcomes
            ],
            "dry_run": inputs.dry_run,
            "parallel": inputs.parallel,
        })

    return verbs


# ---- workflow assembly --------------------------------------------------


def build_workflow() -> Workflow:
    return (
        WorkflowBuilder("fanout", module=MODULE, version="1")
        .entry("resolve_leaves")
        .script("resolve_leaves", verb="resolve_leaves")
            .edge("resolve_leaves", on="success", to="dispatch_leaves")
            .edge("resolve_leaves", on="permanent_failure", to="end_failed")
        .script("dispatch_leaves", verb="dispatch_leaves")
            # The roll-up routing is decided by a tiny router on the leaf counts;
            # the kernel takes the success edge and the result projection reads
            # the verdict. A single success edge keeps the topology simple — the
            # verdict (all_landed vs needs_human vs failed) is a projection of the
            # dispatch outcome, not a separate terminal per case, because a fan-out
            # can legitimately mix landed + surrendered leaves in one pass.
            .edge("dispatch_leaves", on="success", to="end_success")
            .edge("dispatch_leaves", on="permanent_failure", to="end_failed")
        .terminate("end_success", disposition="completed")
        .terminate("end_failed", disposition="failed")
        .humanize({
            "resolve_leaves": "Resolved implementable leaves",
            "dispatch_leaves": "Dispatched leaves in-process",
            "end_success": "Fan-out complete",
            "end_failed": "Fan-out failed",
        })
        .build()
    )


def build_engine(
    log_dir: Path,
    *,
    inputs: FanoutInputs | None = None,
    provider: Any | None = None,
    toolbelt: Toolbelt | None = None,
    gate_handler=None,
) -> Engine:
    """Construct a runnable fan-out Engine.

    With no extras, a self-contained demo: one inline leaf, the happy-path
    provider, a demo toolbelt, ``dry_run=True``. Programmatic callers (the driver,
    tests) supply ``inputs``, ``toolbelt``, and ``provider``.
    """
    if inputs is None:
        inputs = _demo_inputs(log_dir)
    if provider is None:
        provider = impl_mod.happy_path_provider()
    if toolbelt is None:
        toolbelt = Toolbelt.real()
    # ADR-0020 seam: this orchestrator dispatches `implementation` children from
    # inside a script verb (not a DSL subworkflow node), so the kernel's own
    # seam-install (which only fires on the subworkflow path) never runs for us.
    # Install the provider here so each in-process child's `build_engine` inherits
    # the real provider instead of silently faking (B1). The toolbelt is passed
    # explicitly per-leaf, so only the provider needs the seam.
    from requiem import seam as _seam
    _seam.set_seams(provider=provider, gate_handler=gate_handler)
    return Engine(
        workflow=build_workflow(),
        verbs=build_verb_registry(inputs),
        agents=AgentRegistry(),
        provider=provider,
        toolbelt=toolbelt,
        log_dir=log_dir,
        gate_handler=gate_handler,
    )


def _demo_inputs(log_dir: Path) -> FanoutInputs:
    return FanoutInputs(
        root_item_id=9000,
        repo="Owner/Repo",
        repo_path=log_dir / "demo_repo",
        log_dir=log_dir,
        dry_run=True,
        leaves=(FanoutLeaf(real_id=9001, title="demo leaf", body="implement X"),),
    )


# ---- result projection --------------------------------------------------


def fanout_result(completed: dict, final_node: str) -> FanoutResult:
    disp = (completed.get("dispatch_leaves") or {}).get("value") or {}
    total = int(disp.get("leaves_total", 0))
    landed = int(disp.get("leaves_landed", 0))
    needs_human = int(disp.get("leaves_needs_human", 0))
    failed = int(disp.get("leaves_failed", 0))
    dry_run = bool(disp.get("dry_run", False))
    outcomes = tuple(
        LeafOutcome(
            real_id=int(o["real_id"]), disposition=str(o["disposition"]),
            final_node=str(o["final_node"]), child_run_id=str(o["child_run_id"]),
            skipped=bool(o.get("skipped", False)),
            pr_number=o.get("pr_number"), branch_name=str(o.get("branch_name") or ""),
        )
        for o in disp.get("outcomes", [])
    )

    if final_node == "end_failed" and total == 0:
        verdict: Literal[
            "all_landed", "needs_human", "failed", "no_leaves", "previewed"
        ] = "no_leaves"
    elif failed > 0:
        verdict = "failed"
    elif needs_human > 0:
        verdict = "needs_human"
    elif dry_run:
        verdict = "previewed"
    else:
        verdict = "all_landed"

    return FanoutResult(
        root_item_id=0,
        verdict=verdict,
        leaves_total=total,
        leaves_landed=landed,
        leaves_needs_human=needs_human,
        leaves_failed=failed,
        dry_run=dry_run,
        outcomes=outcomes,
    )
