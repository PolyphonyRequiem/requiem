"""Shared engine + canonical workflow data model.

All three variants emit a `Workflow` instance defined here. The engine,
the topology introspector, and the harness/test layer all consume this
exact shape — that is the load-bearing claim of this seam: *the engine
sees the same structure regardless of how the author wrote it*.

Invariants this seam honours (north-star §2):
  - INV-DISCRIMINATED-OUTCOMES: `Outcome` is a tagged union; the engine
    routes off `.kind` only, never inspects payload to decide.
  - INV-NO-CORRUPT-FORWARD: topology validation refuses to construct a
    `Workflow` whose routes reference unknown nodes. Authors cannot
    silently typo their way into a half-wired graph.
  - INV-EVENT-LOG-AUTHORITATIVE: the engine returns a trace of every
    transition; a real engine writes that to `run.events.jsonl`. The
    prototype keeps it in memory.
"""
from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Callable, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ── Verb outcome (discriminated union) ───────────────────────────────────
# A real Requiem verb returns one of these. The tag is the contract; the
# router never looks at the payload to decide what to do next.

class Success(BaseModel):
    kind: Literal["success"] = "success"
    data: dict[str, Any] = Field(default_factory=dict)


class Failure(BaseModel):
    kind: Literal["failure"] = "failure"
    reason: str = ""


class NeedsHuman(BaseModel):
    kind: Literal["needs_human"] = "needs_human"
    prompt: str = ""


class Cancelled(BaseModel):
    kind: Literal["cancelled"] = "cancelled"


Outcome = Annotated[
    Union[Success, Failure, NeedsHuman, Cancelled],
    Field(discriminator="kind"),
]

OUTCOME_KINDS = ("success", "failure", "needs_human", "cancelled")


# ── Node + Route + Workflow ──────────────────────────────────────────────

class NodeKind(str, Enum):
    VERB = "verb"
    HUMAN_GATE = "human_gate"
    SUBWORKFLOW = "subworkflow"


# Implicit terminal pseudo-nodes. Routes may target $end; the engine
# stops cleanly when it lands on one. $start is reserved for symmetry
# with conductor; routing FROM $start is not used (Workflow.entry is
# the explicit entry node).
TERMINALS = {"$start", "$end"}


class Node(BaseModel):
    name: str
    kind: NodeKind
    # populated for kind=VERB; engine calls verb(context) -> Outcome
    verb: Optional[Callable[[dict], Any]] = None
    # populated for kind=HUMAN_GATE
    prompt: Optional[str] = None
    # populated for kind=SUBWORKFLOW; the workflow name to invoke
    subworkflow: Optional[str] = None

    model_config = ConfigDict(arbitrary_types_allowed=True)


class Route(BaseModel):
    from_node: str
    when: str  # an OUTCOME_KIND, or "*" (any)
    to_node: str


class Workflow(BaseModel):
    name: str
    entry: str
    nodes: list[Node]
    routes: list[Route]

    # Topology validation runs at construction. This is the "typo catch"
    # demo: a Workflow whose routes name unknown nodes refuses to exist.
    @model_validator(mode="after")
    def _validate_topology(self) -> "Workflow":
        errors = self.topology_errors()
        if errors:
            raise ValueError(
                f"workflow '{self.name}' has topology errors:\n  - "
                + "\n  - ".join(errors)
            )
        return self

    def topology_errors(self) -> list[str]:
        errs: list[str] = []
        known = {n.name for n in self.nodes} | TERMINALS
        # duplicate node names
        seen: set[str] = set()
        for n in self.nodes:
            if n.name in seen:
                errs.append(f"duplicate node name '{n.name}'")
            seen.add(n.name)
        if self.entry not in known:
            errs.append(f"entry '{self.entry}' is not a known node")
        for r in self.routes:
            if r.from_node not in known:
                errs.append(
                    f"route from unknown node '{r.from_node}' -> '{r.to_node}'"
                )
            if r.to_node not in known:
                errs.append(
                    f"route '{r.from_node}' -> unknown node '{r.to_node}'"
                )
            if r.when not in OUTCOME_KINDS and r.when != "*":
                errs.append(
                    f"route '{r.from_node}' has unknown outcome '{r.when}'"
                )
        # every non-terminal node should have at least one outgoing route
        sources = {r.from_node for r in self.routes}
        for n in self.nodes:
            if n.name not in sources:
                errs.append(f"node '{n.name}' has no outgoing routes")
        return errs


# ── Registry + Engine ────────────────────────────────────────────────────

class WorkflowRegistry:
    """In-memory map of workflow name -> Workflow. A real engine would
    persist this; for the prototype it lives for the run."""

    def __init__(self) -> None:
        self._wfs: dict[str, Workflow] = {}

    def register(self, wf: Workflow) -> None:
        self._wfs[wf.name] = wf

    def get(self, name: str) -> Workflow:
        return self._wfs[name]


class Engine:
    """Thin executor. Takes a Workflow + context, returns the trace.

    A real Requiem engine writes events to run.events.jsonl as it goes;
    the trace is the in-memory projection. For the prototype we return
    the trace directly so demos can `print(trace)`.
    """

    def __init__(
        self,
        registry: WorkflowRegistry,
        human: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.registry = registry
        # default human responder: always approves
        self.human = human or (lambda prompt: Success())

    def run(self, wf: Workflow, context: Optional[dict] = None) -> list[dict]:
        context = context or {}
        trace: list[dict] = []
        current = wf.entry
        guard = 0
        while True:
            guard += 1
            if guard > 1000:
                raise RuntimeError("engine: iteration cap exceeded")
            if current in TERMINALS:
                trace.append({"event": "terminated", "node": current})
                return trace
            node = self._lookup(wf, current)
            trace.append({"event": "node_entered", "node": current})
            outcome = self._invoke(node, context)
            trace.append(
                {"event": "node_completed", "node": current, "outcome": outcome.kind}
            )
            route = self._pick_route(wf, current, outcome)
            if route is None:
                raise RuntimeError(
                    f"no route from '{current}' on outcome '{outcome.kind}'"
                )
            trace.append(
                {
                    "event": "route_taken",
                    "from": route.from_node,
                    "when": route.when,
                    "to": route.to_node,
                }
            )
            current = route.to_node

    def _invoke(self, node: Node, context: dict):
        if node.kind == NodeKind.VERB:
            assert node.verb, f"verb node '{node.name}' has no verb"
            return node.verb(context)
        if node.kind == NodeKind.HUMAN_GATE:
            return self.human(node.prompt or node.name)
        if node.kind == NodeKind.SUBWORKFLOW:
            assert node.subworkflow, f"subworkflow node '{node.name}' has no target"
            child = self.registry.get(node.subworkflow)
            self.run(child, context)
            return Success()
        raise RuntimeError(f"unknown node kind: {node.kind}")

    @staticmethod
    def _lookup(wf: Workflow, name: str) -> Node:
        for n in wf.nodes:
            if n.name == name:
                return n
        raise RuntimeError(f"no node '{name}' in workflow '{wf.name}'")

    @staticmethod
    def _pick_route(wf: Workflow, from_node: str, outcome) -> Optional[Route]:
        # exact-match wins over wildcard
        for r in wf.routes:
            if r.from_node == from_node and r.when == outcome.kind:
                return r
        for r in wf.routes:
            if r.from_node == from_node and r.when == "*":
                return r
        return None


# ── Static topology introspection (for UI + harness, no run required) ────

def next_node(wf: Workflow, from_node: str, when: str) -> Optional[str]:
    """`from node X, on outcome Y, we transition to Z` — answered without
    running. This is the harness's path-coverage primitive."""
    for r in wf.routes:
        if r.from_node == from_node and r.when == when:
            return r.to_node
    for r in wf.routes:
        if r.from_node == from_node and r.when == "*":
            return r.to_node
    return None


def describe(wf: Workflow) -> str:
    """Pretty-print the topology. The UI's graph view would consume the
    same fields; this is the text-mode equivalent."""
    lines = [f"workflow: {wf.name} (entry: {wf.entry})", "  nodes:"]
    for n in wf.nodes:
        extra = ""
        if n.kind == NodeKind.SUBWORKFLOW:
            extra = f" -> {n.subworkflow}"
        elif n.kind == NodeKind.HUMAN_GATE:
            extra = f" [{(n.prompt or '')[:40]}]"
        lines.append(f"    - {n.name} ({n.kind.value}){extra}")
    lines.append("  routes:")
    for r in wf.routes:
        lines.append(f"    - {r.from_node} --[{r.when}]--> {r.to_node}")
    return "\n".join(lines)


# ── Stub verbs and fake agent used by the demos ──────────────────────────
# Real verbs live in the verb library. These stand in.

def verify_done(context: dict) -> Success | Failure:
    """Pretends to verify the work item is complete."""
    if context.get("force_failure"):
        return Failure(reason="prerequisites not met")
    return Success(data={"verified": True})


def archive_run_stub(context: dict) -> Success:
    return Success(data={"archived_to": "blob://runs/2026-05-31"})


def send_notification(context: dict) -> Success:
    return Success(data={"sent_to": "operator"})


class FakeAgent:
    """Stand-in for an LLM/agent invocation. Returns a scripted outcome."""

    def __init__(self, scripted: dict[str, Any]) -> None:
        self.scripted = scripted
        self.calls: list[str] = []

    def __call__(self, prompt: str):
        self.calls.append(prompt)
        return self.scripted.get(prompt, Success())
