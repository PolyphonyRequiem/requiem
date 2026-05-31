"""Verb outcome discriminated union — INV-DISCRIMINATED-OUTCOMES."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field


class Success(BaseModel):
    kind: Literal["success"] = "success"
    payload: dict[str, Any] = Field(default_factory=dict)


class RetryableFailure(BaseModel):
    kind: Literal["retryable"] = "retryable"
    reason: str
    retry_key: str | None = None


class PermanentFailure(BaseModel):
    kind: Literal["permanent"] = "permanent"
    reason: str


class NeedsHuman(BaseModel):
    kind: Literal["needs_human"] = "needs_human"
    prompt: str
    options: list[str]


class Cancelled(BaseModel):
    kind: Literal["cancelled"] = "cancelled"
    reason: str = "cancelled"


Outcome = Annotated[
    Union[Success, RetryableFailure, PermanentFailure, NeedsHuman, Cancelled],
    Field(discriminator="kind"),
]
