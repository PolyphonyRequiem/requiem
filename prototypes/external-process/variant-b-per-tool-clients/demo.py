"""Variant B demo — same 7 demo requirements, expressed through typed clients."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from _invoke import invoke
from git_client import RealGitClient
from gh_client import RealGhClient
from verbs import check_pr, resolve_ref


def banner(n: int, label: str) -> None:
    print(f"\n=== Demo {n}: {label} ===")


def main() -> None:
    git = RealGitClient()
    gh = RealGhClient()
    repo = Path(__file__).resolve().parents[3]

    banner(1, "git rev-parse HEAD on this repo")
    print(resolve_ref(git, repo, "HEAD"))

    banner(2, "git rev-parse on a non-existent ref (stratified by client to RevParseUnknownRef)")
    print(resolve_ref(git, repo, "definitely-not-a-real-ref-xyzzy"))

    banner(3, "missing binary — invoke the helper directly to show the typed signal")
    r = invoke(["definitely-not-a-real-tool", "--help"], timeout_s=2.0)
    print(f"binary_missing={r.binary_missing}")

    banner(4, "timeout — sleep > timeout (cross-platform via sys.executable)")
    started = time.monotonic()
    r = invoke([sys.executable, "-c", "import time; time.sleep(60)"], timeout_s=0.5)
    print(f"timed_out={r.timed_out} elapsed={time.monotonic() - started:.2f}s")

    banner(5, "gh pr view on PR #1 (typed via GhClient)")
    print(check_pr(gh, repo, 1))

    print("\n(See test_verbs.py for fake-substitution and stratification tests.)")


if __name__ == "__main__":
    main()
