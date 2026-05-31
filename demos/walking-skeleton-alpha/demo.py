"""Walking Skeleton α — end-to-end run.

    python demo.py [run_id]

Prints what is happening, drops a real `.events.jsonl` in `.runs/`, and
exits non-zero on failure. Total wall-clock < 5 seconds. No API keys.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

from engine.kernel import Completed, Engine, Failed, Suspended
from engine.toolbelt import Toolbelt
from reviewers import scripted_provider
from workflow import build_agent_registry, build_verb_registry, build_workflow

LOG_DIR = Path(__file__).parent / ".runs"


def _auto_gate(node_id: str, prompt: str, options: tuple[str, ...]) -> str:
    """Daniel-style approval gate. The demo auto-picks `approve` so the
    run completes; the gate is real (the kernel suspends without it)."""
    print(f"  [gate {node_id}] {prompt}")
    print(f"  [gate {node_id}] options: {options} → auto-picking 'approve'")
    return "approve"


async def main(argv: list[str]) -> int:
    run_id = argv[1] if len(argv) > 1 else f"demo-{int(time.time())}"
    if LOG_DIR.exists() and run_id.startswith("demo-"):
        # fresh demo runs start clean; named runs are preserved.
        shutil.rmtree(LOG_DIR, ignore_errors=True)

    workflow = build_workflow()
    verbs = build_verb_registry()
    agents = build_agent_registry()
    provider = scripted_provider()

    print("=" * 72)
    print(f"Walking Skeleton α — run_id={run_id}")
    print("=" * 72)
    print(f"workflow      : {workflow.name}  ({len(workflow.nodes)} nodes, "
          f"{len(workflow.edges)} edges)")
    print(f"recommended   : Stravinsky B + Brahms B + Beethoven C + Bach A")
    print(f"              + Mahler A + Wagner A + Liszt B+C + Pattern #9")
    print(f"log_dir       : {LOG_DIR}")
    print("-" * 72)

    engine = Engine(
        workflow=workflow, verbs=verbs, agents=agents,
        provider=provider, toolbelt=Toolbelt.real(),
        log_dir=LOG_DIR, gate_handler=_auto_gate,
    )

    t0 = time.perf_counter()
    result = await engine.run(run_id)
    dt = time.perf_counter() - t0

    print("-" * 72)
    print(f"wall-clock    : {dt*1000:.1f} ms")
    print(f"result.kind   : {type(result).__name__}")
    match result:
        case Completed(disposition=d, final_node=n, projection=proj):
            print(f"disposition   : {d}  (final_node={n})")
            print(f"projection    : {json.dumps(proj, indent=2)}")
            summary = proj.get("nodes_entered", [])
            print(f"nodes visited : {' → '.join(summary)}")
        case Suspended(node_id=n, prompt=p, options=opts):
            print(f"suspended at  : {n}")
            print(f"  prompt      : {p}")
            print(f"  options     : {opts}")
        case Failed(node_id=n, error_kind=ek, message=m):
            print(f"FAILED at {n}: [{ek}] {m}")

    print("-" * 72)
    log_path = LOG_DIR / f"{run_id}.events.jsonl"
    print(f"event log     : {log_path}  ({log_path.stat().st_size} bytes)")
    print(f"inspect with  : jq -c . {log_path}")
    print(f"resume with   : python demo_resume.py {run_id}")
    print(f"agent calls   : {len(provider.calls)} "
          f"(by agent: { {a: sum(1 for c in provider.calls if c['agent']==a) for a in sorted({c['agent'] for c in provider.calls})} })")
    print("=" * 72)

    if isinstance(result, Failed):
        return 1
    summary_path = LOG_DIR / f"{run_id}.summary.md"
    if summary_path.exists():
        print(f"summary       : {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv)))
