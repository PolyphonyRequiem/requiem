"""Minimal gate-in-middle workflow.

Shape:

    start (script)
      → prep (script)
      → gate (human_gate)
        → on needs_human:approve → post (script) → end (terminate completed)
        → on needs_human:reject  → fail_end (terminate failed)

The auto-approve handler routes every run to ``end``; the matrix tests
override it for the ``reject`` branch.
"""
from __future__ import annotations

from pathlib import Path

from requiem.agent import FakeProvider
from requiem.dsl import AgentRegistry, VerbRegistry, Workflow, WorkflowBuilder
from requiem.kernel import Engine
from requiem.outcomes import Success
from requiem.toolbelt import Toolbelt


def build_workflow() -> Workflow:
    return (
        WorkflowBuilder("gate-in-middle", module="tests.fixtures.gate_workflow")
            .entry("start")
            .script("start", verb="noop")
                .edge("start", on="success", to="prep")
            .script("prep", verb="noop")
                .edge("prep", on="success", to="gate")
            .human_gate(
                "gate",
                prompt="Continue?",
                options=["approve", "reject"],
            )
                .edge("gate", on="needs_human:approve", to="post")
                .edge("gate", on="needs_human:reject", to="fail_end")
            .script("post", verb="noop")
                .edge("post", on="success", to="end")
            .terminate("end", disposition="completed")
            .terminate("fail_end", disposition="failed")
            .build()
    )


def build_verb_registry() -> VerbRegistry:
    verbs = VerbRegistry()

    @verbs.register("noop")
    def _noop(_ctx):
        return Success(value={"ok": True})

    return verbs


def _approve(_node_id: str, _prompt: str, _opts: tuple[str, ...]) -> str:
    return "approve"


def _reject(_node_id: str, _prompt: str, _opts: tuple[str, ...]) -> str:
    return "reject"


def build_engine(log_dir: Path, *, choice: str = "approve") -> Engine:
    handler = _approve if choice == "approve" else _reject
    return Engine(
        workflow=build_workflow(),
        verbs=build_verb_registry(),
        agents=AgentRegistry(),
        provider=FakeProvider(),
        toolbelt=Toolbelt.real(),
        log_dir=log_dir,
        gate_handler=handler,
    )
