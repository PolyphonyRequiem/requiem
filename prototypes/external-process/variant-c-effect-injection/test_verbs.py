"""Variant C tests — fakes injected via Runtime.with_(...)."""

from __future__ import annotations

import unittest
from pathlib import Path

from effects import (
    GhAuthLapse,
    GhFailure,
    GhPrMissing,
    GhTransient,
    GitFailure,
    GitMissingRef,
    GitNotARepo,
    GitSha,
    NeedsHuman,
    PermanentFailure,
    RetryableFailure,
    VerbSuccess,
)
from fake_effects import FakeGh, FakeGit, FrozenClock
from runtime import Runtime
from verbs import check_pr, resolve_ref


def fixture(*, git=None, gh=None, clock=None) -> Runtime:
    return Runtime(
        git=git or FakeGit(),
        gh=gh or FakeGh(),
        clock=clock or FrozenClock(),
    )


class ResolveRefTests(unittest.TestCase):
    def test_success(self) -> None:
        r = fixture(git=FakeGit(GitSha(sha="abc123")))
        out = r.dispatch(resolve_ref, repo=Path("."), ref="HEAD")
        assert isinstance(out, VerbSuccess)
        self.assertEqual(out.value["sha"], "abc123")

    def test_missing_ref_is_permanent(self) -> None:
        r = fixture(git=FakeGit(GitMissingRef(ref="bogus")))
        out = r.dispatch(resolve_ref, repo=Path("."), ref="bogus")
        assert isinstance(out, PermanentFailure)

    def test_not_a_repo_is_permanent(self) -> None:
        r = fixture(git=FakeGit(GitNotARepo()))
        out = r.dispatch(resolve_ref, repo=Path("C:/nope"), ref="HEAD")
        assert isinstance(out, PermanentFailure)

    def test_unknown_failure_routes_to_human(self) -> None:
        r = fixture(git=FakeGit(GitFailure(detail="cosmic ray")))
        out = r.dispatch(resolve_ref, repo=Path("."), ref="HEAD")
        assert isinstance(out, NeedsHuman)

    def test_timeout_is_retryable(self) -> None:
        r = fixture(git=FakeGit(GitFailure(detail="git timed out", is_timeout=True)))
        out = r.dispatch(resolve_ref, repo=Path("."), ref="HEAD")
        assert isinstance(out, RetryableFailure)


class CheckPrTests(unittest.TestCase):
    def test_rate_limit_is_retryable(self) -> None:
        r = fixture(gh=FakeGh(GhTransient(reason="rate_limit")))
        out = r.dispatch(check_pr, repo=Path("."), pr_number=42)
        assert isinstance(out, RetryableFailure)
        self.assertEqual(out.retry_key, "pr-view-42")

    def test_auth_lapse_routes_to_human(self) -> None:
        r = fixture(gh=FakeGh(GhAuthLapse(stderr="HTTP 401")))
        out = r.dispatch(check_pr, repo=Path("."), pr_number=42)
        assert isinstance(out, NeedsHuman)

    def test_not_found_is_permanent(self) -> None:
        r = fixture(gh=FakeGh(GhPrMissing(pr_number=42)))
        out = r.dispatch(check_pr, repo=Path("."), pr_number=42)
        assert isinstance(out, PermanentFailure)

    def test_unknown_failure_routes_to_human(self) -> None:
        # Ravel L-1 again, in effect-injection form.
        r = fixture(gh=FakeGh(GhFailure(detail="weird gh exit")))
        out = r.dispatch(check_pr, repo=Path("."), pr_number=42)
        assert isinstance(out, NeedsHuman)

    def test_dispatch_complains_on_missing_capability(self) -> None:
        # If a verb declares an effect that the runtime doesn't have, it fails fast.
        bare_runtime = Runtime()  # no effects at all
        with self.assertRaises(TypeError) as cm:
            bare_runtime.dispatch(check_pr, repo=Path("."), pr_number=42)
        self.assertIn("no value for parameter", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
