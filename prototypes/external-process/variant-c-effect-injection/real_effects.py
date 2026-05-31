"""Real effect implementations (Windows-safe subprocess wrappers)."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

from effects import (
    GhAuthLapse,
    GhFailure,
    GhPrFound,
    GhPrMissing,
    GhPrViewOutcome,
    GhTransient,
    GitFailure,
    GitMissingRef,
    GitNotARepo,
    GitRevParseOutcome,
    GitSha,
)


def _invoke(
    cmd: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout_s: float | None = None,
) -> tuple[bool, bool, int | None, str, str]:
    """(binary_missing, timed_out, exit_code, stdout, stderr)"""
    try:
        c = subprocess.run(
            list(cmd),
            cwd=str(cwd) if cwd else None,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            shell=False,
            check=False,
        )
    except FileNotFoundError:
        return True, False, None, "", ""
    except (NotADirectoryError, PermissionError) as exc:
        return True, False, None, "", f"cwd unstartable: {exc}"
    except subprocess.TimeoutExpired as exc:
        return False, True, None, _dec(exc.stdout), _dec(exc.stderr)
    return False, False, c.returncode, c.stdout, c.stderr


def _dec(b):
    if b is None:
        return ""
    return b.decode("utf-8", errors="replace") if isinstance(b, bytes) else b


class RealGit:
    def rev_parse(self, repo: Path, ref: str, *, timeout_s: float = 5.0) -> GitRevParseOutcome:
        missing, timed_out, ec, stdout, stderr = _invoke(
            ["git", "rev-parse", "--verify", ref], cwd=repo, timeout_s=timeout_s
        )
        if missing:
            return GitFailure(detail="git not on PATH", is_missing_tool=True)
        if timed_out:
            return GitFailure(detail=f"git rev-parse timed out after {timeout_s}s", is_timeout=True)
        if ec == 0:
            return GitSha(sha=stdout.strip())
        s = stderr.lower()
        if ec == 128 and "not a git repository" in s:
            return GitNotARepo()
        if ec == 128:
            return GitMissingRef(ref=ref)
        return GitFailure(detail=f"git exit {ec}: {stderr.strip()}")


class RealGh:
    def pr_view(self, repo: Path, pr_number: int, *, timeout_s: float = 15.0) -> GhPrViewOutcome:
        missing, timed_out, ec, stdout, stderr = _invoke(
            ["gh", "pr", "view", str(pr_number), "--json", "number,state,title"],
            cwd=repo,
            timeout_s=timeout_s,
        )
        if missing:
            return GhFailure(detail="gh not on PATH", is_missing_tool=True)
        if timed_out:
            return GhTransient(reason="timeout")
        if ec == 0:
            return GhPrFound(raw_json=stdout)
        s = stderr.lower()
        if "no pull requests found" in s or "could not resolve" in s:
            return GhPrMissing(pr_number=pr_number)
        if "rate limit" in s:
            return GhTransient(reason="rate_limit")
        if "http 5" in s or "server error" in s:
            return GhTransient(reason="server_error")
        if "401" in s or "403" in s or "authentication" in s or "token" in s:
            return GhAuthLapse(stderr=stderr)
        # Ravel L-1: catch-all is GhFailure, NOT GhTransient.
        return GhFailure(detail=f"gh exit {ec}: {stderr.strip()}")


class RealClock:
    def now_unix(self) -> float:
        return time.time()
