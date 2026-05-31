"""Variant B — outcomes, reused-in-spirit-from-variant-A.

Even when the underlying library has its own exception taxonomy
(``ModelHTTPError``, ``UsageLimitExceeded``, ``UnexpectedModelBehavior``,
``ModelRetry``, …), the engine still consumes a discriminated outcome.
The conversion happens at the seam in ``provider.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, Literal, TypeVar, Union

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None


@dataclass(frozen=True)
class Success(Generic[T]):
    type: Literal["success"] = "success"
    value: T = field(default=None)  # type: ignore[assignment]
    usage: Usage = field(default_factory=Usage)
    tool_calls: tuple[str, ...] = ()


@dataclass(frozen=True)
class BadOutput:
    type: Literal["bad_output"] = "bad_output"
    raw: str = ""
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class Transient:
    type: Literal["transient"] = "transient"
    reason: str = ""
    retry_after_s: float | None = None


@dataclass(frozen=True)
class Permanent:
    type: Literal["permanent"] = "permanent"
    reason: str = ""


@dataclass(frozen=True)
class Cancelled:
    type: Literal["cancelled"] = "cancelled"
    reason: str = "cancelled"


AgentOutcome = Union[Success[T], BadOutput, Transient, Permanent, Cancelled]
