"""Variant A — Protocol-based ``AgentProvider`` seam.

The boundary is one method:

    async def invoke(self, call: AgentCall) -> AgentOutcome

Everything an agent author touches is in ``AgentSpec`` + ``AgentCall``.
Providers are duck-typed via ``typing.Protocol`` — there is no ABC and
no inheritance requirement. ``FakeProvider`` lives in ``fake.py`` and
satisfies the Protocol structurally; the live LiteLLM-backed provider
is in this file. The engine receives a provider via constructor
injection; the harness swaps in the fake.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from pydantic import BaseModel, ValidationError

from outcomes import AgentOutcome, BadOutput, Cancelled, Permanent, Success, Transient, Usage


# ---- agent author API ------------------------------------------------------


@dataclass(frozen=True)
class Tool:
    """A Python callable exposed to the model.

    The schema is derived from the function's pydantic model parameter.
    Tools are pure local functions — the provider performs the round-trip.
    """

    name: str
    description: str
    parameters: type[BaseModel]
    fn: Callable[..., Any]


@dataclass(frozen=True)
class AgentSpec:
    """Declarative agent definition. Authored once, invoked many times."""

    name: str
    system: str
    response_model: type[BaseModel]
    model: str = "claude-haiku-4-5"
    tools: tuple[Tool, ...] = ()


@dataclass(frozen=True)
class AgentCall:
    """One invocation: the spec + the user message + the run plumbing."""

    spec: AgentSpec
    user_message: str
    # retry_key (INV-RESTART, error-handling-deep-dive §2.3): a stable
    # identifier so that a retried call is idempotent at the engine layer.
    # The provider does not deduplicate — it just stamps this on its trace.
    retry_key: str = ""
    cancel: asyncio.Event | None = None
    # event_callback gets ("prompt", {...}), ("response", {...}),
    # ("tool_call", {...}) for observability; the engine writes these
    # directly to run.events.jsonl per INV-EVENT-LOG-AUTHORITATIVE.
    event_callback: Callable[[str, dict[str, Any]], None] | None = None


# ---- the seam --------------------------------------------------------------


@runtime_checkable
class AgentProvider(Protocol):
    """The one method that crosses the agent boundary."""

    async def invoke(self, call: AgentCall) -> AgentOutcome: ...


# ---- live provider (LiteLLM-backed) ----------------------------------------


class LiveProvider:
    """Production provider. Uses LiteLLM to reach any chat backend.

    Classifies HTTP failures into ``Transient`` vs ``Permanent`` and
    leaves ``BadOutput`` to the parse step. Retry happens *above* the
    provider in :mod:`retry` — the provider is one-shot.
    """

    def __init__(self, default_model: str = "claude-haiku-4-5") -> None:
        self.default_model = default_model

    async def invoke(self, call: AgentCall) -> AgentOutcome:
        if call.cancel is not None and call.cancel.is_set():
            return Cancelled(reason="cancelled before dispatch")

        if call.event_callback:
            call.event_callback(
                "prompt",
                {"agent": call.spec.name, "retry_key": call.retry_key},
            )

        try:
            import litellm  # local import; the seam doesn't require litellm
        except ImportError:
            return Permanent(reason="litellm not installed; install requirements.txt")

        if not _have_credentials(call.spec.model or self.default_model):
            return Permanent(reason="no API credentials configured for live call")

        messages = [
            {"role": "system", "content": call.spec.system},
            {"role": "user", "content": call.user_message},
        ]
        tools_payload = [_tool_to_openai_schema(t) for t in call.spec.tools] or None

        try:
            raw = await asyncio.to_thread(
                litellm.completion,
                model=call.spec.model or self.default_model,
                messages=messages,
                tools=tools_payload,
                response_format=call.spec.response_model,
                timeout=30,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - classification boundary
            return _classify_http_exception(exc)

        return await _parse_and_dispatch(call, raw)


# ---- classification + parsing ---------------------------------------------


_TRANSIENT_MARKERS = ("rate_limit", "429", "503", "504", "timeout", "overloaded")
_AUTH_MARKERS = ("401", "403", "invalid_api_key", "authentication")


def _classify_http_exception(exc: Exception) -> AgentOutcome:
    msg = str(exc).lower()
    if any(m in msg for m in _AUTH_MARKERS):
        # Per the error-handling deep-dive §F: auth and network share the
        # same 3-retry ceiling. Auth is transient on the assumption that
        # token refresh elsewhere may have repaired it; the cap stops the
        # cycle from going further. (Open Q: confirm this with Daniel —
        # see README "OPEN QUESTIONS".)
        return Transient(reason=f"auth: {exc}")
    if any(m in msg for m in _TRANSIENT_MARKERS):
        return Transient(reason=str(exc))
    return Permanent(reason=f"unrecognised HTTP failure: {exc}")


async def _parse_and_dispatch(call: AgentCall, raw: Any) -> AgentOutcome:
    """Round-trip tool calls until the model emits a final structured payload."""
    tool_log: list[str] = []
    current = raw
    for _ in range(8):  # tool-call ceiling per call; configurable later
        choice = current.choices[0]
        tool_calls = getattr(choice.message, "tool_calls", None) or []
        if tool_calls:
            for tc in tool_calls:
                name = tc.function.name
                args = json.loads(tc.function.arguments or "{}")
                tool = next((t for t in call.spec.tools if t.name == name), None)
                if tool is None:
                    return Permanent(reason=f"model called unknown tool {name!r}")
                result = tool.fn(**args)
                tool_log.append(name)
                if call.event_callback:
                    call.event_callback("tool_call", {"name": name, "args": args})
            # Real impl would loop with a follow-up litellm call here.
            # The demo's tool-call coverage goes through FakeProvider; this
            # branch documents the live-path contract.
            break
        break

    text = choice.message.content or ""
    return _bind_output(call.spec.response_model, text, tool_log)


def _bind_output(model: type[BaseModel], text: str, tools: list[str]) -> AgentOutcome:
    try:
        parsed = model.model_validate_json(text)
    except ValidationError as ve:
        return BadOutput(raw=text, errors=tuple(str(e) for e in ve.errors()))
    except ValueError as ve:
        return BadOutput(raw=text, errors=(str(ve),))
    return Success(value=parsed, tool_calls=tuple(tools))


def _tool_to_openai_schema(tool: Tool) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters.model_json_schema(),
        },
    }


def _have_credentials(model: str) -> bool:
    """Best-effort key probe so the demo can degrade cleanly when run dry."""
    if model.startswith(("claude", "anthropic")):
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    if model.startswith(("gpt", "o1", "o4")):
        return bool(os.environ.get("OPENAI_API_KEY"))
    return bool(
        os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"),
    )
