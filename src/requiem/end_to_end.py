"""End-to-end driver: one ADO work item → plan → seed → dispatch.

This is the thin operator command that makes "run Requiem against any ADO
work item" real. It is deliberately **not** a workflow: it runs the three
existing workflows as independent *top-level* engines and threads concrete
artifact paths between them. Running each as a top-level engine (rather than
nesting them as in-process sub-workflows) sidesteps ADR-0013 §B1 — every
stage gets its own real provider/toolbelt, nothing falls back to a fake.

Pipeline::

    planning(item)                          # recursive plan → .plan.tree.json | .plan.md
      ├─ leaf root (decomposable=False) ───→ dispatch the *root item itself*
      │                                       as the single implementable leaf
      └─ decomposable ──→ commit_plan(tree) # seed ADO children → .plan.committed.json
                            └─→ kanban_executor(tree, committed)  # fan-out to Hermes

Safety defaults mirror the workflows: ``commit`` (actually seed ADO children)
and ``live`` (actually spawn Hermes workers) are **off** by default. A
decomposable root with ``commit=False`` stops after planning with a "planned
only" verdict — it never invents real ids or dispatches. A real fan-out needs
a *committed* (non-dry-run) manifest, so the driver always seeds for real when
it proceeds to dispatch; ``live`` then governs only whether workers actually
spawn (the executor's own dispatch dry-run).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from requiem import branch_model
from requiem.kernel import Completed, Engine
from requiem.persistence import replay
from requiem.toolbelt import Toolbelt
from requiem.workflows import commit_plan as commit_plan_mod
from requiem.workflows import feature_pr as feature_pr_mod
from requiem.workflows import kanban_executor as executor_mod
from requiem.workflows import leaf_pr as leaf_pr_mod
from requiem.workflows import planning as planning_mod
from requiem.workflows import trunk_bootstrap as trunk_bootstrap_mod
from requiem.workflows.feature_pr import ItemDisposition, LeafPr
from requiem.workflows.kanban_executor import ExecInputs, LeafSpec

# Engine factories are injected (defaulting to the real ones) so the pipeline
# orchestration is unit-testable with stub engines that don't need an LLM/ADO.
PlanningFactory = Callable[..., Engine]
CommitFactory = Callable[..., Engine]
ExecutorFactory = Callable[..., Engine]
# ADR-0018 step 4: the three trunk-topology workflows the driver owns. Injected
# the same way (real build_engine by default) so the wiring is stub-testable.
TrunkBootstrapFactory = Callable[..., Engine]
LeafPrFactory = Callable[..., Engine]
FeaturePrFactory = Callable[..., Engine]


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """Outcome of an end-to-end run.

    ``stage`` is the furthest stage reached (``planning`` / ``commit`` /
    ``executor``). ``status`` is one of ``delivered`` (executor finished at
    ``end``), ``planned`` (stopped after planning — leaf preview or a
    decomposable root without ``commit``), ``paused`` (a stage needed a human
    / failed). ``detail`` is human-readable.
    """

    item_id: int
    stage: str
    status: str
    detail: str
    decomposable: bool | None = None
    leaf_ids: tuple[str, ...] = ()
    plan_artifact: str | None = None
    committed_path: str | None = None
    executor_final_node: str | None = None
    # -- ADR-0018 step 4: trunk-topology projections (populated only when a
    #    github_repo is threaded; all None/empty on the legacy creds-light path) --
    github_repo: str | None = None
    base_branch: str | None = None
    trunk_branch: str | None = None
    trunk_verdict: str | None = None          # created | exists | previewed | failed
    leaf_pr_verdict: str | None = None        # opened | previewed | needs_human | failed
    leaf_pr_map: tuple[tuple[str, int | None], ...] = ()  # (leaf_id, pr_number)
    leaf_pr_map_path: str | None = None       # persisted {leaf_id: pr_number} artifact
    feature_pr_verdict: str | None = None     # opened | previewed | needs_human | failed
    feature_pr_number: int | None = None
    feature_pr_url: str | None = None


def _completed_map(log_dir: Path, run_id: str) -> dict[str, dict]:
    """Reconstruct the node→outcome map from a finished engine's durable log."""
    completed: dict[str, dict] = {}
    for ev in replay(log_dir / f"{run_id}.events.jsonl"):
        if ev.get("kind") == "verb_completed":
            completed[ev["node_id"]] = ev["payload"]["outcome"]
    return completed


def _gate_opened(log_dir: Path, run_id: str) -> str | None:
    """Return the node_id of the last gate_opened event in a run's log, if any.

    A ``NeedsHuman`` gate records a ``gate_opened`` event (not a Success outcome),
    so this is how the driver learns *which* node fired the human gate — e.g.
    ``verify_readiness`` (a laggard leaf) vs ``verify_dispositions`` (an
    unsatisfied requirement). Returns None if no gate opened.
    """
    node_id: str | None = None
    for ev in replay(log_dir / f"{run_id}.events.jsonl"):
        if ev.get("kind") == "gate_opened":
            node_id = ev.get("node_id")
    return node_id


def _plan_record(completed: dict[str, dict]) -> dict[str, Any] | None:
    """Pull the planning record value (approved or needs-human), if present."""
    for node in ("record_plan", "record_needs_human"):
        block = completed.get(node)
        if block and block.get("kind") == "success":
            return block.get("value")
    return None


# ---- ADR-0018 step 4: trunk-topology helpers -------------------------------
#
# These run only when the driver is given a `github_repo` ("Owner/Repo"). On the
# legacy creds-light path (no github_repo) they are never reached, so the
# executor-only pipeline behaves exactly as before. Each honours the driver's
# `live` flag by threading it as the workflow's `dry_run = not live`, keeping a
# dry run genuinely side-effect-free (no ref create, no PR open).


def _gh_toolbelt(twig: Any | None, gh: Any | None) -> Toolbelt:
    """A toolbelt carrying a real (or injected) gh client for the topology steps.

    The phase-3 executor toolbelt deliberately omits `gh` (it only coordinates a
    remote board). The trunk workflows need `toolbelt.gh`, so we build a small
    real toolbelt and let an injected `gh`/`twig` override it for tests.
    """
    real = Toolbelt.real()
    return Toolbelt(
        git=real.git,
        files=real.files,
        gh=gh if gh is not None else real.gh,
        twig=twig if twig is not None else real.twig,
        kanban=real.kanban,
    )


async def _resolve_base_branch(
    github_repo: str, gh: Any | None, fallback: str = "main",
) -> str:
    """Q2: resolve the repo's real default branch instead of hardcoding `main`.

    Uses the narrow `gh.api()` read escape hatch (no new GhClient mutation
    surface — the branch-ref pair stays the only added methods). Falls back to
    `fallback` if the client is absent or the probe fails for any reason: the
    base is re-validated fail-closed by trunk_bootstrap's `branch_sha` anyway,
    so a wrong guess surfaces there rather than corrupting forward.
    """
    client = gh if gh is not None else Toolbelt.real().gh
    if client is None:
        return fallback
    try:
        payload = await client.api(f"repos/{github_repo}")
    except Exception:
        return fallback
    default = payload.get("default_branch") if isinstance(payload, dict) else None
    return str(default) if default else fallback


def _persist_leaf_pr_map(
    log_dir: Path, item_id: int, leaves: tuple[LeafPr, ...],
) -> Path:
    """Persist the authoritative {leaf_id: pr_number} map as a stage artifact.

    The briefing + ADR-0018 step 2 call this out explicitly: a default
    `gh pr list` is open-only and cannot re-derive merged leaf PR numbers, so
    feature_pr's input must come from this persisted map, not a re-query. We
    follow the per-stage artifact pattern the rest of the driver already uses.
    """
    path = log_dir / f"leaf-pr-map-{item_id}.json"
    payload = {
        "item_id": item_id,
        "leaves": [
            {"leaf_id": lp.leaf_id, "pr_number": lp.pr_number} for lp in leaves
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def load_leaf_pr_map(path: Path) -> tuple[LeafPr, ...]:
    """Re-hydrate the persisted leaf-PR map into feature_pr's input element type.

    The inverse of :func:`_persist_leaf_pr_map`; used by :func:`integrate_pipeline`
    to feed the trunk-readiness gate after the human has merged the leaf PRs.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return tuple(
        LeafPr(leaf_id=str(e["leaf_id"]), pr_number=e.get("pr_number"))
        for e in (payload.get("leaves") or [])
    )


async def run_pipeline(
    item_id: int,
    *,
    log_dir: Path,
    board: str,
    assignee: str | None = None,
    commit: bool = False,
    live: bool = False,
    skills: tuple[str, ...] = (),
    github_repo: str | None = None,
    base_branch: str | None = None,
    twig: Any | None = None,
    provider: Any | None = None,
    kanban: Any | None = None,
    gh: Any | None = None,
    gate_handler: Any | None = None,
    process_config: Any | None = None,
    poll_interval_s: float = 5.0,
    max_polls: int = 120,
    planning_factory: PlanningFactory = planning_mod.build_engine,
    commit_factory: CommitFactory = commit_plan_mod.build_engine,
    executor_factory: ExecutorFactory = executor_mod.build_engine,
    trunk_bootstrap_factory: TrunkBootstrapFactory = trunk_bootstrap_mod.build_engine,
    leaf_pr_factory: LeafPrFactory = leaf_pr_mod.build_engine,
) -> PipelineResult:
    log_dir.mkdir(parents=True, exist_ok=True)

    # -- Phase 1: plan -------------------------------------------------
    plan_run = f"plan-{item_id}"
    plan_engine = planning_factory(
        log_dir, item_id=item_id, twig=twig, provider=provider,
        gate_handler=gate_handler, process_config=process_config,
    )
    plan_outcome = await plan_engine.run(plan_run)
    plan_record = _plan_record(_completed_map(log_dir, plan_run))

    if plan_record is None or plan_record.get("final_verdict") != "approved":
        verdict = (plan_record or {}).get("final_verdict", "unknown")
        return PipelineResult(
            item_id=item_id, stage="planning", status="paused",
            detail=f"planning did not approve a plan (verdict={verdict!r}); "
                   "resolve the plan before dispatching.",
            decomposable=(plan_record or {}).get("decomposable"),
            plan_artifact=(plan_record or {}).get("plan_artifact"),
        )

    decomposable = bool(plan_record.get("decomposable"))
    plan_artifact = plan_record.get("plan_artifact")

    # -- Phase 2: resolve the leaf source ------------------------------
    if not decomposable:
        # Atomic root: the item itself is the single implementable leaf. There
        # is no tree to seed; dispatch it directly (inline-leaves path).
        leaf = LeafSpec(
            leaf_id=str(item_id),
            title=str(plan_record.get("item_title") or f"item {item_id}"),
            body=str(plan_record.get("summary") or ""),
            branch=branch_model.impl_branch(item_id, item_id),
            skills=skills,
        )
        exec_inputs = ExecInputs(
            root_item=str(item_id), board=board, assignee=assignee, live=live,
            leaves=(leaf,), poll_interval_s=poll_interval_s, max_polls=max_polls,
            skills=skills,
        )
        committed_path = None
    else:
        if not commit:
            return PipelineResult(
                item_id=item_id, stage="planning", status="planned",
                detail="plan is decomposable; re-run with commit=True to seed "
                       "ADO children and dispatch the implementable leaves.",
                decomposable=True, plan_artifact=plan_artifact,
            )
        # Seed for real — a faithful fan-out needs real ADO ids, so the manifest
        # must be committed (not a dry-run preview).
        committed_path = log_dir / f"commit-{item_id}.plan.committed.json"
        commit_run = f"commit-{item_id}"
        commit_engine = commit_factory(
            log_dir, plan_tree_path=Path(plan_artifact), dry_run=False,
            twig=twig, manifest_path=committed_path, gate_handler=gate_handler,
        )
        commit_outcome = await commit_engine.run(commit_run)
        if not (isinstance(commit_outcome, Completed)
                and commit_outcome.disposition == "completed"):
            return PipelineResult(
                item_id=item_id, stage="commit", status="paused",
                detail="seeding ADO children did not complete cleanly; "
                       "inspect the commit-plan run before dispatching.",
                decomposable=True, plan_artifact=plan_artifact,
                committed_path=str(committed_path),
            )
        exec_inputs = ExecInputs(
            root_item=str(item_id), board=board, assignee=assignee, live=live,
            plan_tree_path=Path(plan_artifact), committed_path=committed_path,
            poll_interval_s=poll_interval_s, max_polls=max_polls, skills=skills,
        )

    # -- Phase 2.5: trunk bootstrap (ADR-0018 step 4 — BEFORE dispatch) -
    #
    # The integration trunk feature/<root> must exist *before* the leaves are
    # dispatched, because requiem opens each leaf PR with base=feature/<root>
    # right after delivery. This runs only when a github_repo is threaded; the
    # creds-light, executor-only path (no github_repo) skips topology entirely
    # and behaves exactly as before. `live=False` ⇒ dry_run ⇒ read-only probe,
    # no ref create.
    resolved_base: str | None = None
    trunk_verdict: str | None = None
    trunk_branch: str | None = None
    if github_repo is not None:
        resolved_base = base_branch or await _resolve_base_branch(github_repo, gh)
        boot_inputs = trunk_bootstrap_mod.TrunkBootstrapInputs(
            root_item_id=item_id, repo=github_repo,
            base_branch=resolved_base, dry_run=not live,
        )
        boot_run = f"trunk-{item_id}"
        boot_engine = trunk_bootstrap_factory(
            log_dir, inputs=boot_inputs, toolbelt=_gh_toolbelt(twig, gh),
            **({"gate_handler": gate_handler} if gate_handler is not None else {}),
        )
        boot_outcome = await boot_engine.run(boot_run)
        boot_final = (
            boot_outcome.final_node if isinstance(boot_outcome, Completed) else None
        )
        boot_result = trunk_bootstrap_mod.trunk_bootstrap_result(
            _completed_map(log_dir, boot_run), boot_final or "",
        )
        trunk_verdict = boot_result.verdict
        trunk_branch = boot_result.trunk_branch
        if boot_final != "end_success":
            # Fail closed: never fan out onto a trunk we could not establish.
            return PipelineResult(
                item_id=item_id, stage="trunk_bootstrap", status="paused",
                detail=(f"trunk bootstrap did not succeed "
                        f"(verdict={trunk_verdict!r}, node={boot_final!r}); "
                        "resolve before dispatching leaves."),
                decomposable=decomposable, plan_artifact=plan_artifact,
                committed_path=str(committed_path) if committed_path else None,
                github_repo=github_repo, base_branch=resolved_base,
                trunk_branch=trunk_branch, trunk_verdict=trunk_verdict,
            )

    # -- Phase 3: dispatch ---------------------------------------------
    real = Toolbelt.real()
    exec_toolbelt = Toolbelt(
        git=real.git, files=real.files, twig=twig if twig is not None else real.twig,
        kanban=kanban if kanban is not None else real.kanban,
    )
    exec_run = f"exec-{item_id}"
    exec_engine = executor_factory(
        log_dir, inputs=exec_inputs, toolbelt=exec_toolbelt,
        **({"gate_handler": gate_handler} if gate_handler is not None else {}),
    )
    exec_outcome = await exec_engine.run(exec_run)
    exec_completed = _completed_map(log_dir, exec_run)
    leaf_ids = tuple(
        leaf["leaf_id"]
        for leaf in (exec_completed.get("resolve_leaves", {}).get("value", {})
                     .get("leaves") or [])
    )
    final_node = exec_outcome.final_node if isinstance(exec_outcome, Completed) else None
    delivered = final_node == "end"

    if not delivered:
        return PipelineResult(
            item_id=item_id, stage="executor", status="paused",
            detail=f"executor stopped at {final_node!r}",
            decomposable=decomposable, leaf_ids=leaf_ids,
            plan_artifact=plan_artifact,
            committed_path=str(committed_path) if committed_path else None,
            executor_final_node=final_node,
            github_repo=github_repo, base_branch=resolved_base,
            trunk_branch=trunk_branch, trunk_verdict=trunk_verdict,
        )

    # -- Phase 4: leaf PRs (ADR-0018 step 4 — AFTER delivery) ----------
    #
    # The worker has pushed each impl/<root>-<item> branch; now requiem opens
    # (or reuses) the leaf PR base=feature/<root>. This sidesteps Hermes' missing
    # `--base`: requiem owns the PR. The {leaf_id: pr_number} map is PERSISTED —
    # a default `gh pr list` is open-only and can't re-derive merged numbers, so
    # feature_pr (a separate invocation, after the human merges) reads the
    # artifact, never a re-query. Skipped on the creds-light path.
    leaf_pr_verdict: str | None = None
    leaf_pr_leaves: tuple[LeafPr, ...] = ()
    leaf_pr_map_path: Path | None = None
    if github_repo is not None:
        lp_inputs = leaf_pr_mod.LeafPrInputs(
            root_item_id=item_id, repo=github_repo,
            leaf_ids=leaf_ids, dry_run=not live,
        )
        lp_run = f"leafpr-{item_id}"
        lp_engine = leaf_pr_factory(
            log_dir, inputs=lp_inputs, toolbelt=_gh_toolbelt(twig, gh),
            **({"gate_handler": gate_handler} if gate_handler is not None else {}),
        )
        lp_outcome = await lp_engine.run(lp_run)
        lp_final = lp_outcome.final_node if isinstance(lp_outcome, Completed) else None
        lp_result = leaf_pr_mod.leaf_pr_result(
            _completed_map(log_dir, lp_run), lp_final or "",
        )
        leaf_pr_verdict = lp_result.verdict
        leaf_pr_leaves = lp_result.leaves
        # Persist the authoritative map for the (later) feature_pr invocation.
        leaf_pr_map_path = _persist_leaf_pr_map(log_dir, item_id, leaf_pr_leaves)
        if lp_final != "end_success":
            return PipelineResult(
                item_id=item_id, stage="leaf_pr", status="paused",
                detail=(f"leaf PRs did not all open "
                        f"(verdict={leaf_pr_verdict!r}, node={lp_final!r}); "
                        "resolve the offending leaf and re-run."),
                decomposable=decomposable, leaf_ids=leaf_ids,
                plan_artifact=plan_artifact,
                committed_path=str(committed_path) if committed_path else None,
                executor_final_node=final_node,
                github_repo=github_repo, base_branch=resolved_base,
                trunk_branch=trunk_branch, trunk_verdict=trunk_verdict,
                leaf_pr_verdict=leaf_pr_verdict,
                leaf_pr_map=tuple((lp.leaf_id, lp.pr_number) for lp in leaf_pr_leaves),
                leaf_pr_map_path=str(leaf_pr_map_path),
            )

    detail = "all implementable leaves dispatched"
    if github_repo is not None:
        detail = (
            f"leaves dispatched + leaf PRs {leaf_pr_verdict} onto "
            f"{trunk_branch}; merge them, then run integrate_pipeline for the "
            "trunk→base PR."
        )
    return PipelineResult(
        item_id=item_id, stage="executor",
        status="delivered",
        detail=detail,
        decomposable=decomposable, leaf_ids=leaf_ids,
        plan_artifact=plan_artifact,
        committed_path=str(committed_path) if committed_path else None,
        executor_final_node=final_node,
        github_repo=github_repo, base_branch=resolved_base,
        trunk_branch=trunk_branch, trunk_verdict=trunk_verdict,
        leaf_pr_verdict=leaf_pr_verdict,
        leaf_pr_map=tuple((lp.leaf_id, lp.pr_number) for lp in leaf_pr_leaves),
        leaf_pr_map_path=str(leaf_pr_map_path) if leaf_pr_map_path else None,
    )


@dataclass(frozen=True, slots=True)
class IntegrationResult:
    """Outcome of the trunk→base integration leg (ADR-0018 step 4, phase 5).

    Run *after* a human / pr_lifecycle has merged the leaf PRs into the trunk.
    ``status`` is ``opened`` (trunk→base PR opened/reused), ``previewed`` (dry
    run), ``not_ready`` (the readiness gate found a leaf not yet merged — drift
    or laggard), or ``failed``.
    """

    item_id: int
    status: str
    detail: str
    github_repo: str
    base_branch: str
    trunk_branch: str
    feature_pr_verdict: str | None = None
    feature_pr_number: int | None = None
    feature_pr_url: str | None = None
    leaves_total: int = 0
    leaves_ready: int = 0
    dispositions_total: int = 0
    dispositions_satisfied: int = 0


async def integrate_pipeline(
    item_id: int,
    *,
    log_dir: Path,
    github_repo: str,
    leaf_pr_map_path: Path | None = None,
    leaves: tuple[LeafPr, ...] | None = None,
    base_branch: str | None = None,
    dispositions: tuple[ItemDisposition, ...] = (),
    live: bool = False,
    twig: Any | None = None,
    gh: Any | None = None,
    gate_handler: Any | None = None,
    feature_pr_factory: FeaturePrFactory = feature_pr_mod.build_engine,
) -> IntegrationResult:
    """Phase 5 (ADR-0018 step 4): open the trunk→base PR once leaves are merged.

    This is a SEPARATE invocation from :func:`run_pipeline` because feature_pr's
    readiness gate requires every leaf PR to be ``merged==true`` — a state only
    a human / pr_lifecycle can produce, between the two calls (requiem has no
    ``pr_merge``; INV: no self-merge). The expected-leaf set is read from the
    PERSISTED leaf-PR map (``leaf_pr_map_path``), never re-queried, because a
    default ``gh pr list`` is open-only and can't see merged numbers.

    ``dispositions`` carries the in-scope items' requirement dispositions
    (ADR-0006 INV-DRIVER-GATES-FEATURE-MERGE). The caller sources these from the
    committed plan / Twig item states; feature_pr's gate refuses to open the
    feature→base PR while any is unsatisfied (fail-closed). An empty set leaves
    the gate a no-op (the pre-gate behaviour), so a creds-light caller that does
    not track dispositions is unaffected.

    Drift policy (ratified ADR-0018, confirmed live 2026-06-09): feature_pr only
    *opens* the trunk→base PR; an unmergeable (drifted) PR is surfaced to the
    human by pr_lifecycle. No auto-rebase here.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    if leaves is None:
        if leaf_pr_map_path is None:
            raise ValueError(
                "integrate_pipeline needs either `leaves` or `leaf_pr_map_path` "
                "(the persisted {leaf_id: pr_number} map from run_pipeline)"
            )
        leaves = load_leaf_pr_map(Path(leaf_pr_map_path))

    resolved_base = base_branch or await _resolve_base_branch(github_repo, gh)
    fp_inputs = feature_pr_mod.FeaturePrInputs(
        root_item_id=item_id, repo=github_repo, leaves=leaves,
        base_branch=resolved_base, dry_run=not live, dispositions=dispositions,
    )
    fp_run = f"featurepr-{item_id}"
    fp_engine = feature_pr_factory(
        log_dir, inputs=fp_inputs, toolbelt=_gh_toolbelt(twig, gh),
        **({"gate_handler": gate_handler} if gate_handler is not None else {}),
    )
    fp_outcome = await fp_engine.run(fp_run)
    fp_final = fp_outcome.final_node if isinstance(fp_outcome, Completed) else None
    fp_result = feature_pr_mod.feature_pr_result(
        _completed_map(log_dir, fp_run), fp_final or "",
    )
    # The completed-map only carries Success values, so a NeedsHuman gate's
    # identity (which gate fired) is read from the durable gate_opened event.
    fp_gate = _gate_opened(log_dir, fp_run)

    if fp_final == "end_success":
        status = "previewed" if fp_result.dry_run else "opened"
        detail = (
            f"would open {fp_result.trunk_branch} → {resolved_base}"
            if fp_result.dry_run
            else f"integration PR #{fp_result.pr_number} opened "
                 f"({fp_result.trunk_branch} → {resolved_base})"
        )
    elif fp_final == "end_human":
        status = "not_ready"
        # Distinguish the two gate causes via the gate that actually fired: a
        # laggard leaf PR (verify_readiness) vs an unsatisfied in-scope
        # requirement disposition (verify_dispositions — ADR-0006
        # INV-DRIVER-GATES-FEATURE-MERGE).
        if fp_gate == "verify_dispositions":
            detail = (
                f"requirement dispositions not satisfied "
                f"({len(dispositions)} in-scope item(s) gated; resolve the "
                "unsatisfied item(s) and re-run)."
            )
        else:
            detail = (
                f"trunk not ready: {fp_result.leaves_ready}/{fp_result.leaves_total} "
                "expected leaf PRs merged into the trunk. Merge the laggards (or "
                "resolve a drifted/unmergeable leaf PR) and re-run."
            )
    else:
        status = "failed"
        detail = f"feature_pr failed (node={fp_final!r})"

    return IntegrationResult(
        item_id=item_id, status=status, detail=detail,
        github_repo=github_repo, base_branch=resolved_base,
        trunk_branch=fp_result.trunk_branch,
        feature_pr_verdict=fp_result.verdict,
        feature_pr_number=fp_result.pr_number,
        feature_pr_url=fp_result.pr_url,
        leaves_total=fp_result.leaves_total,
        leaves_ready=fp_result.leaves_ready,
        dispositions_total=fp_result.dispositions_total,
        dispositions_satisfied=fp_result.dispositions_satisfied,
    )


# ---- operator CLI ----------------------------------------------------


def _build_arg_parser():
    import argparse

    p = argparse.ArgumentParser(
        prog="requiem-end-to-end",
        description="Run Requiem end-to-end against one ADO work item: "
                    "plan → seed → dispatch implementable leaves to Hermes.",
    )
    p.add_argument("--item", type=int, required=True,
                   help="ADO work-item id to deliver.")
    p.add_argument("--board", required=True,
                   help="Dedicated Hermes kanban board (NEVER 'default').")
    p.add_argument("--assignee", default=None,
                   help="Worker profile to assign dispatched leaf tasks to.")
    p.add_argument("--commit", action="store_true",
                   help="Actually seed ADO children for a decomposable plan "
                        "(default: stop after planning).")
    p.add_argument("--live", action="store_true",
                   help="Actually spawn Hermes workers (default: dispatch dry-run).")
    p.add_argument("--log-dir", type=Path, default=Path(".runs"),
                   help="Durable run-log directory (default: .runs).")
    p.add_argument("--repo", type=Path, default=Path("."),
                   help="Repo root to discover .requiem-config/process.yaml from "
                        "(drives the type-agnostic tier policy; default: cwd).")
    p.add_argument("--github-repo", default=None,
                   help="GitHub repo identity 'Owner/Repo' for trunk topology "
                        "(ADR-0018 step 4). When set, the driver bootstraps "
                        "feature/<root> before dispatch and opens leaf PRs after "
                        "delivery. Omit to run the legacy executor-only pipeline.")
    p.add_argument("--base-branch", default=None,
                   help="Override the trunk's base branch. Default: resolve the "
                        "GitHub repo's real default branch (Q2).")
    p.add_argument("--poll-interval", type=float, default=5.0)
    p.add_argument("--max-polls", type=int, default=120)
    return p


def _build_integrate_arg_parser():
    import argparse

    p = argparse.ArgumentParser(
        prog="requiem-integrate",
        description="ADR-0018 step 4 phase 5: open the trunk→base integration PR "
                    "AFTER the leaf PRs have been merged into feature/<root>. "
                    "Reads the persisted {leaf_id: pr_number} map from run_pipeline.",
    )
    p.add_argument("--item", type=int, required=True,
                   help="Root ADO work-item id (the run root).")
    p.add_argument("--github-repo", required=True,
                   help="GitHub repo identity 'Owner/Repo'.")
    p.add_argument("--leaf-pr-map", type=Path, default=None,
                   help="Path to the persisted leaf-PR map "
                        "(default: <log-dir>/leaf-pr-map-<item>.json).")
    p.add_argument("--base-branch", default=None,
                   help="Override the base branch (default: resolve the repo's "
                        "real default branch).")
    p.add_argument("--live", action="store_true",
                   help="Actually open the trunk→base PR (default: dry-run preview).")
    p.add_argument("--log-dir", type=Path, default=Path(".runs"),
                   help="Durable run-log directory (default: .runs).")
    return p


def main(argv: list[str] | None = None) -> int:
    import asyncio

    from requiem.clients.kanban import KanbanClient
    from requiem.clients.twig import TwigClient
    from requiem.process_config import discover_process_config
    from requiem.providers import default_provider

    args = _build_arg_parser().parse_args(argv)
    if args.board == "default":
        print("refusing to use the 'default' Hermes board; pass a dedicated "
              "--board (e.g. requiem-<item>).")
        return 2

    # Discover the repo's tier policy (falls back to polyphony-equivalent
    # defaults when no .requiem-config/process.yaml is present).
    process_config = discover_process_config(args.repo)

    result = asyncio.run(run_pipeline(
        args.item,
        log_dir=args.log_dir,
        board=args.board,
        assignee=args.assignee,
        commit=args.commit,
        live=args.live,
        github_repo=args.github_repo,
        base_branch=args.base_branch,
        twig=TwigClient(),
        provider=default_provider(),
        kanban=KanbanClient(),
        process_config=process_config,
        poll_interval_s=args.poll_interval,
        max_polls=args.max_polls,
    ))

    print(f"[{result.stage}] {result.status}: {result.detail}")
    if result.leaf_ids:
        print(f"  leaves: {', '.join(result.leaf_ids)}")
    if result.plan_artifact:
        print(f"  plan:   {result.plan_artifact}")
    if result.committed_path:
        print(f"  seeded: {result.committed_path}")
    if result.trunk_branch:
        print(f"  trunk:  {result.trunk_branch} ({result.trunk_verdict}) "
              f"off {result.base_branch}")
    if result.leaf_pr_map:
        rendered = ", ".join(
            f"{lid}#{n}" if n is not None else f"{lid}#?" for lid, n in result.leaf_pr_map
        )
        print(f"  leafPR: {result.leaf_pr_verdict} — {rendered}")
    if result.leaf_pr_map_path:
        print(f"  map:    {result.leaf_pr_map_path}")
    return 0 if result.status in ("delivered", "planned") else 1


def integrate_main(argv: list[str] | None = None) -> int:
    """Entrypoint for the trunk→base integration leg (phase 5)."""
    import asyncio

    from requiem.clients.twig import TwigClient

    args = _build_integrate_arg_parser().parse_args(argv)
    map_path = args.leaf_pr_map or (args.log_dir / f"leaf-pr-map-{args.item}.json")
    if not Path(map_path).exists():
        print(f"leaf-PR map not found: {map_path}\n"
              "Run the dispatch pipeline first (it persists the map), or pass "
              "--leaf-pr-map explicitly.")
        return 2

    result = asyncio.run(integrate_pipeline(
        args.item,
        log_dir=args.log_dir,
        github_repo=args.github_repo,
        leaf_pr_map_path=Path(map_path),
        base_branch=args.base_branch,
        live=args.live,
        twig=TwigClient(),
    ))

    print(f"[integrate] {result.status}: {result.detail}")
    if result.feature_pr_url:
        print(f"  PR:     {result.feature_pr_url}")
    print(f"  trunk:  {result.trunk_branch} → {result.base_branch}")
    print(f"  ready:  {result.leaves_ready}/{result.leaves_total} leaves merged")
    return 0 if result.status in ("opened", "previewed") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
