"""Verb outcomes — Stravinsky B (PEP 604 sealed dataclasses + `match`).

The discriminated union every verb returns. The engine routes on the
*variant tag*. Nothing introspects the payload to decide what happened —
that is `INV-DISCRIMINATED-OUTCOMES`.

Six variants:

* `Success`            — verb succeeded; carries `value` and `inspected_artifacts`.
* `RetryableFailure`   — transient error; the kernel may retry within budget.
* `PermanentFailure`   — non-retryable error; routes to `permanent_failure[:kind]`.
* `BadOutput`          — agent or verb returned output that failed validation.
                         **Distinct from `PermanentFailure`** because BadOutput
                         must NOT be network-retried — it routes to a
                         remediation branch (`bad_output`) when one is wired,
                         otherwise falls through to `permanent_failure`.
* `NeedsHuman`         — surfaces a gate; the kernel suspends or invokes a
                         handler.
* `Cancelled`          — operator/deadline/supersession cancel. Honoured
                         immediately (`INV-CANCEL-SHORT-CIRCUITS-RETRY`).

Every variant carries a peer ``receipts: tuple[Receipt, ...]`` field per
ADR 0004 §4.4 — failure forensics matter as much as success ones.
``Receipt`` is a loose ``dict[str, Any]`` at v0; a typed protocol is
deferred to a Phase D ADR.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, TypeAlias


Receipt: TypeAlias = dict[str, Any]
"""Forensic record attached to an outcome (ADR 0004 §4.4).

Loose dict at v0 — a typed `Receipt` protocol is deferred to a Phase D
ADR. The shape today (set by `requiem.providers._common.make_receipt`):
``{"kind": str, "model": str, "input_tokens": int, "output_tokens": int,
"latency_ms": int, "request_id": str, "error": str}``. Verbs are free to
emit other receipt shapes (e.g. git/filesystem) as long as ``kind`` is
present.
"""


@dataclass(frozen=True, slots=True)
class Success:
    value: dict[str, Any] = field(default_factory=dict)
    inspected_artifacts: tuple[str, ...] = ()
    receipts: tuple[Receipt, ...] = ()


@dataclass(frozen=True, slots=True)
class RetryableFailure:
    retry_key: str
    error_kind: str
    message: str
    attempt: int = 1
    after: float | None = None
    """Seconds to wait before the next attempt. Populated from provider
    ``Retry-After`` headers (or a sensible default per error class).
    ``None`` means "no provider hint"; the kernel retries immediately.
    The kernel bounds the actual sleep at 60s (a runaway ``999999``
    retry-after would otherwise hang the run); larger values are
    surfaced via a NeedsHuman gate instead (ADR 0004 §4.2)."""
    receipts: tuple[Receipt, ...] = ()


@dataclass(frozen=True, slots=True)
class PermanentFailure:
    error_kind: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    receipts: tuple[Receipt, ...] = ()


@dataclass(frozen=True, slots=True)
class BadOutput:
    """Agent/verb produced output that failed structured validation.

    Routed distinctly from `PermanentFailure` so authors can wire a
    remediation branch (e.g. ask the agent to re-emit) without triggering
    a network retry. If no `bad_output` edge exists, the kernel falls
    through to `permanent_failure`.
    """
    error_kind: str
    validation_errors: tuple[str, ...]
    raw_output: str = ""
    receipts: tuple[Receipt, ...] = ()


@dataclass(frozen=True, slots=True)
class NeedsHuman:
    gate: str
    prompt: str
    options: tuple[str, ...]
    context: dict[str, Any] = field(default_factory=dict)
    receipts: tuple[Receipt, ...] = ()


@dataclass(frozen=True, slots=True)
class Cancelled:
    cause: Literal["operator", "deadline", "superseded", "parent_cancelled"]
    at_step: str
    receipts: tuple[Receipt, ...] = ()


Outcome: TypeAlias = (
    Success | RetryableFailure | PermanentFailure | BadOutput | NeedsHuman | Cancelled
)


_REGISTRY: dict[str, type] = {
    "success": Success,
    "retryable_failure": RetryableFailure,
    "permanent_failure": PermanentFailure,
    "bad_output": BadOutput,
    "needs_human": NeedsHuman,
    "cancelled": Cancelled,
}
_TAG: dict[type, str] = {v: k for k, v in _REGISTRY.items()}


def outcome_kind(o: Outcome) -> str:
    """Return the wire-tag for an outcome variant."""
    return _TAG[type(o)]


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
    tuple_fields = {
        "inspected_artifacts", "options", "validation_errors", "receipts",
    }
    for k, v in list(data.items()):
        if isinstance(v, list) and k in tuple_fields:
            data[k] = tuple(v)
    return cls(**data)
