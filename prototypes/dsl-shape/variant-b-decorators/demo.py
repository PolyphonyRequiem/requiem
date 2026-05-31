"""Variant B demo — decorators on a class body."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from decorators import human_gate, route, subworkflow, verb, workflow  # noqa: E402
from core import (  # noqa: E402
    Engine,
    Failure,
    Success,
    WorkflowRegistry,
    describe,
    send_notification,
    verify_done,
)


@workflow("notify", entry="send_notification")
class Notify:
    @verb
    @staticmethod
    def send_notification(ctx):
        return send_notification(ctx)

    routes = [route("send_notification", on="*", to="$end")]


@workflow("close-out", entry="verify")
class CloseOut:
    @verb
    @staticmethod
    def verify(ctx):
        return verify_done(ctx)

    @human_gate(prompt="Approve close-out observations?")
    @staticmethod
    def human_approve(ctx):
        # body is unused for human_gate nodes; kept for jump-to-def UX
        pass

    @subworkflow(calls="notify")
    @staticmethod
    def archive(ctx):
        pass

    routes = [
        route("verify", on="success", to="human_approve"),
        route("verify", on="failure", to="$end"),
        route("human_approve", on="success", to="archive"),
        route("human_approve", on="failure", to="$end"),
        route("archive", on="*", to="$end"),
    ]


def main() -> None:
    print("─" * 70)
    print("VARIANT B — Decorators on functions")
    print("─" * 70)

    registry = WorkflowRegistry()
    registry.register(Notify)
    registry.register(CloseOut)

    print("\n[1] Topology introspection:\n")
    print(describe(CloseOut))
    print()
    print(describe(Notify))

    print("\n[2] Happy-path run:\n")
    engine = Engine(registry, human=lambda prompt: Success())
    for event in engine.run(CloseOut):
        print(f"  {event}")

    print("\n[3] Verify failure:\n")
    for event in engine.run(CloseOut, context={"force_failure": True}):
        print(f"  {event}")

    print("\n[4] Human rejects:\n")
    engine2 = Engine(registry, human=lambda p: Failure(reason="not ready"))
    for event in engine2.run(CloseOut):
        print(f"  {event}")

    print("\n[5] Typo demo — route to non-existent node caught at @workflow:\n")
    try:
        @workflow("bad", entry="a")
        class Bad:
            @verb
            @staticmethod
            def a(ctx):
                return Success()

            routes = [route("a", on="success", to="nope")]
    except ValueError as e:
        print(f"  caught: {e}")


if __name__ == "__main__":
    main()
