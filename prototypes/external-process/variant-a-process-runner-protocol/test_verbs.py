"""Tests using FakeProcessRunner — show how a verb test reads.

The whole point of this seam: tests never spawn a real subprocess. They script
outcomes at the ProcessRunner boundary and assert the verb classifies correctly.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from outcomes import NonZeroExit, NotFound, Success, Timeout
from runner import FakeProcessRunner
from verbs import (
    NeedsHuman,
    PermanentFailure,
    RetryableFailure,
    VerbSuccess,
    gh_pr_view,
    git_current_sha,
)


class GitVerbTests(unittest.TestCase):
    def test_success_returns_sha(self) -> None:
        runner = FakeProcessRunner().when(
            ["git", "rev-parse", "HEAD"],
            Success(stdout="abc123\n", stderr="", duration_s=0.01),
        )
        out = git_current_sha(runner, Path("."))
        assert isinstance(out, VerbSuccess)
        self.assertEqual(out.value, {"sha": "abc123"})

    def test_exit_128_means_not_a_repo(self) -> None:
        runner = FakeProcessRunner().when(
            ["git", "rev-parse"],
            NonZeroExit(exit_code=128, stdout="", stderr="fatal: not a git repository", duration_s=0.01),
        )
        out = git_current_sha(runner, Path("."))
        assert isinstance(out, PermanentFailure)
        self.assertIn("not a git repository", out.reason)

    def test_unknown_exit_code_routes_to_human(self) -> None:
        # Ravel L-1: do not silently auto-retry an unknown failure mode.
        runner = FakeProcessRunner().when(
            ["git"],
            NonZeroExit(exit_code=42, stdout="", stderr="???", duration_s=0.01),
        )
        out = git_current_sha(runner, Path("."))
        assert isinstance(out, NeedsHuman)


class GhVerbTests(unittest.TestCase):
    def test_rate_limit_is_retryable(self) -> None:
        runner = FakeProcessRunner().when(
            ["gh"],
            NonZeroExit(
                exit_code=1,
                stdout="",
                stderr="HTTP 403: API rate limit exceeded for user X",
                duration_s=0.02,
            ),
        )
        out = gh_pr_view(runner, Path("."), 42)
        assert isinstance(out, RetryableFailure)
        self.assertEqual(out.retry_key, "gh-pr-view-42")

    def test_missing_pr_is_permanent(self) -> None:
        runner = FakeProcessRunner().when(
            ["gh"],
            NonZeroExit(exit_code=1, stdout="", stderr="no pull requests found", duration_s=0.02),
        )
        out = gh_pr_view(runner, Path("."), 42)
        assert isinstance(out, PermanentFailure)

    def test_auth_lapse_routes_to_human(self) -> None:
        runner = FakeProcessRunner().when(
            ["gh"],
            NonZeroExit(exit_code=1, stdout="", stderr="HTTP 401: authentication required", duration_s=0.02),
        )
        out = gh_pr_view(runner, Path("."), 42)
        assert isinstance(out, NeedsHuman)

    def test_unknown_failure_routes_to_human(self) -> None:
        # The bug Liszt-2 originally shipped: classifying this as RetryableFailure.
        # Ravel's L-1 correction: refuse to guess; route to a human.
        runner = FakeProcessRunner().when(
            ["gh"],
            NonZeroExit(exit_code=1, stdout="", stderr="cosmic ray flipped a bit", duration_s=0.02),
        )
        out = gh_pr_view(runner, Path("."), 42)
        assert isinstance(out, NeedsHuman)

    def test_timeout_is_retryable(self) -> None:
        runner = FakeProcessRunner().when(
            ["gh"],
            Timeout(timeout_s=15.0, partial_stdout="", partial_stderr=""),
        )
        out = gh_pr_view(runner, Path("."), 42)
        assert isinstance(out, RetryableFailure)

    def test_missing_binary_is_permanent(self) -> None:
        runner = FakeProcessRunner().when(["gh"], NotFound(binary="gh"))
        out = gh_pr_view(runner, Path("."), 42)
        assert isinstance(out, PermanentFailure)


if __name__ == "__main__":
    unittest.main()
