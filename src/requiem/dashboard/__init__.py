"""requiem.dashboard — a stdlib-only, read-only web dashboard for run event logs.

See ADR-0019. The dashboard is a pure projection of the authoritative
``*.events.jsonl`` logs; it adds no runtime dependencies (stdlib ``http.server``
only) and never mutates a run.

Public surface:

* :func:`requiem.dashboard.projection.list_runs` / ``run_detail`` / ``pending_gates``
  — pure projections (unit-testable, no sockets).
* :func:`requiem.dashboard.server.build_server` / ``serve`` — the HTTP transport.
"""
from __future__ import annotations

from requiem.dashboard.projection import (
    PendingGate,
    RunDetail,
    RunSummary,
    TimelineEntry,
    list_runs,
    pending_gates,
    run_detail,
)

__all__ = [
    "RunSummary",
    "RunDetail",
    "TimelineEntry",
    "PendingGate",
    "list_runs",
    "run_detail",
    "pending_gates",
]
