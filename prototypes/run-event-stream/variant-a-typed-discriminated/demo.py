"""Variant A demo. Run from this directory:

    python demo.py

Exercises all six required behaviours:
  1. emits 6 distinct kinds
  2. atomic append to a real .events.jsonl
  3. tail reader yields typed events as they arrive
  4. replay-to-state from log alone
  5. schema evolution (v2 adds RetryAttempted)
  6. corruption surfaces and HALTS the derive
"""

from __future__ import annotations

import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from events import (  # noqa: E402
    SCHEMA_VERSION,
    GateOpened,
    NodeEntered,
    RetryAttempted,
    RunCompleted,
    RunStarted,
    VerbCompleted,
    VerbInvoked,
    parse_v1,
    parse_v2,
)
from outcomes import NeedsHuman, Success  # noqa: E402
from reader import CorruptLine, read_all, tail  # noqa: E402
from state import CorruptionDetected, derive  # noqa: E402
from writer import EventWriter  # noqa: E402


RUN_DIR = HERE / "_run"
RUN_DIR.mkdir(exist_ok=True)


def now() -> datetime:
    return datetime.now(tz=timezone.utc)


def banner(label: str) -> None:
    print(f"\n=== {label} ===")


def main() -> None:
    log_path = RUN_DIR / f"{uuid.uuid4().hex[:8]}.events.jsonl"
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    print(f"variant-a (typed discriminated, schema_version={SCHEMA_VERSION})")
    print(f"log: {log_path}")

    # ---- 1+2: emit 6 distinct kinds to a real .events.jsonl --------------
    banner("emit (v1)")
    writer = EventWriter(log_path)
    eid = iter(range(1, 1_000_000))

    def emit(ev: Any) -> None:
        writer.append(ev)
        print(f"  + {ev.event_type:14s}  {ev.model_dump_json()[:90]}…")

    emit(RunStarted(event_id=next(eid), run_id=run_id, ts=now(),
                    workflow="close-out", workflow_version="0.1.0"))
    emit(NodeEntered(event_id=next(eid), run_id=run_id, ts=now(),
                     node_path="/root/load-guidance", node_kind="verb"))
    emit(VerbInvoked(event_id=next(eid), run_id=run_id, ts=now(),
                     node_path="/root/load-guidance",
                     verb="plan.load_guidance", args_digest="sha256:abc123"))
    emit(VerbCompleted(event_id=next(eid), run_id=run_id, ts=now(),
                       node_path="/root/load-guidance",
                       verb="plan.load_guidance",
                       outcome=Success(value={"guidance_id": "g-42"})))
    emit(GateOpened(event_id=next(eid), run_id=run_id, ts=now(),
                    node_path="/root/await-human",
                    gate_id="gate-1", prompt="approve close-out summary"))
    emit(RunCompleted(event_id=next(eid), run_id=run_id, ts=now(),
                      terminal="completed"))

    # ---- 3: tail reader (concurrent producer + consumer) -----------------
    banner("tail (concurrent producer + consumer)")
    tail_path = RUN_DIR / f"{uuid.uuid4().hex[:8]}.events.jsonl"
    tail_writer = EventWriter(tail_path)
    collected: list[Any] = []
    stop_flag = threading.Event()

    def consumer() -> None:
        for ev in tail(tail_path, parse_v1, stop=stop_flag.is_set):
            collected.append(ev)
            label = getattr(ev, "event_type", type(ev).__name__)
            print(f"  tail<- {label}")

    t = threading.Thread(target=consumer, daemon=True)
    t.start()
    teid = iter(range(1, 100))
    for ev in (
        RunStarted(event_id=next(teid), run_id="r2", ts=now(),
                   workflow="tail-demo", workflow_version="0.1.0"),
        NodeEntered(event_id=next(teid), run_id="r2", ts=now(),
                    node_path="/root", node_kind="verb"),
        VerbInvoked(event_id=next(teid), run_id="r2", ts=now(),
                    verb="noop", args_digest="sha256:0"),
        VerbCompleted(event_id=next(teid), run_id="r2", ts=now(),
                      verb="noop",
                      outcome=NeedsHuman(prompt="confirm")),
        RunCompleted(event_id=next(teid), run_id="r2", ts=now(),
                     terminal="surrendered"),
    ):
        tail_writer.append(ev)
    tail_writer.close()
    stop_flag.set()
    t.join(timeout=2.0)
    print(f"  tail collected: {len(collected)} events")

    # ---- 4: replay-to-state ---------------------------------------------
    banner("derive RunState from log alone")
    state = derive(read_all(log_path, parse_v1))
    print(f"  {state}")

    # ---- 5: schema evolution (v2 adds RetryAttempted) -------------------
    banner("schema evolution: append a v2-only kind")
    writer.append(
        RetryAttempted(event_id=next(eid), run_id=run_id, ts=now(),
                       node_path="/root/retry-node",
                       verb="plan.load_guidance", attempt=2, of=3, delay_ms=400)
    )
    writer.close()

    print("  v1 reader replay (must surface unknown kind without crashing):")
    v1_state = derive(read_all(log_path, parse_v1))
    print(f"    {v1_state}")
    assert v1_state.unknown_kinds_seen == ["retry_attempted"], v1_state

    print("  v2 reader replay (must understand the new kind):")
    v2_state = derive(read_all(log_path, parse_v2))
    print(f"    {v2_state}")
    assert v2_state.retries == 1 and not v2_state.unknown_kinds_seen, v2_state

    # ---- 6: corruption surfaces and halts derive ------------------------
    banner("corruption: truncated JSON line at end of file")
    corrupt = EventWriter(log_path)
    corrupt.append_raw('{"event_type": "node_entered", "event_id": 99, "run_id":')  # no \n, no close
    corrupt.append_raw("\n")
    corrupt.close()

    print("  derive should HALT (not silently skip):")
    try:
        derive(read_all(log_path, parse_v2))
    except CorruptionDetected as exc:
        print(f"    HALTED as required: {exc}")
    else:
        raise SystemExit("BUG: corruption was silently skipped")

    print("\n[variant-a] OK")


if __name__ == "__main__":
    main()
