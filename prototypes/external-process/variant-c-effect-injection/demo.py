"""Variant C demo — same 7 demo requirements, expressed through effect injection."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from effects import GhFailure, GhTransient
from real_effects import RealClock, RealGh, RealGit, _invoke
from runtime import Runtime
from verbs import check_pr, resolve_ref


def banner(n: int, label: str) -> None:
    print(f"\n=== Demo {n}: {label} ===")


def main() -> None:
    runtime = Runtime(git=RealGit(), gh=RealGh(), clock=RealClock())
    repo = Path(__file__).resolve().parents[3]

    banner(1, "git rev-parse HEAD on this repo")
    print(runtime.dispatch(resolve_ref, repo=repo, ref="HEAD"))

    banner(2, "git rev-parse on a non-existent ref")
    print(runtime.dispatch(resolve_ref, repo=repo, ref="definitely-not-a-real-ref-xyzzy"))

    banner(3, "missing binary — invoke helper directly to show signal")
    missing, *_ = _invoke(["definitely-not-a-real-tool", "--help"], timeout_s=2.0)
    print(f"binary_missing={missing}")

    banner(4, "timeout — sleep > timeout")
    started = time.monotonic()
    _, timed_out, *_ = _invoke([sys.executable, "-c", "import time; time.sleep(60)"], timeout_s=0.5)
    print(f"timed_out={timed_out} elapsed={time.monotonic() - started:.2f}s")

    banner(5, "gh pr view on PR #1 (dispatched through runtime)")
    print(runtime.dispatch(check_pr, repo=repo, pr_number=1))

    print("\n(See test_verbs.py for fake-substitution and stratification tests.)")


if __name__ == "__main__":
    main()
