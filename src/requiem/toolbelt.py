"""External-process boundary — Liszt B+C hybrid.

Per-tool typed clients live in their own modules; verbs receive a
frozen `Toolbelt` that bundles them. Verbs never know exit codes or
argv — they pattern-match on the typed outcomes the clients return.

The walking-skeleton ships two clients: `git` (real `git show`) and
`files` (pure-Python). Both have fake counterparts so the harness can
drive the engine fully in-process.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from requiem.clients.fs import FilesystemClient
from requiem.clients.gh import GhClient
from requiem.clients.twig import TwigClient


# ---- files -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FileRead:
    path: Path
    content: str


@dataclass(frozen=True, slots=True)
class FileMissing:
    path: Path


FileOutcome = FileRead | FileMissing


class FileClient(Protocol):
    def read_text(self, path: Path) -> FileOutcome: ...


class RealFileClient:
    def read_text(self, path: Path) -> FileOutcome:
        if not path.exists():
            return FileMissing(path=path)
        return FileRead(path=path, content=path.read_text(encoding="utf-8"))


class FakeFileClient:
    def __init__(self, by_path: dict[Path, str]) -> None:
        self._by_path = {Path(p): v for p, v in by_path.items()}

    def read_text(self, path: Path) -> FileOutcome:
        if path not in self._by_path:
            return FileMissing(path=path)
        return FileRead(path=path, content=self._by_path[path])


# ---- git -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GitShowOk:
    ref: str
    path: Path
    content: str


@dataclass(frozen=True, slots=True)
class GitShowMissing:
    ref: str
    path: Path
    stderr: str


@dataclass(frozen=True, slots=True)
class GitNotARepo:
    cwd: Path


GitShowOutcome = GitShowOk | GitShowMissing | GitNotARepo


class GitClient(Protocol):
    def show(
        self, repo: Path, ref: str, path: Path, *, timeout_s: float = 5.0
    ) -> GitShowOutcome: ...


class RealGitClient:
    def show(
        self, repo: Path, ref: str, path: Path, *, timeout_s: float = 5.0
    ) -> GitShowOutcome:
        try:
            r = subprocess.run(
                ["git", "show", f"{ref}:{path.as_posix()}"],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except FileNotFoundError:
            return GitShowMissing(ref=ref, path=path, stderr="git binary not found")
        if r.returncode == 0:
            return GitShowOk(ref=ref, path=path, content=r.stdout)
        s = r.stderr.lower()
        if "not a git repository" in s:
            return GitNotARepo(cwd=repo)
        return GitShowMissing(ref=ref, path=path, stderr=r.stderr)


# ---- toolbelt --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Toolbelt:
    """Frozen value-object: every verb takes the same shape; no positional
    threading, no globals, no monkey-patching."""

    git: GitClient
    files: FileClient
    # The Phase B clients (`gh`, `twig`, `fs`) are optional so workflows
    # and tests that don't touch them can construct a smaller Toolbelt.
    # Verbs that need a specific client should fail loud (KeyError /
    # AttributeError) rather than silently no-op.
    gh: GhClient | None = None
    twig: TwigClient | None = None
    fs: FilesystemClient | None = None

    @classmethod
    def real(cls) -> "Toolbelt":
        return cls(
            git=RealGitClient(),
            files=RealFileClient(),
            gh=GhClient(),
            twig=TwigClient(),
            fs=None,  # FilesystemClient requires a repo_root; callers bind one.
        )
