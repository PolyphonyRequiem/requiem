"""
Variant A — Pydantic v2 discriminated union.

`kind: Literal[...]` is the discriminator. Pydantic's `Field(discriminator=...)`
gives us free JSON round-trip + narrowing on read. Engine reads `.kind` and
dispatches; exhaustiveness is enforced by `assert_never` after if/elif.

Run:
    python demo.py
    python -m mypy --strict demo.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Annotated, Any, Literal, NoReturn, Union, assert_never

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


# ─────────────────────────────────────────────────────────────────────────────
# THE CONTRACT — five variants of Outcome, discriminated on `kind`.
# ─────────────────────────────────────────────────────────────────────────────


class _OutcomeBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Success(_OutcomeBase):
    kind: Literal["success"] = "success"
    result: dict[str, Any] = Field(default_factory=dict)
    # INV-NO-CORRUPT-FORWARD: every state-mutating verb emits receipts.
    inspected_artifacts: list[str] = Field(default_factory=list)
    domain_signals: list[str] = Field(default_factory=list)


class RetryableFailure(_OutcomeBase):
    kind: Literal["retryable_failure"] = "retryable_failure"
    # INV-RESTART: retry_key is the idempotency key, rendered at first attempt.
    retry_key: str
    error_kind: str  # e.g. "network.timeout", "git.lock_contended"
    message: str
    attempt: int = 1
    cause: dict[str, Any] | None = None


class PermanentFailure(_OutcomeBase):
    kind: Literal["permanent_failure"] = "permanent_failure"
    error_kind: str  # e.g. "auth.invalid_token", "config.missing_field"
    message: str  # human-readable — UI renders this directly
    details: dict[str, Any] = Field(default_factory=dict)


class NeedsHuman(_OutcomeBase):
    kind: Literal["needs_human"] = "needs_human"
    gate: str  # e.g. "evidence_review", "receipts_violation"
    prompt: str
    choices: list[str]
    context: dict[str, Any] = Field(default_factory=dict)


class Cancelled(_OutcomeBase):
    kind: Literal["cancelled"] = "cancelled"
    # INV-CANCEL-SHORT-CIRCUITS-RETRY: cancel-cause influences NO retry decision,
    # but the engine records it for the operator.
    cause: Literal["operator", "deadline", "superseded", "parent_cancelled"]
    at_step: str
    partial_progress: dict[str, Any] | None = None


Outcome = Annotated[
    Union[Success, RetryableFailure, PermanentFailure, NeedsHuman, Cancelled],
    Field(discriminator="kind"),
]

# The TypeAdapter is the JSON gateway: write-then-read narrows back to the
# correct variant without manual dispatch.
OUTCOME_ADAPTER: TypeAdapter[Outcome] = TypeAdapter(Outcome)


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
# ENGINE — reads outcome, picks retry policy / next state.
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    backoff_seconds: float


HTTP_RETRY = RetryPolicy(max_attempts=3, backoff_seconds=1.0)  # hard ceiling per north-star §4
NO_RETRY = RetryPolicy(max_attempts=1, backoff_seconds=0.0)


@dataclass(frozen=True)
class Decision:
    next_state: str
    retry_policy: RetryPolicy
    user_message: str | None = None
    domain_signal: str | None = None


def engine_decide(outcome: Outcome) -> Decision:
    """The single dispatch site. `assert_never` enforces exhaustiveness.

    Surprise from prototyping: mypy --strict does NOT narrow `outcome`
    on `if outcome.kind == "success"` even though pydantic's discriminator
    machinery does the equivalent at runtime. We use `isinstance` to get
    narrowing; the `kind` field is still the wire-level tag and the JSON
    discriminator. See README "Open question 1".
    """
    if isinstance(outcome, Success):
        return Decision(
            next_state="advance",
            retry_policy=NO_RETRY,
            domain_signal=(outcome.domain_signals[0] if outcome.domain_signals else None),
        )
    if isinstance(outcome, RetryableFailure):
        if outcome.attempt >= HTTP_RETRY.max_attempts:
            return Decision(
                next_state="surrender",
                retry_policy=NO_RETRY,
                user_message=f"Gave up after {outcome.attempt} attempts: {outcome.message}",
                domain_signal="retry_exhausted",
            )
        return Decision(
            next_state="retry",
            retry_policy=HTTP_RETRY,
            user_message=f"Retrying ({outcome.attempt}/{HTTP_RETRY.max_attempts}): {outcome.error_kind}",
        )
    if isinstance(outcome, PermanentFailure):
        return Decision(
            next_state="surrender",
            retry_policy=NO_RETRY,
            user_message=outcome.message,
        )
    if isinstance(outcome, NeedsHuman):
        return Decision(
            next_state="human_gate",
            retry_policy=NO_RETRY,
            user_message=outcome.prompt,
        )
    if isinstance(outcome, Cancelled):
        # INV-CANCEL-SHORT-CIRCUITS-RETRY: no retry, no surrender — clean stop.
        return Decision(
            next_state="cancelled",
            retry_policy=NO_RETRY,
            user_message=f"Cancelled ({outcome.cause}) at {outcome.at_step}",
        )
    assert_never(outcome)


# ─────────────────────────────────────────────────────────────────────────────
# JSON ROUND-TRIP — emulates writing to / reading from run.events.jsonl.
# ─────────────────────────────────────────────────────────────────────────────


def write_event(outcome: Outcome) -> str:
    """One line per outcome, JSONL-style."""
    return OUTCOME_ADAPTER.dump_json(outcome).decode("utf-8")


def read_event(line: str) -> Outcome:
    return OUTCOME_ADAPTER.validate_json(line)


# ─────────────────────────────────────────────────────────────────────────────
# UI-FACING RENDER — PermanentFailure → operator string.
# ─────────────────────────────────────────────────────────────────────────────


def render_for_ui(outcome: Outcome) -> str:
    if isinstance(outcome, PermanentFailure):
        hint = ""
        if outcome.error_kind.startswith("auth."):
            hint = "\n  → check credentials, then `requiem reconcile`"
        return f"FAILED ({outcome.error_kind}): {outcome.message}{hint}"
    if isinstance(outcome, RetryableFailure):
        return f"transient ({outcome.error_kind}, attempt {outcome.attempt}): {outcome.message}"
    if isinstance(outcome, NeedsHuman):
        return f"HUMAN GATE [{outcome.gate}]: {outcome.prompt}\n  choices: {outcome.choices}"
    if isinstance(outcome, Cancelled):
        return f"CANCELLED ({outcome.cause}) at {outcome.at_step}"
    if isinstance(outcome, Success):
        return f"OK — receipts: {outcome.inspected_artifacts}"
    assert_never(outcome)


# ─────────────────────────────────────────────────────────────────────────────
# EXHAUSTIVENESS CHECK
# ─────────────────────────────────────────────────────────────────────────────
# If a 6th outcome kind is added to `Outcome`, mypy --strict will reject
# both `engine_decide` and `render_for_ui` at the `assert_never` line.
# The block below demonstrates the failure mode (commented out by default).
#
# class WeirdSixth(_OutcomeBase):
#     kind: Literal["weird_sixth"] = "weird_sixth"
# Outcome = Annotated[Union[Success, RetryableFailure, PermanentFailure,
#                           NeedsHuman, Cancelled, WeirdSixth],
#                     Field(discriminator="kind")]
# # mypy now errors: `assert_never` argument is `Literal["weird_sixth"]`,
# # not the expected `Never`.


def main() -> int:
    verbs = [
        ("happy", verb_happy()),
        ("transient", verb_transient(attempt=1)),
        ("transient-exhausted", verb_transient(attempt=3)),
        ("permanent", verb_permanent()),
        ("needs_human", verb_needs_human()),
        ("cancelled", verb_cancelled()),
    ]
    print("=" * 72)
    print("Variant A — Pydantic v2 discriminated union")
    print("=" * 72)
    for label, outcome in verbs:
        line = write_event(outcome)
        roundtripped = read_event(line)
        assert type(roundtripped) is type(outcome), "round-trip type mismatch"
        decision = engine_decide(roundtripped)
        print(f"\n[{label}] outcome.kind={outcome.kind}")
        print(f"  jsonl       : {line}")
        print(f"  decision    : next_state={decision.next_state!r} retry={decision.retry_policy.max_attempts}x")
        if decision.user_message:
            print(f"  ui          : {render_for_ui(roundtripped)}")
        if decision.domain_signal:
            print(f"  signal      : {decision.domain_signal}")

    # INV-CANCEL-SHORT-CIRCUITS-RETRY assertion:
    cancelled_decision = engine_decide(verb_cancelled())
    assert cancelled_decision.retry_policy is NO_RETRY
    assert cancelled_decision.next_state == "cancelled"
    print("\n[invariant] cancelled never schedules a retry. ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
