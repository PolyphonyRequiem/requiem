"""Tiny workflow primitive.

A Workflow is a dict of nodes plus a start node id. Each node has a
kind (`script`, `agent`, `human_gate`, `subworkflow`, `terminal`) and
either a `next` (string) or `routes` (dict[str, str]).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Node:
    id: str
    kind: str  # "script" | "agent" | "human_gate" | "subworkflow" | "terminal"

    verb: Callable | None = None  # for kind="script": () -> Outcome
    agent: str | None = None  # for kind="agent": name of agent to invoke
    options: list[str] = field(default_factory=list)  # for kind="human_gate"
    subworkflow: "Workflow | None" = None  # for kind="subworkflow"
    terminal_label: str | None = None  # for kind="terminal"

    next: str | None = None  # for linear nodes
    routes: dict[str, str] = field(default_factory=dict)  # for gates / agent decisions

    retry_max: int = 3  # INV: 3 retries on network/auth, never more.


@dataclass
class Workflow:
    name: str
    start: str
    nodes: dict[str, Node]
