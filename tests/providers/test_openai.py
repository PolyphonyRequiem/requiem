"""Tests for `requiem.providers.openai.OpenAIProvider`.

Mirrors the structure of `test_anthropic.py`: HTTP transport mocked via
`httpx.MockTransport` so each test exercises the SDK's real URL building,
header parsing, and exception classification.

One live smoke is gated by ``RUN_LIVE_OPENAI=1``.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI
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
from requiem.providers.openai import OpenAIProvider

from tests.providers._helpers import (
    request_recorder,
    static_handler,
)


class Reply(BaseModel):
    ok: bool
    msg: str


SPEC = AgentSpec(name="t", charter="You are a test agent.", response_model=Reply)


def _provider(handler) -> OpenAIProvider:
    client = AsyncOpenAI(
        api_key="sk-test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return OpenAIProvider(api_key="sk-test", client=client)


def _ok_completion(content: str | None, *, refusal: str | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if refusal is not None:
        msg["refusal"] = refusal
    return {
        "id": "chatcmpl_test_01",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-5.4",
        "choices": [
            {
                "index": 0,
                "message": msg,
                "finish_reason": "stop",
                "logprobs": None,
            }
        ],
        "usage": {"prompt_tokens": 7, "completion_tokens": 9, "total_tokens": 16},
    }


# ---- happy paths -------------------------------------------------------


async def test_happy_path_with_schema_returns_success():
    seen, wrap = request_recorder()
    payload = json.dumps({"ok": True, "msg": "hi"})
    p = _provider(wrap(static_handler(json_body=_ok_completion(payload))))
    out = await p.invoke(AgentCall(spec=SPEC, user_message="say hi"))

    assert isinstance(out, Success)
    assert out.value["parsed"] == {"ok": True, "msg": "hi"}
    assert out.value["agent"] == "t"
    [receipt] = out.value["receipts"]
    assert receipt["kind"] == "llm_call"
    assert receipt["model"] == "gpt-5.4"
    assert receipt["input_tokens"] == 7
    assert receipt["output_tokens"] == 9
    assert receipt["request_id"] == "chatcmpl_test_01"

    # Verify the request used json_schema strict mode.
    assert len(seen) == 1
    body = json.loads(seen[0].content.decode("utf-8"))
    rf = body["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "Reply"
    assert rf["json_schema"]["strict"] is True
    schema = rf["json_schema"]["schema"]
    # _strictify must have closed additionalProperties and listed required.
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"ok", "msg"}


async def test_happy_path_without_schema_returns_success_text():
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _BareSpec:
        name: str
        charter: str
        response_model: Any = None
        model: str = "fake"

    bare = _BareSpec(name="t2", charter="be brief")
    p = _provider(static_handler(json_body=_ok_completion("ok")))
    out = await p.invoke(AgentCall(spec=bare, user_message="say ok"))

    assert isinstance(out, Success)
    assert out.value["parsed"] == {"text": "ok"}


# ---- BadOutput paths (NOT retried) -------------------------------------


async def test_schema_mismatch_returns_bad_output_not_retried():
    seen, wrap = request_recorder()
    payload = json.dumps({"ok": "not a bool", "msg": "hi"})
    p = _provider(wrap(static_handler(json_body=_ok_completion(payload))))
    out = await p.invoke(AgentCall(spec=SPEC, user_message="x"))

    assert isinstance(out, BadOutput)
    assert out.error_kind == "schema_mismatch"
    assert out.validation_errors[0].startswith("__receipt__:")
    assert any("ok" in v for v in out.validation_errors[1:])
    assert out.raw_output == payload  # raw text preserved verbatim
    # CRITICAL: no automatic retry on BadOutput.
    assert len(seen) == 1


async def test_non_json_content_returns_bad_output():
    p = _provider(static_handler(json_body=_ok_completion("definitely not json")))
    out = await p.invoke(AgentCall(spec=SPEC, user_message="x"))
    assert isinstance(out, BadOutput)
    assert any("json decode" in v for v in out.validation_errors)


async def test_refusal_returns_bad_output():
    p = _provider(static_handler(json_body=_ok_completion(None, refusal="I refuse.")))
    out = await p.invoke(AgentCall(spec=SPEC, user_message="x"))
    assert isinstance(out, BadOutput)
    assert "refused" in out.validation_errors[1]
    assert out.raw_output == "I refuse."


# ---- retryable error paths --------------------------------------------


async def test_rate_limit_returns_retryable_with_retry_after():
    p = _provider(
        static_handler(
            status=429,
            json_body={"error": {"message": "slow down", "type": "rate_limit_error"}},
            headers={"retry-after": "33"},
        )
    )
    out = await p.invoke(AgentCall(spec=SPEC, user_message="x"))
    assert isinstance(out, RetryableFailure)
    assert out.error_kind == "rate_limited"
    assert "retry_after=33s" in out.message


async def test_5xx_returns_retryable_provider_unavailable():
    p = _provider(
        static_handler(
            status=500,
            json_body={"error": {"message": "boom", "type": "server_error"}},
        )
    )
    out = await p.invoke(AgentCall(spec=SPEC, user_message="x"))
    assert isinstance(out, RetryableFailure)
    assert out.error_kind == "provider_unavailable"


async def test_network_error_returns_retryable_network_timeout():
    def boom(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("DNS failure")

    p = _provider(boom)
    out = await p.invoke(AgentCall(spec=SPEC, user_message="x"))
    assert isinstance(out, RetryableFailure)
    assert out.error_kind == "network_timeout"


# ---- terminal / human paths -------------------------------------------


async def test_auth_error_returns_needs_human():
    p = _provider(
        static_handler(
            status=401,
            json_body={"error": {"message": "bad key", "type": "authentication_error"}},
        )
    )
    out = await p.invoke(AgentCall(spec=SPEC, user_message="x"))
    assert isinstance(out, NeedsHuman)
    assert out.gate == "provider_auth"


async def test_bad_request_returns_permanent_failure():
    p = _provider(
        static_handler(
            status=400,
            json_body={"error": {"message": "schema busted", "type": "invalid_request_error"}},
        )
    )
    out = await p.invoke(AgentCall(spec=SPEC, user_message="x"))
    assert isinstance(out, PermanentFailure)
    assert out.error_kind == "invalid_request"


async def test_unknown_4xx_returns_needs_human():
    p = _provider(
        static_handler(
            status=418,
            json_body={"error": {"message": "teapot", "type": "teapot"}},
        )
    )
    out = await p.invoke(AgentCall(spec=SPEC, user_message="x"))
    assert isinstance(out, NeedsHuman)
    assert out.gate == "provider_unknown"


# ---- cancellation ------------------------------------------------------


async def test_cancel_event_set_before_call_returns_cancelled():
    payload = json.dumps({"ok": True, "msg": "hi"})
    p = _provider(static_handler(json_body=_ok_completion(payload)))
    cancel = asyncio.Event()
    cancel.set()
    out = await p.invoke(AgentCall(spec=SPEC, user_message="x", cancel=cancel))
    assert isinstance(out, Cancelled)
    assert out.cause == "operator"


async def test_asyncio_cancel_propagates():
    started = asyncio.Event()
    release = asyncio.Event()

    p = _provider(static_handler(json_body=_ok_completion('{"ok":true,"msg":"x"}')))

    async def _forever(**_kw: Any) -> Any:
        started.set()
        await release.wait()
        raise AssertionError("never reached")

    p.client.chat.completions.create = _forever  # type: ignore[assignment]

    task = asyncio.create_task(p.invoke(AgentCall(spec=SPEC, user_message="x")))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---- live smoke -------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_OPENAI") != "1",
    reason="set RUN_LIVE_OPENAI=1 and a real OPENAI_API_KEY to run",
)
async def test_live_openai():
    p = OpenAIProvider()
    out = await p.invoke(AgentCall(spec=SPEC, user_message="Respond with ok=true msg='ok'."))
    assert isinstance(out, Success), out
    assert out.value["parsed"]["ok"] is True
