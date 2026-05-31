"""Mahler A — Protocol-based agent boundary + FakeProvider.

The seam is one method:

    async def invoke(self, call: AgentCall) -> Outcome

There is no ABC, no inheritance requirement. `FakeProvider` satisfies the
Protocol structurally. The provider always returns one of Stravinsky's
outcome variants — `Success | BadOutput | Transient | Permanent | Cancelled`
collapse to the same five tags the engine already routes on.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from pydantic import BaseModel, ValidationError

from engine.outcomes import Cancelled, Outcome, PermanentFailure, RetryableFailure, Success


@dataclass(frozen=True)
class AgentSpec:
    name: str
    charter: str
    response_model: type[BaseModel]
    model: str = "fake"


@dataclass(frozen=True)
class AgentCall:
    spec: AgentSpec
    user_message: str
    retry_key: str = ""
    cancel: asyncio.Event | None = None
    event_callback: Callable[[str, dict[str, Any]], None] | None = None


@runtime_checkable
class AgentProvider(Protocol):
    async def invoke(self, call: AgentCall) -> Outcome: ...


@dataclass
class FakeProvider:
    """Scripted by agent name. Same shape Mahler-2 wants in the polyphony fake.

    Each entry in `scripts[agent_name]` is:
      * dict           — happy path; validated into `response_model` → Success
      * Outcome        — pre-baked outcome returned as-is
    """

    scripts: dict[str, list[Any]] = field(default_factory=dict)
    _cursor: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    calls: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)

    async def invoke(self, call: AgentCall) -> Outcome:
        if call.cancel is not None and call.cancel.is_set():
            return Cancelled(cause="operator", at_step=call.spec.name)
        await asyncio.sleep(0)  # let the cancel test race us

        entries = self.scripts.get(call.spec.name)
        if not entries:
            return PermanentFailure(
                error_kind="fake.unscripted",
                message=f"no scripts[{call.spec.name!r}]",
            )

        idx = self._cursor.get(call.spec.name, 0)
        if idx >= len(entries):
            return PermanentFailure(
                error_kind="fake.exhausted",
                message=f"{call.spec.name!r} called {idx+1}x but only {len(entries)} scripted",
            )
        self._cursor[call.spec.name] = idx + 1
        self.calls.append({"agent": call.spec.name, "retry_key": call.retry_key})

        entry = entries[idx]
        if call.event_callback:
            call.event_callback("prompt", {"agent": call.spec.name})

        if isinstance(entry, (Success, RetryableFailure, PermanentFailure, Cancelled)):
            return entry
        if isinstance(entry, dict):
            try:
                value = call.spec.response_model.model_validate(entry)
            except ValidationError as ve:
                return PermanentFailure(
                    error_kind="bad_output",
                    message=f"schema mismatch on {call.spec.name}",
                    details={"errors": [str(e) for e in ve.errors()],
                             "raw": json.dumps(entry)},
                )
            return Success(value={"agent": call.spec.name, "parsed": value.model_dump()})
        raise TypeError(f"FakeProvider got unknown entry shape: {type(entry).__name__}")
