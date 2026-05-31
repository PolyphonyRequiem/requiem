"""Variant B — event log + periodic snapshots.

Same authoritative substrate as Variant A: the JSONL log is truth.
A snapshot is a derived view: `{at_event_id, projection_json}` written
every N events. Restart loads the latest snapshot whose `at_event_id`
exists in the log, then replays only events with `event_id > at_event_id`.

Snapshots NEVER change the answer — they only change how long restart
takes. A snapshot that disagrees with re-derivation is treated as
corruption (INV-NO-CORRUPT-FORWARD) and refused.

Snapshot files: `<run>.snapshots/<at_event_id>.snapshot.json`.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Iterator, Optional

from pydantic import TypeAdapter, ValidationError

from events import KNOWN_KINDS, Event


_event_adapter: TypeAdapter[Event] = TypeAdapter(Event)


class CorruptLogError(RuntimeError):
    def __init__(self, path: Path, line_no: int, byte_offset: int, detail: str):
        super().__init__(
            f"corrupt event at {path}:line {line_no} (byte offset {byte_offset}): {detail}"
        )
        self.path, self.line_no, self.byte_offset, self.detail = path, line_no, byte_offset, detail


class SnapshotDivergenceError(RuntimeError):
    """Snapshot's projection disagrees with re-derivation from the log.
    Surface loudly; refuse to advance. Operator must `reconcile`."""


# ---------------------------------------------------------------------------
# Projection (identical math to Variant A; reproduced for self-containment)
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
    sub_runs: dict[str, str] = field(default_factory=dict)
    ended: bool = False
    end_outcome: Optional[str] = None
    last_event_id: int = -1
    unknown_kind_count: int = 0

    def apply(self, raw: dict) -> None:
        kind = raw.get("kind")
        if kind not in KNOWN_KINDS:
            self.unknown_kind_count += 1
            self.last_event_id = max(self.last_event_id, int(raw.get("event_id", -1)))
            return
        try:
            ev = _event_adapter.validate_python(raw)
        except ValidationError as e:
            raise CorruptLogError(Path("<in-memory>"), -1, -1, str(e.errors()[:1])) from e
        self.last_event_id = max(self.last_event_id, ev.event_id)
        k = ev.kind
        if k == "run_started":
            self.run_id = ev.run_id; self.root_id = ev.root_id
            self.platform_project = ev.platform_project
            self.created_by = ev.created_by
            self.branch_model_version = ev.branch_model_version
        elif k == "node_entered":
            self.current_node = ev.node
        elif k == "mg_declared":
            self.merge_groups[ev.mg_id] = MergeGroup(
                ev.mg_id, ev.mg_path, ev.parent_mg_path,
                list(ev.items), ev.nesting, ev.isolation,
            )
        elif k == "plan_generation_bumped":
            self.plan_generations[ev.item_key] = self.plan_generations.get(ev.item_key, 0) + 1
        elif k == "human_approval_recorded":
            self.human_approvals.append((ev.gate, ev.approved_by, ev.detail))
        elif k == "mg_retired":
            self.retired_mg_ids.add(ev.mg_id)
        elif k == "subworkflow_invoked":
            self.sub_runs[ev.sub_run_id] = "running"
        elif k == "subworkflow_completed":
            self.sub_runs[ev.sub_run_id] = ev.outcome
        elif k == "run_ended":
            self.ended = True; self.end_outcome = ev.outcome

    # Serialization (snapshot-only; never the truth)

    def to_snapshot_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "root_id": self.root_id,
            "platform_project": self.platform_project,
            "created_by": self.created_by,
            "branch_model_version": self.branch_model_version,
            "current_node": self.current_node,
            "merge_groups": {k: asdict(v) for k, v in self.merge_groups.items()},
            "retired_mg_ids": sorted(self.retired_mg_ids),
            "plan_generations": dict(self.plan_generations),
            "human_approvals": [list(a) for a in self.human_approvals],
            "sub_runs": dict(self.sub_runs),
            "ended": self.ended,
            "end_outcome": self.end_outcome,
            "last_event_id": self.last_event_id,
            "unknown_kind_count": self.unknown_kind_count,
        }

    @classmethod
    def from_snapshot_dict(cls, d: dict) -> "Projection":
        p = cls()
        p.run_id = d.get("run_id")
        p.root_id = d.get("root_id")
        p.platform_project = d.get("platform_project")
        p.created_by = d.get("created_by")
        p.branch_model_version = d.get("branch_model_version", 1)
        p.current_node = d.get("current_node")
        p.merge_groups = {k: MergeGroup(**v) for k, v in d.get("merge_groups", {}).items()}
        p.retired_mg_ids = set(d.get("retired_mg_ids", []))
        p.plan_generations = dict(d.get("plan_generations", {}))
        p.human_approvals = [tuple(a) for a in d.get("human_approvals", [])]
        p.sub_runs = dict(d.get("sub_runs", {}))
        p.ended = bool(d.get("ended", False))
        p.end_outcome = d.get("end_outcome")
        p.last_event_id = int(d.get("last_event_id", -1))
        p.unknown_kind_count = int(d.get("unknown_kind_count", 0))
        return p

    def fingerprint(self) -> str:
        """Stable hash for divergence detection."""
        canonical = json.dumps(self.to_snapshot_dict(), sort_keys=True, default=str)
        return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


SNAPSHOT_INTERVAL = 5  # take a snapshot every 5 events. Tunable.


class EventStore:
    def __init__(self, log_path: Path, snapshot_dir: Path):
        self.log_path = log_path
        self.snapshot_dir = snapshot_dir
        log_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._next_id = self._scan_next_id()

    def _scan_next_id(self) -> int:
        if not self.log_path.exists():
            return 0
        last = -1
        with self.log_path.open("rb") as f:
            for line_no, raw in enumerate(f, start=1):
                line = raw.rstrip(b"\n")
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as e:
                    raise CorruptLogError(self.log_path, line_no, f.tell() - len(raw), str(e))
                last = max(last, int(obj.get("event_id", -1)))
        return last + 1

    def append(self, payload: dict) -> int:
        with self._lock:
            payload = dict(payload)
            payload["event_id"] = self._next_id
            line = json.dumps(payload, default=str, separators=(",", ":"))
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._next_id += 1
            return payload["event_id"]

    def iter_raw(self, after: int = -1) -> Iterator[dict]:
        if not self.log_path.exists():
            return
        with self.log_path.open("rb") as f:
            offset = 0
            for line_no, raw in enumerate(f, start=1):
                start = offset
                offset += len(raw)
                line = raw.rstrip(b"\n")
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as e:
                    raise CorruptLogError(self.log_path, line_no, start, str(e))
                if int(obj.get("event_id", -1)) > after:
                    yield obj

    def write_snapshot(self, projection: Projection) -> Path:
        path = self.snapshot_dir / f"{projection.last_event_id:012d}.snapshot.json"
        body = {
            "at_event_id": projection.last_event_id,
            "fingerprint": projection.fingerprint(),
            "projection": projection.to_snapshot_dict(),
        }
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(body, f, default=str, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return path

    def latest_snapshot(self) -> Optional[dict]:
        snaps = sorted(self.snapshot_dir.glob("*.snapshot.json"))
        if not snaps:
            return None
        return json.loads(snaps[-1].read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class Engine:
    _instances: dict[str, "Engine"] = {}
    _instances_lock = threading.Lock()

    def __init__(self, run_id: str, log_dir: Path,
                 verify_snapshot: bool = True,
                 snapshot_interval: int = SNAPSHOT_INTERVAL):
        self.run_id = run_id
        self.snapshot_interval = snapshot_interval
        self.store = EventStore(
            log_dir / f"{run_id}.events.jsonl",
            log_dir / f"{run_id}.snapshots",
        )
        self._projection = self._rebuild(verify_snapshot=verify_snapshot)
        self._events_since_snapshot = 0

    @classmethod
    def open(cls, run_id: str, log_dir: Path, *, verify_snapshot: bool = True) -> "Engine":
        with cls._instances_lock:
            key = f"{log_dir.resolve()}::{run_id}"
            if key not in cls._instances:
                cls._instances[key] = cls(run_id, log_dir, verify_snapshot=verify_snapshot)
            return cls._instances[key]

    @classmethod
    def forget_all(cls) -> None:
        with cls._instances_lock:
            cls._instances.clear()

    def _rebuild(self, *, verify_snapshot: bool) -> Projection:
        snap = self.store.latest_snapshot()
        if snap is None:
            proj = Projection()
            for raw in self.store.iter_raw():
                proj.apply(raw)
            return proj

        # Snapshot exists. Load it; replay only events after it.
        proj = Projection.from_snapshot_dict(snap["projection"])
        at = int(snap["at_event_id"])
        for raw in self.store.iter_raw(after=at):
            proj.apply(raw)

        if verify_snapshot:
            # Re-derive purely from the log and compare to the snapshot-anchored
            # projection. Divergence is corruption; refuse to advance.
            pure = Projection()
            for raw in self.store.iter_raw():
                pure.apply(raw)
            if pure.fingerprint() != proj.fingerprint():
                raise SnapshotDivergenceError(
                    f"snapshot {at} disagrees with pure log replay:\n"
                    f"  snapshot-derived: {proj.fingerprint()}\n"
                    f"  pure-log-derived: {pure.fingerprint()}"
                )
        return proj

    @property
    def projection(self) -> Projection:
        return self._projection

    def append(self, payload: dict) -> int:
        eid = self.store.append(payload)
        merged = dict(payload); merged["event_id"] = eid
        self._projection.apply(merged)
        self._events_since_snapshot += 1
        if self._events_since_snapshot >= self.snapshot_interval:
            self.store.write_snapshot(self._projection)
            self._events_since_snapshot = 0
        return eid


@contextmanager
def fresh_process(log_dir: Path) -> Iterator[None]:
    Engine.forget_all()
    try:
        yield
    finally:
        pass
