"""Event log: append-only JSONL.

Placeholder for Brahms's seam (#2). When Brahms's schema lands this
file dies. The kernel ONLY needs: (a) append, (b) replay-in-order,
(c) monotone event_id. Anything else (indexing, fanout, SSE) is
Brahms's problem.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Iterator


def utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# Canonical event types (closed taxonomy — kernel-owned).
# These names appear in every variant. If you add one, update all three.
EVENT_TYPES = {
    "workflow_started",
    "node_entered",
    "node_completed",
    "route_taken",
    "retry_attempted",
    "human_gate_presented",
    "human_gate_resolved",
    "subworkflow_started",
    "subworkflow_completed",
    "cancel_received",
    "workflow_terminated",
}


class EventLog:
    """Append-only JSONL log. One file per run."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # next event_id derived from current contents on construction
        self._next_id = 1 + sum(1 for _ in self._read_raw())

    def _read_raw(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line)

    def append(self, event_type: str, **fields: Any) -> dict[str, Any]:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unknown event type: {event_type}")
        evt = {
            "event_id": self._next_id,
            "ts": utc_iso(),
            "type": event_type,
            **fields,
        }
        self._next_id += 1
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(evt, sort_keys=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return evt

    def replay(self) -> list[dict[str, Any]]:
        return list(self._read_raw())
