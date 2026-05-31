"""Variant C — replay-side.

Reads a YAML recording, builds a `FakeProvider` from `agent_calls`,
a gate handler from `gate_decisions`, and runs the engine. After the
run completes, asserts the resulting event stream is a superset-by-
prefix of the recorded events (so the replay didn't drift)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _engine import EventLog, FakeProvider, WorkflowEngine, examples  # noqa: E402

WORKFLOWS = {
    "tiny_three_node": lambda: examples.tiny_three_node(),
    "transient_failure_workflow": lambda: examples.transient_failure_workflow(fail_times=2),
    "gated_workflow": lambda: examples.gated_workflow(),
    "parent_with_subworkflow": lambda: examples.parent_with_subworkflow(),
}


def replay(recording_path: Path, log_path: Path | None = None) -> dict[str, Any]:
    rec = yaml.safe_load(recording_path.read_text(encoding="utf-8"))
    built = WORKFLOWS[rec["workflow"]]()
    workflow, _attempts = built if isinstance(built, tuple) else (built, None)

    fp = FakeProvider()
    # Replays may include a child agent; route by agent name only — sub-workflow
    # scoping is preserved because we wire `subworkflow_provider_for` to the same
    # FakeProvider (every agent name in the recording is unique by construction
    # of the parent/child workflows we ship today; recordings of workflows that
    # collide on agent names must declare `agent_scope` per call — see open
    # questions in README).
    by_agent: dict[str, list[dict[str, Any]]] = {}
    for call in rec.get("agent_calls", []):
        by_agent.setdefault(call["agent"], []).append(call["reply"])
    for name, replies in by_agent.items():
        fp.script(name, *replies)

    gate_decisions = list(rec.get("gate_decisions", []))

    def gate_handler(name: str, options: list[str]):
        for d in gate_decisions:
            if d["gate"] == name:
                gate_decisions.remove(d)
                return d["value"], d.get("additional_input") or {}
        raise RuntimeError(f"Replay: no recorded decision for gate {name!r}")

    log_path = log_path or recording_path.parent / f".{recording_path.stem}.replay.events.jsonl"
    if log_path.exists():
        log_path.unlink()
    log = EventLog(log_path)

    engine = WorkflowEngine(
        workflow=workflow,
        provider=fp,
        event_log=log,
        gate_handler=gate_handler if rec.get("gate_decisions") else None,
        subworkflow_provider_for=lambda _name: fp,
    )
    terminal = engine.run(run_id=rec["run_id"], inputs=rec.get("inputs"))

    expected_terminal = rec.get("expect", {}).get("terminal")
    if expected_terminal is not None:
        assert terminal == expected_terminal, (
            f"replay drift: terminal {terminal!r} != recorded {expected_terminal!r}"
        )

    # Drift check: every (type, node) tuple in the recording must appear in
    # the replay in the same order. We compare only on type+node because
    # event_ids and timestamps differ; this is the spiritual equivalent of
    # VCR's "match_on" filter.
    def fingerprint(evt) -> tuple:
        if isinstance(evt, dict):
            return (evt["type"], evt.get("node"))
        return (evt.type, evt.node)

    recorded_fps = [fingerprint(e) for e in rec.get("events", [])]
    replay_fps = [fingerprint(e) for e in log]
    if recorded_fps != replay_fps:
        raise AssertionError(
            "Replay drift detected.\n"
            f"  recorded: {recorded_fps}\n"
            f"  replay:   {replay_fps}"
        )

    return {"terminal": terminal, "events": len(replay_fps)}


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if target is None:
        here = Path(__file__).resolve().parent / "recordings"
        for rec in sorted(here.glob("*.yaml")):
            print(f"=== {rec.name} ===")
            print(replay(rec))
    else:
        print(replay(target))
