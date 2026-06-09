"""requiem.dashboard.projection — pure projections of the event log for the web UI.

Everything the dashboard shows is a projection of the authoritative
``*.events.jsonl`` logs (ADR-0002, INV-EVENT-LOG-AUTHORITATIVE). These functions
are deliberately **pure** — they take a log directory (and maybe a run id),
``replay()`` the relevant log(s), and return JSON-able dataclasses. No sockets,
no HTML, no ``rich`` — so they unit-test exactly like every other requiem
projection.

The run-status logic mirrors the CLI's ``_summarize_run`` (cli/main.py) on
purpose: the dashboard and ``requiem list-runs`` must never disagree about
whether a run is Running / Suspended / Completed / Failed / Cancelled.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from requiem.persistence import CorruptLogError, replay

LOG_SUFFIX = ".events.jsonl"


# ---- value objects ------------------------------------------------------


@dataclass(frozen=True)
class RunSummary:
    """One row in the runs list."""

    run_id: str
    workflow: str
    status: str            # Running | Suspended | Completed | Failed | Cancelled | Corrupt
    started: str | None
    last_ts: str | None
    events: int
    final_node: str
    gate_open: bool        # a human gate is currently awaiting resolution

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TimelineEntry:
    """One humanized event in a run's timeline."""

    event_id: int
    kind: str
    glyph: str
    node: str | None
    summary: str
    ts: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RunDetail:
    """A single run: its summary plus the full humanized timeline."""

    run_id: str
    workflow: str
    status: str
    started: str | None
    last_ts: str | None
    final_node: str
    gate: dict[str, Any] | None       # the open gate's prompt/options, if suspended
    timeline: list[TimelineEntry] = field(default_factory=list)
    corrupt: str | None = None        # error detail if the log is torn

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["timeline"] = [t.to_dict() for t in self.timeline]
        return d


@dataclass(frozen=True)
class PendingGate:
    """A run currently blocked on a human gate — the actionable #8 queue."""

    run_id: str
    workflow: str
    node: str | None
    prompt: str
    options: list[str]
    opened_ts: str | None
    auto: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---- per-event humanization (dependency-free; rich stays CLI-only) ------

_GLYPH = {
    "run_started": "▶",
    "node_entered": "·",
    "verb_invoked": "▶",
    "verb_completed": "✓",
    "retry_attempted": "🔁",
    "route_taken": "→",
    "team_dispatched": "⇉",
    "team_branch_completed": "✓",
    "gate_opened": "🚦",
    "gate_resolved": "✓",
    "cancel_requested": "✕",
    "subworkflow_started": "⤷",
    "subworkflow_completed": "⤶",
    "subworkflow_cancelled": "✕",
    "run_completed": "■",
}


def _summarize_event(kind: str, payload: dict[str, Any], node: str | None) -> str:
    """A short human string for one event — no rich, no colour."""
    p = payload or {}
    if kind == "run_started":
        wf = p.get("workflow", "")
        ver = p.get("workflow_version")
        return f"run started — {wf}" + (f" v{ver}" if ver and ver != "0" else "")
    if kind == "node_entered":
        attempt = p.get("attempt", 1)
        return f"entered {node}" + (f" (attempt {attempt})" if attempt and attempt > 1 else "")
    if kind == "verb_invoked":
        return f"{node}: {p.get('verb', '')}"
    if kind == "verb_completed":
        outcome = p.get("outcome") or {}
        ok = outcome.get("kind", "?")
        return f"{node}: {ok}"
    if kind == "retry_attempted":
        return f"{node}: retry {p.get('attempt')}→{p.get('next_attempt')} ({p.get('reason', '')})"
    if kind == "route_taken":
        return f"{node} --{p.get('key', '')}--> {p.get('to_node', '')}"
    if kind == "team_dispatched":
        return f"{node}: dispatched team {p.get('team_id', '')} ({len(p.get('branches') or [])} branches)"
    if kind == "team_branch_completed":
        return f"{node}: branch {p.get('agent_id', '')} done"
    if kind == "gate_opened":
        return f"{node}: gate — {p.get('prompt', '')}"
    if kind == "gate_resolved":
        return f"{node}: resolved → {p.get('choice', '')}"
    if kind == "cancel_requested":
        return f"cancel requested ({p.get('reason', '')})"
    if kind == "subworkflow_started":
        return f"{node}: → {p.get('sub_workflow_module', '')}"
    if kind == "subworkflow_completed":
        return f"{node}: child {p.get('disposition', '')}"
    if kind == "subworkflow_cancelled":
        return f"{node}: child cancelled"
    if kind == "run_completed":
        return f"run {p.get('terminal', '')} at {p.get('final_node', '')}"
    return kind


# ---- internal fold ------------------------------------------------------


@dataclass
class _Folded:
    workflow: str = ""
    started: str | None = None
    last_ts: str | None = None
    status: str = "Running"
    events: int = 0
    final_node: str = ""
    gate: dict[str, Any] | None = None     # currently-open gate payload
    corrupt: str | None = None
    timeline: list[TimelineEntry] = field(default_factory=list)


def _fold(log_path: Path, *, with_timeline: bool) -> _Folded:
    """Single pass over a run's log, computing status (+ optional timeline)."""
    f = _Folded()
    try:
        for ev in replay(log_path):
            f.events += 1
            kind = ev.get("kind", "")
            ts = ev.get("ts")
            node = ev.get("node_id")
            payload = ev.get("payload") or {}
            if f.started is None and ts:
                f.started = ts
            if ts:
                f.last_ts = ts
            if kind == "run_started":
                f.workflow = payload.get("workflow", "")
            elif kind == "gate_opened":
                f.status = "Suspended"
                f.gate = {
                    "node": node,
                    "prompt": payload.get("prompt", ""),
                    "options": list(payload.get("options") or []),
                    "auto": bool(payload.get("auto", False)),
                    "opened_ts": ts,
                }
            elif kind == "gate_resolved":
                f.status = "Running"
                f.gate = None
            elif kind == "cancel_requested":
                f.status = "Cancelled"
            elif kind == "run_completed":
                terminal = payload.get("terminal", "")
                f.final_node = payload.get("final_node", "")
                f.gate = None
                if terminal == "cancelled":
                    f.status = "Cancelled"
                elif terminal == "failed":
                    f.status = "Failed"
                elif terminal == "needs_human":
                    f.status = "Needs human"
                else:
                    f.status = "Completed"
            if with_timeline:
                f.timeline.append(TimelineEntry(
                    event_id=int(ev.get("event_id", -1)),
                    kind=kind,
                    glyph=_GLYPH.get(kind, "·"),
                    node=node,
                    summary=_summarize_event(kind, payload, node),
                    ts=ts,
                ))
    except CorruptLogError as e:
        # INV-LOG-STRICT-STOP-ON-CORRUPTION: surface the tear, don't hide it.
        f.status = "Corrupt"
        f.corrupt = str(e)
    return f


# ---- public projections -------------------------------------------------


def _run_ids(log_dir: Path) -> list[tuple[str, Path]]:
    if not log_dir.exists():
        return []
    out: list[tuple[str, Path]] = []
    for path in sorted(log_dir.glob(f"*{LOG_SUFFIX}")):
        out.append((path.name[: -len(LOG_SUFFIX)], path))
    return out


def list_runs(log_dir: Path) -> list[RunSummary]:
    """One :class:`RunSummary` per ``*.events.jsonl`` under ``log_dir``.

    Sorted newest-started first (None-started sinks to the bottom).
    """
    summaries: list[RunSummary] = []
    for run_id, path in _run_ids(log_dir):
        f = _fold(path, with_timeline=False)
        summaries.append(RunSummary(
            run_id=run_id,
            workflow=f.workflow,
            status=f.status,
            started=f.started,
            last_ts=f.last_ts,
            events=f.events,
            final_node=f.final_node,
            gate_open=f.gate is not None,
        ))
    summaries.sort(key=lambda s: (s.started or ""), reverse=True)
    return summaries


def run_detail(log_dir: Path, run_id: str) -> RunDetail | None:
    """The full humanized timeline for one run, or None if its log is absent."""
    path = log_dir / f"{run_id}{LOG_SUFFIX}"
    if not path.exists():
        return None
    f = _fold(path, with_timeline=True)
    return RunDetail(
        run_id=run_id,
        workflow=f.workflow,
        status=f.status,
        started=f.started,
        last_ts=f.last_ts,
        final_node=f.final_node,
        gate=f.gate,
        timeline=f.timeline,
        corrupt=f.corrupt,
    )


def pending_gates(log_dir: Path) -> list[PendingGate]:
    """Every run currently blocked on an unresolved human gate.

    This is the actionable queue v0 non-negotiable #8 is fundamentally about:
    "where does an operator go to see what needs a human?"
    """
    gates: list[PendingGate] = []
    for run_id, path in _run_ids(log_dir):
        f = _fold(path, with_timeline=False)
        if f.gate is not None:
            gates.append(PendingGate(
                run_id=run_id,
                workflow=f.workflow,
                node=f.gate.get("node"),
                prompt=f.gate.get("prompt", ""),
                options=list(f.gate.get("options") or []),
                opened_ts=f.gate.get("opened_ts"),
                auto=bool(f.gate.get("auto", False)),
            ))
    gates.sort(key=lambda g: (g.opened_ts or ""), reverse=True)
    return gates


def short_ts(ts: str | None) -> str:
    """Compact timestamp for display. Tolerant of missing/garbled input."""
    if not ts:
        return "—"
    try:
        return datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M:%SZ")
    except ValueError:
        return ts[:19]
