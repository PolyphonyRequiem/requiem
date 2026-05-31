"""Node classes — explicit class hierarchy.

Each node is a class with an `execute(ctx) -> Outcome` method. The
engine calls execute, reads the outcome's kind, and consults the
workflow's transition table to pick the next node.

Authoring ergonomics: workflows are built by instantiating nodes and
registering them with a Workflow. The transition table is a separate
mapping (node_id, outcome_kind, route_label) -> next_node_id.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from outcomes import (
    Outcome,
    Success,
    RetryableFailure,
    PermanentFailure,
    NeedsHuman,
    Cancelled,
)


# ----- Execution context passed to every node -----


@dataclass
class NodeContext:
    """What a node sees. Read-only from the node's POV."""
    run_id: str
    workflow_id: str
    node_id: str
    inputs: dict[str, Any]
    # Outputs of previously-completed nodes in THIS run (in-memory derived
    # from event log on resume — never carried across the seam).
    completed: dict[str, dict[str, Any]]
    # Attempt number on the current node (1-based).
    attempt: int
    # A cooperative cancel flag. Nodes that loop or sleep must check.
    cancel_requested: Callable[[], bool]


# ----- Node base + concrete types -----


@dataclass
class Node:
    node_id: str
    # retry budget consumed by RetryableFailure outcomes.
    # 0 means "do not retry"; 2 means "up to 3 attempts total".
    retry_max: int = 0

    def execute(self, ctx: NodeContext) -> Outcome:
        raise NotImplementedError


@dataclass
class AgentStep(Node):
    """Wraps an LLM-shaped verb. Body is a plain callable for the prototype.

    In real Requiem this is Stravinsky's agent boundary; the kernel
    only sees: input -> Outcome.
    """
    body: Callable[[NodeContext], Outcome] = field(default=lambda _: Success())

    def execute(self, ctx: NodeContext) -> Outcome:
        return self.body(ctx)


@dataclass
class ScriptStep(Node):
    """Wraps a deterministic verb. Same shape as AgentStep at this seam."""
    body: Callable[[NodeContext], Outcome] = field(default=lambda _: Success())

    def execute(self, ctx: NodeContext) -> Outcome:
        return self.body(ctx)


@dataclass
class HumanGate(Node):
    """Always returns NeedsHuman. The engine suspends; a separate
    `resolve_gate` call resumes with the operator's choice."""
    prompt: str = ""
    options: list[str] = field(default_factory=list)

    def execute(self, ctx: NodeContext) -> Outcome:
        return NeedsHuman(prompt=self.prompt, options=list(self.options))


@dataclass
class Route(Node):
    """A pure routing node: inspects prior outputs and emits a Success
    whose payload contains a 'route' field. Engine reads that field to
    pick the next transition. No external side effects."""
    chooser: Callable[[NodeContext], str] = field(default=lambda _: "default")

    def execute(self, ctx: NodeContext) -> Outcome:
        choice = self.chooser(ctx)
        return Success(value={"route": choice})


@dataclass
class SubworkflowCall(Node):
    """Invokes another workflow as a sub-run. Engine handles recursion."""
    target_workflow: str = ""
    inputs_from: Callable[[NodeContext], dict[str, Any]] = field(
        default=lambda _: {}
    )

    def execute(self, ctx: NodeContext) -> Outcome:
        # The engine intercepts this kind before calling execute; this
        # body is a safety net.
        raise RuntimeError("SubworkflowCall must be handled by the engine")


@dataclass
class Terminate(Node):
    """Terminal node. The engine stops the run with the given disposition.

    `disposition` is one of: completed | abandoned | superseded | failed.
    Mirrors the abandonment-typology decision from the deep dive.
    """
    disposition: str = "completed"

    def execute(self, ctx: NodeContext) -> Outcome:
        return Success(value={"disposition": self.disposition})
