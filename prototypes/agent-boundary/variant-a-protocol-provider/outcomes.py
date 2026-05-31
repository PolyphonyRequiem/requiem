"""Discriminated outcome union for the agent boundary.

INV-DISCRIMINATED-OUTCOMES (north-star §2): every call returns one of
``Success | BadOutput | Transient | Permanent | Cancelled``. The variant
tag *is* the contract — no consumer inspects an ``error`` field, no
caller checks ``output is None``. The router keys off ``type``.

``BadOutput`` is the LLM-specific failure mode: the model returned, the
HTTP call succeeded, but the bytes do not parse into the requested
``response_model``. Mahler classes this as *neither* transient nor
permanent — it is its own variant because the workflow author may want
to remediate (re-prompt) without exhausting the network retry budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, Literal, TypeVar, Union

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class Usage:
    """Token / cost accounting; provider fills what it knows."""

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
    """Model returned but output failed ``response_model`` validation."""

    type: Literal["bad_output"] = "bad_output"
    raw: str = ""
    errors: tuple[str, ...] = ()
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True)
class Transient:
    """Network/auth/rate-limit. Subject to retry budget (max 3, hard cap)."""

    type: Literal["transient"] = "transient"
    reason: str = ""
    retry_after_s: float | None = None


@dataclass(frozen=True)
class Permanent:
    """No retry will help. Routes to surrender / human gate per workflow."""

    type: Literal["permanent"] = "permanent"
    reason: str = ""


@dataclass(frozen=True)
class Cancelled:
    """Caller-initiated cancel. INV-CANCEL-SHORT-CIRCUITS-RETRY: never retried."""

    type: Literal["cancelled"] = "cancelled"
    reason: str = "cancelled"


AgentOutcome = Union[Success[T], BadOutput, Transient, Permanent, Cancelled]
