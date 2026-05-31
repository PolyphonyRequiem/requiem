"""Replay-to-state. Proves INV-EVENT-LOG-AUTHORITATIVE.

The derived state holds nothing the log doesn't contain. On `CorruptLine`,
the derive function HALTS with `CorruptionDetected`; never silently skips.
An `UnknownEvent` (forward-compat case) is *recorded* (so the projection
acknowledges it happened) but does not advance any state machine the old
reader does not understand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from events import (
    GateOpened,
    NodeEntered,
    RetryAttempted,
    RunCompleted,
    RunStarted,
    UnknownEvent,
    VerbCompleted,
    VerbInvoked,
)
from reader import CorruptLine


class CorruptionDetected(RuntimeError):
    """Raised when the projection encounters a CorruptLine. Caller routes
    to the human gate per INV-NO-CORRUPT-FORWARD."""


@dataclass
class RunState:
    run_id: str | None = None
    workflow: str | None = None
    status: str = "pending"
    nodes_visited: list[str] = field(default_factory=list)
    verbs_invoked: int = 0
    verbs_completed: int = 0
    retries: int = 0
    gates_open: list[str] = field(default_factory=list)
    last_outcome: str | None = None
    terminal: str | None = None
    unknown_kinds_seen: list[str] = field(default_factory=list)


def derive(events: Iterable[object]) -> RunState:
    s = RunState()
    for ev in events:
        if isinstance(ev, CorruptLine):
            raise CorruptionDetected(
                f"line {ev.line_no} @ byte {ev.byte_offset}: {ev.error}"
            )
        if isinstance(ev, RunStarted):
            s.run_id = ev.run_id
            s.workflow = ev.workflow
            s.status = "running"
        elif isinstance(ev, NodeEntered):
            s.nodes_visited.append(ev.node_path or "?")
        elif isinstance(ev, VerbInvoked):
            s.verbs_invoked += 1
        elif isinstance(ev, VerbCompleted):
            s.verbs_completed += 1
            s.last_outcome = ev.outcome.kind
        elif isinstance(ev, GateOpened):
            s.gates_open.append(ev.gate_id)
        elif isinstance(ev, RunCompleted):
            s.status = "terminal"
            s.terminal = ev.terminal
        elif isinstance(ev, RetryAttempted):  # v2-only
            s.retries += 1
        elif isinstance(ev, UnknownEvent):
            s.unknown_kinds_seen.append(ev.event_type)
        else:
            raise CorruptionDetected(f"unexpected event object: {type(ev).__name__}")
    return s
