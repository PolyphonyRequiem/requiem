"""Variant B demo. Run from this directory:

    python demo.py
"""

from __future__ import annotations

import sys
import threading
import uuid
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from events import (  # noqa: E402
    PAYLOAD_REGISTRY_V1,
    PAYLOAD_REGISTRY_V2,
    SCHEMA_VERSION,
    Event,
    make_typed_parser,
    now,
)
from reader import CorruptLine, read_all, tail  # noqa: E402
from state import CorruptionDetected, derive  # noqa: E402
from writer import EventWriter  # noqa: E402

RUN_DIR = HERE / "_run"
RUN_DIR.mkdir(exist_ok=True)

parse_v1 = make_typed_parser(PAYLOAD_REGISTRY_V1)
parse_v2 = make_typed_parser(PAYLOAD_REGISTRY_V2)


def banner(label: str) -> None:
    print(f"\n=== {label} ===")


def ev(eid: int, run_id: str, kind: str, payload: dict, node_path: str | None = None) -> Event:
    return Event(
        event_id=eid, run_id=run_id, ts=now(), kind=kind,
        schema_version=SCHEMA_VERSION, node_path=node_path, payload=payload,
    )


def main() -> None:
    log_path = RUN_DIR / f"{uuid.uuid4().hex[:8]}.events.jsonl"
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    print(f"variant-b (envelope-loose, schema_version={SCHEMA_VERSION})")
    print(f"log: {log_path}")

    # ---- 1+2: emit 6 distinct kinds --------------------------------------
    banner("emit (v1)")
    w = EventWriter(log_path)
    eid = iter(range(1, 1_000_000))

    def emit(e: Event) -> None:
        w.append(e)
        print(f"  + {e.kind:14s}  {e.model_dump_json()[:100]}…")

    emit(ev(next(eid), run_id, "run_started",
            {"workflow": "close-out", "workflow_version": "0.1.0"}))
    emit(ev(next(eid), run_id, "node_entered",
            {"node_kind": "verb"}, node_path="/root/load-guidance"))
    emit(ev(next(eid), run_id, "verb_invoked",
            {"verb": "plan.load_guidance", "args_digest": "sha256:abc123"},
            node_path="/root/load-guidance"))
    emit(ev(next(eid), run_id, "verb_completed",
            {"verb": "plan.load_guidance",
             "outcome": {"kind": "success", "value": {"guidance_id": "g-42"}}},
            node_path="/root/load-guidance"))
    emit(ev(next(eid), run_id, "gate_opened",
            {"gate_id": "gate-1", "prompt": "approve close-out summary"},
            node_path="/root/await-human"))
    emit(ev(next(eid), run_id, "run_completed", {"terminal": "completed"}))

    # ---- 3: tail reader --------------------------------------------------
    banner("tail (concurrent producer + consumer)")
    tail_path = RUN_DIR / f"{uuid.uuid4().hex[:8]}.events.jsonl"
    tw = EventWriter(tail_path)
    collected: list[Any] = []
    stop_flag = threading.Event()

    def consumer() -> None:
        for x in tail(tail_path, parse_v1, stop=stop_flag.is_set):
            collected.append(x)
            kind = getattr(getattr(x, "envelope", None), "kind", type(x).__name__)
            print(f"  tail<- {kind}")

    t = threading.Thread(target=consumer, daemon=True)
    t.start()
    teid = iter(range(1, 100))
    for e in (
        ev(next(teid), "r2", "run_started", {"workflow": "tail-demo", "workflow_version": "0.1.0"}),
        ev(next(teid), "r2", "node_entered", {"node_kind": "verb"}, node_path="/root"),
        ev(next(teid), "r2", "verb_invoked", {"verb": "noop", "args_digest": "sha256:0"}),
        ev(next(teid), "r2", "verb_completed",
           {"verb": "noop", "outcome": {"kind": "needs_human", "prompt": "confirm"}}),
        ev(next(teid), "r2", "run_completed", {"terminal": "surrendered"}),
    ):
        tw.append(e)
    tw.close()
    stop_flag.set()
    t.join(timeout=2.0)
    print(f"  tail collected: {len(collected)} events")

    # ---- 4: replay-to-state ---------------------------------------------
    banner("derive RunState from log alone")
    state = derive(read_all(log_path, parse_v1))
    print(f"  {state}")

    # ---- 5: schema evolution --------------------------------------------
    banner("schema evolution: append a v2-only kind")
    w.append(ev(next(eid), run_id, "retry_attempted",
                {"verb": "plan.load_guidance", "attempt": 2, "of": 3, "delay_ms": 400},
                node_path="/root/retry-node"))
    w.close()

    print("  v1 reader replay (envelope decodes, payload unknown):")
    v1_state = derive(read_all(log_path, parse_v1))
    print(f"    {v1_state}")
    assert v1_state.unknown_kinds_seen == ["retry_attempted"], v1_state

    print("  v2 reader replay (kind now in registry):")
    v2_state = derive(read_all(log_path, parse_v2))
    print(f"    {v2_state}")
    assert v2_state.retries == 1 and not v2_state.unknown_kinds_seen, v2_state

    # ---- 6: corruption ---------------------------------------------------
    banner("corruption: truncated JSON line at end of file")
    c = EventWriter(log_path)
    c.append_raw('{"event_id": 99, "run_id": "x", "ts": "2026-01-01T00:00:00Z", "kind": "noop"')
    c.append_raw("\n")
    c.close()

    print("  derive should HALT:")
    try:
        derive(read_all(log_path, parse_v2))
    except CorruptionDetected as exc:
        print(f"    HALTED as required: {exc}")
    else:
        raise SystemExit("BUG: corruption was silently skipped")

    print("\n[variant-b] OK")


if __name__ == "__main__":
    main()
