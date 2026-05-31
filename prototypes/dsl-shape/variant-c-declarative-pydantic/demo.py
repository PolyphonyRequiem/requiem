"""Variant C — Declarative pydantic data model.

Author writes pydantic data directly:

    wf = Workflow(
        name="close-out",
        entry="verify",
        nodes=[
            Node(name="verify", kind=NodeKind.VERB, verb=verify_done),
            Node(name="human_approve", kind=NodeKind.HUMAN_GATE,
                 prompt="Approve?"),
            Node(name="archive", kind=NodeKind.SUBWORKFLOW,
                 subworkflow="notify"),
        ],
        routes=[
            Route(from_node="verify", when="success",
                  to_node="human_approve"),
            ...
        ],
    )

Strengths: least magic — the data IS the topology. Trivially
serialisable to JSON for round-trip, persistence, or YAML migration.
Pydantic gives free schema generation for the UI to consume. Static
analysis (mypy, pyright) sees the full shape.

Weaknesses: verbose. Every node and every route is a constructor call
with named fields. A 30-node workflow becomes a wall of pydantic. The
authoring ergonomics gap vs the fluent builder is the largest single
trade-off in this seam.

NOTE: this file IS the demo. Variants A and B both lower to this same
data model — variant C just doesn't lower from anything. It's the floor.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import (  # noqa: E402
    Engine,
    Failure,
    Node,
    NodeKind,
    Route,
    Success,
    Workflow,
    WorkflowRegistry,
    describe,
    send_notification,
    verify_done,
)


NOTIFY = Workflow(
    name="notify",
    entry="send_notification",
    nodes=[
        Node(name="send_notification", kind=NodeKind.VERB, verb=send_notification),
    ],
    routes=[
        Route(from_node="send_notification", when="*", to_node="$end"),
    ],
)


CLOSE_OUT = Workflow(
    name="close-out",
    entry="verify",
    nodes=[
        Node(name="verify", kind=NodeKind.VERB, verb=verify_done),
        Node(
            name="human_approve",
            kind=NodeKind.HUMAN_GATE,
            prompt="Approve close-out observations?",
        ),
        Node(name="archive", kind=NodeKind.SUBWORKFLOW, subworkflow="notify"),
    ],
    routes=[
        Route(from_node="verify", when="success", to_node="human_approve"),
        Route(from_node="verify", when="failure", to_node="$end"),
        Route(from_node="human_approve", when="success", to_node="archive"),
        Route(from_node="human_approve", when="failure", to_node="$end"),
        Route(from_node="archive", when="*", to_node="$end"),
    ],
)


def serialise_topology(wf: Workflow) -> str:
    """Show the round-trip story — pydantic dumps to JSON natively.
    Verbs are callables so they're excluded; a real impl would store
    verb references by name and resolve at load time."""
    payload = wf.model_dump(exclude={"nodes": {"__all__": {"verb"}}})
    return json.dumps(payload, indent=2, default=str)


def main() -> None:
    print("─" * 70)
    print("VARIANT C — Declarative pydantic")
    print("─" * 70)

    registry = WorkflowRegistry()
    registry.register(NOTIFY)
    registry.register(CLOSE_OUT)

    print("\n[1] Topology introspection:\n")
    print(describe(CLOSE_OUT))
    print()
    print(describe(NOTIFY))

    print("\n[2] Serialised topology (data IS the workflow):\n")
    print(serialise_topology(CLOSE_OUT))

    print("\n[3] Happy-path run:\n")
    engine = Engine(registry, human=lambda prompt: Success())
    for event in engine.run(CLOSE_OUT):
        print(f"  {event}")

    print("\n[4] Verify failure:\n")
    for event in engine.run(CLOSE_OUT, context={"force_failure": True}):
        print(f"  {event}")

    print("\n[5] Human rejects:\n")
    engine2 = Engine(registry, human=lambda p: Failure(reason="not ready"))
    for event in engine2.run(CLOSE_OUT):
        print(f"  {event}")

    print("\n[6] Typo demo — pydantic model_validator catches at construction:\n")
    try:
        Workflow(
            name="bad",
            entry="a",
            nodes=[Node(name="a", kind=NodeKind.VERB, verb=verify_done)],
            routes=[Route(from_node="a", when="success", to_node="nope")],
        )
    except Exception as e:
        print(f"  caught: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
