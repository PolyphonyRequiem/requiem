"""
Variant C — ABC sealed hierarchy with `Outcome.dispatch(handler)`.

OO-first. Each outcome class implements `dispatch(handler)` to call the
correct method on a `Protocol`-shaped handler. The engine is one handler;
the UI renderer is another. Adding a sixth kind forces every Protocol
implementer to add a method — a mypy `--strict` error at every consumer.

Trade-off vs A/B: heavier boilerplate (every outcome has a `dispatch`
method); strongest guarantee that *every* consumer handles every kind
(B's `assert_never` only fires inside the function that uses `match`).

Run:
    python demo.py
    python -m mypy --strict demo.py
"""

from __future__ import annotations

import json
import sys
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Generic, Literal, Protocol, TypeAlias, TypeVar

T = TypeVar("T", covariant=True)


# ─────────────────────────────────────────────────────────────────────────────
# THE CONTRACT — abstract base + 5 sealed leaves + visitor protocol.
# ─────────────────────────────────────────────────────────────────────────────


class OutcomeHandler(Protocol, Generic[T]):
    """A consumer of outcomes. Every method MUST be implemented;
    a missing method is a mypy --strict error at the call site of `dispatch`."""

    def on_success(self, o: "Success") -> T: ...
    def on_retryable_failure(self, o: "RetryableFailure") -> T: ...
    def on_permanent_failure(self, o: "PermanentFailure") -> T: ...
    def on_needs_human(self, o: "NeedsHuman") -> T: ...
    def on_cancelled(self, o: "Cancelled") -> T: ...


class Outcome(ABC):
    @abstractmethod
    def dispatch(self, handler: OutcomeHandler[T]) -> T: ...

    @abstractmethod
    def to_dict(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class Success(Outcome):
    result: dict[str, Any] = field(default_factory=dict)
    inspected_artifacts: list[str] = field(default_factory=list)
    domain_signals: list[str] = field(default_factory=list)

    def dispatch(self, handler: OutcomeHandler[T]) -> T:
        return handler.on_success(self)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "success", **asdict(self)}


@dataclass(frozen=True, slots=True)
class RetryableFailure(Outcome):
    retry_key: str
    error_kind: str
    message: str
    attempt: int = 1
    cause: dict[str, Any] | None = None

    def dispatch(self, handler: OutcomeHandler[T]) -> T:
        return handler.on_retryable_failure(self)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "retryable_failure", **asdict(self)}


@dataclass(frozen=True, slots=True)
class PermanentFailure(Outcome):
    error_kind: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def dispatch(self, handler: OutcomeHandler[T]) -> T:
        return handler.on_permanent_failure(self)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "permanent_failure", **asdict(self)}


@dataclass(frozen=True, slots=True)
class NeedsHuman(Outcome):
    gate: str
    prompt: str
    choices: list[str]
    context: dict[str, Any] = field(default_factory=dict)

    def dispatch(self, handler: OutcomeHandler[T]) -> T:
        return handler.on_needs_human(self)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "needs_human", **asdict(self)}


CancelCause: TypeAlias = Literal["operator", "deadline", "superseded", "parent_cancelled"]


@dataclass(frozen=True, slots=True)
class Cancelled(Outcome):
    cause: CancelCause
    at_step: str
    partial_progress: dict[str, Any] | None = None

    def dispatch(self, handler: OutcomeHandler[T]) -> T:
        return handler.on_cancelled(self)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "cancelled", **asdict(self)}


_REGISTRY: dict[str, type[Outcome]] = {
    "success": Success,
    "retryable_failure": RetryableFailure,
    "permanent_failure": PermanentFailure,
    "needs_human": NeedsHuman,
    "cancelled": Cancelled,
}


def outcome_from_dict(data: dict[str, Any]) -> Outcome:
    kind = data.pop("kind")
    cls = _REGISTRY.get(kind)
    if cls is None:
        raise ValueError(f"unknown outcome kind: {kind!r}")
    return cls(**data)


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
# ENGINE — implemented as an OutcomeHandler[Decision].
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


class EngineHandler:
    """A Decision-returning OutcomeHandler. Structurally satisfies the Protocol."""

    def on_success(self, o: Success) -> Decision:
        return Decision(
            next_state="advance",
            retry_policy=NO_RETRY,
            domain_signal=(o.domain_signals[0] if o.domain_signals else None),
        )

    def on_retryable_failure(self, o: RetryableFailure) -> Decision:
        if o.attempt >= HTTP_RETRY.max_attempts:
            return Decision(
                next_state="surrender",
                retry_policy=NO_RETRY,
                user_message=f"Gave up after {o.attempt} attempts: {o.message}",
                domain_signal="retry_exhausted",
            )
        return Decision(
            next_state="retry",
            retry_policy=HTTP_RETRY,
            user_message=f"Retrying ({o.attempt}/{HTTP_RETRY.max_attempts}): {o.error_kind}",
        )

    def on_permanent_failure(self, o: PermanentFailure) -> Decision:
        return Decision(next_state="surrender", retry_policy=NO_RETRY, user_message=o.message)

    def on_needs_human(self, o: NeedsHuman) -> Decision:
        return Decision(next_state="human_gate", retry_policy=NO_RETRY, user_message=o.prompt)

    def on_cancelled(self, o: Cancelled) -> Decision:
        # INV-CANCEL-SHORT-CIRCUITS-RETRY
        return Decision(
            next_state="cancelled",
            retry_policy=NO_RETRY,
            user_message=f"Cancelled ({o.cause}) at {o.at_step}",
        )


class UiRenderHandler:
    """Renders outcomes for the operator. Same Protocol shape, different return."""

    def on_success(self, o: Success) -> str:
        return f"OK — receipts: {o.inspected_artifacts}"

    def on_retryable_failure(self, o: RetryableFailure) -> str:
        return f"transient ({o.error_kind}, attempt {o.attempt}): {o.message}"

    def on_permanent_failure(self, o: PermanentFailure) -> str:
        hint = "\n  → check credentials, then `requiem reconcile`" if o.error_kind.startswith("auth.") else ""
        return f"FAILED ({o.error_kind}): {o.message}{hint}"

    def on_needs_human(self, o: NeedsHuman) -> str:
        return f"HUMAN GATE [{o.gate}]: {o.prompt}\n  choices: {o.choices}"

    def on_cancelled(self, o: Cancelled) -> str:
        return f"CANCELLED ({o.cause}) at {o.at_step}"


# ─────────────────────────────────────────────────────────────────────────────
# JSON ROUND-TRIP
# ─────────────────────────────────────────────────────────────────────────────


def write_event(outcome: Outcome) -> str:
    return json.dumps(outcome.to_dict(), separators=(",", ":"))


def read_event(line: str) -> Outcome:
    return outcome_from_dict(json.loads(line))


# ─────────────────────────────────────────────────────────────────────────────
# EXHAUSTIVENESS CHECK
# ─────────────────────────────────────────────────────────────────────────────
# Uncomment the new leaf below and add `on_weird_sixth` to the Protocol.
# Every existing Protocol implementer (EngineHandler, UiRenderHandler) will
# then fail mypy --strict — they don't satisfy the Protocol any more.
# That's the difference vs B: there the burden is on the dispatch *function*;
# here it's on every *consumer class*. Stronger fan-out guarantee.
#
# @dataclass(frozen=True, slots=True)
# class WeirdSixth(Outcome):
#     reason: str
#     def dispatch(self, handler): return handler.on_weird_sixth(self)  # Protocol must grow
#     def to_dict(self): return {"kind": "weird_sixth", **asdict(self)}


def main() -> int:
    engine = EngineHandler()
    ui = UiRenderHandler()
    verbs: list[tuple[str, Outcome]] = [
        ("happy", verb_happy()),
        ("transient", verb_transient(attempt=1)),
        ("transient-exhausted", verb_transient(attempt=3)),
        ("permanent", verb_permanent()),
        ("needs_human", verb_needs_human()),
        ("cancelled", verb_cancelled()),
    ]
    print("=" * 72)
    print("Variant C — ABC sealed hierarchy with visitor dispatch")
    print("=" * 72)
    for label, outcome in verbs:
        line = write_event(outcome)
        roundtripped = read_event(line)
        assert type(roundtripped) is type(outcome), "round-trip type mismatch"
        decision = roundtripped.dispatch(engine)
        print(f"\n[{label}] type={type(outcome).__name__}")
        print(f"  jsonl       : {line}")
        print(f"  decision    : next_state={decision.next_state!r} retry={decision.retry_policy.max_attempts}x")
        if decision.user_message:
            print(f"  ui          : {roundtripped.dispatch(ui)}")
        if decision.domain_signal:
            print(f"  signal      : {decision.domain_signal}")

    cancelled_decision = verb_cancelled().dispatch(engine)
    assert cancelled_decision.retry_policy is NO_RETRY
    assert cancelled_decision.next_state == "cancelled"
    print("\n[invariant] cancelled never schedules a retry. ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
