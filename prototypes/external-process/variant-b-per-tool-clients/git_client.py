"""GitClient — domain-typed methods. Classification lives here, not in the verb.

The verb that uses GitClient does NOT touch exit codes. It pattern-matches on
GitRevParseOutcome variants only.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from _invoke import invoke
from outcomes import (
    GitRevParseOutcome,
    RevParseNotARepo,
    RevParseResolved,
    RevParseTimeout,
    RevParseToolMissing,
    RevParseUnknown,
    RevParseUnknownRef,
)


class GitClient(Protocol):
    def rev_parse(self, repo: Path, ref: str, *, timeout_s: float = 5.0) -> GitRevParseOutcome: ...


class RealGitClient:
    def rev_parse(self, repo: Path, ref: str, *, timeout_s: float = 5.0) -> GitRevParseOutcome:
        r = invoke(["git", "rev-parse", "--verify", ref], cwd=repo, timeout_s=timeout_s)
        if r.binary_missing:
            return RevParseToolMissing()
        if r.timed_out:
            return RevParseTimeout(timeout_s=timeout_s)
        if r.exit_code == 0:
            return RevParseResolved(sha=r.stdout.strip())
        if r.exit_code == 128:
            s = r.stderr.lower()
            if "not a git repository" in s:
                return RevParseNotARepo(cwd=str(repo))
            # "unknown revision", "ambiguous argument", "needed a single revision" all share exit 128
            return RevParseUnknownRef(ref=ref, stderr=r.stderr)
        return RevParseUnknown(exit_code=r.exit_code or -1, stderr=r.stderr)


# ---- Fake -----------------------------------------------------------

class FakeGitClient:
    """Per-method scripted fake. Records every call for assertion."""

    def __init__(self) -> None:
        self._rev_parse_outcomes: list[GitRevParseOutcome] = []
        self.rev_parse_calls: list[tuple[Path, str]] = []

    def script_rev_parse(self, outcome: GitRevParseOutcome) -> "FakeGitClient":
        self._rev_parse_outcomes.append(outcome)
        return self

    def rev_parse(self, repo: Path, ref: str, *, timeout_s: float = 5.0) -> GitRevParseOutcome:
        self.rev_parse_calls.append((repo, ref))
        if not self._rev_parse_outcomes:
            raise AssertionError(f"FakeGitClient.rev_parse({repo!r}, {ref!r}) with no scripted outcome")
        return self._rev_parse_outcomes.pop(0)
