"""Verb outcome discriminated union (Stravinsky's shape).

Carried inline as the `outcome` payload of `VerbCompleted` events.
Variant A keeps the union strongly typed all the way down.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Success(_Frozen):
    kind: Literal["success"] = "success"
    value: dict = Field(default_factory=dict)


class RetryableFailure(_Frozen):
    kind: Literal["retryable_failure"] = "retryable_failure"
    reason: str
    transient_class: str = "unknown"


class PermanentFailure(_Frozen):
    kind: Literal["permanent_failure"] = "permanent_failure"
    reason: str


class NeedsHuman(_Frozen):
    kind: Literal["needs_human"] = "needs_human"
    prompt: str
    surface: str = "ui"


class Cancelled(_Frozen):
    kind: Literal["cancelled"] = "cancelled"
    reason: str = "cancel_token"


Outcome = Annotated[
    Union[Success, RetryableFailure, PermanentFailure, NeedsHuman, Cancelled],
    Field(discriminator="kind"),
]
