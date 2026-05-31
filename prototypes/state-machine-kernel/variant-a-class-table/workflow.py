"""Workflow = nodes + explicit transition table.

Authoring shape:

    wf = Workflow("demo", start="ingest")
    wf.add(AgentStep(node_id="ingest", body=...))
    wf.add(HumanGate(node_id="approve", prompt="ok?", options=["yes","no"]))
    wf.add(Route(node_id="branch", chooser=lambda ctx: ...))
    wf.add(Terminate(node_id="done", disposition="completed"))
    wf.edge("ingest", "success", "approve")
    wf.edge("approve", "needs_human:yes", "branch")
    wf.edge("approve", "needs_human:no",  "abandon")
    wf.route("branch", "fast", "done")
    wf.route("branch", "slow", "done")

The edge keys are (node_id, "<outcome_kind>[:<label>]"):
  - kind only:   "success", "permanent_failure", ...
  - kind:label:  "needs_human:yes" (human picked "yes")
                 "success:fast"    (a Route's chosen route is "fast")
"""
from __future__ import annotations

from dataclasses import dataclass, field

from nodes import Node, Terminate


@dataclass
class Workflow:
    workflow_id: str
    start: str
    nodes: dict[str, Node] = field(default_factory=dict)
    # (from_node_id, outcome_key) -> to_node_id
    edges: dict[tuple[str, str], str] = field(default_factory=dict)

    def add(self, node: Node) -> "Workflow":
        if node.node_id in self.nodes:
            raise ValueError(f"duplicate node_id {node.node_id}")
        self.nodes[node.node_id] = node
        return self

    def edge(self, from_id: str, outcome_key: str, to_id: str) -> "Workflow":
        if from_id not in self.nodes:
            raise ValueError(f"unknown from_id {from_id}")
        # to_id may be a Terminate that does not exist yet at edge() time
        # if author calls edge first; we allow forward references and
        # validate at end via .validate().
        self.edges[(from_id, outcome_key)] = to_id
        return self

    def route(self, from_id: str, route_label: str, to_id: str) -> "Workflow":
        """Sugar for a Route node: success:<label> -> to_id."""
        return self.edge(from_id, f"success:{route_label}", to_id)

    def validate(self) -> None:
        for (from_id, _), to_id in self.edges.items():
            if to_id not in self.nodes:
                raise ValueError(f"edge {from_id!r} -> {to_id!r}: unknown target")
        if self.start not in self.nodes:
            raise ValueError(f"start node {self.start!r} not registered")

    def transition_for(self, from_id: str, key: str) -> str | None:
        return self.edges.get((from_id, key))

    def terminals(self) -> list[str]:
        return [nid for nid, n in self.nodes.items() if isinstance(n, Terminate)]
