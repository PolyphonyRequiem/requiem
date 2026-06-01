"""Tests for `requiem.providers.anthropic.AnthropicProvider`.

We mock the SDK's HTTP transport via `httpx.MockTransport`, so every test
exercises the SDK's real URL building, header parsing, and exception
classification — the layers the provider's outcome-mapping depends on.

One live smoke is gated by ``RUN_LIVE_ANTHROPIC=1`` and skipped otherwise.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx
import pytest
from anthropic import AsyncAnthropic
from pydantic import BaseModel

from requiem.agent import AgentCall, AgentSpec
from requiem.outcomes import (
    BadOutput,
    Cancelled,
    NeedsHuman,
    PermanentFailure,
    RetryableFailure,
    Success,
)
from requiem.providers.anthropic import AnthropicProvider

from tests.providers._helpers import (
    request_recorder,
    static_handler,
)


class Reply(BaseModel):
    ok: bool
    msg: str


SPEC = AgentSpec(name="t", charter="You are a test agent.", response_model=Reply)


def _provider(handler) -> AnthropicProvider:
    client = AsyncAnthropic(
        api_key="sk-test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return AnthropicProvider(api_key="sk-test", client=client)


def _ok_tool_use_response(input_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "msg_test_01",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4.6",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_01",
                "name": "Reply",
                "input": input_payload,
            }
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 11, "output_tokens": 22},
    }


def _ok_text_response(text: str) -> dict[str, Any]:
    return {
        "id": "msg_test_02",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4.6",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 5, "output_tokens": 6},
    }


# ---- happy paths -------------------------------------------------------


async def test_happy_path_with_schema_returns_success():
    seen, wrap = request_recorder()
    p = _provider(wrap(static_handler(json_body=_ok_tool_use_response({"ok": True, "msg": "hi"}))))
    out = await p.invoke(AgentCall(spec=SPEC, user_message="say hi"))

    assert isinstance(out, Success)
    assert out.value["parsed"] == {"ok": True, "msg": "hi"}
    assert out.value["agent"] == "t"
    [receipt] = out.value["receipts"]
    assert receipt["kind"] == "llm_call"
    assert receipt["model"] == "claude-sonnet-4.6"
    assert receipt["input_tokens"] == 11
    assert receipt["output_tokens"] == 22
    assert receipt["request_id"] == "msg_test_01"

    # ADR 0004 §4.4: receipts are also exposed on the peer field on the
    # outcome envelope. Same receipt dict in both places at v0 — the
    # in-value copy stays for backwards-compat.
    assert out.receipts == (receipt,)

    # Request payload sanity: forced tool_choice, the right tool, the charter.
    assert len(seen) == 1
    body = json.loads(seen[0].content.decode("utf-8"))
    assert body["tool_choice"] == {"type": "tool", "name": "Reply"}
    assert body["tools"][0]["name"] == "Reply"
    assert body["system"] == "You are a test agent."


async def test_happy_path_without_schema_returns_success_text():
    spec = AgentSpec(name="t2", charter="be brief", response_model=Reply)
    # Override `response_model` to None via a fresh spec subclass to exercise
    # the freeform path. AgentSpec requires response_model; we drop it by
    # constructing a stand-in dataclass that satisfies the same attribute
    # surface the provider reads.
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _BareSpec:
        name: str
        charter: str
        response_model: Any = None
        model: str = "fake"

    bare = _BareSpec(name="t2", charter="be brief")
    p = _provider(static_handler(json_body=_ok_text_response("ok")))
    out = await p.invoke(AgentCall(spec=bare, user_message="say ok"))

    assert isinstance(out, Success)
    assert out.value["parsed"] == {"text": "ok"}
    assert out.value["receipts"][0]["model"] == "claude-sonnet-4.6"


# ---- BadOutput paths (NOT retried) -------------------------------------


async def test_schema_mismatch_returns_bad_output_not_retried():
    seen, wrap = request_recorder()
    # A list won't coerce to bool under pydantic's lax mode.
    p = _provider(wrap(static_handler(json_body=_ok_tool_use_response({"ok": [1, 2, 3], "msg": "hi"}))))
    out = await p.invoke(AgentCall(spec=SPEC, user_message="x"))

    assert isinstance(out, BadOutput)
    assert out.error_kind == "schema_mismatch"
    # validation_errors[0] is our receipt marker; the rest are pydantic errors.
    assert out.validation_errors[0].startswith("__receipt__:")
    assert len(out.validation_errors) >= 2
    assert any("ok" in v for v in out.validation_errors[1:])
    # CRITICAL: exactly one HTTP call. The provider must NOT have retried.
    assert len(seen) == 1


async def test_missing_tool_use_block_returns_bad_output():
    """Model ignored the forced tool_choice and answered in text."""
    p = _provider(static_handler(json_body=_ok_text_response("I refuse to use the tool.")))
    out = await p.invoke(AgentCall(spec=SPEC, user_message="x"))
    assert isinstance(out, BadOutput)
    assert "no tool_use block" in out.validation_errors[1]


# ---- retryable error paths --------------------------------------------


async def test_rate_limit_returns_retryable_with_retry_after():
    p = _provider(
        static_handler(
            status=429,
            json_body={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
            headers={"retry-after": "42"},
        )
    )
    out = await p.invoke(AgentCall(spec=SPEC, user_message="x"))
    assert isinstance(out, RetryableFailure)
    assert out.error_kind == "rate_limited"
    assert "retry_after=42s" in out.message
    # ADR 0004 §4.2: typed ``after`` field carries the same hint so the
    # kernel can sleep on it without re-parsing the message suffix.
    assert out.after == 42.0
    # Peer receipt populated per ADR 0004 §4.4.
    assert out.receipts and out.receipts[0]["kind"] == "llm_call"


async def test_rate_limit_without_header_uses_default_after():
    p = _provider(
        static_handler(
            status=429,
            json_body={"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}},
        )
    )
    out = await p.invoke(AgentCall(spec=SPEC, user_message="x"))
    assert isinstance(out, RetryableFailure)
    assert "retry_after=60s" in out.message


async def test_5xx_returns_retryable_provider_unavailable():
    p = _provider(
        static_handler(
            status=503,
            json_body={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}},
        )
    )
    out = await p.invoke(AgentCall(spec=SPEC, user_message="x"))
    assert isinstance(out, RetryableFailure)
    assert out.error_kind == "provider_unavailable"
    assert "retry_after=30s" in out.message


async def test_network_error_returns_retryable_network_timeout():
    def boom(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("DNS failure")

    p = _provider(boom)
    out = await p.invoke(AgentCall(spec=SPEC, user_message="x"))
    assert isinstance(out, RetryableFailure)
    assert out.error_kind == "network_timeout"
    assert "retry_after=15s" in out.message


# ---- terminal / human paths -------------------------------------------


async def test_auth_error_returns_needs_human():
    p = _provider(
        static_handler(
            status=401,
            json_body={
                "type": "error",
                "error": {"type": "authentication_error", "message": "bad key"},
            },
        )
    )
    out = await p.invoke(AgentCall(spec=SPEC, user_message="x"))
    assert isinstance(out, NeedsHuman)
    assert out.gate == "provider_auth"


async def test_bad_request_returns_permanent_failure():
    p = _provider(
        static_handler(
            status=400,
            json_body={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "schema busted"},
            },
        )
    )
    out = await p.invoke(AgentCall(spec=SPEC, user_message="x"))
    assert isinstance(out, PermanentFailure)
    assert out.error_kind == "invalid_request"


async def test_unknown_4xx_returns_needs_human():
    p = _provider(
        static_handler(
            status=418,
            json_body={"type": "error", "error": {"type": "teapot", "message": "i am a teapot"}},
        )
    )
    out = await p.invoke(AgentCall(spec=SPEC, user_message="x"))
    assert isinstance(out, NeedsHuman)
    assert out.gate == "provider_unknown"


# ---- cancellation ------------------------------------------------------


async def test_cancel_event_set_before_call_returns_cancelled():
    p = _provider(static_handler(json_body=_ok_tool_use_response({"ok": True, "msg": "hi"})))
    cancel = asyncio.Event()
    cancel.set()
    out = await p.invoke(
        AgentCall(spec=SPEC, user_message="x", cancel=cancel)
    )
    assert isinstance(out, Cancelled)
    assert out.cause == "operator"


async def test_asyncio_cancel_propagates():
    """If the kernel cancels the task mid-flight, the exception must
    propagate. The provider must not catch `CancelledError` and silently
    map it to a Retryable/Permanent outcome — that would corrupt the
    kernel's cancellation accounting."""
    started = asyncio.Event()
    release = asyncio.Event()

    def slow(_req: httpx.Request) -> httpx.Response:
        # Synchronous handlers don't help us await; trigger by raising
        # from a side-channel below instead.
        raise AssertionError("should not be reached")

    # Monkey-build a provider whose client.messages.create awaits forever
    # so we can cancel from outside.
    p = _provider(slow)

    async def _forever(**_kw: Any) -> Any:
        started.set()
        await release.wait()
        raise AssertionError("never reached")

    p.client.messages.create = _forever  # type: ignore[assignment]

    task = asyncio.create_task(p.invoke(AgentCall(spec=SPEC, user_message="x")))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---- live smoke (skipped unless RUN_LIVE_ANTHROPIC=1) -----------------


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_ANTHROPIC") != "1",
    reason="set RUN_LIVE_ANTHROPIC=1 and a real ANTHROPIC_API_KEY to run",
)
async def test_live_anthropic():
    """Hit the real API. Marked `live` so `-k live` selects it."""
    p = AnthropicProvider()  # reads ANTHROPIC_API_KEY
    out = await p.invoke(AgentCall(spec=SPEC, user_message="Respond with ok=true msg='ok'."))
    assert isinstance(out, Success), out
    assert out.value["parsed"]["ok"] is True
