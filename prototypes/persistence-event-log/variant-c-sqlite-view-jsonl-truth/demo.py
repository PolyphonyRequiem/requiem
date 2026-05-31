"""Variant C demo — SQLite view + JSONL truth."""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

from store import CorruptLogError, Engine, ViewAheadOfLogError, fresh_process


HERE = Path(__file__).parent
LOG_DIR = HERE / ".demo-runs"


def reset() -> None:
    if LOG_DIR.exists():
        shutil.rmtree(LOG_DIR)
    LOG_DIR.mkdir()


def banner(t: str) -> None:
    print(); print("=" * 72); print(f"  {t}"); print("=" * 72)


def seed(eng: Engine, run_id: str) -> None:
    eng.append({"kind": "run_started", "run_id": run_id,
                "root_id": 3401, "platform_project": "x", "created_by": "u"})
    eng.append({"kind": "node_entered", "run_id": run_id, "node": "preflight"})
    eng.append({"kind": "node_completed", "run_id": run_id, "node": "preflight", "outcome": "Success"})
    eng.append({"kind": "mg_declared", "run_id": run_id, "mg_id": "data-layer",
                "mg_path": "data-layer", "items": [4001]})
    eng.append({"kind": "mg_declared", "run_id": run_id, "mg_id": "api",
                "mg_path": "api", "items": [4010]})
    eng.append({"kind": "plan_generation_bumped", "run_id": run_id,
                "item_key": "root", "cause": "pr:#42", "pr_number": 42, "merge_commit": "abc"})
    eng.append({"kind": "human_approval_recorded", "run_id": run_id,
                "gate": "deep_nesting_depth_4", "approved_by": "dangreen", "detail": "ok"})
    eng.append({"kind": "subworkflow_invoked", "run_id": run_id,
                "parent_node": "implement", "sub_run_id": "sub-1", "workflow": "feature-pr"})
    eng.append({"kind": "subworkflow_completed", "run_id": run_id, "sub_run_id": "sub-1", "outcome": "Success"})
    eng.append({"kind": "mg_retired", "run_id": run_id, "mg_id": "data-layer", "reason": "replan"})
    eng.append({"kind": "node_entered", "run_id": run_id, "node": "close-out"})
    eng.append({"kind": "run_ended", "run_id": run_id, "outcome": "Success"})


def scenario_1_and_2() -> None:
    banner("Scenarios 1+2 — write events, query SQLite directly")
    eng = Engine.open("run-001", LOG_DIR)
    seed(eng, "run-001")
    print(f"  current node:                 {eng.current_node()}  (single SQL lookup)")
    print(f"  is MG 'data-layer' retired?   {eng.is_mg_retired('data-layer')}  (indexed)")
    print(f"  is MG 'api' retired?          {eng.is_mg_retired('api')}")
    p = eng.projection
    print(f"  approvals at deep_nesting:    "
          f"{[a for a in p.human_approvals if a[0] == 'deep_nesting_depth_4']}")


def scenario_3_restart() -> None:
    banner("Scenario 3 — restart with SQLite already up-to-date → no replay")
    t0 = time.perf_counter()
    with fresh_process(LOG_DIR):
        eng = Engine.open("run-001", LOG_DIR)
        dt = (time.perf_counter() - t0) * 1000
        assert eng.current_node() == "close-out"
        assert eng.is_mg_retired("data-layer")
        print(f"  ✓ restart latency:  {dt:.2f} ms")
        print(f"  ✓ same answers: current_node={eng.current_node()}")


def scenario_3b_crash_between_log_and_view() -> None:
    banner("Scenario 3b — crash between log-append and view-update → tail replay")
    # simulate the engine appending an event to the log but dying before
    # the SQLite UPDATE.
    log = LOG_DIR / "run-001.events.jsonl"
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "kind": "node_entered", "run_id": "run-001",
            "event_id": 12, "ts": "2026-06-01T00:00:01Z", "node": "post-crash",
        }) + "\n")
    with fresh_process(LOG_DIR):
        eng = Engine.open("run-001", LOG_DIR)
        # reconciliation must have caught up
        assert eng.current_node() == "post-crash", eng.current_node()
        print(f"  ✓ view caught up from log: current_node = {eng.current_node()}")


def scenario_4_corruption_truncate() -> None:
    banner("Scenario 4a — truncated log line surfaces loudly on next open")
    log = LOG_DIR / "run-001.events.jsonl"
    raw = log.read_bytes()
    # corrupt by writing a partial line; back up the good log first
    backup = LOG_DIR / "run-001.events.jsonl.bak"
    backup.write_bytes(raw)
    keep = raw.rfind(b"\n", 0, len(raw) - 1) + 1
    bad = raw[:keep] + b'{"kind":"node_entered","event_id":9999'  # truncated
    log.write_bytes(bad)
    # also delete the view so we don't trip view-ahead-of-log first
    view = LOG_DIR / "run-001.view.sqlite"
    if view.exists():
        view.unlink()
    for ext in ("-wal", "-shm"):
        sidecar = LOG_DIR / f"run-001.view.sqlite{ext}"
        if sidecar.exists():
            sidecar.unlink()
    with fresh_process(LOG_DIR):
        try:
            Engine.open("run-001", LOG_DIR)
        except CorruptLogError as e:
            print(f"  ✓ LOUD: {e}")
        else:
            print("  ✗ expected CorruptLogError"); sys.exit(1)
    # restore the good log + view for subsequent scenarios
    log.write_bytes(raw)


def scenario_4b_view_ahead_of_log() -> None:
    banner("Scenario 4b — view ahead of log (log rolled back) → refuse to advance")
    # rebuild a clean state first
    with fresh_process(LOG_DIR):
        eng = Engine.open("run-001", LOG_DIR)
        baseline = eng.current_node()
        assert baseline is not None
    # now simulate: someone restored an old log but kept the new SQLite
    log = LOG_DIR / "run-001.events.jsonl"
    raw = log.read_bytes()
    # drop the last two events from the log only
    lines = raw.split(b"\n")
    truncated = b"\n".join(lines[:-3]) + b"\n"
    log.write_bytes(truncated)
    with fresh_process(LOG_DIR):
        try:
            Engine.open("run-001", LOG_DIR)
        except ViewAheadOfLogError as e:
            print(f"  ✓ LOUD: ViewAheadOfLogError raised")
            print(f"    {e}")
        else:
            print("  ✗ expected ViewAheadOfLogError"); sys.exit(1)
    # restore log
    log.write_bytes(raw)


def scenario_5_schema_evolution() -> None:
    banner("Scenario 5 — schema evolution: new event kind, old DB still loads")
    # reset to a clean reconciled state for the run, then drop SQLite to
    # force the loader to rebuild the view purely from the (now extended) log.
    log = LOG_DIR / "run-001.events.jsonl"
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "kind": "evidence_pr_opened", "event_id": 13,
            "run_id": "run-001", "ts": "2026-06-01T00:00:02Z",
            "pr": 77, "branch": "evidence/run-001",
        }) + "\n")
    view = LOG_DIR / "run-001.view.sqlite"
    if view.exists():
        view.unlink()
    for ext in ("-wal", "-shm"):
        sidecar = LOG_DIR / f"run-001.view.sqlite{ext}"
        if sidecar.exists():
            sidecar.unlink()
    with fresh_process(LOG_DIR):
        eng = Engine.open("run-001", LOG_DIR)
        p = eng.projection
        assert p.unknown_kind_count >= 1
        print(f"  ✓ unknown_kind_count = {p.unknown_kind_count}; current_node = {eng.current_node()}")
        print("  ✓ old DB schema rebuilt from log including future-kind events")


def scenario_6_isolation() -> None:
    banner("Scenario 6 — per-run isolation (distinct SQLite + JSONL per run)")
    Engine.forget_all()
    a = Engine.open("iso-A", LOG_DIR)
    b = Engine.open("iso-B", LOG_DIR)
    a.append({"kind": "run_started", "run_id": "iso-A",
              "root_id": 1, "platform_project": "x", "created_by": "u"})
    a.append({"kind": "node_entered", "run_id": "iso-A", "node": "A-only"})
    b.append({"kind": "run_started", "run_id": "iso-B",
              "root_id": 2, "platform_project": "x", "created_by": "u"})
    b.append({"kind": "node_entered", "run_id": "iso-B", "node": "B-only"})
    assert a.current_node() == "A-only"
    assert b.current_node() == "B-only"
    assert (LOG_DIR / "iso-A.view.sqlite").exists()
    assert (LOG_DIR / "iso-B.view.sqlite").exists()
    print("  ✓ distinct logs + distinct SQLite files; no cross-talk")


def main() -> int:
    reset()
    scenario_1_and_2()
    scenario_3_restart()
    scenario_3b_crash_between_log_and_view()
    scenario_4_corruption_truncate()
    scenario_4b_view_ahead_of_log()
    scenario_5_schema_evolution()
    scenario_6_isolation()
    banner("VARIANT C — all 6 scenarios passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
