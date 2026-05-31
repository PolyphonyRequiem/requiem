"""Variant A — one typed pydantic model per event kind, unionized by discriminator.

Schema evolution: adding a kind = adding a model + extending the union.
Old readers that haven't been recompiled with the new kind will see it as
`UnknownEvent` (caught at parse time so we never silently lose a line).

`SCHEMA_VERSION` is the *envelope* version; per-event payload changes within
a kind are additive (pydantic ignores unknown fields when configured to, but
we deliberately keep `extra="forbid"` to detect drift and force a bump).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from outcomes import Outcome

SCHEMA_VERSION = 1


class _Event(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: int
    run_id: str
    ts: datetime
    node_path: str | None = None


class RunStarted(_Event):
    event_type: Literal["run_started"] = "run_started"
    workflow: str
    workflow_version: str


class NodeEntered(_Event):
    event_type: Literal["node_entered"] = "node_entered"
    node_kind: str


class VerbInvoked(_Event):
    event_type: Literal["verb_invoked"] = "verb_invoked"
    verb: str
    args_digest: str


class VerbCompleted(_Event):
    event_type: Literal["verb_completed"] = "verb_completed"
    verb: str
    outcome: Outcome


class GateOpened(_Event):
    event_type: Literal["gate_opened"] = "gate_opened"
    gate_id: str
    prompt: str


class RunCompleted(_Event):
    event_type: Literal["run_completed"] = "run_completed"
    terminal: Literal["completed", "surrendered", "superseded"]


# ---- v2 patch: a new kind appears later in the project's life ------------

class RetryAttempted(_Event):
    """Added in v2 of the schema. v1 readers will not have this in their union."""

    event_type: Literal["retry_attempted"] = "retry_attempted"
    verb: str
    attempt: int
    of: int
    delay_ms: int


KnownEventV1 = Annotated[
    Union[RunStarted, NodeEntered, VerbInvoked, VerbCompleted, GateOpened, RunCompleted],
    Field(discriminator="event_type"),
]
KnownEventV2 = Annotated[
    Union[
        RunStarted, NodeEntered, VerbInvoked, VerbCompleted, GateOpened, RunCompleted, RetryAttempted,
    ],
    Field(discriminator="event_type"),
]

V1_ADAPTER = TypeAdapter(KnownEventV1)
V2_ADAPTER = TypeAdapter(KnownEventV2)


class UnknownEvent(BaseModel):
    """Sentinel returned (never silently discarded) when the reader's union
    doesn't recognize the kind. Carries the raw dict so a later reader can
    re-parse, and `event_id`/`ts` so projection-side ordering still works.

    A derive function MUST decide whether to halt or tolerate; we do not
    pretend the event didn't happen (INV-NO-CORRUPT-FORWARD).
    """

    model_config = ConfigDict(frozen=True)

    event_id: int
    run_id: str
    ts: datetime
    event_type: str
    raw: dict


def parse_v1(raw: dict[str, Any]) -> Any:
    try:
        return V1_ADAPTER.validate_python(raw)
    except ValidationError as exc:
        # Discriminator miss = forward-compat case; field-shape miss = corruption.
        if _is_unknown_discriminator(exc):
            return UnknownEvent(
                event_id=int(raw.get("event_id", -1)),
                run_id=str(raw.get("run_id", "")),
                ts=_coerce_ts(raw.get("ts")),
                event_type=str(raw.get("event_type", "?")),
                raw=raw,
            )
        raise


def parse_v2(raw: dict[str, Any]) -> Any:
    return V2_ADAPTER.validate_python(raw)


def _is_unknown_discriminator(exc: ValidationError) -> bool:
    return any(e.get("type") == "union_tag_invalid" for e in exc.errors())


def _coerce_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now(tz=timezone.utc)
