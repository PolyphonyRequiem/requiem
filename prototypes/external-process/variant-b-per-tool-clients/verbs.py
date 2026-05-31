"""Verbs that consume typed per-tool clients.

Notice: no exit codes, no stderr substring checks. The verb is pure
domain logic over tool-method outcomes.
"""

from __future__ import annotations

from pathlib import Path

from git_client import GitClient
from gh_client import GhClient
from outcomes import (
    NeedsHuman,
    PermanentFailure,
    PrViewAuthLapse,
    PrViewFound,
    PrViewNotFound,
    PrViewRateLimited,
    PrViewServerError,
    PrViewTimeout,
    PrViewToolMissing,
    PrViewUnknown,
    RetryableFailure,
    RevParseNotARepo,
    RevParseResolved,
    RevParseTimeout,
    RevParseToolMissing,
    RevParseUnknown,
    RevParseUnknownRef,
    VerbOutcome,
    VerbSuccess,
)


def resolve_ref(git: GitClient, repo: Path, ref: str) -> VerbOutcome:
    match git.rev_parse(repo, ref):
        case RevParseResolved(sha=sha):
            return VerbSuccess(value={"sha": sha})
        case RevParseUnknownRef(ref=r):
            return PermanentFailure(reason=f"ref {r!r} does not exist")
        case RevParseNotARepo(cwd=cwd):
            return PermanentFailure(reason=f"not a git repository at {cwd}")
        case RevParseToolMissing():
            return PermanentFailure(reason="git not on PATH")
        case RevParseTimeout():
            return RetryableFailure(reason="git timed out", retry_key=f"rev-parse-{ref}")
        case RevParseUnknown(exit_code=ec, stderr=stderr):
            return NeedsHuman(
                reason=f"unrecognized git exit {ec}",
                diagnostic={"stderr": stderr},
            )


def check_pr(gh: GhClient, repo: Path, pr_number: int) -> VerbOutcome:
    match gh.pr_view(repo, pr_number):
        case PrViewFound(raw_json=raw):
            return VerbSuccess(value={"raw_json": raw})
        case PrViewNotFound(pr_number=n):
            return PermanentFailure(reason=f"pr {n} not found")
        case PrViewRateLimited():
            return RetryableFailure(reason="github rate limit", retry_key=f"pr-view-{pr_number}")
        case PrViewServerError():
            return RetryableFailure(reason="github 5xx", retry_key=f"pr-view-{pr_number}")
        case PrViewAuthLapse(stderr=stderr):
            return NeedsHuman(reason="gh auth lapse", diagnostic={"stderr": stderr})
        case PrViewToolMissing():
            return PermanentFailure(reason="gh not on PATH")
        case PrViewTimeout():
            return RetryableFailure(reason="gh timed out", retry_key=f"pr-view-{pr_number}")
        case PrViewUnknown(exit_code=ec, stderr=stderr):
            return NeedsHuman(
                reason=f"unrecognized gh failure (exit {ec})",
                diagnostic={"stderr": stderr},
            )
