"""Variant C — CloudEvents 1.0 envelope.

The on-disk record is a CloudEvents 1.0 *Structured Mode JSON* envelope
(spec: https://github.com/cloudevents/spec/blob/v1.0.2/cloudevents/spec.md).

Envelope fields we set (and validate strictly):
  specversion : "1.0"
  type        : reverse-DNS event type, e.g. "io.requiem.run.started"
  source      : URI of the producer, e.g. "/requiem/engine/run/<id>"
  id          : unique within `source`; we use `<run_id>:<event_id>`
  time        : RFC3339 timestamp
  datacontenttype : "application/json"
  dataschema  : URI pointer to the body schema (carries the per-kind version)
  data        : the typed payload (per-kind body)

Why this is interesting for Requiem:
- Standard envelope = tools already exist (cloudevents SDKs, Knative
  receivers, Otel bridges, Hermes-style notifiers). Zero-cost interop signal.
- Envelope/body separation by construction. The body can evolve under
  `dataschema` without re-cutting the envelope.
- `id`+`source` are spec'd to be globally addressable; the file is one of
  many possible delivery channels (e.g., re-broadcast over SSE without
  re-mapping fields).

The cost: ~2× line size vs variant B, and Requiem-specific fields (run_id,
node_path) need a home — we put them under `data` because CE extension
attributes must be flat top-level strings/numbers/bool, which can't carry
the structured payload Requiem wants.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1
SOURCE_PREFIX = "/requiem/engine"
TYPE_PREFIX = "io.requiem"


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RunStartedD(_Payload):
    run_id: str
    node_path: str | None = None
    workflow: str
    workflow_version: str


class NodeEnteredD(_Payload):
    run_id: str
    node_path: str
    node_kind: str


class VerbInvokedD(_Payload):
    run_id: str
    node_path: str
    verb: str
    args_digest: str


class OutcomeD(_Payload):
    kind: Literal["success", "retryable_failure", "permanent_failure", "needs_human", "cancelled"]
    reason: str | None = None
    prompt: str | None = None
    value: dict[str, Any] | None = None


class VerbCompletedD(_Payload):
    run_id: str
    node_path: str
    verb: str
    outcome: OutcomeD


class GateOpenedD(_Payload):
    run_id: str
    node_path: str
    gate_id: str
    prompt: str


class RunCompletedD(_Payload):
    run_id: str
    terminal: Literal["completed", "surrendered", "superseded"]


class RetryAttemptedD(_Payload):  # v2 patch
    run_id: str
    node_path: str
    verb: str
    attempt: int
    of: int
    delay_ms: int


TYPE_REGISTRY_V1: dict[str, type[_Payload]] = {
    f"{TYPE_PREFIX}.run.started": RunStartedD,
    f"{TYPE_PREFIX}.node.entered": NodeEnteredD,
    f"{TYPE_PREFIX}.verb.invoked": VerbInvokedD,
    f"{TYPE_PREFIX}.verb.completed": VerbCompletedD,
    f"{TYPE_PREFIX}.gate.opened": GateOpenedD,
    f"{TYPE_PREFIX}.run.completed": RunCompletedD,
}
TYPE_REGISTRY_V2: dict[str, type[_Payload]] = {
    **TYPE_REGISTRY_V1,
    f"{TYPE_PREFIX}.verb.retry_attempted": RetryAttemptedD,
}


class CloudEvent(BaseModel):
    """Structured-mode CloudEvent envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    specversion: Literal["1.0"] = "1.0"
    type: str
    source: str
    id: str
    time: datetime
    datacontenttype: Literal["application/json"] = "application/json"
    dataschema: str
    data: dict[str, Any] = Field(default_factory=dict)


class TypedCloudEvent:
    __slots__ = ("envelope", "data", "known")

    def __init__(self, envelope: CloudEvent, data: _Payload | None, known: bool):
        self.envelope = envelope
        self.data = data
        self.known = known

    KNOWN: ClassVar[str] = "known"

    def __repr__(self) -> str:  # pragma: no cover
        return f"TypedCloudEvent(type={self.envelope.type!r}, known={self.known})"


def make_parser(registry: dict[str, type[_Payload]]):
    def parse(raw: dict[str, Any]) -> TypedCloudEvent:
        env = CloudEvent.model_validate(raw)
        model = registry.get(env.type)
        if model is None:
            return TypedCloudEvent(env, None, known=False)
        return TypedCloudEvent(env, model.model_validate(env.data), known=True)
    return parse


def now() -> datetime:
    return datetime.now(tz=timezone.utc)


def make_id(run_id: str, event_id: int) -> str:
    return f"{run_id}:{event_id}"


def schema_uri(type_: str, version: int) -> str:
    return f"https://requiem.dev/schemas/{type_.replace('.', '/')}/v{version}.json"
