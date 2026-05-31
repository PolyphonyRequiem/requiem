"""Variant B demo — coroutine-based nodes.

Same 8-scenario suite as variant A. The differences are subtle but real:

  - Node bodies are `async def`; can `await` directly (LLM HTTP, asyncio.sleep).
  - Engine.run() is awaitable; sub-workflows are `await self.run(child, ...)`.
  - Cancellation can be both event-driven (cancel_event) and exception-driven
    (asyncio.CancelledError caught and converted to a `Cancelled` outcome).
  - Parallel composition would be trivial (asyncio.gather), though not
    exercised here.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from outcomes import Success, RetryableFailure, Cancelled
from engine import (
    Engine, Workflow, NodeContext,
    agent, human_gate, route, subworkflow, terminate,
    Suspended, Completed, RunCancelled, RunFailed,
)


HERE = Path(__file__).parent
LOG_ROOT = HERE / ".runs"


async def fake_llm(prompt: str) -> str:
    # Demonstrates awaiting I/O inside a node body without blocking.
    await asyncio.sleep(0)
    return f"LLM[{prompt}] -> ok"


def build_basic(retry_max: int = 0, fail_attempts: int = 0) -> Workflow:
    async def ingest(ctx: NodeContext):
        return Success(value={"text": "raw input"})

    async def llm_step(ctx: NodeContext):
        if ctx.attempt <= fail_attempts:
            return RetryableFailure(
                reason=f"flaky LLM, attempt {ctx.attempt}",
                error_kind="external.llm.transient",
            )
        text = ctx.completed["ingest"]["value"]["text"]
        out = await fake_llm(text)
        return Success(value={"llm_output": out})

    def chooser(ctx: NodeContext) -> str:
        return "fast" if "ok" in ctx.completed["llm_step"]["value"]["llm_output"] else "slow"

    wf = Workflow("demo_basic", start="ingest")
    wf.add(agent("ingest", ingest))
    wf.add(agent("llm_step", llm_step, retry_max=retry_max))
    wf.add(human_gate("approve", "Approve LLM output?", ["yes", "no"]))
    wf.add(route("branch", chooser))
    wf.add(terminate("done", "completed"))
    wf.add(terminate("abandoned", "abandoned"))
    wf.add(terminate("surrender", "failed"))
    wf.edge("ingest", "success", "llm_step")
    wf.edge("llm_step", "success", "approve")
    wf.edge("llm_step", "retry_exhausted", "surrender")
    wf.edge("approve", "needs_human:yes", "branch")
    wf.edge("approve", "needs_human:no", "abandoned")
    wf.route("branch", "fast", "done")
    wf.route("branch", "slow", "done")
    return wf


def build_child() -> Workflow:
    async def greet(ctx: NodeContext):
        await asyncio.sleep(0)
        return Success(value={"greeting": f"hello {ctx.inputs.get('name','x')}"})
    wf = Workflow("demo_child", start="greet")
    wf.add(agent("greet", greet))
    wf.add(terminate("done", "completed"))
    wf.edge("greet", "success", "done")
    return wf


def build_parent() -> Workflow:
    async def kick(ctx): return Success(value={"name": "world"})
    async def report(ctx):
        d = ctx.completed["call_child"]["value"]["disposition"]
        return Success(value={"sub_disposition": d})
    wf = Workflow("demo_parent", start="kick")
    wf.add(agent("kick", kick))
    wf.add(subworkflow("call_child", "demo_child",
                       lambda c: {"name": c.completed["kick"]["value"]["name"]}))
    wf.add(agent("report", report))
    wf.add(terminate("done", "completed"))
    wf.edge("kick", "success", "call_child")
    wf.edge("call_child", "success", "report")
    wf.edge("report", "success", "done")
    return wf


def fresh() -> Path:
    if LOG_ROOT.exists():
        shutil.rmtree(LOG_ROOT)
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    return LOG_ROOT


def banner(s: str) -> None:
    print(f"\n{'=' * 70}\n  {s}\n{'=' * 70}")


def show(run_id: str, log_dir: Path) -> None:
    p = log_dir / f"{run_id}.events.jsonl"
    print(f"  event log → {p.relative_to(HERE)}  ({sum(1 for _ in p.open()):d} events)")
    for line in p.read_text().splitlines():
        e = json.loads(line)
        extra = ""
        if e["type"] == "node_completed":
            extra = f"  kind={e['outcome']['kind']}"
        elif e["type"] == "route_taken":
            extra = f"  {e['from_node']}--[{e['key']}]-->{e['to_node']}"
        elif e["type"] == "retry_attempted":
            extra = f"  attempt={e['attempt']}→{e['next_attempt']}/{e['retry_max']}"
        elif e["type"] == "subworkflow_started":
            extra = f"  → {e['child_workflow']}"
        elif e["type"] == "subworkflow_completed":
            extra = f"  ← {e['result']['kind']}"
        elif e["type"] == "human_gate_resolved":
            extra = f"  choice={e['choice']}"
        elif e["type"] == "workflow_terminated":
            extra = f"  disposition={e.get('disposition')}"
        print(f"    [{e['event_id']:>2}] {e['type']:<26s} {extra}")


async def scenario_basic(log_dir: Path):
    banner("Scenario 1 — basic: start→agent→gate→route→end")
    e = Engine({"demo_basic": build_basic()}, log_dir)
    rid = "basic-001"
    r = await e.run("demo_basic", rid)
    assert isinstance(r, Suspended)
    print(f"  suspended on {r.node_id!r}: {r.prompt}")
    e.resolve_gate(rid, "yes")
    r = await e.run("demo_basic", rid)
    print(f"  result: {r!r}")
    assert isinstance(r, Completed)
    show(rid, log_dir)


async def scenario_retry(log_dir: Path):
    banner("Scenario 2 — retry budget: 2 fails then success")
    e = Engine({"demo_basic": build_basic(retry_max=2, fail_attempts=2)}, log_dir)
    rid = "retry-001"
    r = await e.run("demo_basic", rid)
    assert isinstance(r, Suspended)
    e.resolve_gate(rid, "yes")
    r = await e.run("demo_basic", rid)
    print(f"  result: {r!r}")
    assert isinstance(r, Completed)
    show(rid, log_dir)


async def scenario_cancel(log_dir: Path):
    banner("Scenario 3 — cancel short-circuits retry (INV-CANCEL-SHORT-CIRCUITS-RETRY)")
    e = Engine({"demo_basic": build_basic(retry_max=99, fail_attempts=999)}, log_dir)
    rid = "cancel-001"
    e.cancel(rid, "operator")
    r = await e.run("demo_basic", rid)
    print(f"  result: {r!r}")
    assert isinstance(r, RunCancelled)
    show(rid, log_dir)
    evts = [json.loads(l) for l in
            (log_dir / f"{rid}.events.jsonl").read_text().splitlines()]
    idx = next(i for i, x in enumerate(evts) if x["type"] == "cancel_received")
    later = [x for x in evts[idx + 1:] if x["type"] == "retry_attempted"]
    assert later == []
    print("  ✓ no retry_attempted after cancel_received")


async def scenario_subworkflow(log_dir: Path):
    banner("Scenario 4 — sub-workflow via await self.run(child, ...)")
    e = Engine({"demo_parent": build_parent(), "demo_child": build_child()}, log_dir)
    rid = "sub-001"
    r = await e.run("demo_parent", rid)
    print(f"  result: {r!r}")
    assert isinstance(r, Completed)
    show(rid, log_dir)
    print(f"\n  --- child run log ---")
    show(f"{rid}__call_child", log_dir)


async def scenario_crash(log_dir: Path):
    banner("Scenario 5a — CRASH (intentional os._exit inside async node)")

    async def crashing(ctx):
        print("    !! about to os._exit(99) mid-node")
        sys.stdout.flush()
        os._exit(99)

    async def ingest(ctx): return Success(value={"x": 1})
    async def llm_step(ctx): return Success(value={"llm_output": "ok"})

    wf = Workflow("demo_crash", start="ingest")
    wf.add(agent("ingest", ingest))
    wf.add(agent("mid_crash", crashing))
    wf.add(agent("llm_step", llm_step))
    wf.add(terminate("done", "completed"))
    wf.edge("ingest", "success", "mid_crash")
    wf.edge("mid_crash", "success", "llm_step")
    wf.edge("llm_step", "success", "done")

    e = Engine({"demo_crash": wf}, log_dir)
    await e.run("demo_crash", "crash-001")


async def scenario_resume(log_dir: Path):
    banner("Scenario 5b — RESUME after crash from event log")
    async def already_ran(ctx):
        print(f"    !! re-executing supposedly-completed {ctx.node_id!r}")
        return Success(value={"x": 1})
    async def llm_step(ctx):
        print(f"    → llm_step (attempt {ctx.attempt})")
        return Success(value={"llm_output": "ok"})
    async def mid_replay(ctx):
        print(f"    → mid_crash re-executing (idempotent contract)")
        return Success(value={"text": "raw"})

    wf = Workflow("demo_crash", start="ingest")
    wf.add(agent("ingest", already_ran))
    wf.add(agent("mid_crash", mid_replay))
    wf.add(agent("llm_step", llm_step))
    wf.add(terminate("done", "completed"))
    wf.edge("ingest", "success", "mid_crash")
    wf.edge("mid_crash", "success", "llm_step")
    wf.edge("llm_step", "success", "done")

    e = Engine({"demo_crash": wf}, log_dir)
    r = await e.run("demo_crash", "crash-001")
    print(f"  result: {r!r}")
    assert isinstance(r, Completed)
    show("crash-001", log_dir)


async def all_scenarios():
    log_dir = fresh()
    await scenario_basic(log_dir)
    await scenario_retry(log_dir)
    await scenario_cancel(log_dir)
    await scenario_subworkflow(log_dir)

    banner("Scenario 5 — crash + resume (subprocess driven)")
    sub_log_dir = log_dir / "crash"
    sub_log_dir.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["DEMO_LOG_DIR"] = str(sub_log_dir)
    p = subprocess.run([sys.executable, __file__, "--scenario", "crash"],
                       env=env, capture_output=True, text=True)
    print(p.stdout)
    print(f"  child exited code {p.returncode} (expected 99)")
    assert p.returncode == 99, p.returncode
    await scenario_resume(sub_log_dir)
    print("\n✓ ALL SCENARIOS PASSED (variant B)")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--scenario", default="all")
    a = p.parse_args()
    if a.scenario == "all":
        asyncio.run(all_scenarios())
        return
    log_dir = Path(os.environ.get("DEMO_LOG_DIR", LOG_ROOT))
    log_dir.mkdir(parents=True, exist_ok=True)
    fn = {
        "basic": scenario_basic, "retry": scenario_retry,
        "cancel": scenario_cancel, "subworkflow": scenario_subworkflow,
        "crash": scenario_crash, "resume": scenario_resume,
    }[a.scenario]
    asyncio.run(fn(log_dir))


if __name__ == "__main__":
    main()
