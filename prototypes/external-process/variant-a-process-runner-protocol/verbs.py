"""Verbs — the second layer that turns ProcessOutcomes into VerbOutcomes.

Per INV-DISCRIMINATED-OUTCOMES every verb returns one of:
  Success | RetryableFailure | PermanentFailure | NeedsHuman | Cancelled

Ravel's L-1: an ambiguous exit code is NEVER silently classified as transient.
If the verb cannot prove the failure is retryable, it routes to NeedsHuman.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from outcomes import NonZeroExit, NotFound, Success as ProcessSuccess, Timeout
from runner import ProcessRunner


class VerbSuccess(BaseModel):
    kind: Literal["success"] = "success"
    value: dict


class RetryableFailure(BaseModel):
    kind: Literal["retryable"] = "retryable"
    reason: str
    retry_key: str


class PermanentFailure(BaseModel):
    kind: Literal["permanent"] = "permanent"
    reason: str


class NeedsHuman(BaseModel):
    kind: Literal["needs_human"] = "needs_human"
    reason: str
    diagnostic: dict


class Cancelled(BaseModel):
    kind: Literal["cancelled"] = "cancelled"


VerbOutcome = Annotated[
    Union[VerbSuccess, RetryableFailure, PermanentFailure, NeedsHuman, Cancelled],
    Field(discriminator="kind"),
]


def git_current_sha(runner: ProcessRunner, repo: Path) -> VerbOutcome:
    """git rev-parse HEAD — stratify exit 128 ('not a repo') vs other failures."""
    out = runner.run(["git", "rev-parse", "HEAD"], cwd=repo, timeout_s=5.0)
    match out:
        case ProcessSuccess(stdout=stdout):
            return VerbSuccess(value={"sha": stdout.strip()})
        case NotFound(binary=binary):
            return PermanentFailure(reason=f"binary not on PATH: {binary}")
        case Timeout():
            return RetryableFailure(reason="git rev-parse timed out", retry_key="git-rev-parse-head")
        case NonZeroExit(exit_code=128, stderr=stderr):
            return PermanentFailure(reason=f"not a git repository at {repo}: {stderr.strip()}")
        case NonZeroExit(exit_code=129):
            return PermanentFailure(reason="git usage error (bug in verb)")
        case NonZeroExit(exit_code=ec, stderr=stderr):
            return NeedsHuman(
                reason=f"unrecognized git exit code {ec}",
                diagnostic={"stderr": stderr, "stdout": out.stdout},
            )


def gh_pr_view(runner: ProcessRunner, repo: Path, pr_number: int) -> VerbOutcome:
    """gh pr view — Ravel's exemplar: exit 1 is ambiguous, stderr must disambiguate.

    Cases observed in polyphony field reports:
      stderr contains 'no pull requests found' → PermanentFailure (no PR exists)
      stderr contains 'API rate limit exceeded' or 'rate limit' → RetryableFailure
      stderr contains 'HTTP 5' (5xx) → RetryableFailure
      stderr contains 'authentication' / 'token' → NeedsHuman (operator must reauth)
      anything else → NeedsHuman (don't auto-retry an unknown gh failure)
    """
    out = runner.run(
        ["gh", "pr", "view", str(pr_number), "--json", "number,state,title"],
        cwd=repo,
        timeout_s=15.0,
    )
    match out:
        case ProcessSuccess(stdout=stdout):
            return VerbSuccess(value={"raw_json": stdout})
        case NotFound(binary=binary):
            return PermanentFailure(reason=f"binary not on PATH: {binary}")
        case Timeout():
            return RetryableFailure(reason="gh pr view timed out", retry_key=f"gh-pr-view-{pr_number}")
        case NonZeroExit(exit_code=exit_code, stderr=stderr):
            s = stderr.lower()
            if "no pull requests found" in s or "could not resolve" in s:
                return PermanentFailure(reason=f"pr {pr_number} not found")
            if "rate limit" in s:
                return RetryableFailure(
                    reason="github api rate limit",
                    retry_key=f"gh-pr-view-{pr_number}",
                )
            if "http 5" in s or "server error" in s:
                return RetryableFailure(
                    reason="github transient server error",
                    retry_key=f"gh-pr-view-{pr_number}",
                )
            if "authentication" in s or "token" in s or "401" in s or "403" in s:
                return NeedsHuman(
                    reason="gh auth lapse — operator must reauthenticate",
                    diagnostic={"stderr": stderr, "exit_code": exit_code},
                )
            # Ravel L-1: unknown gh failure does NOT default to retryable.
            return NeedsHuman(
                reason=f"unrecognized gh failure (exit {exit_code}); refusing to guess",
                diagnostic={"stderr": stderr, "exit_code": exit_code},
            )


def git_resolve_ref(runner: ProcessRunner, repo: Path, ref: str) -> VerbOutcome:
    """git rev-parse <ref> — exit 128 + 'unknown revision' = PermanentFailure (no such ref)."""
    out = runner.run(["git", "rev-parse", "--verify", ref], cwd=repo, timeout_s=5.0)
    match out:
        case ProcessSuccess(stdout=stdout):
            return VerbSuccess(value={"sha": stdout.strip()})
        case NonZeroExit(exit_code=128, stderr=stderr):
            return PermanentFailure(reason=f"ref {ref!r} does not exist: {stderr.strip()}")
        case NonZeroExit(exit_code=ec, stderr=stderr):
            return NeedsHuman(
                reason=f"unrecognized git rev-parse exit {ec}",
                diagnostic={"stderr": stderr},
            )
        case NotFound(binary=binary):
            return PermanentFailure(reason=f"binary not on PATH: {binary}")
        case Timeout():
            return RetryableFailure(reason="git rev-parse timed out", retry_key=f"resolve-{ref}")


def long_running_probe(runner: ProcessRunner, timeout_s: float) -> VerbOutcome:
    """Verb that uses sys.executable to portably probe a sleep — cross-platform timeout demo."""
    import sys
    out = runner.run(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        timeout_s=timeout_s,
    )
    match out:
        case ProcessSuccess():
            return VerbSuccess(value={"note": "completed (unexpected)"})
        case Timeout(timeout_s=ts):
            return RetryableFailure(reason=f"probe exceeded {ts}s budget", retry_key="probe")
        case NotFound(binary=binary):
            return PermanentFailure(reason=f"python interpreter missing: {binary}")
        case NonZeroExit(exit_code=ec):
            return NeedsHuman(reason=f"probe exited {ec} unexpectedly", diagnostic={})


def missing_binary_verb(runner: ProcessRunner) -> VerbOutcome:
    out = runner.run(["definitely-not-a-real-tool", "--help"], timeout_s=2.0)
    match out:
        case NotFound(binary=binary):
            return PermanentFailure(reason=f"required tool missing from PATH: {binary}")
        case _:
            return NeedsHuman(reason="expected NotFound, got something else", diagnostic={"outcome": str(out)})
