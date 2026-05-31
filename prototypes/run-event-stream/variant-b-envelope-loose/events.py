"""Variant B — single envelope `Event(kind: str, schema_version: int, payload: dict)`.

The top-level contract is intentionally loose. Per-kind pydantic models live
in a `PAYLOAD_REGISTRY` consulted by the consumer when (and only when) it
needs typed access to a particular kind. Unknown kinds round-trip safely.

Why this shape is interesting for Requiem:
- Forward-compat is free: any reader can ingest any future kind.
- Mixed-version cohorts (engine vN, UI backend vN-1) Just Work.
- The 20 domain signals can hitch a ride on the same envelope (one kind:
  `domain_signal`) without inventing a second file.

The cost is real, though: top-level type loss means the consumer must opt
into validation per-kind. We pay that cost explicitly in `parse_typed`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1


class Event(BaseModel):
    """The on-disk envelope. The ONLY shape every reader must understand."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: int
    run_id: str
    ts: datetime
    kind: str
    schema_version: int = SCHEMA_VERSION
    node_path: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


# ---- per-kind payload models (consumer-side; not on the wire) -----------

class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RunStartedP(_Payload):
    workflow: str
    workflow_version: str


class NodeEnteredP(_Payload):
    node_kind: str


class VerbInvokedP(_Payload):
    verb: str
    args_digest: str


class OutcomeP(_Payload):
    kind: Literal["success", "retryable_failure", "permanent_failure", "needs_human", "cancelled"]
    reason: str | None = None
    prompt: str | None = None
    value: dict[str, Any] | None = None


class VerbCompletedP(_Payload):
    verb: str
    outcome: OutcomeP


class GateOpenedP(_Payload):
    gate_id: str
    prompt: str


class RunCompletedP(_Payload):
    terminal: Literal["completed", "surrendered", "superseded"]


# v2 patch:
class RetryAttemptedP(_Payload):
    verb: str
    attempt: int
    of: int
    delay_ms: int


PAYLOAD_REGISTRY_V1: dict[str, type[_Payload]] = {
    "run_started": RunStartedP,
    "node_entered": NodeEnteredP,
    "verb_invoked": VerbInvokedP,
    "verb_completed": VerbCompletedP,
    "gate_opened": GateOpenedP,
    "run_completed": RunCompletedP,
}

PAYLOAD_REGISTRY_V2: dict[str, type[_Payload]] = {
    **PAYLOAD_REGISTRY_V1,
    "retry_attempted": RetryAttemptedP,
}


class TypedEvent:
    """Convenience wrapper combining the envelope and the parsed payload."""

    __slots__ = ("envelope", "payload", "known")

    def __init__(self, envelope: Event, payload: _Payload | None, known: bool):
        self.envelope = envelope
        self.payload = payload  # None when known=False
        self.known = known

    def __repr__(self) -> str:  # pragma: no cover
        body = f"payload={self.payload!r}" if self.known else "payload=<unknown>"
        return f"TypedEvent(kind={self.envelope.kind!r}, {body})"

    KNOWN: ClassVar[str] = "known"


def parse_envelope(raw: dict[str, Any]) -> Event:
    return Event.model_validate(raw)


def make_typed_parser(registry: dict[str, type[_Payload]]):
    def parse(raw: dict[str, Any]) -> TypedEvent:
        env = parse_envelope(raw)
        model = registry.get(env.kind)
        if model is None:
            return TypedEvent(env, None, known=False)
        return TypedEvent(env, model.model_validate(env.payload), known=True)
    return parse


def now() -> datetime:
    return datetime.now(tz=timezone.utc)
