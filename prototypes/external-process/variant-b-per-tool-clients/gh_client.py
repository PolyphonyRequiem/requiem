"""GhClient — owns the gh-specific stderr fingerprinting Ravel L-1 demands.

If gh changes its error wording in a future release, only this file changes —
no verb has to re-learn the dialect.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from _invoke import invoke
from outcomes import (
    GhPrViewOutcome,
    PrViewAuthLapse,
    PrViewFound,
    PrViewNotFound,
    PrViewRateLimited,
    PrViewServerError,
    PrViewTimeout,
    PrViewToolMissing,
    PrViewUnknown,
)


class GhClient(Protocol):
    def pr_view(self, repo: Path, pr_number: int, *, timeout_s: float = 15.0) -> GhPrViewOutcome: ...


class RealGhClient:
    def pr_view(self, repo: Path, pr_number: int, *, timeout_s: float = 15.0) -> GhPrViewOutcome:
        r = invoke(
            ["gh", "pr", "view", str(pr_number), "--json", "number,state,title"],
            cwd=repo,
            timeout_s=timeout_s,
        )
        if r.binary_missing:
            return PrViewToolMissing()
        if r.timed_out:
            return PrViewTimeout(timeout_s=timeout_s)
        if r.exit_code == 0:
            return PrViewFound(raw_json=r.stdout)

        s = r.stderr.lower()
        if "no pull requests found" in s or "could not resolve" in s:
            return PrViewNotFound(pr_number=pr_number)
        if "rate limit" in s:
            return PrViewRateLimited(stderr=r.stderr)
        if "http 5" in s or "server error" in s:
            return PrViewServerError(stderr=r.stderr)
        if "401" in s or "403" in s or "authentication" in s or "token" in s:
            return PrViewAuthLapse(stderr=r.stderr)
        # Ravel L-1: unknown gh failures do NOT degrade to "retryable" silently.
        return PrViewUnknown(exit_code=r.exit_code or -1, stderr=r.stderr)


# ---- Fake -----------------------------------------------------------

class FakeGhClient:
    def __init__(self) -> None:
        self._pr_view_outcomes: list[GhPrViewOutcome] = []
        self.pr_view_calls: list[tuple[Path, int]] = []

    def script_pr_view(self, outcome: GhPrViewOutcome) -> "FakeGhClient":
        self._pr_view_outcomes.append(outcome)
        return self

    def pr_view(self, repo: Path, pr_number: int, *, timeout_s: float = 15.0) -> GhPrViewOutcome:
        self.pr_view_calls.append((repo, pr_number))
        if not self._pr_view_outcomes:
            raise AssertionError(f"FakeGhClient.pr_view({repo!r}, {pr_number!r}) with no scripted outcome")
        return self._pr_view_outcomes.pop(0)
