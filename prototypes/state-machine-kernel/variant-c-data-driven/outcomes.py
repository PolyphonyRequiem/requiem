"""Verb outcome discriminated union — variant C copy."""
from __future__ import annotations
from typing import Any, Literal, Union
from pydantic import BaseModel, Field


class Success(BaseModel):
    kind: Literal["success"] = "success"
    value: dict[str, Any] = Field(default_factory=dict)


class RetryableFailure(BaseModel):
    kind: Literal["retryable_failure"] = "retryable_failure"
    reason: str = ""
    error_kind: str = "unknown"


class PermanentFailure(BaseModel):
    kind: Literal["permanent_failure"] = "permanent_failure"
    reason: str = ""
    error_kind: str = "unknown"


class NeedsHuman(BaseModel):
    kind: Literal["needs_human"] = "needs_human"
    prompt: str = ""
    options: list[str] = Field(default_factory=list)
    schema_hint: dict[str, Any] = Field(default_factory=dict)


class Cancelled(BaseModel):
    kind: Literal["cancelled"] = "cancelled"
    reason: str = ""


Outcome = Union[Success, RetryableFailure, PermanentFailure, NeedsHuman, Cancelled]
