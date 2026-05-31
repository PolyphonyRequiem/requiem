"""ProcessRunner protocol + real and fake implementations.

Cross-platform notes (Windows is Daniel's primary):
  - shell=False everywhere; no /bin/sh assumptions.
  - cmd is always list[str]; pathlib.Path is stringified at the seam.
  - env defaults to os.environ-merged so PATH/PATHEXT/SYSTEMROOT survive.
  - Text mode with utf-8 + errors='replace' avoids decoder crashes on `gh` output.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from outcomes import NonZeroExit, NotFound, ProcessOutcome, Success, Timeout


class ProcessRunner(Protocol):
    def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> ProcessOutcome: ...


class RealProcessRunner:
    def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> ProcessOutcome:
        merged_env = {**os.environ, **(env or {})}
        started = time.monotonic()
        try:
            completed = subprocess.run(
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
            return NotFound(binary=cmd[0])
        except (NotADirectoryError, PermissionError) as exc:
            # Windows raises NotADirectoryError when cwd is missing; Unix raises
            # FileNotFoundError. Either way the process never started — same shape.
            return NotFound(binary=f"{cmd[0]} (cwd unstartable: {exc})")
        except subprocess.TimeoutExpired as exc:
            return Timeout(
                timeout_s=timeout_s or 0.0,
                partial_stdout=_decode(exc.stdout),
                partial_stderr=_decode(exc.stderr),
            )

        duration_s = time.monotonic() - started
        if completed.returncode == 0:
            return Success(
                stdout=completed.stdout,
                stderr=completed.stderr,
                duration_s=duration_s,
            )
        return NonZeroExit(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_s=duration_s,
        )


def _decode(buf: bytes | str | None) -> str:
    if buf is None:
        return ""
    if isinstance(buf, bytes):
        return buf.decode("utf-8", errors="replace")
    return buf


class FakeProcessRunner:
    """Scripts outcomes by argv prefix-match — order matters; first match wins."""

    def __init__(self) -> None:
        self._scripts: list[tuple[Sequence[str], ProcessOutcome]] = []
        self.calls: list[Sequence[str]] = []

    def when(self, argv_prefix: Sequence[str], outcome: ProcessOutcome) -> "FakeProcessRunner":
        self._scripts.append((list(argv_prefix), outcome))
        return self

    def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> ProcessOutcome:
        self.calls.append(list(cmd))
        for prefix, outcome in self._scripts:
            if list(cmd)[: len(prefix)] == list(prefix):
                return outcome
        raise AssertionError(f"FakeProcessRunner: no script for {list(cmd)!r}")
