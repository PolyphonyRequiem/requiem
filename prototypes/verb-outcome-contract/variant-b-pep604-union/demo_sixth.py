"""
Variant B — PEP 604 type-union with sealed dataclasses + `match`.

Type-first. Each outcome kind is a frozen dataclass; the union is a TypeAlias.
Engine dispatch is a `match` statement. Exhaustiveness is enforced by
`assert_never` in the trailing `case _:` — mypy --strict will flag a missing
arm at the call site.

JSON is custom (one `kind` field synthesized at the seam), but the type
system never has to look at it: validation maps `kind` → class once and the
rest is pure dataclass construction.

Run:
    python demo.py
    python -m mypy --strict demo.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, NoReturn, TypeAlias, assert_never


# ─────────────────────────────────────────────────────────────────────────────
# THE CONTRACT — five sealed dataclasses, one union TypeAlias.
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Success:
    result: dict[str, Any] = field(default_factory=dict)
    inspected_artifacts: list[str] = field(default_factory=list)
    domain_signals: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RetryableFailure:
    retry_key: str
    error_kind: str
    message: str
    attempt: int = 1
    cause: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class PermanentFailure:
    error_kind: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NeedsHuman:
    gate: str
    prompt: str
    choices: list[str]
    context: dict[str, Any] = field(default_factory=dict)


CancelCause: TypeAlias = Literal["operator", "deadline", "superseded", "parent_cancelled"]


@dataclass(frozen=True, slots=True)
class Cancelled:
    cause: CancelCause
    at_step: str
    partial_progress: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class WeirdSixth:
    reason: str


Outcome: TypeAlias = Success | RetryableFailure | PermanentFailure | NeedsHuman | Cancelled | WeirdSixth


# The class registry is the only mutable piece; it's how `from_dict` maps the
# wire-level `kind` tag back to a concrete dataclass. Adding a sixth kind
# without registering it is a runtime error; *failing* to add it to the
# match statement is a mypy error (the load-bearing check).
_REGISTRY: dict[str, type] = {
    "success": Success,
    "retryable_failure": RetryableFailure,
    "permanent_failure": PermanentFailure,
    "needs_human": NeedsHuman,
    "cancelled": Cancelled,
}
_TAG: dict[type, str] = {v: k for k, v in _REGISTRY.items()}


# ─────────────────────────────────────────────────────────────────────────────
# DEMO VERBS — one per kind.
# ─────────────────────────────────────────────────────────────────────────────


def verb_happy() -> Outcome:
    return Success(
        result={"feature_branch": "feature/req-42"},
        inspected_artifacts=["git:refs/heads/feature/req-42@abc123"],
        domain_signals=["surface_opened"],
    )


def verb_transient(attempt: int = 1) -> Outcome:
    return RetryableFailure(
        retry_key="run-7:gh_create_pr:req-42",
        error_kind="network.timeout",
        message="gh api timed out after 30s",
        attempt=attempt,
    )


def verb_permanent() -> Outcome:
    return PermanentFailure(
        error_kind="auth.invalid_token",
        message="GitHub returned 401 Bad credentials. Refresh `gh auth login` and re-run.",
        details={"http_status": 401, "scope_required": "repo"},
    )


def verb_needs_human() -> Outcome:
    return NeedsHuman(
        gate="receipts_violation",
        prompt="Reviewer claimed PR #123 was approved, but git shows no approving review.",
        choices=["trust_reviewer", "reroll", "surrender"],
        context={"pr": 123, "claimed_reviewers": ["alice"], "actual_reviews": []},
    )


def verb_cancelled() -> Outcome:
    return Cancelled(
        cause="deadline",
        at_step="agent.coder_loop",
        partial_progress={"files_written": 3, "tests_added": 0},
    )


# ─────────────────────────────────────────────────────────────────────────────
# ENGINE — `match` on the dataclass type. Exhaustiveness checked by mypy.
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    backoff_seconds: float


HTTP_RETRY = RetryPolicy(max_attempts=3, backoff_seconds=1.0)
NO_RETRY = RetryPolicy(max_attempts=1, backoff_seconds=0.0)


@dataclass(frozen=True)
class Decision:
    next_state: str
    retry_policy: RetryPolicy
    user_message: str | None = None
    domain_signal: str | None = None


def engine_decide(outcome: Outcome) -> Decision:
    match outcome:
        case Success(domain_signals=signals):
            return Decision(
                next_state="advance",
                retry_policy=NO_RETRY,
                domain_signal=(signals[0] if signals else None),
            )
        case RetryableFailure(attempt=a) if a >= HTTP_RETRY.max_attempts:
            return Decision(
                next_state="surrender",
                retry_policy=NO_RETRY,
                user_message=f"Gave up after {outcome.attempt} attempts: {outcome.message}",
                domain_signal="retry_exhausted",
            )
        case RetryableFailure():
            return Decision(
                next_state="retry",
                retry_policy=HTTP_RETRY,
                user_message=f"Retrying ({outcome.attempt}/{HTTP_RETRY.max_attempts}): {outcome.error_kind}",
            )
        case PermanentFailure(message=msg):
            return Decision(next_state="surrender", retry_policy=NO_RETRY, user_message=msg)
        case NeedsHuman(prompt=prompt):
            return Decision(next_state="human_gate", retry_policy=NO_RETRY, user_message=prompt)
        case Cancelled(cause=cause, at_step=step):
            # INV-CANCEL-SHORT-CIRCUITS-RETRY
            return Decision(
                next_state="cancelled",
                retry_policy=NO_RETRY,
                user_message=f"Cancelled ({cause}) at {step}",
            )
        case _:
            assert_never(outcome)


# ─────────────────────────────────────────────────────────────────────────────
# JSON ROUND-TRIP — synthesize `kind` on write; dispatch on `kind` on read.
# ─────────────────────────────────────────────────────────────────────────────


def write_event(outcome: Outcome) -> str:
    payload = asdict(outcome)
    payload["kind"] = _TAG[type(outcome)]
    return json.dumps(payload, separators=(",", ":"))


def read_event(line: str) -> Outcome:
    data = json.loads(line)
    kind = data.pop("kind")
    cls = _REGISTRY.get(kind)
    if cls is None:
        # INV-NO-CORRUPT-FORWARD: unknown kind = corruption signal, not coerce.
        raise ValueError(f"unknown outcome kind: {kind!r}")
    return cls(**data)  # type: ignore[no-any-return]


# ─────────────────────────────────────────────────────────────────────────────
# UI-FACING RENDER — same exhaustiveness story as engine_decide.
# ─────────────────────────────────────────────────────────────────────────────


def render_for_ui(outcome: Outcome) -> str:
    match outcome:
        case Success(inspected_artifacts=arts):
            return f"OK — receipts: {arts}"
        case RetryableFailure(error_kind=k, attempt=a, message=m):
            return f"transient ({k}, attempt {a}): {m}"
        case PermanentFailure(error_kind=k, message=m):
            hint = "\n  → check credentials, then `requiem reconcile`" if k.startswith("auth.") else ""
            return f"FAILED ({k}): {m}{hint}"
        case NeedsHuman(gate=g, prompt=p, choices=c):
            return f"HUMAN GATE [{g}]: {p}\n  choices: {c}"
        case Cancelled(cause=c, at_step=s):
            return f"CANCELLED ({c}) at {s}"
        case _:
            assert_never(outcome)


# ─────────────────────────────────────────────────────────────────────────────
# EXHAUSTIVENESS CHECK
# ─────────────────────────────────────────────────────────────────────────────
# Uncomment to see mypy --strict reject `engine_decide` and `render_for_ui`:
#
# @dataclass(frozen=True, slots=True)
# class WeirdSixth:
#     reason: str
# Outcome = Success | RetryableFailure | PermanentFailure | NeedsHuman | Cancelled | WeirdSixth  # noqa: E501
# # mypy errors at `assert_never(outcome)`: argument is `WeirdSixth`, not `Never`.


def main() -> int:
    verbs: list[tuple[str, Outcome]] = [
        ("happy", verb_happy()),
        ("transient", verb_transient(attempt=1)),
        ("transient-exhausted", verb_transient(attempt=3)),
        ("permanent", verb_permanent()),
        ("needs_human", verb_needs_human()),
        ("cancelled", verb_cancelled()),
    ]
    print("=" * 72)
    print("Variant B — PEP 604 sealed-dataclass union + match")
    print("=" * 72)
    for label, outcome in verbs:
        line = write_event(outcome)
        roundtripped = read_event(line)
        assert type(roundtripped) is type(outcome), "round-trip type mismatch"
        decision = engine_decide(roundtripped)
        print(f"\n[{label}] type={type(outcome).__name__}")
        print(f"  jsonl       : {line}")
        print(f"  decision    : next_state={decision.next_state!r} retry={decision.retry_policy.max_attempts}x")
        if decision.user_message:
            print(f"  ui          : {render_for_ui(roundtripped)}")
        if decision.domain_signal:
            print(f"  signal      : {decision.domain_signal}")

    cancelled_decision = engine_decide(verb_cancelled())
    assert cancelled_decision.retry_policy is NO_RETRY
    assert cancelled_decision.next_state == "cancelled"
    print("\n[invariant] cancelled never schedules a retry. ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
