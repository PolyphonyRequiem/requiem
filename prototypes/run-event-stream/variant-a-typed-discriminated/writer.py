"""Append-only writer with line-buffered fsync option.

For Phase A this is the smallest correct shape:
- file opened in 'a' mode (atomic positional append on POSIX < PIPE_BUF; on
  Windows single-process appends are serialized by the OS).
- one event = one line of JSON terminated by \\n.
- `fsync=True` for crash-resilience tests; default False to mirror real cost.

Single-process invariant (INV-SINGLE-PROCESS) means we don't need cross-process
locking. A multi-writer story would require flock/LockFileEx; out of scope.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class EventWriter:
    def __init__(self, path: Path, *, fsync: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fsync = fsync
        self._fh = open(self.path, "a", encoding="utf-8", buffering=1)  # line-buffered

    def append(self, event: BaseModel) -> None:
        line = event.model_dump_json()
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()
            if self._fsync:
                os.fsync(self._fh.fileno())

    def append_raw(self, raw: str) -> None:
        """Escape hatch for the corruption-handling demo only."""
        with self._lock:
            self._fh.write(raw)
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            self._fh.close()

    def __enter__(self) -> "EventWriter":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
