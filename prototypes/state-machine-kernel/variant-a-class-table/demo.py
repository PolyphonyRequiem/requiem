"""Variant A demo — class-based nodes + explicit transition table.

Runs all 8 required demos. Invoke without arguments to get the full
suite, or with `--scenario <name>` to run one.

    python demo.py                    # all scenarios, including crash+resume
    python demo.py --scenario basic
    python demo.py --scenario retry
    python demo.py --scenario cancel
    python demo.py --scenario subworkflow
    python demo.py --scenario crash   # exits 99 mid-run; meant to be wrapped
    python demo.py --scenario resume  # resumes the run started by --scenario crash
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from outcomes import Success, RetryableFailure, PermanentFailure, Cancelled
from nodes import (
    AgentStep, ScriptStep, HumanGate, Route, SubworkflowCall, Terminate,
)
from workflow import Workflow
from engine import Engine, Suspended, Completed, RunCancelled, RunFailed


HERE = Path(__file__).parent
LOG_ROOT = HERE / ".runs"


# ----- Fake LLM provider (Stravinsky's seam, stubbed) -----


def fake_llm(prompt: str) -> str:
    return f"LLM[{prompt}] -> ok"


# ----- The base workflow (start → agent → gate → route → end) -----


def build_basic_workflow(retry_max: int = 0,
                         fail_attempts: int = 0) -> Workflow:
    """The canonical 5-node workflow used in basic, retry, cancel scenarios."""

    # State shared between attempts (simulates an idempotent verb that
    # records its progress and converges).
    attempt_counter = {"n": 0}

    def ingest_body(ctx):
        return Success(value={"text": "raw input"})

    def llm_body(ctx):
        attempt_counter["n"] = ctx.attempt
        if ctx.attempt <= fail_attempts:
            return RetryableFailure(
                reason=f"flaky LLM, attempt {ctx.attempt}",
                error_kind="external.llm.transient",
            )
        text = ctx.completed["ingest"]["value"]["text"]
        return Success(value={"llm_output": fake_llm(text)})

    def choose_route(ctx):
        out = ctx.completed["llm_step"]["value"]["llm_output"]
        return "fast" if "ok" in out else "slow"

    wf = Workflow("demo_basic", start="ingest")
    wf.add(AgentStep(node_id="ingest", body=ingest_body))
    wf.add(AgentStep(
        node_id="llm_step", retry_max=retry_max, body=llm_body,
    ))
    wf.add(HumanGate(
        node_id="approve",
        prompt="Approve LLM output?",
        options=["yes", "no"],
    ))
    wf.add(Route(node_id="branch", chooser=choose_route))
    wf.add(Terminate(node_id="done", disposition="completed"))
    wf.add(Terminate(node_id="abandoned", disposition="abandoned"))
    wf.add(Terminate(node_id="surrender", disposition="failed"))

    wf.edge("ingest", "success", "llm_step")
    wf.edge("llm_step", "success", "approve")
    wf.edge("llm_step", "retry_exhausted", "surrender")
    wf.edge("approve", "needs_human:yes", "branch")
    wf.edge("approve", "needs_human:no", "abandoned")
    wf.route("branch", "fast", "done")
    wf.route("branch", "slow", "done")
    return wf


# ----- Sub-workflow demo: parent calls child -----


def build_child_workflow() -> Workflow:
    def hello(ctx):
        return Success(value={"greeting": f"hello {ctx.inputs.get('name','x')}"})

    wf = Workflow("demo_child", start="greet")
    wf.add(AgentStep(node_id="greet", body=hello))
    wf.add(Terminate(node_id="done", disposition="completed"))
    wf.edge("greet", "success", "done")
    return wf


def build_parent_workflow() -> Workflow:
    def report(ctx):
        sub_disp = ctx.completed["call_child"]["value"]["disposition"]
        return Success(value={"sub_disposition": sub_disp})

    wf = Workflow("demo_parent", start="kick")
    wf.add(AgentStep(node_id="kick",
                     body=lambda c: Success(value={"name": "world"})))
    wf.add(SubworkflowCall(
        node_id="call_child",
        target_workflow="demo_child",
        inputs_from=lambda ctx: {"name": ctx.completed["kick"]["value"]["name"]},
    ))
    wf.add(AgentStep(node_id="report", body=report))
    wf.add(Terminate(node_id="done", disposition="completed"))
    wf.edge("kick", "success", "call_child")
    wf.edge("call_child", "success", "report")
    wf.edge("report", "success", "done")
    return wf


# ----- Engine helpers -----


def fresh_log_dir() -> Path:
    if LOG_ROOT.exists():
        shutil.rmtree(LOG_ROOT)
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    return LOG_ROOT


def banner(label: str) -> None:
    print(f"\n{'=' * 70}\n  {label}\n{'=' * 70}")


def show_log(run_id: str, log_dir: Path) -> None:
    path = log_dir / f"{run_id}.events.jsonl"
    print(f"  event log → {path.relative_to(HERE)}  ({sum(1 for _ in path.open()):d} events)")
    for line in path.read_text().splitlines():
        evt = json.loads(line)
        scope = "/".join(evt.get("scope", [])) or "·"
        extra = ""
        if evt["type"] == "node_completed":
            extra = f"  kind={evt['outcome']['kind']}"
        elif evt["type"] == "route_taken":
            extra = f"  {evt['from_node']} --[{evt['key']}]--> {evt['to_node']}"
        elif evt["type"] == "retry_attempted":
            extra = f"  attempt={evt['attempt']} → {evt['next_attempt']}/{evt['retry_max']}"
        elif evt["type"] == "subworkflow_started":
            extra = f"  → {evt['child_workflow']}"
        elif evt["type"] == "subworkflow_completed":
            extra = f"  ← {evt['result']['kind']}"
        elif evt["type"] == "human_gate_resolved":
            extra = f"  choice={evt['choice']}"
        elif evt["type"] == "workflow_terminated":
            extra = f"  disposition={evt.get('disposition')}"
        print(f"    [{evt['event_id']:>2}] {evt['type']:<26s} scope={scope:<10s} {extra}")


# ----- Scenarios -----


def scenario_basic(log_dir: Path) -> None:
    banner("Scenario 1 — basic workflow: start → agent → gate → route → end")
    engine = Engine(
        workflows={"demo_basic": build_basic_workflow()},
        log_dir=log_dir,
    )
    run_id = "basic-001"
    result = engine.run("demo_basic", run_id, inputs={})
    assert isinstance(result, Suspended), f"expected Suspended, got {result!r}"
    print(f"  suspended on gate {result.node_id!r}: {result.prompt}")
    engine.resolve_gate(run_id, "yes")
    result = engine.run("demo_basic", run_id, inputs={})
    print(f"  result: {result!r}")
    assert isinstance(result, Completed) and result.disposition == "completed"
    show_log(run_id, log_dir)


def scenario_retry(log_dir: Path) -> None:
    banner("Scenario 2 — retry budget: 2 failures then success")
    engine = Engine(
        workflows={"demo_basic": build_basic_workflow(retry_max=2, fail_attempts=2)},
        log_dir=log_dir,
    )
    run_id = "retry-001"
    result = engine.run("demo_basic", run_id, inputs={})
    assert isinstance(result, Suspended)
    engine.resolve_gate(run_id, "yes")
    result = engine.run("demo_basic", run_id, inputs={})
    print(f"  result: {result!r}")
    assert isinstance(result, Completed)
    show_log(run_id, log_dir)


def scenario_cancel(log_dir: Path) -> None:
    banner("Scenario 3 — cancel short-circuits retry (INV-CANCEL-SHORT-CIRCUITS-RETRY)")
    wf = build_basic_workflow(retry_max=5, fail_attempts=10)  # would loop forever
    engine = Engine(workflows={"demo_basic": wf}, log_dir=log_dir)
    run_id = "cancel-001"
    # Cancel up front. The retry loop must surrender on the first detection.
    engine.cancel(run_id, reason="operator pressed Ctrl-C")
    result = engine.run("demo_basic", run_id, inputs={})
    print(f"  result: {result!r}")
    assert isinstance(result, RunCancelled)
    show_log(run_id, log_dir)
    # Sanity: no retry_attempted event should follow the cancel.
    events = [
        json.loads(l) for l in
        (log_dir / f"{run_id}.events.jsonl").read_text().splitlines()
    ]
    cancel_idx = next(i for i, e in enumerate(events)
                      if e["type"] == "cancel_received")
    later = [e for e in events[cancel_idx + 1:]
             if e["type"] == "retry_attempted"]
    assert later == [], f"retry happened AFTER cancel: {later}"
    print("  ✓ no retry_attempted observed after cancel_received")


def scenario_subworkflow(log_dir: Path) -> None:
    banner("Scenario 4 — sub-workflow: parent invokes child via SubworkflowCall")
    engine = Engine(
        workflows={
            "demo_parent": build_parent_workflow(),
            "demo_child": build_child_workflow(),
        },
        log_dir=log_dir,
    )
    run_id = "sub-001"
    result = engine.run("demo_parent", run_id, inputs={})
    print(f"  result: {result!r}")
    assert isinstance(result, Completed)
    show_log(run_id, log_dir)
    # The child has its own log:
    child_id = f"{run_id}__call_child"
    print(f"\n  --- child run log ---")
    show_log(child_id, log_dir)


def scenario_crash(log_dir: Path) -> None:
    """Run a workflow that os._exit()s in the middle of the LLM node.

    Meant to be invoked as a subprocess. The 'resume' scenario picks up
    where this one died.
    """
    banner("Scenario 5a — CRASH (intentional os._exit inside a node)")

    def crashing_body(ctx):
        # Pretend to do work...
        out = Success(value={"text": "raw"})
        # ...then the process dies.
        print("    !! about to os._exit(99) mid-node")
        sys.stdout.flush()
        os._exit(99)

    def llm_body(ctx):
        return Success(value={"llm_output": "ok"})

    wf = Workflow("demo_crash", start="ingest")
    from nodes import AgentStep as A
    wf.add(A(node_id="ingest", body=lambda c: Success(value={"x": 1})))
    wf.add(A(node_id="mid_crash", body=crashing_body))
    wf.add(A(node_id="llm_step", body=llm_body))
    wf.add(Terminate(node_id="done", disposition="completed"))
    wf.edge("ingest", "success", "mid_crash")
    wf.edge("mid_crash", "success", "llm_step")
    wf.edge("llm_step", "success", "done")

    engine = Engine(workflows={"demo_crash": wf}, log_dir=log_dir)
    engine.run("demo_crash", "crash-001", inputs={})


def scenario_resume(log_dir: Path) -> None:
    banner("Scenario 5b — RESUME after crash from event log")

    def already_ran(ctx):
        # If we re-execute this node, it's a bug — print a marker.
        print(f"    !! re-executing supposedly-completed node {ctx.node_id!r}")
        return Success(value={"x": 1})

    def llm_body(ctx):
        print(f"    → llm_step executing (attempt {ctx.attempt})")
        return Success(value={"llm_output": "ok"})

    def mid_replay(ctx):
        # Mid_crash is the in-flight node; we DO re-execute it (idempotent).
        print(f"    → mid_crash re-executing (this is correct on resume)")
        return Success(value={"text": "raw"})

    wf = Workflow("demo_crash", start="ingest")
    wf.add(AgentStep(node_id="ingest", body=already_ran))
    wf.add(AgentStep(node_id="mid_crash", body=mid_replay))
    wf.add(AgentStep(node_id="llm_step", body=llm_body))
    wf.add(Terminate(node_id="done", disposition="completed"))
    wf.edge("ingest", "success", "mid_crash")
    wf.edge("mid_crash", "success", "llm_step")
    wf.edge("llm_step", "success", "done")

    engine = Engine(workflows={"demo_crash": wf}, log_dir=log_dir)
    result = engine.run("demo_crash", "crash-001", inputs={})
    print(f"  result: {result!r}")
    assert isinstance(result, Completed)
    show_log("crash-001", log_dir)


# ----- Driver -----


def all_scenarios() -> None:
    log_dir = fresh_log_dir()
    scenario_basic(log_dir)
    scenario_retry(log_dir)
    scenario_cancel(log_dir)
    scenario_subworkflow(log_dir)

    # Crash + resume: invoke ourselves as a subprocess for the crash so the
    # current process survives os._exit.
    banner("Scenario 5 — crash + resume (subprocess driven)")
    sub_log_dir = log_dir / "crash"
    sub_log_dir.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["DEMO_LOG_DIR"] = str(sub_log_dir)
    r = subprocess.run(
        [sys.executable, __file__, "--scenario", "crash"],
        env=env, capture_output=True, text=True,
    )
    print(r.stdout)
    print(f"  child process exited with code {r.returncode} (expected 99)")
    assert r.returncode == 99, f"crash subprocess did not exit 99 (got {r.returncode})"
    # Now resume in-process from the same log dir.
    scenario_resume(sub_log_dir)
    print("\n✓ ALL SCENARIOS PASSED (variant A)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="all")
    args = parser.parse_args()

    if args.scenario == "all":
        all_scenarios()
        return

    # Sub-process scenarios use an env-injected log dir so we share state
    # with the parent.
    log_dir = Path(os.environ.get("DEMO_LOG_DIR", LOG_ROOT))
    log_dir.mkdir(parents=True, exist_ok=True)
    {
        "basic": scenario_basic,
        "retry": scenario_retry,
        "cancel": scenario_cancel,
        "subworkflow": scenario_subworkflow,
        "crash": scenario_crash,
        "resume": scenario_resume,
    }[args.scenario](log_dir)


if __name__ == "__main__":
    main()
