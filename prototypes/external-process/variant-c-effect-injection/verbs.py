"""Verbs as plain functions declaring their capabilities via parameters.

Compare with Variant B: same domain logic, same outcome union. The difference
is the *call site* — the runtime fills in `git` and `gh` automatically.
"""

from __future__ import annotations

from pathlib import Path

from effects import (
    Clock,
    Gh,
    GhAuthLapse,
    GhFailure,
    GhPrFound,
    GhPrMissing,
    GhTransient,
    Git,
    GitFailure,
    GitMissingRef,
    GitNotARepo,
    GitSha,
    NeedsHuman,
    PermanentFailure,
    RetryableFailure,
    VerbOutcome,
    VerbSuccess,
)


def resolve_ref(git: Git, repo: Path, ref: str) -> VerbOutcome:
    match git.rev_parse(repo, ref):
        case GitSha(sha=sha):
            return VerbSuccess(value={"sha": sha})
        case GitMissingRef(ref=r):
            return PermanentFailure(reason=f"ref {r!r} does not exist")
        case GitNotARepo():
            return PermanentFailure(reason=f"not a git repository at {repo}")
        case GitFailure(detail=detail, is_missing_tool=True):
            return PermanentFailure(reason=f"git binary unavailable: {detail}")
        case GitFailure(detail=detail, is_timeout=True):
            return RetryableFailure(reason=detail, retry_key=f"rev-parse-{ref}")
        case GitFailure(detail=detail):
            return NeedsHuman(reason="unrecognized git failure", diagnostic={"detail": detail})


def check_pr(gh: Gh, clock: Clock, repo: Path, pr_number: int) -> VerbOutcome:
    # `clock` shows up in the signature → runtime supplies it.
    # We use it to bake a deterministic retry_key suffix (useful for journal correlation).
    ts = int(clock.now_unix())
    match gh.pr_view(repo, pr_number):
        case GhPrFound(raw_json=raw):
            return VerbSuccess(value={"raw_json": raw, "checked_at": ts})
        case GhPrMissing(pr_number=n):
            return PermanentFailure(reason=f"pr {n} not found")
        case GhTransient(reason=reason):
            return RetryableFailure(reason=f"gh transient: {reason}", retry_key=f"pr-view-{pr_number}")
        case GhAuthLapse(stderr=stderr):
            return NeedsHuman(reason="gh auth lapse", diagnostic={"stderr": stderr})
        case GhFailure(detail=detail, is_missing_tool=True):
            return PermanentFailure(reason=f"gh binary unavailable: {detail}")
        case GhFailure(detail=detail):
            return NeedsHuman(reason="unrecognized gh failure", diagnostic={"detail": detail})
