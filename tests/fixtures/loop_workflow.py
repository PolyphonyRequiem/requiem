"""Minimal edge-loop workflow — distinct from single-node retry loops.

Shape (revision-style loop, max 3 iterations):

    start (script)
      → revise (script: bumps an in-state counter)
      → check  (script: emits BadOutput while counter < 3, Success at == 3)
            → on bad_output         → revise   (loop back)
            → on success            → end (terminate completed)
            → on permanent_failure  → fail_end (terminate failed)

The loop-back edge fires on the ``bad_output`` outcome (a distinct
discriminated-outcome variant, per INV-DISCRIMINATED-OUTCOMES) rather than
on a single-node retry, so the cursor visits ``revise`` and ``check``
*alternately* and ``completed`` is overwritten each loop pass.

The iteration counter is derived by replaying the event log every time
``check`` runs — INV-EVENT-LOG-AUTHORITATIVE. No in-memory counter; resume
rebuilds it deterministically.
"""
from __future__ import annotations

from pathlib import Path

from requiem.agent import FakeProvider
from requiem.dsl import AgentRegistry, VerbRegistry, Workflow, WorkflowBuilder
from requiem.kernel import Engine
from requiem.outcomes import BadOutput, Success
from requiem.persistence import replay
from requiem.toolbelt import Toolbelt


MAX_ITERATIONS = 3


def _check_entries_in_log(log_dir: Path, run_id: str) -> int:
    path = log_dir / f"{run_id}.events.jsonl"
    if not path.exists():
        return 0
    return sum(
        1
        for ev in replay(path)
        if ev.get("kind") == "node_entered" and ev.get("node_id") == "check"
    )


def build_workflow() -> Workflow:
    return (
        WorkflowBuilder("revision-loop", module="tests.fixtures.loop_workflow")
            .entry("start")
            .script("start", verb="noop")
                .edge("start", on="success", to="revise")
            .script("revise", verb="noop")
                .edge("revise", on="success", to="check")
            .script("check", verb="check_iteration")
                .edge("check", on="bad_output", to="revise")
                .edge("check", on="success", to="end")
                .edge("check", on="permanent_failure", to="fail_end")
            .terminate("end", disposition="completed")
            .terminate("fail_end", disposition="failed")
            .build()
    )


def build_verb_registry(log_dir: Path) -> VerbRegistry:
    verbs = VerbRegistry()

    @verbs.register("noop")
    def _noop(_ctx):
        return Success(value={"ok": True})

    @verbs.register("check_iteration")
    def _check(ctx):
        # ``node_entered`` for the current ``check`` fired moments before
        # this verb ran, so it IS included in the count.
        iteration = _check_entries_in_log(log_dir, ctx.run_id)
        if iteration < MAX_ITERATIONS:
            return BadOutput(
                error_kind="needs_revision",
                validation_errors=(f"iteration {iteration} requires revision",),
                raw_output=str(iteration),
            )
        return Success(value={"final_iteration": iteration})

    return verbs


def build_engine(log_dir: Path) -> Engine:
    return Engine(
        workflow=build_workflow(),
        verbs=build_verb_registry(log_dir),
        agents=AgentRegistry(),
        provider=FakeProvider(),
        toolbelt=Toolbelt.real(),
        log_dir=log_dir,
    )
