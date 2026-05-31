"""Fake effect implementations — for tests and the harness.

Note how a test reads: pass scripted fakes into runtime.dispatch(); no
subprocess, no method-level mocking, no patch.object().
"""

from __future__ import annotations

from pathlib import Path

from effects import GhPrViewOutcome, GitRevParseOutcome


class FakeGit:
    def __init__(self, *outcomes: GitRevParseOutcome) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[tuple[Path, str]] = []

    def rev_parse(self, repo: Path, ref: str, *, timeout_s: float = 5.0) -> GitRevParseOutcome:
        self.calls.append((repo, ref))
        if not self._outcomes:
            raise AssertionError(f"FakeGit.rev_parse({repo!r}, {ref!r}) with no scripted outcome")
        return self._outcomes.pop(0)


class FakeGh:
    def __init__(self, *outcomes: GhPrViewOutcome) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[tuple[Path, int]] = []

    def pr_view(self, repo: Path, pr_number: int, *, timeout_s: float = 15.0) -> GhPrViewOutcome:
        self.calls.append((repo, pr_number))
        if not self._outcomes:
            raise AssertionError(f"FakeGh.pr_view({repo!r}, {pr_number!r}) with no scripted outcome")
        return self._outcomes.pop(0)


class FrozenClock:
    def __init__(self, now: float = 1717000000.0) -> None:
        self._now = now

    def now_unix(self) -> float:
        return self._now
