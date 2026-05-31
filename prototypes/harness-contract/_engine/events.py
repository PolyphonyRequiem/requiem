"""Append-only JSONL event log — INV-EVENT-LOG-AUTHORITATIVE.

Only enough event taxonomy to demo the seam:
  RunStarted, NodeEntered, NodeCompleted, RetryAttempted,
  HumanGatePresented, HumanGateResolved, SubworkflowStarted,
  SubworkflowCompleted, RunTerminated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, Field


class Event(BaseModel):
    event_id: int
    run_id: str
    type: str
    node: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_json(self) -> str:
        return self.model_dump_json()


class EventLog:
    """Append-only writer + iterator. Backed by a JSONL file so that
    the engine can be killed and restarted against the same path."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._next_id = self._discover_next_id()
        self._subscribers: list = []

    def _discover_next_id(self) -> int:
        if not self.path.exists():
            return 1
        last_id = 0
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            last_id = json.loads(line)["event_id"]
        return last_id + 1

    def append(self, type: str, run_id: str, *, node: str | None = None, **payload) -> Event:
        evt = Event(event_id=self._next_id, run_id=run_id, type=type, node=node, payload=payload)
        self._next_id += 1
        with self.path.open("a", encoding="utf-8") as f:
            f.write(evt.to_json() + "\n")
        for sub in self._subscribers:
            sub(evt)
        return evt

    def subscribe(self, fn) -> None:
        """In-process listener — for variants that want push, not pull."""
        self._subscribers.append(fn)

    def __iter__(self) -> Iterator[Event]:
        if not self.path.exists():
            return iter(())
        return (
            Event.model_validate_json(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )

    def find(self, type: str, *, node: str | None = None, **payload_match) -> list[Event]:
        out = []
        for e in self:
            if e.type != type:
                continue
            if node is not None and e.node != node:
                continue
            if all(e.payload.get(k) == v for k, v in payload_match.items()):
                out.append(e)
        return out

    def last_completed_node(self, run_id: str | None = None) -> str | None:
        last = None
        for e in self:
            if e.type != "NodeCompleted":
                continue
            if run_id is not None and e.run_id != run_id:
                continue
            last = e.node
        return last
