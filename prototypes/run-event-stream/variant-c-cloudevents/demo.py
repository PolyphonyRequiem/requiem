"""Variant C demo. Run from this directory:

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
    SCHEMA_VERSION,
    SOURCE_PREFIX,
    TYPE_PREFIX,
    TYPE_REGISTRY_V1,
    TYPE_REGISTRY_V2,
    CloudEvent,
    make_id,
    make_parser,
    now,
    schema_uri,
)
from reader import CorruptLine, read_all, tail  # noqa: E402
from state import CorruptionDetected, derive  # noqa: E402
from writer import EventWriter  # noqa: E402


RUN_DIR = HERE / "_run"
RUN_DIR.mkdir(exist_ok=True)

parse_v1 = make_parser(TYPE_REGISTRY_V1)
parse_v2 = make_parser(TYPE_REGISTRY_V2)


def banner(label: str) -> None:
    print(f"\n=== {label} ===")


def ce(run_id: str, eid: int, type_short: str, data: dict) -> CloudEvent:
    type_ = f"{TYPE_PREFIX}.{type_short}"
    return CloudEvent(
        type=type_,
        source=f"{SOURCE_PREFIX}/run/{run_id}",
        id=make_id(run_id, eid),
        time=now(),
        dataschema=schema_uri(type_, SCHEMA_VERSION),
        data=data,
    )


def main() -> None:
    log_path = RUN_DIR / f"{uuid.uuid4().hex[:8]}.events.jsonl"
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    print(f"variant-c (CloudEvents 1.0, schema_version={SCHEMA_VERSION})")
    print(f"log: {log_path}")

    # ---- 1+2 -------------------------------------------------------------
    banner("emit (v1)")
    w = EventWriter(log_path)
    eid = iter(range(1, 1_000_000))

    def emit(e: CloudEvent) -> None:
        w.append(e)
        print(f"  + {e.type:38s}  {e.model_dump_json()[:80]}…")

    emit(ce(run_id, next(eid), "run.started",
            {"run_id": run_id, "workflow": "close-out", "workflow_version": "0.1.0"}))
    emit(ce(run_id, next(eid), "node.entered",
            {"run_id": run_id, "node_path": "/root/load-guidance", "node_kind": "verb"}))
    emit(ce(run_id, next(eid), "verb.invoked",
            {"run_id": run_id, "node_path": "/root/load-guidance",
             "verb": "plan.load_guidance", "args_digest": "sha256:abc123"}))
    emit(ce(run_id, next(eid), "verb.completed",
            {"run_id": run_id, "node_path": "/root/load-guidance",
             "verb": "plan.load_guidance",
             "outcome": {"kind": "success", "value": {"guidance_id": "g-42"}}}))
    emit(ce(run_id, next(eid), "gate.opened",
            {"run_id": run_id, "node_path": "/root/await-human",
             "gate_id": "gate-1", "prompt": "approve close-out summary"}))
    emit(ce(run_id, next(eid), "run.completed",
            {"run_id": run_id, "terminal": "completed"}))

    # ---- 3: tail ---------------------------------------------------------
    banner("tail (concurrent producer + consumer)")
    tail_path = RUN_DIR / f"{uuid.uuid4().hex[:8]}.events.jsonl"
    tw = EventWriter(tail_path)
    collected: list[Any] = []
    stop_flag = threading.Event()

    def consumer() -> None:
        for x in tail(tail_path, parse_v1, stop=stop_flag.is_set):
            collected.append(x)
            t = getattr(getattr(x, "envelope", None), "type", type(x).__name__)
            print(f"  tail<- {t}")

    t = threading.Thread(target=consumer, daemon=True)
    t.start()
    teid = iter(range(1, 100))
    r2 = "r2"
    for e in (
        ce(r2, next(teid), "run.started",
           {"run_id": r2, "workflow": "tail-demo", "workflow_version": "0.1.0"}),
        ce(r2, next(teid), "node.entered",
           {"run_id": r2, "node_path": "/root", "node_kind": "verb"}),
        ce(r2, next(teid), "verb.invoked",
           {"run_id": r2, "node_path": "/root", "verb": "noop", "args_digest": "sha256:0"}),
        ce(r2, next(teid), "verb.completed",
           {"run_id": r2, "node_path": "/root", "verb": "noop",
            "outcome": {"kind": "needs_human", "prompt": "confirm"}}),
        ce(r2, next(teid), "run.completed",
           {"run_id": r2, "terminal": "surrendered"}),
    ):
        tw.append(e)
    tw.close()
    stop_flag.set()
    t.join(timeout=2.0)
    print(f"  tail collected: {len(collected)} events")

    # ---- 4 ---------------------------------------------------------------
    banner("derive RunState from log alone")
    state = derive(read_all(log_path, parse_v1))
    print(f"  {state}")

    # ---- 5 ---------------------------------------------------------------
    banner("schema evolution: append a v2-only type")
    w.append(ce(run_id, next(eid), "verb.retry_attempted",
                {"run_id": run_id, "node_path": "/root/retry-node",
                 "verb": "plan.load_guidance", "attempt": 2, "of": 3, "delay_ms": 400}))
    w.close()

    print("  v1 reader replay (envelope decodes, body unknown):")
    v1_state = derive(read_all(log_path, parse_v1))
    print(f"    {v1_state}")
    assert v1_state.unknown_types_seen == [f"{TYPE_PREFIX}.verb.retry_attempted"], v1_state

    print("  v2 reader replay (type now registered):")
    v2_state = derive(read_all(log_path, parse_v2))
    print(f"    {v2_state}")
    assert v2_state.retries == 1 and not v2_state.unknown_types_seen, v2_state

    # ---- 6 ---------------------------------------------------------------
    banner("corruption: truncated JSON line at end of file")
    c = EventWriter(log_path)
    c.append_raw('{"specversion": "1.0", "type": "io.requiem.run.started", "source": "/x"')
    c.append_raw("\n")
    c.close()

    print("  derive should HALT:")
    try:
        derive(read_all(log_path, parse_v2))
    except CorruptionDetected as exc:
        print(f"    HALTED as required: {exc}")
    else:
        raise SystemExit("BUG: corruption was silently skipped")

    print("\n[variant-c] OK")


if __name__ == "__main__":
    main()
