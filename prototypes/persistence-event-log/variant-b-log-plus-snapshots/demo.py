"""Variant B demo — log + periodic snapshots."""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

from store import (
    Engine,
    EventStore,
    SnapshotDivergenceError,
    fresh_process,
)


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
    banner("Scenarios 1+2 — write + query (snapshots written every 5 events)")
    eng = Engine.open("run-001", LOG_DIR)
    seed(eng, "run-001")
    p = eng.projection
    print(f"  current node:                {p.current_node}")
    print(f"  is MG 'data-layer' retired?  {'data-layer' in p.retired_mg_ids}")
    print(f"  approvals at deep_nesting:   "
          f"{[a for a in p.human_approvals if a[0] == 'deep_nesting_depth_4']}")
    snaps = sorted((LOG_DIR / 'run-001.snapshots').glob('*.json'))
    print(f"  snapshots written:           {len(snaps)} ({[s.name for s in snaps]})")


def scenario_3_restart() -> None:
    banner("Scenario 3 — restart loads latest snapshot + replays tail")
    t0 = time.perf_counter()
    with fresh_process(LOG_DIR):
        eng = Engine.open("run-001", LOG_DIR)
        p = eng.projection
        dt = (time.perf_counter() - t0) * 1000
        assert p.current_node == "close-out"
        assert "data-layer" in p.retired_mg_ids
        print(f"  ✓ restart latency:  {dt:.2f} ms")
        print(f"  ✓ same answers post-restart: current_node={p.current_node}, ended={p.ended}")


def scenario_4_corruption_truncate() -> None:
    banner("Scenario 4a — truncated last log line surfaces loudly")
    path = LOG_DIR / "run-001.events.jsonl"
    raw = path.read_bytes()
    keep = raw.rfind(b"\n", 0, len(raw) - 1) + 1
    bad = raw[:keep] + raw[keep:][: max(1, (len(raw) - keep) // 2)]
    p2 = LOG_DIR / "run-001-truncated.events.jsonl"
    p2.write_bytes(bad)
    store = EventStore.__new__(EventStore)
    store.log_path = p2
    store.snapshot_dir = LOG_DIR / "noop"
    store.snapshot_dir.mkdir(exist_ok=True)
    from store import CorruptLogError
    try:
        list(store.iter_raw())
    except CorruptLogError as e:
        print(f"  ✓ LOUD: {e}")
    else:
        print("  ✗ expected CorruptLogError"); sys.exit(1)


def scenario_4_corruption_divergence() -> None:
    banner("Scenario 4b — tampered snapshot disagrees with log → refuse to advance")
    snap_dir = LOG_DIR / "run-001.snapshots"
    snaps = sorted(snap_dir.glob("*.snapshot.json"))
    target = snaps[-1]
    body = json.loads(target.read_text(encoding="utf-8"))
    # forge a lie: pretend the retired MG was never retired
    body["projection"]["retired_mg_ids"] = []
    # NOTE: we deliberately leave the (now-stale) fingerprint untouched, but
    # the verifier re-derives from the log and compares pure-log fingerprint
    # to snapshot-anchored fingerprint, so the lie is caught either way.
    target.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")
    with fresh_process(LOG_DIR):
        try:
            Engine.open("run-001", LOG_DIR)
        except SnapshotDivergenceError as e:
            print(f"  ✓ LOUD: SnapshotDivergenceError raised on restart")
            print(f"    {str(e).splitlines()[0]}")
        else:
            print("  ✗ expected SnapshotDivergenceError"); sys.exit(1)


def scenario_5_schema_evolution() -> None:
    banner("Scenario 5 — schema evolution (unknown kind)")
    # restore the snapshot we tampered with so we can keep playing
    snap_dir = LOG_DIR / "run-001.snapshots"
    for f in snap_dir.glob("*.snapshot.json"):
        f.unlink()
    # rebuild snapshots from log fresh
    with fresh_process(LOG_DIR):
        eng = Engine.open("run-001", LOG_DIR)
        # force a snapshot
        eng.store.write_snapshot(eng.projection)
    # now append a future-kind event directly
    log = LOG_DIR / "run-001.events.jsonl"
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "kind": "evidence_pr_opened", "event_id": 9999,
            "run_id": "run-001", "ts": "2026-06-01T00:00:00Z",
            "pr": 77, "branch": "evidence/run-001",
        }) + "\n")
    with fresh_process(LOG_DIR):
        eng = Engine.open("run-001", LOG_DIR)
        p = eng.projection
        assert p.unknown_kind_count == 1
        print(f"  ✓ unknown_kind_count = {p.unknown_kind_count}; current_node = {p.current_node}")
        print("  ✓ snapshot from older schema + tail replay still loads")


def scenario_6_isolation() -> None:
    banner("Scenario 6 — per-run isolation")
    Engine.forget_all()
    a = Engine.open("iso-A", LOG_DIR)
    b = Engine.open("iso-B", LOG_DIR)
    a.append({"kind": "run_started", "run_id": "iso-A",
              "root_id": 1, "platform_project": "x", "created_by": "u"})
    a.append({"kind": "node_entered", "run_id": "iso-A", "node": "A-only"})
    b.append({"kind": "run_started", "run_id": "iso-B",
              "root_id": 2, "platform_project": "x", "created_by": "u"})
    b.append({"kind": "node_entered", "run_id": "iso-B", "node": "B-only"})
    assert a.projection.current_node == "A-only"
    assert b.projection.current_node == "B-only"
    assert (LOG_DIR / "iso-A.snapshots").exists()
    assert (LOG_DIR / "iso-B.snapshots").exists()
    print("  ✓ distinct logs + distinct snapshot dirs; no cross-talk")


def main() -> int:
    reset()
    scenario_1_and_2()
    scenario_3_restart()
    scenario_4_corruption_truncate()
    scenario_4_corruption_divergence()
    scenario_5_schema_evolution()
    scenario_6_isolation()
    banner("VARIANT B — all 6 scenarios passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
