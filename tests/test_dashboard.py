"""Tests for the read-only web dashboard (ADR-0019, v0 non-negotiable #8).

Two layers:
- Pure projection tests (no socket): list_runs / run_detail / pending_gates fold
  the event log the same way the CLI's list-runs does.
- A thin server smoke test: bind an ephemeral port, hit the JSON API + page with
  urllib, assert shapes. No browser, no external deps.
"""
from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from requiem.dashboard import projection
from requiem.dashboard.server import build_server


# ---- log fixtures -------------------------------------------------------


def _write_run(log_dir: Path, run_id: str, events: list[dict]) -> Path:
    """Write a minimal *.events.jsonl with auto-assigned event_ids + ts."""
    path = log_dir / f"{run_id}.events.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for i, ev in enumerate(events):
            ev = dict(ev)
            ev.setdefault("event_id", i)
            ev.setdefault("run_id", run_id)
            ev.setdefault("ts", f"2026-06-09T10:00:{i:02d}+00:00")
            ev.setdefault("node_id", ev.get("node_id"))
            ev.setdefault("payload", ev.get("payload", {}))
            fh.write(json.dumps(ev) + "\n")
    return path


def _completed_run(log_dir, run_id="run-done", *, terminal="completed", final="end"):
    return _write_run(log_dir, run_id, [
        {"kind": "run_started", "payload": {"workflow": "demo", "workflow_version": "1"}},
        {"kind": "node_entered", "node_id": "start", "payload": {"attempt": 1}},
        {"kind": "verb_completed", "node_id": "start",
         "payload": {"outcome": {"kind": "success"}}},
        {"kind": "run_completed", "node_id": final,
         "payload": {"terminal": terminal, "final_node": final}},
    ])


def _suspended_run(log_dir, run_id="run-gate"):
    return _write_run(log_dir, run_id, [
        {"kind": "run_started", "payload": {"workflow": "needs-human-wf"}},
        {"kind": "node_entered", "node_id": "gate", "payload": {}},
        {"kind": "gate_opened", "node_id": "gate",
         "payload": {"prompt": "Approve the batch?", "options": ["approve", "abort"],
                     "auto": False}},
    ])


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs"
    d.mkdir()
    return d


# ---- list_runs ----------------------------------------------------------


def test_list_runs_empty_dir_returns_empty(tmp_path):
    assert projection.list_runs(tmp_path / "nope") == []


def test_list_runs_projects_status_and_workflow(log_dir):
    _completed_run(log_dir, "a")
    _suspended_run(log_dir, "b")
    runs = {r.run_id: r for r in projection.list_runs(log_dir)}
    assert runs["a"].status == "Completed"
    assert runs["a"].workflow == "demo"
    assert runs["a"].gate_open is False
    assert runs["b"].status == "Suspended"
    assert runs["b"].gate_open is True


def test_list_runs_status_variants(log_dir):
    _completed_run(log_dir, "ok", terminal="completed")
    _completed_run(log_dir, "bad", terminal="failed")
    _completed_run(log_dir, "cx", terminal="cancelled")
    _completed_run(log_dir, "nh", terminal="needs_human")
    runs = {r.run_id: r.status for r in projection.list_runs(log_dir)}
    assert runs["ok"] == "Completed"
    assert runs["bad"] == "Failed"
    assert runs["cx"] == "Cancelled"
    assert runs["nh"] == "Needs human"


def test_list_runs_sorted_newest_first(log_dir):
    _write_run(log_dir, "old", [
        {"kind": "run_started", "ts": "2026-06-01T00:00:00+00:00",
         "payload": {"workflow": "w"}}])
    _write_run(log_dir, "new", [
        {"kind": "run_started", "ts": "2026-06-09T00:00:00+00:00",
         "payload": {"workflow": "w"}}])
    order = [r.run_id for r in projection.list_runs(log_dir)]
    assert order == ["new", "old"]


# ---- run_detail ---------------------------------------------------------


def test_run_detail_absent_returns_none(log_dir):
    assert projection.run_detail(log_dir, "ghost") is None


def test_run_detail_humanizes_timeline(log_dir):
    _completed_run(log_dir, "a")
    detail = projection.run_detail(log_dir, "a")
    assert detail is not None
    assert detail.status == "Completed"
    assert detail.final_node == "end"
    kinds = [e.kind for e in detail.timeline]
    assert kinds == ["run_started", "node_entered", "verb_completed", "run_completed"]
    # every entry is humanized with a glyph + summary
    assert all(e.glyph for e in detail.timeline)
    assert any("run started" in e.summary for e in detail.timeline)


def test_run_detail_surfaces_open_gate(log_dir):
    _suspended_run(log_dir, "g")
    detail = projection.run_detail(log_dir, "g")
    assert detail.status == "Suspended"
    assert detail.gate is not None
    assert detail.gate["prompt"] == "Approve the batch?"
    assert detail.gate["options"] == ["approve", "abort"]


def test_run_detail_marks_corrupt_log(log_dir):
    path = log_dir / "torn.events.jsonl"
    path.write_text(
        json.dumps({"event_id": 0, "run_id": "torn", "kind": "run_started",
                    "ts": "2026-06-09T10:00:00+00:00", "payload": {"workflow": "w"}}) + "\n"
        + '{"event_id": 1, "kind": "node_ent',  # torn line
        encoding="utf-8",
    )
    detail = projection.run_detail(log_dir, "torn")
    assert detail.status == "Corrupt"
    assert detail.corrupt is not None


# ---- pending_gates ------------------------------------------------------


def test_pending_gates_lists_only_open(log_dir):
    _completed_run(log_dir, "done")
    _suspended_run(log_dir, "waiting")
    gates = projection.pending_gates(log_dir)
    assert [g.run_id for g in gates] == ["waiting"]
    assert gates[0].prompt == "Approve the batch?"
    assert gates[0].options == ["approve", "abort"]


def test_resolved_gate_is_not_pending(log_dir):
    _write_run(log_dir, "resolved", [
        {"kind": "run_started", "payload": {"workflow": "w"}},
        {"kind": "gate_opened", "node_id": "g",
         "payload": {"prompt": "?", "options": ["ok"]}},
        {"kind": "gate_resolved", "node_id": "g", "payload": {"choice": "ok"}},
    ])
    assert projection.pending_gates(log_dir) == []


# ---- server smoke (ephemeral port, urllib, no browser) ------------------


@pytest.fixture
def running_server(log_dir):
    _completed_run(log_dir, "srv-run")
    _suspended_run(log_dir, "srv-gate")
    httpd = build_server(log_dir, host="127.0.0.1", port=0)  # 0 = ephemeral
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    host, port = httpd.socket.getsockname()
    base = f"http://{host}:{port}"
    try:
        yield base
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read()


def test_server_serves_html_page(running_server):
    status, body = _get(running_server + "/")
    assert status == 200
    assert b"requiem dashboard" in body or b"<title>requiem" in body


def test_server_healthz(running_server):
    status, body = _get(running_server + "/healthz")
    assert status == 200
    assert json.loads(body)["ok"] is True


def test_server_api_runs(running_server):
    status, body = _get(running_server + "/api/runs")
    assert status == 200
    runs = json.loads(body)["runs"]
    ids = {r["run_id"]: r for r in runs}
    assert "srv-run" in ids and "srv-gate" in ids
    assert ids["srv-gate"]["gate_open"] is True


def test_server_api_run_detail(running_server):
    status, body = _get(running_server + "/api/runs/srv-run")
    assert status == 200
    detail = json.loads(body)
    assert detail["run_id"] == "srv-run"
    assert detail["status"] == "Completed"
    assert len(detail["timeline"]) == 4


def test_server_api_gates(running_server):
    status, body = _get(running_server + "/api/gates")
    assert status == 200
    gates = json.loads(body)["gates"]
    assert [g["run_id"] for g in gates] == ["srv-gate"]


def test_server_unknown_run_is_404(running_server):
    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as ei:
        _get(running_server + "/api/runs/does-not-exist")
    assert ei.value.code == 404


def test_server_rejects_path_traversal_run_id(running_server):
    import urllib.error
    # a slash in the run id can't reach the handler as a path segment, but a
    # backslash / encoded form must still 404 rather than touch the filesystem.
    with pytest.raises(urllib.error.HTTPError) as ei:
        _get(running_server + "/api/runs/..%5Cevil")
    assert ei.value.code == 404
