"""Variant A demo — exercises all 7 capabilities.

Run:  ``python -m prototypes.agent-boundary.variant-a-protocol-provider.demo``
(from the repo root) — or use ``run_all.py``.

Each numbered section maps to one item in Mahler's task contract.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from agents import CODE_REVIEWER, ReviewFinding, ReviewVerdict  # noqa: E402
from fake import FakeProvider, ToolRoundTrip  # noqa: E402
from outcomes import BadOutput, Cancelled, Permanent, Success, Transient  # noqa: E402
from provider import AgentCall, AgentProvider, LiveProvider  # noqa: E402

logging.basicConfig(level=logging.INFO, format="  %(message)s")
H = lambda s: print(f"\n=== {s} ===")  # noqa: E731


def events() -> tuple:
    log: list = []
    return log, lambda kind, data: log.append((kind, data))


async def demo() -> None:
    # 1. Declaring an agent ----------------------------------------------------
    H("1. Agent declaration")
    print(f"  name={CODE_REVIEWER.name}  model={CODE_REVIEWER.model}")
    print(f"  response_model={CODE_REVIEWER.response_model.__name__}")
    print(f"  tools={[t.name for t in CODE_REVIEWER.tools]}")

    # 2. Live invocation (best-effort; degrades to a notice when no keys) -----
    H("2. Live invocation against LiveProvider")
    live: AgentProvider = LiveProvider()
    live_log, live_cb = events()
    live_outcome = await live.invoke(
        AgentCall(
            spec=CODE_REVIEWER,
            user_message="Review src/auth.py.",
            retry_key="demo-live-1",
            event_callback=live_cb,
        )
    )
    print(f"  outcome.type={live_outcome.type}")
    if live_outcome.type == "permanent":
        print(f"  (expected when no API keys: {live_outcome.reason})")

    # 3. Same agent under FakeProvider ----------------------------------------
    H("3. Same agent under FakeProvider (scripted success)")
    fake = FakeProvider(
        scripts={
            "code_reviewer": [
                {
                    "summary": "looks fine",
                    "findings": [],
                    "recommend_merge": True,
                },
            ],
        }
    )
    fake_log, fake_cb = events()
    outcome = await fake.invoke(
        AgentCall(
            spec=CODE_REVIEWER,
            user_message="Review src/auth.py.",
            retry_key="demo-fake-1",
            event_callback=fake_cb,
        )
    )
    assert outcome.type == "success", outcome
    assert isinstance(outcome.value, ReviewVerdict)
    print(f"  parsed verdict: recommend_merge={outcome.value.recommend_merge}")
    print(f"  fake recorded {len(fake.calls)} call(s); events={[k for k, _ in fake_log]}")

    # 4. Structured-output validation failure → BadOutput ---------------------
    H("4. Validation failure → BadOutput")
    bad_fake = FakeProvider(
        scripts={
            "code_reviewer": [
                {"summary": "missing fields"},  # findings + recommend_merge absent
            ],
        }
    )
    outcome = await bad_fake.invoke(
        AgentCall(spec=CODE_REVIEWER, user_message="x", retry_key="bad-1")
    )
    assert outcome.type == "bad_output", outcome
    assert isinstance(outcome, BadOutput)
    print(f"  outcome.type=bad_output  errors={len(outcome.errors)}")
    print(f"  first error: {outcome.errors[0][:80]}...")

    # 5. Tool round-trip ------------------------------------------------------
    H("5. Tool round-trip (model calls read_file then count_lines, then emits verdict)")
    tool_fake = FakeProvider(
        scripts={
            "code_reviewer": [
                ToolRoundTrip(
                    calls=[
                        ("read_file", {"path": "src/auth.py"}),
                        ("count_lines", {"path": "src/auth.py"}),
                    ],
                    final={
                        "summary": "TODO is a blocker",
                        "findings": [
                            {"severity": "blocking", "line": 2, "message": "stubbed auth"}
                        ],
                        "recommend_merge": False,
                    },
                )
            ]
        }
    )
    tool_log, tool_cb = events()
    outcome = await tool_fake.invoke(
        AgentCall(
            spec=CODE_REVIEWER,
            user_message="full review",
            retry_key="tool-1",
            event_callback=tool_cb,
        )
    )
    assert outcome.type == "success"
    assert outcome.tool_calls == ("read_file", "count_lines")
    print(f"  tools invoked: {outcome.tool_calls}")
    print(f"  events recorded: {[k for k, _ in tool_log]}")

    # 6. Transient → retry → success ------------------------------------------
    H("6. Transient retry (429 once, success on attempt 2)")
    from retry import with_retry

    flaky = FakeProvider(
        scripts={
            "code_reviewer": [
                Transient(reason="HTTP 429 rate_limit_exceeded", retry_after_s=0.01),
                {
                    "summary": "recovered",
                    "findings": [],
                    "recommend_merge": True,
                },
            ]
        }
    )
    outcome = await with_retry(
        flaky.invoke,
        AgentCall(spec=CODE_REVIEWER, user_message="x", retry_key="flaky-1"),
    )
    assert outcome.type == "success", outcome
    print(f"  recovered after {len(flaky.calls)} attempt(s)")
    print(f"  retry_keys stamped: {[c['retry_key'] for c in flaky.calls]}")

    # Show the 3-attempt ceiling for permanent transient failures:
    persistently_flaky = FakeProvider(
        scripts={
            "code_reviewer": [Transient(reason="429")] * 5,  # only 3 should fire
        }
    )
    outcome = await with_retry(
        persistently_flaky.invoke,
        AgentCall(spec=CODE_REVIEWER, user_message="x", retry_key="dead-1"),
    )
    assert outcome.type == "permanent", outcome
    assert len(persistently_flaky.calls) == 3
    print(f"  ceiling honoured: {len(persistently_flaky.calls)} attempts, then Permanent")
    print(f"  final outcome: {outcome.reason}")

    # 7. Cancellation mid-call ------------------------------------------------
    H("7. Cancellation mid-flight short-circuits retry")
    cancel = asyncio.Event()

    slow_fake = _SlowFake()

    async def cancel_soon() -> None:
        await asyncio.sleep(0.02)
        cancel.set()

    cancel_task = asyncio.create_task(cancel_soon())
    outcome = await with_retry(
        slow_fake.invoke,
        AgentCall(
            spec=CODE_REVIEWER,
            user_message="x",
            retry_key="cancel-1",
            cancel=cancel,
        ),
    )
    await cancel_task
    assert outcome.type == "cancelled", outcome
    print(f"  outcome.type=cancelled  reason={outcome.reason}")

    print("\nVariant A demo complete.")


class _SlowFake:
    """A fake that always returns Transient slowly, to give cancel time to win."""

    async def invoke(self, call: AgentCall):
        # Cooperative check at start.
        if call.cancel is not None and call.cancel.is_set():
            return Cancelled(reason="cancelled before slow work")
        await asyncio.sleep(0.05)
        if call.cancel is not None and call.cancel.is_set():
            return Cancelled(reason="cancelled mid-flight")
        return Transient(reason="will retry forever if not cancelled", retry_after_s=0.05)


if __name__ == "__main__":
    asyncio.run(demo())
