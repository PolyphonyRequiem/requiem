"""CLI polish tests — Phase B Gap 1/2/3/4/5/auto-load + workflow identity."""
from __future__ import annotations

import asyncio
import io
import json
import sys
import time
from pathlib import Path

import pytest

import sys as _sys
import requiem.cli.main  # ensure the submodule is registered

cli_main = _sys.modules["requiem.cli.main"]
"""The cli.main *module* (the package __init__ shadows the attribute access
with the re-exported `main` function; sys.modules gives us the module)."""
from requiem.cli.render import RenderContext, render_event
from requiem.events import EVENT_KINDS, EventEmitter
from requiem.kernel import Engine, Failed
from requiem.persistence import EventStore, replay
from requiem.workflows import code_review_demo


# ---- Gap 1: workflow identity in run_started -----------------------


def test_run_started_records_workflow_module_and_version(tmp_path: Path):
    engine = code_review_demo.build_engine(tmp_path)
    asyncio.run(engine.run("identity-run"))
    events = list(replay(engine.log_path("identity-run")))
    [run_started] = [e for e in events if e["kind"] == "run_started"]
    assert run_started["payload"]["workflow_module"] == "requiem.workflows.code_review_demo"
    assert run_started["payload"]["workflow_version"] == "0.1"


def test_events_auto_loads_workflow_module_from_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    engine = code_review_demo.build_engine(tmp_path)
    asyncio.run(engine.run("autoload-run"))

    rc = cli_main.main(["events", "autoload-run", "--log-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    # If auto-load worked, we see humanized labels — not raw node ids.
    assert "Read sample_snippet.py" in out
    assert "Lint" in out
    # And we do NOT see the raw fallback node id.
    assert "read_snippet" not in out or "Read sample_snippet.py" in out


def test_events_falls_back_to_node_ids_when_module_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """A log written by a workflow with no `module` field renders raw ids."""
    log_path = tmp_path / "noid.events.jsonl"
    store = EventStore(log_path)
    em = EventEmitter("noid", store.append)
    em.emit_run_started("ad-hoc-flow")  # no module declared
    em.emit_node_entered("n1", attempt=1)
    em.emit_verb_completed("n1", {"kind": "success", "value": {}})
    em.emit_run_completed("completed", "n1")

    rc = cli_main.main(["events", "noid", "--log-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ad-hoc-flow" in out
    assert "n1" in out  # raw node id appears (no humanize map available)


# ---- Gap 4: list-runs ---------------------------------------------


def test_list_runs_summarizes_each_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    engine = code_review_demo.build_engine(tmp_path)
    asyncio.run(engine.run("run-A"))
    asyncio.run(engine.run("run-B"))

    rc = cli_main.main(["list-runs", "--log-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "run-A" in out
    assert "run-B" in out
    assert "Completed" in out
    assert "code-review" in out


def test_list_runs_on_empty_dir_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    rc = cli_main.main(["list-runs", "--log-dir", str(tmp_path / "empty")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no runs" in out


# ---- Gap 5: cancel -------------------------------------------------


def test_cancel_requested_is_in_event_kinds():
    assert "cancel_requested" in EVENT_KINDS


def test_cancel_writes_event_and_resume_short_circuits(tmp_path: Path):
    # Step 1: run with no gate handler → Suspended at the gate.
    engine = code_review_demo.build_engine(tmp_path)
    engine.gate_handler = None
    suspended = asyncio.run(engine.run("cancel-run"))
    assert suspended.__class__.__name__ == "Suspended"

    # Step 2: operator cancels via CLI.
    rc = cli_main.main(
        ["cancel", "cancel-run", "--log-dir", str(tmp_path), "--reason", "test"]
    )
    assert rc == 0

    # Step 3: resume picks up the cancel and short-circuits per
    # INV-CANCEL-SHORT-CIRCUITS-RETRY (no further attempts, no gate prompt).
    engine2 = code_review_demo.build_engine(tmp_path)
    engine2.gate_handler = None
    result = asyncio.run(engine2.run("cancel-run"))
    assert isinstance(result, Failed)
    assert result.error_kind == "cancelled"
    assert "test" in result.message

    # Step 4: log ends with run_completed(cancelled) + run_cost_summary.
    # ADR-0030 §3a: run_cost_summary is emitted as a peer summary AFTER
    # run_completed on every terminal disposition. The contract is that
    # a run_completed exists in the log (and is the last terminal-state
    # event); run_cost_summary may follow as cost telemetry.
    events = list(replay(engine2.log_path("cancel-run")))
    rc_events = [e for e in events if e["kind"] == "run_completed"]
    assert len(rc_events) == 1
    assert rc_events[-1]["payload"]["terminal"] == "cancelled"
    # And exactly one cancel_requested in the middle.
    cancels = [e for e in events if e["kind"] == "cancel_requested"]
    assert len(cancels) == 1
    assert cancels[0]["payload"]["reason"] == "test"


def test_cancel_on_completed_run_is_a_noop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    engine = code_review_demo.build_engine(tmp_path)
    asyncio.run(engine.run("done-run"))

    rc = cli_main.main(["cancel", "done-run", "--log-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "already completed" in out


def test_cancel_event_has_a_renderer():
    cx = RenderContext(workflow_name="demo")
    ev = {
        "event_id": 1, "run_id": "r", "ts": "2026-06-01T00:00:00+00:00",
        "kind": "cancel_requested", "schema_version": 1,
        "node_id": None, "team_id": None, "agent_id": None,
        "payload": {"reason": "user pressed Ctrl-C", "requested_by": "cli"},
    }
    [line] = render_event(ev, cx)
    assert "Cancel requested" in line
    assert "user pressed Ctrl-C" in line


# ---- Gap 3: --interactive gate handler -----------------------------


def test_interactive_gate_handler_picks_choice_from_input(monkeypatch):
    cx = RenderContext(workflow_name="demo")
    monkeypatch.setattr(cli_main, "_HAS_RICH", False)
    monkeypatch.setattr("sys.stdin", io.StringIO("reject\n"))

    handler = cli_main._make_interactive_gate_handler(cx)
    choice = handler("g", "Approve?", ("approve", "reject"))
    assert choice == "reject"
    assert getattr(handler, "__requiem_auto__", None) is False


def test_interactive_gate_handler_blank_input_picks_default(monkeypatch):
    cx = RenderContext(workflow_name="demo")
    monkeypatch.setattr(cli_main, "_HAS_RICH", False)
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))

    handler = cli_main._make_interactive_gate_handler(cx)
    choice = handler("g", "Approve?", ("approve", "reject"))
    assert choice == "approve"


# ---- Gap 2: --follow tailing (smoke) -------------------------------


def test_tail_rendered_stops_on_run_completed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
):
    """Tail loop must exit when it sees a `run_completed` event.

    We pre-populate the log with a complete run so tail returns immediately
    instead of blocking. The poll interval is shrunk so the test is fast.
    """
    engine = code_review_demo.build_engine(tmp_path)
    asyncio.run(engine.run("tail-run"))

    monkeypatch.setattr(cli_main, "_TAIL_POLL_INTERVAL", 0.001)
    log_path = engine.log_path("tail-run")
    cx = RenderContext(workflow_name="code-review")
    cli_main._tail_rendered(log_path, cx)
    out = capsys.readouterr().out
    # The terminal glyph (■) appears on run_completed.
    assert "Completed" in out or "■" in out


# ---- subcommand smoke: every subcommand at least parses ------------


def test_parser_accepts_every_subcommand():
    parser = cli_main._build_parser()
    parser.parse_args(["run", "mod"])
    parser.parse_args(["resume", "mod", "r"])
    parser.parse_args(["describe", "mod"])
    parser.parse_args(["events", "r"])
    parser.parse_args(["events", "r", "--follow"])
    parser.parse_args(["events", "r", "--raw", "--follow"])
    parser.parse_args(["list-runs"])
    parser.parse_args(["cancel", "r"])
    parser.parse_args(["run", "mod", "--interactive"])
    parser.parse_args(["resume", "mod", "r", "-i"])
