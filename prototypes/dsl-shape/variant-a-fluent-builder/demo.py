"""Variant A demo — build, introspect, run, and demonstrate typo catching."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from builder import WorkflowBuilder  # noqa: E402
from core import (  # noqa: E402
    Engine,
    Failure,
    Success,
    WorkflowRegistry,
    describe,
    send_notification,
    verify_done,
)


def build_notify_subworkflow():
    return (
        WorkflowBuilder("notify")
        .entry("send_notification")
        .verb("send_notification", send_notification)
        .route("send_notification", on="*", to="$end")
        .build()
    )


def build_close_out():
    return (
        WorkflowBuilder("close-out")
        .entry("verify")
        .verb("verify", verify_done)
        .route("verify", on="success", to="human_approve")
        .route("verify", on="failure", to="$end")
        .human_gate("human_approve", prompt="Approve close-out observations?")
        .route("human_approve", on="success", to="archive")
        .route("human_approve", on="failure", to="$end")
        .subworkflow("archive", calls="notify")
        .route("archive", on="*", to="$end")
        .build()
    )


def main() -> None:
    print("─" * 70)
    print("VARIANT A — Fluent builder")
    print("─" * 70)

    notify = build_notify_subworkflow()
    wf = build_close_out()
    registry = WorkflowRegistry()
    registry.register(notify)
    registry.register(wf)

    print("\n[1] Topology introspection (the UI consumes the same fields):\n")
    print(describe(wf))
    print()
    print(describe(notify))

    print("\n[2] Happy-path run (human approves):\n")
    engine = Engine(registry, human=lambda prompt: Success())
    trace = engine.run(wf)
    for event in trace:
        print(f"  {event}")

    print("\n[3] Verify failure (verb returns Failure -> short-circuit to $end):\n")
    trace = engine.run(wf, context={"force_failure": True})
    for event in trace:
        print(f"  {event}")

    print("\n[4] Human rejects (human gate returns Failure):\n")
    engine2 = Engine(registry, human=lambda prompt: Failure(reason="not yet"))
    trace = engine2.run(wf)
    for event in trace:
        print(f"  {event}")

    print("\n[5] Typo demo — route to non-existent node caught at .build():\n")
    try:
        (
            WorkflowBuilder("bad")
            .entry("a")
            .verb("a", verify_done)
            .route("a", on="success", to="b_typo")  # b_typo doesn't exist
            .build()
        )
    except ValueError as e:
        print(f"  caught: {e}")


if __name__ == "__main__":
    main()
