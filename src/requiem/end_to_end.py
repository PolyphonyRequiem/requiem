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

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from requiem.kernel import Completed, Engine
from requiem.persistence import replay
from requiem.toolbelt import Toolbelt
from requiem.workflows import commit_plan as commit_plan_mod
from requiem.workflows import kanban_executor as executor_mod
from requiem.workflows import planning as planning_mod
from requiem.workflows.kanban_executor import ExecInputs, LeafSpec

# Engine factories are injected (defaulting to the real ones) so the pipeline
# orchestration is unit-testable with stub engines that don't need an LLM/ADO.
PlanningFactory = Callable[..., Engine]
CommitFactory = Callable[..., Engine]
ExecutorFactory = Callable[..., Engine]


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


def _completed_map(log_dir: Path, run_id: str) -> dict[str, dict]:
    """Reconstruct the node→outcome map from a finished engine's durable log."""
    completed: dict[str, dict] = {}
    for ev in replay(log_dir / f"{run_id}.events.jsonl"):
        if ev.get("kind") == "verb_completed":
            completed[ev["node_id"]] = ev["payload"]["outcome"]
    return completed


def _plan_record(completed: dict[str, dict]) -> dict[str, Any] | None:
    """Pull the planning record value (approved or needs-human), if present."""
    for node in ("record_plan", "record_needs_human"):
        block = completed.get(node)
        if block and block.get("kind") == "success":
            return block.get("value")
    return None


async def run_pipeline(
    item_id: int,
    *,
    log_dir: Path,
    board: str,
    assignee: str | None = None,
    commit: bool = False,
    live: bool = False,
    skills: tuple[str, ...] = (),
    twig: Any | None = None,
    provider: Any | None = None,
    kanban: Any | None = None,
    gate_handler: Any | None = None,
    process_config: Any | None = None,
    poll_interval_s: float = 5.0,
    max_polls: int = 120,
    planning_factory: PlanningFactory = planning_mod.build_engine,
    commit_factory: CommitFactory = commit_plan_mod.build_engine,
    executor_factory: ExecutorFactory = executor_mod.build_engine,
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
            branch=f"impl/{item_id}-{item_id}",
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
        l["leaf_id"]
        for l in (exec_completed.get("resolve_leaves", {}).get("value", {})
                  .get("leaves") or [])
    )
    final_node = exec_outcome.final_node if isinstance(exec_outcome, Completed) else None
    delivered = final_node == "end"
    return PipelineResult(
        item_id=item_id, stage="executor",
        status="delivered" if delivered else "paused",
        detail=("all implementable leaves dispatched"
                if delivered else f"executor stopped at {final_node!r}"),
        decomposable=decomposable, leaf_ids=leaf_ids,
        plan_artifact=plan_artifact,
        committed_path=str(committed_path) if committed_path else None,
        executor_final_node=final_node,
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
    p.add_argument("--poll-interval", type=float, default=5.0)
    p.add_argument("--max-polls", type=int, default=120)
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
    return 0 if result.status in ("delivered", "planned") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
