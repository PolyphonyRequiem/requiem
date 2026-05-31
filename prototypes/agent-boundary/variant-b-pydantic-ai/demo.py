"""Variant B demo — exercises all 7 capabilities atop pydantic-ai."""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from outcomes import BadOutput, Cancelled, Permanent, Success, Transient  # noqa: E402
from provider import (  # noqa: E402
    ReviewVerdict,
    build_code_reviewer,
    have_live_credentials,
    run_agent,
)
from fake import FakeProviderState, ScriptedToolCall, ScriptedTurn, make_fake_model  # noqa: E402
from retry import with_retry  # noqa: E402

from pydantic_ai.exceptions import ModelHTTPError

logging.basicConfig(level=logging.INFO, format="  %(message)s")
H = lambda s: print(f"\n=== {s} ===")  # noqa: E731


async def demo() -> None:
    # 1. Declaring an agent ----------------------------------------------------
    H("1. Agent declaration (pydantic_ai.Agent)")
    agent_meta = build_code_reviewer("test")
    print(f"  output_type={ReviewVerdict.__name__}")
    print(f"  tools registered: {sorted(agent_meta.toolsets[0].tools.keys())}")

    # 2. Live invocation (only if creds present) ------------------------------
    H("2. Live invocation against anthropic:claude-haiku-4-5")
    live_model = "anthropic:claude-haiku-4-5"
    if have_live_credentials(live_model):
        live_agent = build_code_reviewer(live_model)
        outcome = await run_agent(live_agent, "Review src/auth.py.", retry_key="live-1")
        print(f"  outcome.type={outcome.type}")
        if outcome.type == "success":
            print(f"  recommend_merge={outcome.value.recommend_merge}")
    else:
        print("  skipped: no ANTHROPIC_API_KEY (seam supports it; degrades cleanly)")

    # 3. Same agent under FakeProvider (FunctionModel) ------------------------
    H("3. Same agent under FakeProvider (FunctionModel)")
    state_sink: dict[str, FakeProviderState] = {}
    fake_model = make_fake_model(
        name="code_reviewer",
        turns=[
            ScriptedTurn(
                text=json.dumps(
                    {"summary": "looks fine", "findings": [], "recommend_merge": True}
                )
            )
        ],
        state_sink=state_sink,
    )
    agent = build_code_reviewer(fake_model)
    outcome = await run_agent(agent, "Review src/auth.py.", retry_key="fake-1")
    assert outcome.type == "success", outcome
    assert isinstance(outcome.value, ReviewVerdict)
    print(f"  parsed verdict: recommend_merge={outcome.value.recommend_merge}")
    print(f"  fake invocations: {state_sink['code_reviewer'].invocations}")

    # 4. Validation failure → BadOutput ---------------------------------------
    H("4. Validation failure → BadOutput")
    bad_model = make_fake_model(
        name="code_reviewer",
        turns=[
            # Malformed JSON for ReviewVerdict; repeat so pydantic-ai's
            # internal output_retries (default=1) is also exhausted.
            ScriptedTurn(text='{"summary": "no other fields"}'),
            ScriptedTurn(text='{"summary": "still bad"}'),
            ScriptedTurn(text='{"summary": "still bad"}'),
            ScriptedTurn(text='{"summary": "still bad"}'),
        ],
    )
    bad_agent = build_code_reviewer(bad_model)
    outcome = await run_agent(bad_agent, "x", retry_key="bad-1")
    assert outcome.type == "bad_output", outcome
    assert isinstance(outcome, BadOutput)
    print(f"  outcome.type=bad_output  (errors tuple len={len(outcome.errors)})")

    # 5. Tool round-trip ------------------------------------------------------
    H("5. Tool round-trip via FunctionModel")
    tools_model = make_fake_model(
        name="code_reviewer",
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall("read_file", {"path": "src/auth.py"}),
                    ScriptedToolCall("count_lines", {"path": "src/auth.py"}),
                ]
            ),
            ScriptedTurn(
                text=json.dumps(
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
        ],
    )
    tools_agent = build_code_reviewer(tools_model)
    outcome = await run_agent(tools_agent, "full review", retry_key="tool-1")
    assert outcome.type == "success", outcome
    print(f"  tool_calls observed: {outcome.tool_calls}")
    assert "read_file" in outcome.tool_calls and "count_lines" in outcome.tool_calls

    # 6. Transient → retry → success ------------------------------------------
    H("6. Transient retry (HTTP 429 once, success on attempt 2)")

    class FlakyModel:
        def __init__(self) -> None:
            self.calls = 0
            self.delegate = make_fake_model(
                name="code_reviewer",
                turns=[
                    ScriptedTurn(
                        text=json.dumps(
                            {
                                "summary": "recovered",
                                "findings": [],
                                "recommend_merge": True,
                            }
                        )
                    )
                ],
            )

    flaky = FlakyModel()
    sequence = ["transient", "ok"]

    async def do_call(rk: str):
        if sequence[0] == "transient":
            sequence.pop(0)
            return Transient(reason="HTTP 429", retry_after_s=0.01)
        agent = build_code_reviewer(flaky.delegate)
        return await run_agent(agent, "x", retry_key=rk)

    outcome = await with_retry(do_call, retry_key="flaky-1")
    assert outcome.type == "success", outcome
    print(f"  recovered: {outcome.value.summary}")

    # And the ceiling:
    persistent_sequence = ["transient"] * 5

    async def do_call_dead(rk: str):
        persistent_sequence.pop(0)
        return Transient(reason="HTTP 429", retry_after_s=0.005)

    outcome = await with_retry(do_call_dead, retry_key="dead-1")
    assert outcome.type == "permanent", outcome
    print(f"  ceiling honoured: {5 - len(persistent_sequence)} attempts, then Permanent")

    # 7. Cancellation mid-call ------------------------------------------------
    H("7. Cancellation mid-flight short-circuits retry")
    cancel = asyncio.Event()

    async def slow_call(rk: str):
        # Simulate a long-running LLM call.
        await asyncio.sleep(0.2)
        return Success(value=None)

    async def trip_cancel():
        await asyncio.sleep(0.02)
        cancel.set()

    cancel_task = asyncio.create_task(trip_cancel())

    # We use the underlying run_agent's cancel handling directly:
    slow_turns = [ScriptedTurn(text='{"summary":"x","findings":[],"recommend_merge":true}')]

    class SlowModel:
        async def request(self, *args, **kwargs):
            await asyncio.sleep(1.0)
            raise RuntimeError("should have been cancelled")
        model_name = "slow"

    # Use pydantic-ai with a deliberately-slow FunctionModel.
    def slow_fn(messages, info):
        import time
        time.sleep(0.5)  # synchronous; pydantic-ai will await it via to_thread
        return None  # unreachable

    from pydantic_ai.models.function import FunctionModel as FM

    async def slow_async_fn(messages, info):
        await asyncio.sleep(1.0)
        return None  # unreachable; cancelled first

    slow_agent = build_code_reviewer(FM(slow_async_fn, model_name="slow"))
    outcome = await run_agent(slow_agent, "x", retry_key="cancel-1", cancel=cancel)
    await cancel_task
    assert outcome.type == "cancelled", outcome
    print(f"  outcome.type=cancelled  reason={outcome.reason}")

    print("\nVariant B demo complete.")


if __name__ == "__main__":
    asyncio.run(demo())
