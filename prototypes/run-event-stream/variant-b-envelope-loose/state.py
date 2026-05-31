"""Replay-to-state for variant B."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from events import TypedEvent
from reader import CorruptLine


class CorruptionDetected(RuntimeError):
    pass


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
        if not isinstance(ev, TypedEvent):
            raise CorruptionDetected(f"unexpected event object: {type(ev).__name__}")
        env = ev.envelope
        if not ev.known:
            s.unknown_kinds_seen.append(env.kind)
            continue
        k = env.kind
        p = ev.payload
        if k == "run_started":
            s.run_id = env.run_id
            s.workflow = p.workflow  # type: ignore[attr-defined]
            s.status = "running"
        elif k == "node_entered":
            s.nodes_visited.append(env.node_path or "?")
        elif k == "verb_invoked":
            s.verbs_invoked += 1
        elif k == "verb_completed":
            s.verbs_completed += 1
            s.last_outcome = p.outcome.kind  # type: ignore[attr-defined]
        elif k == "gate_opened":
            s.gates_open.append(p.gate_id)  # type: ignore[attr-defined]
        elif k == "run_completed":
            s.status = "terminal"
            s.terminal = p.terminal  # type: ignore[attr-defined]
        elif k == "retry_attempted":
            s.retries += 1
        else:
            raise CorruptionDetected(f"known-in-registry but no handler: {k}")
    return s
