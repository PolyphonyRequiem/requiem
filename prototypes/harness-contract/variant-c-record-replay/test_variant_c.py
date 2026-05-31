"""Variant C — CI test: every recording in `recordings/` must replay.

Plus a special INV-RESTART test that:
  1. Picks recording 04 (the "goal trace" for a successful tiny run).
  2. Drives the engine through `load`, kills it via ChaosHook.
  3. Restarts a fresh engine against the same on-disk event log,
     feeding it the recorded `agent_calls`.
  4. Asserts the final event stream matches the recorded "goal trace".

This is the answer to "how do you record/replay an INV-RESTART
scenario?" — you don't record the kill, you record the goal trace and
let the test framework drive the chaos.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _engine import (  # noqa: E402
    ChaosHook,
    EventLog,
    FakeProvider,
    KillRequested,
    WorkflowEngine,
    tiny_three_node,
)

from replayer import replay  # noqa: E402

RECORDINGS = sorted((Path(__file__).resolve().parent / "recordings").glob("*.yaml"))


@pytest.mark.parametrize("recording", RECORDINGS, ids=lambda p: p.stem)
def test_replay(recording: Path, tmp_path: Path):
    replay(recording, log_path=tmp_path / f"{recording.stem}.events.jsonl")


def test_inv_restart_with_recorded_goal_trace(tmp_path: Path):
    rec = yaml.safe_load((Path(__file__).resolve().parent / "recordings"
                          / "04_inv_restart_goal_trace.yaml").read_text(encoding="utf-8"))

    log_path = tmp_path / "restart.events.jsonl"
    log = EventLog(log_path)

    by_agent = {}
    for call in rec["agent_calls"]:
        by_agent.setdefault(call["agent"], []).append(call["reply"])

    fp = FakeProvider()
    for name, replies in by_agent.items():
        fp.script(name, *replies)

    chaos = ChaosHook(on_event=lambda e: (_ for _ in ()).throw(KillRequested())
                       if e.type == "NodeCompleted" and e.node == "load" else None)
    eng = WorkflowEngine(workflow=tiny_three_node(), provider=fp, event_log=log, chaos=chaos)

    with pytest.raises(KillRequested):
        eng.run(run_id=rec["run_id"])

    # Restart: fresh engine, fresh fake (same scripted replies — replays are
    # idempotent by design), same log path.
    fp2 = FakeProvider()
    for name, replies in by_agent.items():
        fp2.script(name, *replies)
    log2 = EventLog(log_path)
    eng2 = WorkflowEngine(workflow=tiny_three_node(), provider=fp2, event_log=log2)
    terminal = eng2.run(run_id=rec["run_id"])

    assert terminal == rec["expect"]["terminal"]
    # The post-restart trace is NOT byte-equal to the recording (it has an
    # extra RunResumed event) but the (type, node) sequence excluding
    # RunResumed must match.
    replay_fps = [(e.type, e.node) for e in log2 if e.type != "RunResumed"]
    recorded_fps = [(e["type"], e.get("node")) for e in rec["events"]]
    assert replay_fps == recorded_fps, (
        "Post-restart trace must converge on the recorded goal trace "
        f"(modulo RunResumed events).\n  recorded: {recorded_fps}\n  replay:   {replay_fps}"
    )
