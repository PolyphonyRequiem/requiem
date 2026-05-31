from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from events import TYPE_PREFIX, TypedCloudEvent
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
    unknown_types_seen: list[str] = field(default_factory=list)


def derive(events: Iterable[object]) -> RunState:
    s = RunState()
    for ev in events:
        if isinstance(ev, CorruptLine):
            raise CorruptionDetected(
                f"line {ev.line_no} @ byte {ev.byte_offset}: {ev.error}"
            )
        if not isinstance(ev, TypedCloudEvent):
            raise CorruptionDetected(f"unexpected event object: {type(ev).__name__}")
        env = ev.envelope
        if not ev.known:
            s.unknown_types_seen.append(env.type)
            continue
        t = env.type
        d = ev.data
        if t == f"{TYPE_PREFIX}.run.started":
            s.run_id = d.run_id  # type: ignore[attr-defined]
            s.workflow = d.workflow  # type: ignore[attr-defined]
            s.status = "running"
        elif t == f"{TYPE_PREFIX}.node.entered":
            s.nodes_visited.append(d.node_path)  # type: ignore[attr-defined]
        elif t == f"{TYPE_PREFIX}.verb.invoked":
            s.verbs_invoked += 1
        elif t == f"{TYPE_PREFIX}.verb.completed":
            s.verbs_completed += 1
            s.last_outcome = d.outcome.kind  # type: ignore[attr-defined]
        elif t == f"{TYPE_PREFIX}.gate.opened":
            s.gates_open.append(d.gate_id)  # type: ignore[attr-defined]
        elif t == f"{TYPE_PREFIX}.run.completed":
            s.status = "terminal"
            s.terminal = d.terminal  # type: ignore[attr-defined]
        elif t == f"{TYPE_PREFIX}.verb.retry_attempted":
            s.retries += 1
        else:
            raise CorruptionDetected(f"known-in-registry but no handler: {t}")
    return s
