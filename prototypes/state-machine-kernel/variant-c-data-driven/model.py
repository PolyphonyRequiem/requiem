"""Variant C: workflow is a pydantic data model. Engine is an
interpreter over the model.

Key consequences:
  - Workflows-as-data: the model serialises to JSON; a UI can render
    the topology without executing the workflow.
  - Static analysis is trivial: walk the model, check edge
    well-formedness, dead-node detection, unreachable terminals,
    type-check verb refs against the verb registry.
  - Verb bodies live in a SEPARATE `VerbRegistry`. The workflow refers
    to verbs by name (string). This is the cleanest separation of
    *what the workflow is* from *what it does*.
  - Forces a clean DSL/data boundary that Wagner's DSL seam will sit on.

The interpreter mirrors variants A and B: same event log, same
suspension model, same retry budget, same cancel short-circuit.
"""
from __future__ import annotations

from typing import Any, Callable, Literal, Union
from pydantic import BaseModel, Field

from outcomes import Outcome


# ----- Node shape variants (discriminated union over `kind`) -----


class AgentNode(BaseModel):
    kind: Literal["agent"] = "agent"
    node_id: str
    verb: str                       # name in the VerbRegistry
    retry_max: int = 0


class ScriptNode(BaseModel):
    kind: Literal["script"] = "script"
    node_id: str
    verb: str
    retry_max: int = 0


class HumanGateNode(BaseModel):
    kind: Literal["human_gate"] = "human_gate"
    node_id: str
    prompt: str
    options: list[str]


class RouteNode(BaseModel):
    kind: Literal["route"] = "route"
    node_id: str
    chooser: str                    # name in registry; returns str


class SubworkflowNode(BaseModel):
    kind: Literal["subworkflow"] = "subworkflow"
    node_id: str
    target_workflow: str
    inputs_from: str                # name in registry; returns dict


class TerminateNode(BaseModel):
    kind: Literal["terminate"] = "terminate"
    node_id: str
    disposition: Literal["completed", "abandoned", "superseded", "failed"] = "completed"


NodeModel = Union[
    AgentNode, ScriptNode, HumanGateNode, RouteNode,
    SubworkflowNode, TerminateNode,
]


class Edge(BaseModel):
    """One transition in the table. The kernel uses outcome_key as a
    string: `success`, `permanent_failure`, `permanent_failure:<error_kind>`,
    `needs_human:<choice>`, `success:<route_label>`, `retry_exhausted`."""
    from_node: str
    outcome_key: str
    to_node: str


class WorkflowModel(BaseModel):
    workflow_id: str
    start: str
    nodes: list[NodeModel] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)

    def node_map(self) -> dict[str, NodeModel]:
        m = {n.node_id: n for n in self.nodes}
        if len(m) != len(self.nodes):
            raise ValueError("duplicate node_id in workflow")
        return m

    def edge_map(self) -> dict[tuple[str, str], str]:
        return {(e.from_node, e.outcome_key): e.to_node for e in self.edges}

    def validate_topology(self) -> list[str]:
        """Returns a list of errors (empty = healthy)."""
        errs: list[str] = []
        nm = {n.node_id: n for n in self.nodes}
        for e in self.edges:
            if e.from_node not in nm:
                errs.append(f"edge from unknown node: {e.from_node!r}")
            if e.to_node not in nm:
                errs.append(f"edge to unknown node: {e.to_node!r}")
        if self.start not in nm:
            errs.append(f"start {self.start!r} not in nodes")
        # Reachability check (BFS from start)
        reachable: set[str] = set()
        frontier: list[str] = [self.start] if self.start in nm else []
        while frontier:
            x = frontier.pop()
            if x in reachable: continue
            reachable.add(x)
            for e in self.edges:
                if e.from_node == x and e.to_node in nm:
                    frontier.append(e.to_node)
        for n in self.nodes:
            if n.node_id not in reachable:
                errs.append(f"unreachable node: {n.node_id!r}")
        # Terminal presence
        terminals = [n for n in self.nodes if isinstance(n, TerminateNode)]
        if not terminals:
            errs.append("workflow has no Terminate node")
        return errs


# ----- Verb registry: name -> callable -----


VerbFn = Callable[..., Any]  # signature varies by node kind


class VerbRegistry:
    """Maps verb names to Python callables. The interpreter looks up
    verbs here. Workflows themselves are pure data; the registry is
    where executable behaviour lives."""

    def __init__(self):
        self._verbs: dict[str, VerbFn] = {}

    def register(self, name: str, fn: VerbFn) -> None:
        if name in self._verbs:
            raise ValueError(f"verb {name!r} already registered")
        self._verbs[name] = fn

    def get(self, name: str) -> VerbFn:
        if name not in self._verbs:
            raise KeyError(f"verb {name!r} not registered")
        return self._verbs[name]
