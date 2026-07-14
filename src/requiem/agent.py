"""Agent boundary — Mahler A (Protocol `AgentProvider` + `FakeProvider`).

The seam is one method:

    async def invoke(self, call: AgentCall) -> Outcome

There is no ABC, no inheritance requirement. `FakeProvider` satisfies the
Protocol structurally.

Validation failures produce `BadOutput` (not `PermanentFailure`) so the
kernel can route them to a remediation branch without triggering a
network retry.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from pydantic import BaseModel, ValidationError

from requiem.outcomes import (
    BadOutput,
    Cancelled,
    Outcome,
    PermanentFailure,
    RetryableFailure,
    Success,
)
from requiem.review_schemas import LeafReviewReport


@dataclass(frozen=True)
class AgentSpec:
    name: str
    charter: str
    response_model: type[BaseModel]
    model: str = "fake"
    role: str | None = None
    """Optional Requiem role tag (``planner``, ``reviewer``, ``implementer``,
    ``closer``, ``judge``, …). When set, the kernel asks
    :func:`requiem.model_routing.resolve_model_for_role` for an override
    against the loaded :class:`~requiem.process_config.ProcessConfig`.
    When ``None`` (default), the kernel uses ``model`` as-is — backward-
    compatible with every existing workflow (ADR-0030 §2)."""
    model_options: Mapping[str, Any] = field(default_factory=dict)
    """Provider-specific defaults for this agent.

    The kernel copies these into :class:`AgentCall.model_options`; resolved
    process configuration may override individual keys for the call.
    """


@dataclass(frozen=True)
class AgentCall:
    spec: AgentSpec
    user_message: str
    retry_key: str = ""
    cancel: asyncio.Event | None = None
    event_callback: Callable[[str, dict[str, Any]], None] | None = None
    model_options: Mapping[str, Any] = field(default_factory=dict)
    """ADR-0030 §2 (run #28 follow-up): per-call provider-specific knobs
    resolved from the operator's :class:`ProcessConfig.models.<role>`
    block. The kernel populates this when a routed role specifies extra
    fields beyond provider/model/max_tokens (e.g.
    ``reasoning_effort``, ``reasoning_summary``, ``context_tier`` for
    the Copilot provider). Each provider decides which keys it
    understands and silently ignores the rest — this lets operators
    add new knobs to process.yaml without coordinated provider changes.

    Empty dict (the default) preserves v0 behaviour: the provider
    uses its own constructor-time defaults for every knob."""


LEAF_REVIEWER = AgentSpec(
    name="leaf_reviewer",
    charter=(
        "You are Requiem's implementation-leaf reviewer. Review the leaf PR "
        "diff against its feature/<root> base, then return exactly one verdict: "
        "`approve`, `request_changes`, or `needs_human`. Use `request_changes` "
        "only when you can name concrete, code-level fixes; use `needs_human` "
        "for ambiguous risk, missing context, or anything unsafe to auto-merge. "
        "Every comment must include file, optional line, body, and severity."
    ),
    response_model=LeafReviewReport,
    role="reviewer",
)


@runtime_checkable
class AgentProvider(Protocol):
    async def invoke(self, call: AgentCall) -> Outcome: ...


@dataclass
class FakeProvider:
    """Scripted by agent name.

    Each entry in `scripts[agent_name]` is one of:

    * ``dict``    — happy path; validated into ``response_model`` →
                    ``Success``. Validation failure → ``BadOutput``.
    * ``Outcome`` — pre-baked outcome returned as-is.
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
                message=(
                    f"{call.spec.name!r} called {idx + 1}x "
                    f"but only {len(entries)} scripted"
                ),
            )
        self._cursor[call.spec.name] = idx + 1
        self.calls.append({
            "agent": call.spec.name,
            "retry_key": call.retry_key,
            "user_message": call.user_message,
            "model_options": dict(call.model_options),
        })

        entry = entries[idx]
        if call.event_callback:
            call.event_callback("prompt", {"agent": call.spec.name})

        if isinstance(
            entry,
            (Success, RetryableFailure, PermanentFailure, BadOutput, Cancelled),
        ):
            return entry
        if isinstance(entry, dict):
            try:
                value = call.spec.response_model.model_validate(entry)
            except ValidationError as ve:
                return BadOutput(
                    error_kind="schema_mismatch",
                    validation_errors=tuple(str(e) for e in ve.errors()),
                    raw_output=json.dumps(entry),
                )
            return Success(value={"agent": call.spec.name, "parsed": value.model_dump()})
        raise TypeError(
            f"FakeProvider got unknown entry shape: {type(entry).__name__}"
        )
