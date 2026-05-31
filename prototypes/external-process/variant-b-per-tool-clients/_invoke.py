"""Shared subprocess helper used by every Real*Client.

Note: this is implementation detail of the client layer. Verbs never see it.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass
class RawResult:
    completed: bool          # True iff process started and exited normally
    timed_out: bool
    binary_missing: bool
    exit_code: int | None
    stdout: str
    stderr: str


def invoke(
    cmd: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout_s: float | None = None,
) -> RawResult:
    merged_env = {**os.environ, **(env or {})}
    try:
        c = subprocess.run(
            list(cmd),
            cwd=str(cwd) if cwd else None,
            env=merged_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            shell=False,
            check=False,
        )
    except FileNotFoundError:
        return RawResult(False, False, True, None, "", "")
    except (NotADirectoryError, PermissionError) as exc:
        return RawResult(False, False, True, None, "", f"cwd unstartable: {exc}")
    except subprocess.TimeoutExpired as exc:
        return RawResult(False, True, False, None, _decode(exc.stdout), _decode(exc.stderr))
    return RawResult(True, False, False, c.returncode, c.stdout, c.stderr)


def _decode(buf: bytes | str | None) -> str:
    if buf is None:
        return ""
    return buf.decode("utf-8", errors="replace") if isinstance(buf, bytes) else buf
