"""Variant B — Decorator-based DSL on Python functions.

Author writes:

    @workflow("close-out", entry="verify")
    class CloseOut:
        @verb
        def verify(ctx): ...

        @human_gate(prompt="Approve close-out?")
        def human_approve(ctx): ...

        @subworkflow(calls="notify")
        def archive(ctx): ...

        routes = [
            route("verify", on="success", to="human_approve"),
            route("verify", on="failure", to="$end"),
            ...
        ]

Strengths: functions are first-class; jump-to-definition works on every
node; type hints sit naturally on verb signatures.

Weaknesses: route declarations are *outside* the function, so the
"which node does this go to next?" question requires looking at a list
elsewhere in the class. Two-spotting tax compared to YAML / fluent.

Design choice: we use a *class* as the workflow container so the
decorators register against `cls`. A `@workflow def fn()` version would
require module-level globals; the class form keeps state local.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import Node, NodeKind, Route, Workflow  # noqa: E402


# ── Decorators ────────────────────────────────────────────────────────────

def verb(fn: Callable) -> Callable:
    """Marks a method as a verb node. Name is taken from the method name."""
    fn._requiem_kind = NodeKind.VERB  # type: ignore[attr-defined]
    return fn


def human_gate(*, prompt: str = "") -> Callable:
    def deco(fn: Callable) -> Callable:
        fn._requiem_kind = NodeKind.HUMAN_GATE  # type: ignore[attr-defined]
        fn._requiem_prompt = prompt  # type: ignore[attr-defined]
        return fn
    return deco


def subworkflow(*, calls: str) -> Callable:
    def deco(fn: Callable) -> Callable:
        fn._requiem_kind = NodeKind.SUBWORKFLOW  # type: ignore[attr-defined]
        fn._requiem_calls = calls  # type: ignore[attr-defined]
        return fn
    return deco


def route(from_node: str, *, on: str, to: str) -> Route:
    """Inert factory — produces a Route record. Class collects them in
    a `routes = [...]` attribute."""
    return Route(from_node=from_node, when=on, to_node=to)


def workflow(name: str, *, entry: str) -> Callable:
    """Class decorator that assembles a Workflow from a class body."""

    def deco(cls: type) -> Workflow:
        nodes: list[Node] = []
        for attr_name, attr in cls.__dict__.items():
            kind = getattr(attr, "_requiem_kind", None)
            if kind is None:
                continue
            if kind == NodeKind.VERB:
                nodes.append(Node(name=attr_name, kind=kind, verb=attr))
            elif kind == NodeKind.HUMAN_GATE:
                nodes.append(
                    Node(
                        name=attr_name,
                        kind=kind,
                        prompt=getattr(attr, "_requiem_prompt", ""),
                    )
                )
            elif kind == NodeKind.SUBWORKFLOW:
                nodes.append(
                    Node(
                        name=attr_name,
                        kind=kind,
                        subworkflow=getattr(attr, "_requiem_calls"),
                    )
                )
        routes = list(getattr(cls, "routes", []))
        # Workflow.__init__ validates topology and raises on typo.
        return Workflow(name=name, entry=entry, nodes=nodes, routes=routes)

    return deco
