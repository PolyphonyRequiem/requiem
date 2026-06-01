"""Tiny demo: a parent workflow that invokes the code-review demo as a
sub-workflow.

Demonstrates the Phase B sub-workflow primitive (ADR 0005). Run via::

    requiem run requiem.workflows.subworkflow_demo --run-id wrap

The CLI output shows the parent's spawn-marker, the child's full
narration, the parent's return-marker, and parent terminate. The
child's events live in their own ``{sub_run_id}.events.jsonl`` per
INV-SUBWORKFLOW-LOG-ISOLATION.
"""
from __future__ import annotations

from pathlib import Path

from requiem.agent import FakeProvider
from requiem.dsl import AgentRegistry, VerbRegistry, Workflow, WorkflowBuilder
from requiem.kernel import Engine
from requiem.outcomes import Success
from requiem.toolbelt import Toolbelt
from requiem.workflows import code_review_demo


def build_workflow() -> Workflow:
    return (
        WorkflowBuilder(
            "subworkflow-demo",
            module="requiem.workflows.subworkflow_demo",
            version="0.1",
        )
            .entry("announce")
            .script("announce", verb="announce")
                .edge("announce", on="success", to="invoke_review")
            .subworkflow(
                "invoke_review",
                workflow="requiem.workflows.code_review_demo",
            )
                .edge("invoke_review", on="success",           to="done")
                .edge("invoke_review", on="permanent_failure", to="failed_end")
            .terminate("done", disposition="completed")
            .terminate("failed_end", disposition="failed")
            .humanize({
                "announce":      "wrapping code-review",
                "invoke_review": "code-review (as sub-workflow)",
                "done":          "subworkflow-demo",
                "failed_end":    "subworkflow-demo",
            })
            .build()
    )


def build_verb_registry() -> VerbRegistry:
    verbs = VerbRegistry()

    @verbs.register("announce")
    def _announce(ctx):
        return Success(value={"about_to_invoke": "code_review_demo"})

    return verbs


def _parent_gate_handler(node_id, prompt, options):
    """If a child gate bubbles up to the parent, auto-approve for the demo."""
    return "approve" if "approve" in options else options[0]


_parent_gate_handler.__requiem_auto__ = True  # type: ignore[attr-defined]


def build_engine(log_dir: Path, *, gate_handler=None) -> Engine:
    """Parent engine. Child's own ``build_engine`` is invoked by the kernel."""
    return Engine(
        workflow=build_workflow(),
        verbs=build_verb_registry(),
        agents=AgentRegistry(),
        provider=FakeProvider(),
        toolbelt=Toolbelt.real(),
        log_dir=log_dir,
        gate_handler=gate_handler or _parent_gate_handler,
    )


def render_hints() -> dict:
    return {
        "details": {},
        "gate_contexts": {},
        # The child's verb_completed lines tell the full story; the parent's
        # `announce` is a one-line setup verb whose label already conveys it.
        "silent_nodes": frozenset({"announce", "done", "failed_end"}),
    }


# Re-export the child's specs/verbs so `requiem run` can still find the
# child workflow's agents/verbs without separate import wiring.
__all__ = [
    "build_workflow",
    "build_engine",
    "build_verb_registry",
    "render_hints",
    "code_review_demo",
]
