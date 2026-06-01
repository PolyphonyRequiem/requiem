import json
from pathlib import Path

import pytest

from requiem.persistence import CorruptLogError, EventStore, replay


def test_append_assigns_ids(tmp_path: Path):
    store = EventStore(tmp_path / "run.jsonl")
    assert store.append({"kind": "x", "run_id": "r", "payload": {}}) == 0
    assert store.append({"kind": "y", "run_id": "r", "payload": {}}) == 1
    rows = list(replay(tmp_path / "run.jsonl"))
    assert [r["event_id"] for r in rows] == [0, 1]


def test_replay_empty_file_yields_nothing(tmp_path: Path):
    assert list(replay(tmp_path / "missing.jsonl")) == []


def test_corrupt_log_halts_replay(tmp_path: Path):
    p = tmp_path / "broken.jsonl"
    p.write_text(json.dumps({"event_id": 0, "kind": "ok", "payload": {}}) + "\n"
                 + "not-json\n", encoding="utf-8")
    with pytest.raises(CorruptLogError) as ei:
        list(replay(p))
    assert ei.value.line_no == 2


def test_store_recovers_next_id_after_restart(tmp_path: Path):
    s1 = EventStore(tmp_path / "r.jsonl")
    s1.append({"kind": "a", "run_id": "r", "payload": {}})
    s1.append({"kind": "b", "run_id": "r", "payload": {}})
    s2 = EventStore(tmp_path / "r.jsonl")
    assert s2.append({"kind": "c", "run_id": "r", "payload": {}}) == 2
