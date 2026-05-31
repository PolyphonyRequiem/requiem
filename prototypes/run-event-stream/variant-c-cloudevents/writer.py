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
        self._fh = open(self.path, "a", encoding="utf-8", buffering=1)

    def append(self, event: BaseModel) -> None:
        line = event.model_dump_json()
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()
            if self._fsync:
                os.fsync(self._fh.fileno())

    def append_raw(self, raw: str) -> None:
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
