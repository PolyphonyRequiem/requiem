"""requiem.dashboard.auto_resume — opt-in continuation after a dashboard gate resolve.

The dashboard's gate resolution (``resolution.resolve_gate``) is deliberately
*append-only*: it writes the ``gate_resolved`` event and stops, leaving the actual
continuation to a human running ``requiem resume`` (ADR-0019 drew the dashboard as
an observe-and-record surface, not an engine host). That is the safe default.

This module is the **opt-in** bridge that closes the ergonomic gap: when the
operator launches the dashboard with ``--auto-resume``, a successful resolution
also fires ``requiem resume <workflow_module> <run_id>`` as a **detached
subprocess** — the exact same path the human would run by hand. The engine still
runs *out of process*, so the dashboard's request thread never hosts a kernel; the
read-only-projection boundary for observation is preserved.

Off by default. Best-effort: a spawn failure is reported in the resolve response
but never corrupts the (already-committed, append-only) ``gate_resolved`` event.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from requiem.dashboard.projection import run_detail


class AutoResumeError(Exception):
    """A non-fatal failure to spawn the resume subprocess."""


def resolve_workflow_module(log_dir: Path, run_id: str) -> str | None:
    """The dotted workflow module for ``run_id`` (from its run_started event).

    ``requiem resume`` needs the module to rebuild the engine. We read it from the
    same projection the dashboard already trusts. Returns None when the run or its
    workflow identity is absent.
    """
    detail = run_detail(log_dir, run_id)
    if detail is None:
        return None
    return detail.workflow or None


def spawn_resume(
    log_dir: Path,
    run_id: str,
    *,
    workflow_module: str | None = None,
    python: str | None = None,
) -> subprocess.Popen:
    """Fire ``requiem resume <module> <run_id>`` as a detached subprocess.

    Returns the ``Popen`` handle (the caller does not wait — the run continues
    independently). Raises :class:`AutoResumeError` when the workflow module can't
    be resolved or the spawn fails.
    """
    module = workflow_module or resolve_workflow_module(log_dir, run_id)
    if not module:
        raise AutoResumeError(
            f"cannot auto-resume {run_id!r}: no workflow module in its event log"
        )
    argv = [
        python or sys.executable, "-m", "requiem.cli.main", "resume",
        module, run_id, "--log-dir", str(log_dir),
    ]
    try:
        # Detached: stdout/stderr to DEVNULL, no wait. The resumed run writes its
        # own continuation into the same authoritative event log, which the
        # dashboard then projects on its next poll.
        return subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            cwd=str(Path.cwd()),
        )
    except OSError as e:  # pragma: no cover - spawn failure is environment-specific
        raise AutoResumeError(f"failed to spawn requiem resume: {e}") from e
