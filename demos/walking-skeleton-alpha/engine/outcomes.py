"""Stravinsky B — PEP 604 sealed dataclasses + `match`.

The engine routes on the *variant tag*. Nothing introspects the payload
to decide what happened — that is INV-DISCRIMINATED-OUTCOMES.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, TypeAlias


@dataclass(frozen=True, slots=True)
class Success:
    value: dict[str, Any] = field(default_factory=dict)
    inspected_artifacts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RetryableFailure:
    retry_key: str
    error_kind: str
    message: str
    attempt: int = 1


@dataclass(frozen=True, slots=True)
class PermanentFailure:
    error_kind: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NeedsHuman:
    gate: str
    prompt: str
    options: tuple[str, ...]
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Cancelled:
    cause: Literal["operator", "deadline", "superseded", "parent_cancelled"]
    at_step: str


Outcome: TypeAlias = Success | RetryableFailure | PermanentFailure | NeedsHuman | Cancelled

_REGISTRY: dict[str, type] = {
    "success": Success, "retryable_failure": RetryableFailure,
    "permanent_failure": PermanentFailure, "needs_human": NeedsHuman,
    "cancelled": Cancelled,
}
_TAG: dict[type, str] = {v: k for k, v in _REGISTRY.items()}


def outcome_to_dict(o: Outcome) -> dict[str, Any]:
    d = asdict(o)
    d["kind"] = _TAG[type(o)]
    return d


def outcome_from_dict(d: dict[str, Any]) -> Outcome:
    data = dict(d)
    kind = data.pop("kind")
    cls = _REGISTRY.get(kind)
    if cls is None:
        raise ValueError(f"unknown outcome kind {kind!r}")
    # tuples got serialized to lists; restore where the dataclass expects them.
    for k, v in list(data.items()):
        if isinstance(v, list):
            data[k] = tuple(v) if k in {"inspected_artifacts", "options"} else v
    return cls(**data)
