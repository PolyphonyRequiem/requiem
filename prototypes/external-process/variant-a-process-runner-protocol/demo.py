"""Variant A demo — runs the 7 demo requirements end-to-end."""

from __future__ import annotations

from pathlib import Path

from runner import RealProcessRunner
from verbs import (
    git_current_sha,
    git_resolve_ref,
    gh_pr_view,
    long_running_probe,
    missing_binary_verb,
)


def banner(n: int, label: str) -> None:
    print(f"\n=== Demo {n}: {label} ===")


def main() -> None:
    runner = RealProcessRunner()
    repo = Path(__file__).resolve().parents[3]

    banner(1, "git rev-parse HEAD on this repo")
    print(git_current_sha(runner, repo))

    banner(2, "git rev-parse on a non-existent ref (exit 128, stratified as PermanentFailure)")
    print(git_resolve_ref(runner, repo, "definitely-not-a-real-ref-xyzzy"))

    banner(3, "missing binary → typed NotFound → PermanentFailure")
    print(missing_binary_verb(runner))

    banner(4, "timeout → typed Timeout → RetryableFailure")
    print(long_running_probe(runner, timeout_s=0.5))

    banner(5, "gh pr view on PR #1 of this repo (exact outcome depends on auth/PR state)")
    print(gh_pr_view(runner, repo, 1))

    print("\n(See test_verbs.py for fake-substitution and stratification tests.)")


if __name__ == "__main__":
    main()
