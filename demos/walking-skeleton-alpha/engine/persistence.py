"""Bach A — pure log.

The `.events.jsonl` file is the run. No sidecar manifest, no snapshot,
no view database. Restart = open the file, fold from event_id=0, done.

INV-EVENT-LOG-AUTHORITATIVE is trivially honoured because nothing else
persists.

INV-NO-CORRUPT-FORWARD: a torn or garbled line raises `CorruptLogError`
with the byte offset; we refuse to project past it.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


class CorruptLogError(RuntimeError):
    def __init__(self, path: Path, line_no: int, byte_offset: int, detail: str):
        super().__init__(
            f"corrupt event at {path}:line {line_no} (byte {byte_offset}): {detail}"
        )
        self.path = path
        self.line_no = line_no
        self.byte_offset = byte_offset


@dataclass
class EventStore:
    path: Path

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._next_id = self._scan_next_id()

    def _scan_next_id(self) -> int:
        if not self.path.exists():
            return 0
        last = -1
        with self.path.open("rb") as f:
            for line_no, raw in enumerate(f, start=1):
                line = raw.rstrip(b"\n")
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as e:
                    raise CorruptLogError(self.path, line_no, f.tell() - len(raw), str(e)) from e
                last = max(last, int(obj.get("event_id", -1)))
        return last + 1

    def append(self, envelope: dict[str, Any]) -> int:
        with self._lock:
            envelope = dict(envelope)
            envelope["event_id"] = self._next_id
            line = json.dumps(envelope, default=str, separators=(",", ":"))
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
            self._next_id += 1
            return envelope["event_id"]


def replay(path: Path) -> Iterator[dict[str, Any]]:
    """Yield every event in order. Halts on the first corrupt line."""
    if not path.exists():
        return
    with path.open("rb") as f:
        offset = 0
        for line_no, raw in enumerate(f, start=1):
            start = offset
            offset += len(raw)
            line = raw.rstrip(b"\n")
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                raise CorruptLogError(path, line_no, start, str(e))
