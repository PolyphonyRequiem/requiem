"""Variant A demo — run all six required scenarios in one go.

Usage:  python demo.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from events import RunStarted  # noqa: F401  (import to ensure schema loads)
from store import CorruptLogError, Engine, EventStore, fresh_process


HERE = Path(__file__).parent
LOG_DIR = HERE / ".demo-runs"


def reset() -> None:
    if LOG_DIR.exists():
        shutil.rmtree(LOG_DIR)
    LOG_DIR.mkdir()


def banner(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def scenario_1_and_2_write_and_query() -> None:
    banner("Scenarios 1+2 — write the canonical event sequence, then query")
    eng = Engine.open("run-001", LOG_DIR)
    eng.append({"kind": "run_started", "run_id": "run-001",
                "root_id": 3401, "platform_project": "dev.azure.com/x/Twig",
                "created_by": "dangreen"})
    eng.append({"kind": "node_entered", "run_id": "run-001", "node": "preflight"})
    eng.append({"kind": "node_completed", "run_id": "run-001",
                "node": "preflight", "outcome": "Success"})
    eng.append({"kind": "mg_declared", "run_id": "run-001",
                "mg_id": "data-layer", "mg_path": "data-layer",
                "items": [4001, 4002]})
    eng.append({"kind": "mg_declared", "run_id": "run-001",
                "mg_id": "api", "mg_path": "api", "items": [4010]})
    eng.append({"kind": "plan_generation_bumped", "run_id": "run-001",
                "item_key": "root", "cause": "plan_pr_merged:#42",
                "pr_number": 42, "merge_commit": "abc123"})
    eng.append({"kind": "human_approval_recorded", "run_id": "run-001",
                "gate": "deep_nesting_depth_4", "approved_by": "dangreen",
                "detail": "approved for one level"})
    eng.append({"kind": "subworkflow_invoked", "run_id": "run-001",
                "parent_node": "implement", "sub_run_id": "sub-001",
                "workflow": "feature-pr"})
    eng.append({"kind": "subworkflow_completed", "run_id": "run-001",
                "sub_run_id": "sub-001", "outcome": "Success"})
    eng.append({"kind": "mg_retired", "run_id": "run-001",
                "mg_id": "data-layer", "reason": "superseded by replan"})
    eng.append({"kind": "node_entered", "run_id": "run-001", "node": "close-out"})
    eng.append({"kind": "run_ended", "run_id": "run-001", "outcome": "Success"})

    p = eng.projection
    print(f"  current node:                {p.current_node}")
    print(f"  is MG 'data-layer' retired?  {'data-layer' in p.retired_mg_ids}")
    print(f"  is MG 'api' retired?         {'api' in p.retired_mg_ids}")
    print(f"  approvals at deep_nesting:   "
          f"{[a for a in p.human_approvals if a[0] == 'deep_nesting_depth_4']}")
    print(f"  plan generations:            {p.plan_generations}")
    print(f"  run ended outcome:           {p.end_outcome}")


def scenario_3_restart() -> None:
    banner("Scenario 3 — engine restart (drop in-mem state, reopen, requery)")
    with fresh_process(LOG_DIR):
        eng = Engine.open("run-001", LOG_DIR)
        p = eng.projection
        assert p.current_node == "close-out", p.current_node
        assert "data-layer" in p.retired_mg_ids
        assert p.end_outcome == "Success"
        print("  ✓ restart produced identical projection from log replay")
        print(f"  ✓ last_event_id after replay: {p.last_event_id}")


def scenario_4_corruption() -> None:
    banner("Scenario 4 — corruption surfacing")
    # truncate the last event mid-line
    path = LOG_DIR / "run-001.events.jsonl"
    raw = path.read_bytes()
    keep = raw.rfind(b"\n", 0, len(raw) - 1) + 1
    # write everything up to and including the second-to-last newline,
    # then a truncated fragment of the last line.
    last_line = raw[keep:].rstrip(b"\n")
    bad = raw[:keep] + last_line[: max(1, len(last_line) // 2)]
    corrupt_path = LOG_DIR / "run-001-corrupt.events.jsonl"
    corrupt_path.write_bytes(bad)
    store = EventStore.__new__(EventStore)
    store.path = corrupt_path
    try:
        list(store.iter_raw())
    except CorruptLogError as e:
        print(f"  ✓ LOUD failure: {e}")
        print(f"    (operator can `head -c {e.byte_offset}` and see exactly where)")
    else:
        print("  ✗ expected CorruptLogError, did not raise")
        sys.exit(1)


def scenario_5_schema_evolution() -> None:
    banner("Scenario 5 — schema evolution (unknown kind, old runs still load)")
    # simulate "future Requiem" writing a new event kind directly to the log.
    path = LOG_DIR / "run-001.events.jsonl"
    import json
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "kind": "evidence_pr_opened",            # future kind we don't know yet
            "event_id": 999,
            "run_id": "run-001",
            "ts": "2026-06-01T00:00:00Z",
            "pr": 77, "branch": "evidence/run-001",
        }) + "\n")
    with fresh_process(LOG_DIR):
        eng = Engine.open("run-001", LOG_DIR)
        p = eng.projection
        assert p.unknown_kind_count == 1, p.unknown_kind_count
        print(f"  ✓ unknown_kind_count = {p.unknown_kind_count} (preserved, not silently dropped)")
        print(f"  ✓ known queries still answer: current_node = {p.current_node}, "
              f"ended = {p.ended}")
        print("  ✓ INV-NO-CORRUPT-FORWARD: unknown payload is COUNTED but not PROJECTED")


def scenario_6_isolation() -> None:
    banner("Scenario 6 — per-run isolation (two runs in the same process)")
    Engine.forget_all()
    a = Engine.open("run-iso-A", LOG_DIR)
    b = Engine.open("run-iso-B", LOG_DIR)
    a.append({"kind": "run_started", "run_id": "run-iso-A",
              "root_id": 1, "platform_project": "x", "created_by": "u"})
    a.append({"kind": "node_entered", "run_id": "run-iso-A", "node": "A-only"})
    b.append({"kind": "run_started", "run_id": "run-iso-B",
              "root_id": 2, "platform_project": "x", "created_by": "u"})
    b.append({"kind": "node_entered", "run_id": "run-iso-B", "node": "B-only"})
    assert a.projection.current_node == "A-only"
    assert b.projection.current_node == "B-only"
    assert a.projection.root_id == 1
    assert b.projection.root_id == 2
    print(f"  ✓ run-iso-A.current_node = {a.projection.current_node}")
    print(f"  ✓ run-iso-B.current_node = {b.projection.current_node}")
    print("  ✓ two distinct files, two distinct Engine instances, no cross-talk")


def main() -> int:
    reset()
    scenario_1_and_2_write_and_query()
    scenario_3_restart()
    scenario_4_corruption()
    scenario_5_schema_evolution()
    scenario_6_isolation()
    banner("VARIANT A — all 6 scenarios passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
