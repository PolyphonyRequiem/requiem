"""Variant A — ``FakeProvider`` implementing the AgentProvider Protocol.

Scripted by agent name. Each scripted entry is one of:

* ``dict``                       — happy path; parsed into the agent's
                                   ``response_model`` and returned as ``Success``.
* ``BadOutput | Transient | ...``— pre-baked outcome returned as-is.
* ``ToolRoundTrip([calls], dict)`` — the fake first dispatches the listed
                                   tool calls (driving the engine's
                                   round-trip path) then returns the
                                   final structured payload.

The fake never inspects ``user_message``. Prompt assertions belong in
lint-layer tests, not the harness (Mahler-2, error-handling deep-dive).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Union

from pydantic import ValidationError

from outcomes import AgentOutcome, BadOutput, Cancelled, Success, Usage
from provider import AgentCall


@dataclass
class ToolRoundTrip:
    """Drive the engine through a tool round-trip before final output."""

    calls: list[tuple[str, dict[str, Any]]]
    final: dict[str, Any]


ScriptedEntry = Union[dict, AgentOutcome, ToolRoundTrip]


class FakeProviderError(RuntimeError):
    """The scenario didn't script a response the workflow asked for."""


@dataclass
class FakeProvider:
    """Replays scripted entries by agent name."""

    scripts: dict[str, list[ScriptedEntry]] = field(default_factory=dict)
    _cursor: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    calls: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)

    async def invoke(self, call: AgentCall) -> AgentOutcome:
        # Cooperative cancel: honoured before *and* during the call.
        if call.cancel is not None and call.cancel.is_set():
            return Cancelled(reason="cancelled before dispatch")

        # A brief await so the cancellation test can race us mid-flight.
        await asyncio.sleep(0)

        entries = self.scripts.get(call.spec.name)
        if not entries:
            raise FakeProviderError(
                f"FakeProvider has no scripted entries for agent "
                f"{call.spec.name!r}; add scripts[{call.spec.name!r}] = [...]"
            )

        idx = self._cursor.get(call.spec.name, 0)
        if idx >= len(entries):
            raise FakeProviderError(
                f"agent {call.spec.name!r} called {idx + 1} times but only "
                f"{len(entries)} entries scripted"
            )

        entry = entries[idx]
        self._cursor[call.spec.name] = idx + 1
        self.calls.append(
            {"agent": call.spec.name, "retry_key": call.retry_key, "entry_index": idx}
        )

        if call.event_callback:
            call.event_callback(
                "prompt", {"agent": call.spec.name, "retry_key": call.retry_key}
            )

        # Pre-baked outcomes (Transient / Permanent / Cancelled / BadOutput) — just return.
        if hasattr(entry, "type") and entry.type in {  # type: ignore[attr-defined]
            "transient",
            "permanent",
            "cancelled",
            "bad_output",
            "success",
        }:
            return entry  # type: ignore[return-value]

        if isinstance(entry, ToolRoundTrip):
            return await self._handle_tool_round_trip(call, entry)

        # dict happy-path → parse to response_model
        assert isinstance(entry, dict)
        return _bind(call, entry, tool_log=())

    async def _handle_tool_round_trip(
        self, call: AgentCall, entry: ToolRoundTrip
    ) -> AgentOutcome:
        tool_names: list[str] = []
        for name, args in entry.calls:
            tool = next((t for t in call.spec.tools if t.name == name), None)
            if tool is None:
                raise FakeProviderError(
                    f"scripted tool call {name!r} not registered on agent "
                    f"{call.spec.name!r}; registered: "
                    f"{[t.name for t in call.spec.tools]}"
                )
            result = tool.fn(**args)
            tool_names.append(name)
            if call.event_callback:
                call.event_callback(
                    "tool_call",
                    {"name": name, "args": args, "result": _safe_json(result)},
                )
        return _bind(call, entry.final, tool_log=tuple(tool_names))


def _bind(call: AgentCall, payload: dict[str, Any], tool_log: tuple[str, ...]) -> AgentOutcome:
    model = call.spec.response_model
    try:
        value = model.model_validate(payload)
    except ValidationError as ve:
        return BadOutput(
            raw=json.dumps(payload),
            errors=tuple(str(e) for e in ve.errors()),
        )
    return Success(value=value, tool_calls=tool_log, usage=Usage(model="fake"))


def _safe_json(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return repr(value)
