"""Variant B tests — fakes are per-tool, not per-subprocess.

Compare with Variant A: the test never thinks about exit codes, only about
the tool-method's typed outcome. This is the ergonomic win of Variant B.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from git_client import FakeGitClient
from gh_client import FakeGhClient
from outcomes import (
    NeedsHuman,
    PermanentFailure,
    PrViewAuthLapse,
    PrViewNotFound,
    PrViewRateLimited,
    PrViewServerError,
    PrViewTimeout,
    PrViewUnknown,
    RetryableFailure,
    RevParseNotARepo,
    RevParseResolved,
    RevParseUnknown,
    RevParseUnknownRef,
    VerbSuccess,
)
from verbs import check_pr, resolve_ref


class ResolveRefTests(unittest.TestCase):
    def test_success(self) -> None:
        git = FakeGitClient().script_rev_parse(RevParseResolved(sha="abc123"))
        out = resolve_ref(git, Path("."), "HEAD")
        assert isinstance(out, VerbSuccess)
        self.assertEqual(out.value["sha"], "abc123")

    def test_unknown_ref_is_permanent(self) -> None:
        git = FakeGitClient().script_rev_parse(
            RevParseUnknownRef(ref="bogus", stderr="fatal: Needed a single revision"),
        )
        out = resolve_ref(git, Path("."), "bogus")
        assert isinstance(out, PermanentFailure)

    def test_not_a_repo_is_permanent(self) -> None:
        git = FakeGitClient().script_rev_parse(RevParseNotARepo(cwd="C:/nope"))
        out = resolve_ref(git, Path("C:/nope"), "HEAD")
        assert isinstance(out, PermanentFailure)

    def test_unknown_exit_routes_to_human(self) -> None:
        git = FakeGitClient().script_rev_parse(RevParseUnknown(exit_code=42, stderr="???"))
        out = resolve_ref(git, Path("."), "HEAD")
        assert isinstance(out, NeedsHuman)


class CheckPrTests(unittest.TestCase):
    def test_rate_limit_is_retryable(self) -> None:
        gh = FakeGhClient().script_pr_view(PrViewRateLimited(stderr="HTTP 403: rate limit"))
        out = check_pr(gh, Path("."), 42)
        assert isinstance(out, RetryableFailure)
        self.assertEqual(out.retry_key, "pr-view-42")

    def test_server_error_is_retryable(self) -> None:
        gh = FakeGhClient().script_pr_view(PrViewServerError(stderr="HTTP 502"))
        out = check_pr(gh, Path("."), 42)
        assert isinstance(out, RetryableFailure)

    def test_auth_lapse_routes_to_human(self) -> None:
        gh = FakeGhClient().script_pr_view(PrViewAuthLapse(stderr="HTTP 401"))
        out = check_pr(gh, Path("."), 42)
        assert isinstance(out, NeedsHuman)

    def test_not_found_is_permanent(self) -> None:
        gh = FakeGhClient().script_pr_view(PrViewNotFound(pr_number=42))
        out = check_pr(gh, Path("."), 42)
        assert isinstance(out, PermanentFailure)

    def test_unknown_routes_to_human(self) -> None:
        # Ravel L-1: don't auto-classify unknown gh exit-1 as retryable.
        gh = FakeGhClient().script_pr_view(PrViewUnknown(exit_code=1, stderr="weird"))
        out = check_pr(gh, Path("."), 42)
        assert isinstance(out, NeedsHuman)

    def test_timeout_is_retryable(self) -> None:
        gh = FakeGhClient().script_pr_view(PrViewTimeout(timeout_s=15.0))
        out = check_pr(gh, Path("."), 42)
        assert isinstance(out, RetryableFailure)


if __name__ == "__main__":
    unittest.main()
