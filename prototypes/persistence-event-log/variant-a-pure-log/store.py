"""Variant A — pure event log.

The log IS the run. Manifest is a pure function over the log, cached in
memory, invalidated whenever a new event is appended. Restart = open log,
fold from event_id=0, done.

INV-EVENT-LOG-AUTHORITATIVE: trivially honoured — nothing else persists.
INV-NO-CORRUPT-FORWARD: a truncated/garbled line aborts replay with a
typed `CorruptLogError`; we refuse to project past the bad line.
INV-RESTART: open(path) + fold; no other state to load.
"""
from __future__ import annotations

import io
import json
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from pydantic import TypeAdapter, ValidationError

from events import KNOWN_KINDS, Event


_event_adapter: TypeAdapter[Event] = TypeAdapter(Event)


class CorruptLogError(RuntimeError):
    """Raised when a log line cannot be decoded. Includes byte offset so
    the operator can `head -c <offset>` and see exactly where the run died."""

    def __init__(self, path: Path, line_no: int, byte_offset: int, detail: str):
        super().__init__(
            f"corrupt event at {path}:line {line_no} (byte offset {byte_offset}): {detail}"
        )
        self.path = path
        self.line_no = line_no
        self.byte_offset = byte_offset
        self.detail = detail


# ---------------------------------------------------------------------------
# Projection — the manifest, derived
# ---------------------------------------------------------------------------


@dataclass
class MergeGroup:
    mg_id: str
    mg_path: str
    parent_mg_path: Optional[str]
    items: list[int]
    nesting: str
    isolation: str


@dataclass
class Projection:
    """The 'manifest' in pure-log world: a snapshot of what the engine
    needs in memory. Built from events; never written to disk."""

    run_id: Optional[str] = None
    root_id: Optional[int] = None
    platform_project: Optional[str] = None
    created_by: Optional[str] = None
    branch_model_version: int = 1
    current_node: Optional[str] = None
    merge_groups: dict[str, MergeGroup] = field(default_factory=dict)
    retired_mg_ids: set[str] = field(default_factory=set)
    plan_generations: dict[str, int] = field(default_factory=dict)
    human_approvals: list[tuple[str, str, Optional[str]]] = field(default_factory=list)
    sub_runs: dict[str, str] = field(default_factory=dict)  # sub_run_id -> outcome|"running"
    ended: bool = False
    end_outcome: Optional[str] = None
    last_event_id: int = -1
    # forward-compat: events of unknown kind that we round-tripped but
    # refused to project. operator-visible via diagnose.
    unknown_kind_count: int = 0

    def apply(self, raw: dict) -> None:
        kind = raw.get("kind")
        if kind not in KNOWN_KINDS:
            # forward-compat: count but do not mutate state. INV-NO-CORRUPT-FORWARD
            # — we do not pretend to understand a payload we cannot type.
            self.unknown_kind_count += 1
            self.last_event_id = max(self.last_event_id, int(raw.get("event_id", -1)))
            return
        try:
            ev = _event_adapter.validate_python(raw)
        except ValidationError as e:
            raise CorruptLogError(
                Path("<in-memory>"), -1, -1, f"validation failed for {kind}: {e.errors()[:2]}"
            ) from e
        self.last_event_id = max(self.last_event_id, ev.event_id)
        match ev.kind:
            case "run_started":
                self.run_id = ev.run_id
                self.root_id = ev.root_id
                self.platform_project = ev.platform_project
                self.created_by = ev.created_by
                self.branch_model_version = ev.branch_model_version
            case "node_entered":
                self.current_node = ev.node
            case "node_completed":
                # current_node stays — the router decides what's next.
                pass
            case "mg_declared":
                self.merge_groups[ev.mg_id] = MergeGroup(
                    mg_id=ev.mg_id,
                    mg_path=ev.mg_path,
                    parent_mg_path=ev.parent_mg_path,
                    items=list(ev.items),
                    nesting=ev.nesting,
                    isolation=ev.isolation,
                )
            case "plan_generation_bumped":
                self.plan_generations[ev.item_key] = self.plan_generations.get(ev.item_key, 0) + 1
            case "human_approval_recorded":
                self.human_approvals.append((ev.gate, ev.approved_by, ev.detail))
            case "mg_retired":
                self.retired_mg_ids.add(ev.mg_id)
            case "subworkflow_invoked":
                self.sub_runs[ev.sub_run_id] = "running"
            case "subworkflow_completed":
                self.sub_runs[ev.sub_run_id] = ev.outcome
            case "run_ended":
                self.ended = True
                self.end_outcome = ev.outcome


# ---------------------------------------------------------------------------
# Store — one append-only file per run
# ---------------------------------------------------------------------------


class EventStore:
    """Single-writer append-only JSONL store. Per-run isolation via the
    one-store-per-path invariant: caller passes distinct paths for distinct
    runs and `Engine` enforces that mapping."""

    def __init__(self, path: Path):
        self.path = path
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
                    # bail at first bad line; do not skip silently.
                    raise CorruptLogError(self.path, line_no, f.tell() - len(raw), str(e)) from e
                last = max(last, int(obj.get("event_id", -1)))
        return last + 1

    def append(self, event_payload: dict) -> int:
        """Append one event. Assigns event_id; fsync before returning."""
        with self._lock:
            event_payload = dict(event_payload)
            event_payload["event_id"] = self._next_id
            line = json.dumps(event_payload, default=str, separators=(",", ":"))
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._next_id += 1
            return event_payload["event_id"]

    def iter_raw(self) -> Iterator[tuple[int, int, dict]]:
        """Yield (line_no, byte_offset, raw_dict) for every event. Raises
        CorruptLogError at the first un-decodable line."""
        if not self.path.exists():
            return
        with self.path.open("rb") as f:
            offset = 0
            for line_no, raw in enumerate(f, start=1):
                start = offset
                offset += len(raw)
                line = raw.rstrip(b"\n")
                if not line:
                    continue
                try:
                    yield line_no, start, json.loads(line)
                except json.JSONDecodeError as e:
                    raise CorruptLogError(self.path, line_no, start, str(e))


# ---------------------------------------------------------------------------
# Engine — caches projection in memory, invalidates on append
# ---------------------------------------------------------------------------


class Engine:
    """In-memory engine. One Engine per run_id. The Projection is rebuilt
    by replaying the log; reads after writes are O(1) because we apply the
    new event directly rather than re-folding the whole log."""

    _instances: dict[str, "Engine"] = {}
    _instances_lock = threading.Lock()

    def __init__(self, run_id: str, log_dir: Path):
        self.run_id = run_id
        self.store = EventStore(log_dir / f"{run_id}.events.jsonl")
        self._projection = self._rebuild()

    @classmethod
    def open(cls, run_id: str, log_dir: Path) -> "Engine":
        with cls._instances_lock:
            key = f"{log_dir.resolve()}::{run_id}"
            if key not in cls._instances:
                cls._instances[key] = cls(run_id, log_dir)
            return cls._instances[key]

    @classmethod
    def forget_all(cls) -> None:
        """Drop in-memory cache — used to simulate process restart."""
        with cls._instances_lock:
            cls._instances.clear()

    def _rebuild(self) -> Projection:
        proj = Projection()
        for _, _, raw in self.store.iter_raw():
            proj.apply(raw)
        return proj

    @property
    def projection(self) -> Projection:
        return self._projection

    def append(self, payload: dict) -> int:
        """Append an event and apply it to the in-memory projection."""
        eid = self.store.append(payload)
        # Re-fetch the raw row so apply() sees the canonical event_id.
        merged = dict(payload)
        merged["event_id"] = eid
        self._projection.apply(merged)
        return eid


@contextmanager
def fresh_process(log_dir: Path) -> Iterator[None]:
    """Context manager that simulates an engine restart by dropping all
    in-memory state. The next Engine.open(...) call replays from disk."""
    Engine.forget_all()
    try:
        yield
    finally:
        pass
