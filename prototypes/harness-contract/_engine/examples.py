"""Example workflows used by all three variants.

Building these in Python (not YAML) keeps the engine seam tight and
lets each variant focus on its scenario-contract shape, not on
workflow-definition syntax."""

from __future__ import annotations

from .outcomes import RetryableFailure, Success
from .workflow import Node, Workflow


def tiny_three_node() -> Workflow:
    """script(load) -> agent(architect) -> terminal(completed).
    Smoke-tests every variant's "scripted happy path"."""
    return Workflow(
        name="tiny",
        start="load",
        nodes={
            "load": Node(id="load", kind="script", verb=lambda: Success(payload={"loaded": True}), next="architect"),
            "architect": Node(id="architect", kind="agent", agent="architect", next="end"),
            "end": Node(id="end", kind="terminal", terminal_label="completed"),
        },
    )


def transient_failure_workflow(fail_times: int = 2):
    """A single script node that fails `fail_times` then succeeds.
    Used to assert retry count."""
    attempts = {"n": 0}

    def flaky() -> object:
        attempts["n"] += 1
        if attempts["n"] <= fail_times:
            return RetryableFailure(reason=f"simulated transient #{attempts['n']}")
        return Success(payload={"attempt_succeeded_on": attempts["n"]})

    return Workflow(
        name="flaky",
        start="flaky",
        nodes={
            "flaky": Node(id="flaky", kind="script", verb=flaky, next="end"),
            "end": Node(id="end", kind="terminal", terminal_label="completed"),
        },
    ), attempts


def gated_workflow() -> Workflow:
    """architect -> gate(approve/abort) -> {merge | abort_run}.
    Used for human-gate branch-coverage scenarios."""
    return Workflow(
        name="gated",
        start="architect",
        nodes={
            "architect": Node(id="architect", kind="agent", agent="architect", next="gate"),
            "gate": Node(
                id="gate",
                kind="human_gate",
                options=["approve", "abort"],
                routes={"approve": "merge", "abort": "abort_run"},
            ),
            "merge": Node(id="merge", kind="script", verb=lambda: Success(payload={"merged": True}), next="end_ok"),
            "abort_run": Node(id="abort_run", kind="script", verb=lambda: Success(payload={"aborted": True}), next="end_abort"),
            "end_ok": Node(id="end_ok", kind="terminal", terminal_label="completed"),
            "end_abort": Node(id="end_abort", kind="terminal", terminal_label="aborted"),
        },
    )


def parent_with_subworkflow() -> Workflow:
    """parent.preamble -> parent.child(subworkflow) -> parent.epilogue -> end.

    The child workflow has its own agent ("reviewer") so the variants
    must demonstrate how to script the child's agents."""
    child = Workflow(
        name="child",
        start="reviewer",
        nodes={
            "reviewer": Node(id="reviewer", kind="agent", agent="reviewer", next="child_end"),
            "child_end": Node(id="child_end", kind="terminal", terminal_label="completed"),
        },
    )
    return Workflow(
        name="parent",
        start="preamble",
        nodes={
            "preamble": Node(id="preamble", kind="agent", agent="architect", next="call_child"),
            "call_child": Node(id="call_child", kind="subworkflow", subworkflow=child, next="epilogue"),
            "epilogue": Node(id="epilogue", kind="script", verb=lambda: Success(payload={"done": True}), next="end"),
            "end": Node(id="end", kind="terminal", terminal_label="completed"),
        },
    )
