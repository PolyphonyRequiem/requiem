"""Variant C — SQLite (derived view) + JSONL (truth).

JSONL is the truth: a tampered or absent SQLite file is recoverable.
SQLite is the hot store the engine reads from for fast queries — every
write is append-to-JSONL-then-update-SQLite, in that order, under the
engine's lock.

Restart: open log, compare highest event_id in SQLite to highest in
JSONL. If they match, trust SQLite. If JSONL is ahead, replay the tail
into SQLite. If SQLite is ahead of JSONL: corruption — refuse to advance.

Per-run isolation: one SQLite file per run, one JSONL per run.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
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


class ViewAheadOfLogError(RuntimeError):
    """SQLite view contains an event_id the JSONL log does not. This means
    the log was rolled back / truncated without rebuilding the view. Refuse."""


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', '1');
INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('last_event_id', '-1');

CREATE TABLE IF NOT EXISTS run (
    run_id TEXT PRIMARY KEY,
    root_id INTEGER,
    platform_project TEXT,
    created_by TEXT,
    branch_model_version INTEGER,
    current_node TEXT,
    ended INTEGER NOT NULL DEFAULT 0,
    end_outcome TEXT
);

CREATE TABLE IF NOT EXISTS merge_group (
    run_id TEXT NOT NULL,
    mg_id TEXT NOT NULL,
    mg_path TEXT NOT NULL,
    parent_mg_path TEXT,
    items_json TEXT NOT NULL,
    nesting TEXT NOT NULL,
    isolation TEXT NOT NULL,
    retired INTEGER NOT NULL DEFAULT 0,
    retired_reason TEXT,
    PRIMARY KEY (run_id, mg_id)
);

CREATE TABLE IF NOT EXISTS plan_generation (
    run_id TEXT NOT NULL,
    item_key TEXT NOT NULL,
    generation INTEGER NOT NULL,
    PRIMARY KEY (run_id, item_key)
);

CREATE TABLE IF NOT EXISTS human_approval (
    run_id TEXT NOT NULL,
    event_id INTEGER NOT NULL,
    gate TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    detail TEXT,
    PRIMARY KEY (run_id, event_id)
);

CREATE TABLE IF NOT EXISTS sub_run (
    run_id TEXT NOT NULL,
    sub_run_id TEXT NOT NULL,
    workflow TEXT NOT NULL,
    outcome TEXT NOT NULL,        -- "running" or terminal outcome
    PRIMARY KEY (run_id, sub_run_id)
);

CREATE TABLE IF NOT EXISTS unknown_event (
    run_id TEXT NOT NULL,
    event_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    PRIMARY KEY (run_id, event_id)
);
"""


# ---------------------------------------------------------------------------
# Projection (return shape for tests / demos)
# ---------------------------------------------------------------------------


@dataclass
class MergeGroupRow:
    mg_id: str
    mg_path: str
    parent_mg_path: Optional[str]
    items: list[int]
    nesting: str
    isolation: str
    retired: bool
    retired_reason: Optional[str]


@dataclass
class Projection:
    run_id: Optional[str] = None
    root_id: Optional[int] = None
    platform_project: Optional[str] = None
    created_by: Optional[str] = None
    current_node: Optional[str] = None
    merge_groups: dict[str, MergeGroupRow] = field(default_factory=dict)
    retired_mg_ids: set[str] = field(default_factory=set)
    plan_generations: dict[str, int] = field(default_factory=dict)
    human_approvals: list[tuple[str, str, Optional[str]]] = field(default_factory=list)
    sub_runs: dict[str, str] = field(default_factory=dict)
    ended: bool = False
    end_outcome: Optional[str] = None
    last_event_id: int = -1
    unknown_kind_count: int = 0


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class Engine:
    _instances: dict[str, "Engine"] = {}
    _instances_lock = threading.Lock()

    def __init__(self, run_id: str, log_dir: Path):
        self.run_id = run_id
        self.log_path = log_dir / f"{run_id}.events.jsonl"
        self.view_path = log_dir / f"{run_id}.view.sqlite"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.view_path, isolation_level=None)
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(DDL)
            self._reconcile_on_open()
        except BaseException:
            # If anything in startup fails, release the file handle so the
            # operator (or the next scenario) can move/delete the view file.
            self._conn.close()
            raise

    @classmethod
    def open(cls, run_id: str, log_dir: Path) -> "Engine":
        with cls._instances_lock:
            key = f"{log_dir.resolve()}::{run_id}"
            if key not in cls._instances:
                cls._instances[key] = cls(run_id, log_dir)
            return cls._instances[key]

    @classmethod
    def forget_all(cls) -> None:
        with cls._instances_lock:
            for e in cls._instances.values():
                e._conn.close()
            cls._instances.clear()

    # ----- log helpers -----

    def _scan_log_max_id(self) -> int:
        if not self.log_path.exists():
            return -1
        last = -1
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
                last = max(last, int(obj.get("event_id", -1)))
        return last

    def _iter_log(self, after: int) -> Iterator[dict]:
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

    # ----- reconcile (the heart of "JSONL is truth") -----

    def _view_last_id(self) -> int:
        cur = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key='last_event_id'"
        )
        return int(cur.fetchone()[0])

    def _reconcile_on_open(self) -> None:
        log_max = self._scan_log_max_id()
        view_max = self._view_last_id()
        if view_max > log_max:
            raise ViewAheadOfLogError(
                f"SQLite view for run {self.run_id!r} is ahead of JSONL log: "
                f"view@{view_max}, log@{log_max}. Delete the view file and "
                f"restart to rebuild from the log."
            )
        if view_max < log_max:
            # Catch up the view by re-projecting tail events. INV-EVENT-LOG-AUTHORITATIVE:
            # SQLite gets rebuilt from JSONL, not the other way around.
            for raw in self._iter_log(after=view_max):
                self._project(raw)

    # ----- write path -----

    def append(self, payload: dict) -> int:
        with self._lock:
            # 1) write to JSONL first, fsync. The log is authoritative; the
            #    view is downstream and rebuildable.
            payload = dict(payload)
            payload["event_id"] = self._view_last_id() + 1
            line = json.dumps(payload, default=str, separators=(",", ":"))
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            # 2) update the view. If we crash between 1 and 2, _reconcile_on_open
            #    will catch up on next start.
            self._project(payload)
            return payload["event_id"]

    # ----- projection into SQLite -----

    def _project(self, raw: dict) -> None:
        kind = raw.get("kind")
        eid = int(raw["event_id"])
        run_id = raw["run_id"]
        if kind not in KNOWN_KINDS:
            self._conn.execute(
                "INSERT OR IGNORE INTO unknown_event(run_id, event_id, kind) VALUES (?,?,?)",
                (run_id, eid, str(kind)),
            )
            self._bump_last_id(eid)
            return
        try:
            ev = _event_adapter.validate_python(raw)
        except ValidationError as e:
            # Refuse to advance: typed event failed validation.
            raise CorruptLogError(self.log_path, -1, -1, str(e.errors()[:1])) from e

        c = self._conn
        k = ev.kind
        if k == "run_started":
            c.execute(
                "INSERT OR REPLACE INTO run (run_id, root_id, platform_project, "
                "created_by, branch_model_version, current_node, ended, end_outcome) "
                "VALUES (?,?,?,?,?,?,0,NULL)",
                (ev.run_id, ev.root_id, ev.platform_project, ev.created_by,
                 ev.branch_model_version, None),
            )
        elif k == "node_entered":
            c.execute("UPDATE run SET current_node=? WHERE run_id=?", (ev.node, ev.run_id))
        elif k == "node_completed":
            pass
        elif k == "mg_declared":
            c.execute(
                "INSERT OR REPLACE INTO merge_group (run_id, mg_id, mg_path, "
                "parent_mg_path, items_json, nesting, isolation, retired, retired_reason) "
                "VALUES (?,?,?,?,?,?,?,COALESCE((SELECT retired FROM merge_group "
                "  WHERE run_id=? AND mg_id=?),0),"
                "  (SELECT retired_reason FROM merge_group WHERE run_id=? AND mg_id=?))",
                (ev.run_id, ev.mg_id, ev.mg_path, ev.parent_mg_path,
                 json.dumps(list(ev.items)), ev.nesting, ev.isolation,
                 ev.run_id, ev.mg_id, ev.run_id, ev.mg_id),
            )
        elif k == "plan_generation_bumped":
            c.execute(
                "INSERT INTO plan_generation (run_id, item_key, generation) VALUES (?,?,1) "
                "ON CONFLICT(run_id, item_key) DO UPDATE SET generation = generation + 1",
                (ev.run_id, ev.item_key),
            )
        elif k == "human_approval_recorded":
            c.execute(
                "INSERT OR REPLACE INTO human_approval (run_id, event_id, gate, approved_by, detail) "
                "VALUES (?,?,?,?,?)",
                (ev.run_id, ev.event_id, ev.gate, ev.approved_by, ev.detail),
            )
        elif k == "mg_retired":
            c.execute(
                "UPDATE merge_group SET retired=1, retired_reason=? WHERE run_id=? AND mg_id=?",
                (ev.reason, ev.run_id, ev.mg_id),
            )
        elif k == "subworkflow_invoked":
            c.execute(
                "INSERT OR REPLACE INTO sub_run (run_id, sub_run_id, workflow, outcome) "
                "VALUES (?,?,?,'running')",
                (ev.run_id, ev.sub_run_id, ev.workflow),
            )
        elif k == "subworkflow_completed":
            c.execute(
                "UPDATE sub_run SET outcome=? WHERE run_id=? AND sub_run_id=?",
                (ev.outcome, ev.run_id, ev.sub_run_id),
            )
        elif k == "run_ended":
            c.execute(
                "UPDATE run SET ended=1, end_outcome=? WHERE run_id=?",
                (ev.outcome, ev.run_id),
            )
        self._bump_last_id(eid)

    def _bump_last_id(self, eid: int) -> None:
        cur = self._conn.execute("SELECT value FROM schema_meta WHERE key='last_event_id'")
        current = int(cur.fetchone()[0])
        if eid > current:
            self._conn.execute(
                "UPDATE schema_meta SET value=? WHERE key='last_event_id'", (str(eid),)
            )

    # ----- read path -----

    @property
    def projection(self) -> Projection:
        c = self._conn
        proj = Projection(run_id=self.run_id, last_event_id=self._view_last_id())
        row = c.execute(
            "SELECT root_id, platform_project, created_by, current_node, ended, end_outcome "
            "FROM run WHERE run_id=?", (self.run_id,)
        ).fetchone()
        if row:
            (proj.root_id, proj.platform_project, proj.created_by,
             proj.current_node, ended, proj.end_outcome) = row
            proj.ended = bool(ended)
        for r in c.execute(
            "SELECT mg_id, mg_path, parent_mg_path, items_json, nesting, isolation, "
            "retired, retired_reason FROM merge_group WHERE run_id=?", (self.run_id,)
        ):
            mg = MergeGroupRow(
                mg_id=r[0], mg_path=r[1], parent_mg_path=r[2],
                items=json.loads(r[3]), nesting=r[4], isolation=r[5],
                retired=bool(r[6]), retired_reason=r[7],
            )
            proj.merge_groups[mg.mg_id] = mg
            if mg.retired:
                proj.retired_mg_ids.add(mg.mg_id)
        for r in c.execute(
            "SELECT item_key, generation FROM plan_generation WHERE run_id=?", (self.run_id,)
        ):
            proj.plan_generations[r[0]] = int(r[1])
        for r in c.execute(
            "SELECT gate, approved_by, detail FROM human_approval "
            "WHERE run_id=? ORDER BY event_id", (self.run_id,)
        ):
            proj.human_approvals.append((r[0], r[1], r[2]))
        for r in c.execute(
            "SELECT sub_run_id, outcome FROM sub_run WHERE run_id=?", (self.run_id,)
        ):
            proj.sub_runs[r[0]] = r[1]
        (count,) = c.execute(
            "SELECT COUNT(*) FROM unknown_event WHERE run_id=?", (self.run_id,)
        ).fetchone()
        proj.unknown_kind_count = int(count)
        return proj

    # ----- targeted fast queries (the reason this variant exists) -----

    def is_mg_retired(self, mg_id: str) -> bool:
        row = self._conn.execute(
            "SELECT retired FROM merge_group WHERE run_id=? AND mg_id=?",
            (self.run_id, mg_id),
        ).fetchone()
        return bool(row and row[0])

    def current_node(self) -> Optional[str]:
        row = self._conn.execute(
            "SELECT current_node FROM run WHERE run_id=?", (self.run_id,)
        ).fetchone()
        return row[0] if row else None


@contextmanager
def fresh_process(log_dir: Path) -> Iterator[None]:
    """Simulate a process restart: close every SQLite handle and drop
    every cached Engine. Connections are also closed on exit so callers
    can safely mutate underlying files (e.g. to simulate corruption)
    immediately after the block."""
    Engine.forget_all()
    try:
        yield
    finally:
        Engine.forget_all()
