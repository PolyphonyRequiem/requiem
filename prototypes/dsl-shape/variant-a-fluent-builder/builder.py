"""Variant A — Fluent Python builder.

Author writes:

    wf = (
        WorkflowBuilder("close-out")
            .entry("verify")
            .verb("verify", verify_done)
            .route("verify", on="success", to="human_approve")
            .route("verify", on="failure", to="$end")
            .human_gate("human_approve", prompt="Approve close-out?")
            ...
            .build()
    )

Strengths: readable left-to-right; IDE autocomplete on every method;
all topology lives in one expression. Errors surface at `.build()`.

Weaknesses: indirection between method calls and the data they produce
(authors who like to grep YAML may find this opaque); ordering of route
calls vs node calls is a style choice the team must agree on.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import Node, NodeKind, Route, Workflow  # noqa: E402


class WorkflowBuilder:
    def __init__(self, name: str) -> None:
        self._name = name
        self._entry: Optional[str] = None
        self._nodes: list[Node] = []
        self._routes: list[Route] = []

    def entry(self, node_name: str) -> "WorkflowBuilder":
        self._entry = node_name
        return self

    def verb(self, name: str, fn: Callable) -> "WorkflowBuilder":
        self._nodes.append(Node(name=name, kind=NodeKind.VERB, verb=fn))
        return self

    def human_gate(self, name: str, prompt: str = "") -> "WorkflowBuilder":
        self._nodes.append(
            Node(name=name, kind=NodeKind.HUMAN_GATE, prompt=prompt)
        )
        return self

    def subworkflow(self, name: str, calls: str) -> "WorkflowBuilder":
        self._nodes.append(
            Node(name=name, kind=NodeKind.SUBWORKFLOW, subworkflow=calls)
        )
        return self

    def route(self, from_node: str, *, on: str, to: str) -> "WorkflowBuilder":
        self._routes.append(Route(from_node=from_node, when=on, to_node=to))
        return self

    def build(self) -> Workflow:
        if self._entry is None:
            raise ValueError(f"workflow '{self._name}' has no entry node")
        # Workflow.__init__ runs topology validation. Typos die here.
        return Workflow(
            name=self._name,
            entry=self._entry,
            nodes=self._nodes,
            routes=self._routes,
        )
