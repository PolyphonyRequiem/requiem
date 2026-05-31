"""Variant C demo — exercises all 7 capabilities atop direct LiteLLM calls."""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from agents import (  # noqa: E402
    CodeReviewerAgent,
    ReviewVerdict,
    default_completion_fn,
    have_live_credentials,
)
from fake import FakeCompletionFn, FakeToolCall, FakeTurn  # noqa: E402
from outcomes import BadOutput, Cancelled, Permanent, Success, Transient  # noqa: E402
from retry import with_retry  # noqa: E402

logging.basicConfig(level=logging.INFO, format="  %(message)s")
H = lambda s: print(f"\n=== {s} ===")  # noqa: E731


def events():
    log: list = []
    return log, lambda kind, data: log.append((kind, data))


async def demo() -> None:
    # 1. Declaring an agent ----------------------------------------------------
    H("1. Agent declaration (plain dataclass + function)")
    agent = CodeReviewerAgent()
    print(f"  model={agent.model}  response_model={agent.response_model.__name__}")
    print(f"  validators: {len(agent.validators)} (schema + domain)")

    # 2. Live invocation ------------------------------------------------------
    H("2. Live invocation via default_completion_fn (litellm.completion)")
    if have_live_credentials(agent.model):
        outcome = await agent.invoke("Review src/auth.py.", retry_key="live-1")
        print(f"  outcome.type={outcome.type}")
    else:
        print("  skipped: no API keys (seam takes any completion_fn; degrades cleanly)")

    # 3. Same agent under FakeCompletionFn ------------------------------------
    H("3. Same agent under FakeCompletionFn")
    fake = FakeCompletionFn(
        turns=[
            FakeTurn(
                content=json.dumps(
                    {"summary": "looks fine", "findings": [], "recommend_merge": True}
                )
            )
        ]
    )
    log, cb = events()
    outcome = await agent.invoke(
        "Review src/auth.py.",
        retry_key="fake-1",
        completion_fn=fake,
        event_callback=cb,
    )
    assert outcome.type == "success", outcome
    assert isinstance(outcome.value, ReviewVerdict)
    print(f"  parsed: recommend_merge={outcome.value.recommend_merge}")
    print(f"  fake calls: {len(fake.calls)}  events: {[k for k, _ in log]}")

    # 4. Validation failure → BadOutput (both schema and domain validators) ----
    H("4. BadOutput — domain validator catches inconsistent verdict")
    inconsistent = FakeCompletionFn(
        turns=[
            FakeTurn(
                content=json.dumps(
                    {
                        "summary": "blocker present but recommending merge",
                        "findings": [
                            {
                                "severity": "blocking",
                                "line": 1,
                                "message": "auth stub",
                            }
                        ],
                        "recommend_merge": True,  # validator should reject
                    }
                )
            )
        ]
    )
    outcome = await agent.invoke("x", retry_key="bad-1", completion_fn=inconsistent)
    assert outcome.type == "bad_output", outcome
    print(f"  outcome.type=bad_output  errors={outcome.errors}")

    # 5. Tool round-trip ------------------------------------------------------
    H("5. Tool round-trip (read_file then count_lines then verdict)")
    tools = FakeCompletionFn(
        turns=[
            FakeTurn(
                tool_calls=[
                    FakeToolCall("read_file", {"path": "src/auth.py"}),
                    FakeToolCall("count_lines", {"path": "src/auth.py"}),
                ]
            ),
            FakeTurn(
                content=json.dumps(
                    {
                        "summary": "TODO blocker",
                        "findings": [
                            {
                                "severity": "blocking",
                                "line": 2,
                                "message": "stubbed auth",
                            }
                        ],
                        "recommend_merge": False,
                    }
                )
            ),
        ]
    )
    log, cb = events()
    outcome = await agent.invoke(
        "full review", retry_key="tool-1", completion_fn=tools, event_callback=cb
    )
    assert outcome.type == "success", outcome
    print(f"  tools invoked: {outcome.tool_calls}")
    print(f"  events: {[k for k, _ in log]}")
    assert outcome.tool_calls == ("read_file", "count_lines")

    # 6. Transient → retry → success ------------------------------------------
    H("6. Transient retry (429 once, success on attempt 2)")
    flaky = FakeCompletionFn(
        turns=[
            FakeTurn(raise_exc=RuntimeError("HTTP 429 rate_limit_exceeded")),
            FakeTurn(
                content=json.dumps(
                    {"summary": "recovered", "findings": [], "recommend_merge": True}
                )
            ),
        ]
    )

    async def do_call(rk: str):
        return await agent.invoke("x", retry_key=rk, completion_fn=flaky)

    outcome = await with_retry(do_call, retry_key="flaky-1")
    assert outcome.type == "success", outcome
    print(f"  recovered after {len(flaky.calls)} attempt(s)")

    # Ceiling:
    persistent = FakeCompletionFn(
        turns=[FakeTurn(raise_exc=RuntimeError("HTTP 429"))] * 5
    )

    async def do_call_dead(rk: str):
        return await agent.invoke("x", retry_key=rk, completion_fn=persistent)

    outcome = await with_retry(do_call_dead, retry_key="dead-1")
    assert outcome.type == "permanent", outcome
    assert len(persistent.calls) == 3, persistent.calls
    print(f"  ceiling honoured: {len(persistent.calls)} attempts, then Permanent")

    # 7. Cancellation mid-flight ----------------------------------------------
    H("7. Cancellation mid-call short-circuits")
    cancel = asyncio.Event()

    async def slow_completion_fn(**_: Any) -> Any:
        # Yields control until cancelled.
        for _i in range(100):
            await asyncio.sleep(0.01)
            if cancel.is_set():
                raise asyncio.CancelledError()
        raise RuntimeError("never reached")

    async def trip():
        await asyncio.sleep(0.03)
        cancel.set()

    cancel_task = asyncio.create_task(trip())
    # Race the agent.invoke against the cancel signal directly.
    invoke_task = asyncio.create_task(
        agent.invoke(
            "x",
            retry_key="cancel-1",
            cancel=cancel,
            completion_fn=slow_completion_fn,
        )
    )
    await cancel_task
    # The cancel event will trip slow_completion_fn into raising CancelledError,
    # which agent.invoke re-raises; we catch at this top layer.
    try:
        outcome = await invoke_task
    except asyncio.CancelledError:
        outcome = Cancelled(reason="cancel observed mid-flight")
    print(f"  outcome.type={outcome.type}  reason={outcome.reason}")
    assert outcome.type == "cancelled", outcome

    print("\nVariant C demo complete.")


from typing import Any  # noqa: E402  (used in slow_completion_fn closure)


if __name__ == "__main__":
    asyncio.run(demo())
