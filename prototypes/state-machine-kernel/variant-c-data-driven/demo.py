"""Variant C demo — workflows-as-data, verbs in a registry.

Shows the same 8 scenarios as A and B, plus two distinct things:
  - workflow JSON round-trip: model_dump_json / model_validate_json
  - static topology validation: validate_topology() before running

Also illustrates the workflow author's experience: define a verbs
module, register each callable by name, build the WorkflowModel with
references-by-string. The data model could equally come from YAML
later; the kernel never sees Python code, only the model.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from outcomes import Success, RetryableFailure
from model import (
    WorkflowModel, AgentNode, HumanGateNode, RouteNode,
    SubworkflowNode, TerminateNode, Edge, VerbRegistry,
)
from engine import (
    Engine, Suspended, Completed, RunCancelled, RunFailed,
)


HERE = Path(__file__).parent
LOG_ROOT = HERE / ".runs"


def fake_llm(prompt: str) -> str:
    return f"LLM[{prompt}] -> ok"


# ----- Verbs (registered separately from workflow data) -----


def build_verbs(fail_attempts: int = 0) -> VerbRegistry:
    r = VerbRegistry()

    def ingest(ctx): return Success(value={"text": "raw input"})

    def llm_step(ctx):
        if ctx.attempt <= fail_attempts:
            return RetryableFailure(
                reason=f"flaky LLM attempt {ctx.attempt}",
                error_kind="external.llm.transient",
            )
        return Success(value={"llm_output": fake_llm(ctx.completed["ingest"]["value"]["text"])})

    def choose_branch(ctx) -> str:
        out = ctx.completed["llm_step"]["value"]["llm_output"]
        return "fast" if "ok" in out else "slow"

    def kick(ctx): return Success(value={"name": "world"})
    def report(ctx):
        d = ctx.completed["call_child"]["value"]["disposition"]
        return Success(value={"sub_disposition": d})
    def greet(ctx):
        return Success(value={"greeting": f"hello {ctx.inputs.get('name','x')}"})
    def child_inputs(ctx):
        return {"name": ctx.completed["kick"]["value"]["name"]}

    # Crash + resume verbs registered upfront for both modes.
    def crashing(ctx):
        print("    !! about to os._exit(99) mid-node")
        sys.stdout.flush()
        os._exit(99)
    def replay_ingest(ctx):
        print("    !! re-executing supposedly-completed 'ingest'")
        return Success(value={"x": 1})
    def replay_mid(ctx):
        print("    → mid_crash re-executing (idempotent)")
        return Success(value={"text": "raw"})
    def crash_ingest(ctx): return Success(value={"x": 1})
    def crash_llm(ctx):
        print(f"    → llm_step attempt {ctx.attempt}")
        return Success(value={"llm_output": "ok"})

    for name, fn in [
        ("ingest", ingest), ("llm_step", llm_step), ("choose_branch", choose_branch),
        ("kick", kick), ("report", report), ("greet", greet),
        ("child_inputs", child_inputs),
        ("crash_ingest", crash_ingest), ("crashing", crashing),
        ("crash_llm", crash_llm),
        ("replay_ingest", replay_ingest), ("replay_mid", replay_mid),
    ]:
        r.register(name, fn)
    return r


# ----- Workflow models (pure data) -----


def basic_workflow(retry_max: int = 0) -> WorkflowModel:
    return WorkflowModel(
        workflow_id="demo_basic", start="ingest",
        nodes=[
            AgentNode(node_id="ingest", verb="ingest"),
            AgentNode(node_id="llm_step", verb="llm_step", retry_max=retry_max),
            HumanGateNode(node_id="approve", prompt="Approve LLM output?",
                          options=["yes", "no"]),
            RouteNode(node_id="branch", chooser="choose_branch"),
            TerminateNode(node_id="done", disposition="completed"),
            TerminateNode(node_id="abandoned", disposition="abandoned"),
            TerminateNode(node_id="surrender", disposition="failed"),
        ],
        edges=[
            Edge(from_node="ingest", outcome_key="success", to_node="llm_step"),
            Edge(from_node="llm_step", outcome_key="success", to_node="approve"),
            Edge(from_node="llm_step", outcome_key="retry_exhausted", to_node="surrender"),
            Edge(from_node="approve", outcome_key="needs_human:yes", to_node="branch"),
            Edge(from_node="approve", outcome_key="needs_human:no", to_node="abandoned"),
            Edge(from_node="branch", outcome_key="success:fast", to_node="done"),
            Edge(from_node="branch", outcome_key="success:slow", to_node="done"),
        ],
    )


def child_workflow() -> WorkflowModel:
    return WorkflowModel(
        workflow_id="demo_child", start="greet",
        nodes=[AgentNode(node_id="greet", verb="greet"),
               TerminateNode(node_id="done", disposition="completed")],
        edges=[Edge(from_node="greet", outcome_key="success", to_node="done")],
    )


def parent_workflow() -> WorkflowModel:
    return WorkflowModel(
        workflow_id="demo_parent", start="kick",
        nodes=[
            AgentNode(node_id="kick", verb="kick"),
            SubworkflowNode(node_id="call_child", target_workflow="demo_child",
                            inputs_from="child_inputs"),
            AgentNode(node_id="report", verb="report"),
            TerminateNode(node_id="done", disposition="completed"),
        ],
        edges=[
            Edge(from_node="kick", outcome_key="success", to_node="call_child"),
            Edge(from_node="call_child", outcome_key="success", to_node="report"),
            Edge(from_node="report", outcome_key="success", to_node="done"),
        ],
    )


def crash_workflow_for_crash() -> WorkflowModel:
    return WorkflowModel(
        workflow_id="demo_crash", start="ingest",
        nodes=[
            AgentNode(node_id="ingest", verb="crash_ingest"),
            AgentNode(node_id="mid_crash", verb="crashing"),
            AgentNode(node_id="llm_step", verb="crash_llm"),
            TerminateNode(node_id="done", disposition="completed"),
        ],
        edges=[
            Edge(from_node="ingest", outcome_key="success", to_node="mid_crash"),
            Edge(from_node="mid_crash", outcome_key="success", to_node="llm_step"),
            Edge(from_node="llm_step", outcome_key="success", to_node="done"),
        ],
    )


def crash_workflow_for_resume() -> WorkflowModel:
    # Same shape; different verbs for replay-vs-crash bodies.
    return WorkflowModel(
        workflow_id="demo_crash", start="ingest",
        nodes=[
            AgentNode(node_id="ingest", verb="replay_ingest"),
            AgentNode(node_id="mid_crash", verb="replay_mid"),
            AgentNode(node_id="llm_step", verb="crash_llm"),
            TerminateNode(node_id="done", disposition="completed"),
        ],
        edges=[
            Edge(from_node="ingest", outcome_key="success", to_node="mid_crash"),
            Edge(from_node="mid_crash", outcome_key="success", to_node="llm_step"),
            Edge(from_node="llm_step", outcome_key="success", to_node="done"),
        ],
    )


# ----- Helpers -----


def fresh() -> Path:
    if LOG_ROOT.exists():
        shutil.rmtree(LOG_ROOT)
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    return LOG_ROOT


def banner(s): print(f"\n{'=' * 70}\n  {s}\n{'=' * 70}")


def show(run_id: str, log_dir: Path):
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


# ----- Scenarios -----


def scenario_basic(log_dir):
    banner("Scenario 1 — basic")
    e = Engine({"demo_basic": basic_workflow()}, build_verbs(), log_dir)
    rid = "basic-001"
    r = e.run("demo_basic", rid)
    assert isinstance(r, Suspended)
    print(f"  suspended on {r.node_id!r}: {r.prompt}")
    e.resolve_gate(rid, "yes")
    r = e.run("demo_basic", rid)
    print(f"  result: {r!r}")
    assert isinstance(r, Completed)
    show(rid, log_dir)


def scenario_retry(log_dir):
    banner("Scenario 2 — retry budget")
    e = Engine({"demo_basic": basic_workflow(retry_max=2)},
               build_verbs(fail_attempts=2), log_dir)
    rid = "retry-001"
    r = e.run("demo_basic", rid)
    assert isinstance(r, Suspended)
    e.resolve_gate(rid, "yes")
    r = e.run("demo_basic", rid)
    print(f"  result: {r!r}")
    assert isinstance(r, Completed)
    show(rid, log_dir)


def scenario_cancel(log_dir):
    banner("Scenario 3 — cancel short-circuits retry")
    e = Engine({"demo_basic": basic_workflow(retry_max=99)},
               build_verbs(fail_attempts=999), log_dir)
    rid = "cancel-001"
    e.cancel(rid, "operator")
    r = e.run("demo_basic", rid)
    print(f"  result: {r!r}")
    assert isinstance(r, RunCancelled)
    show(rid, log_dir)


def scenario_subworkflow(log_dir):
    banner("Scenario 4 — sub-workflow")
    e = Engine({"demo_parent": parent_workflow(), "demo_child": child_workflow()},
               build_verbs(), log_dir)
    rid = "sub-001"
    r = e.run("demo_parent", rid)
    print(f"  result: {r!r}")
    assert isinstance(r, Completed)
    show(rid, log_dir)
    print(f"\n  --- child run log ---")
    show(f"{rid}__call_child", log_dir)


def scenario_crash(log_dir):
    banner("Scenario 5a — CRASH (os._exit inside verb)")
    e = Engine({"demo_crash": crash_workflow_for_crash()}, build_verbs(), log_dir)
    e.run("demo_crash", "crash-001")


def scenario_resume(log_dir):
    banner("Scenario 5b — RESUME after crash")
    e = Engine({"demo_crash": crash_workflow_for_resume()}, build_verbs(), log_dir)
    r = e.run("demo_crash", "crash-001")
    print(f"  result: {r!r}")
    assert isinstance(r, Completed)
    show("crash-001", log_dir)


def scenario_static_introspect(log_dir):
    banner("Scenario 6 — STATIC introspection: workflow JSON + topology validation")
    wf = basic_workflow(retry_max=2)
    out_path = log_dir / "demo_basic.workflow.json"
    out_path.write_text(wf.model_dump_json(indent=2))
    print(f"  serialised workflow → {out_path.relative_to(HERE)}")
    rt = WorkflowModel.model_validate_json(out_path.read_text())
    assert rt.model_dump() == wf.model_dump()
    print("  ✓ round-trip JSON identical")

    # Introduce a deliberate topology error to exercise validate_topology.
    broken = wf.model_copy(deep=True)
    broken.edges.append(Edge(from_node="ingest", outcome_key="zzz", to_node="nowhere"))
    errs = broken.validate_topology()
    print(f"  intentionally broken workflow → {len(errs)} errors:")
    for e in errs:
        print(f"    - {e}")
    assert any("nowhere" in x for x in errs)


def all_scenarios():
    log_dir = fresh()
    scenario_basic(log_dir)
    scenario_retry(log_dir)
    scenario_cancel(log_dir)
    scenario_subworkflow(log_dir)
    scenario_static_introspect(log_dir)

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
    scenario_resume(sub_log_dir)
    print("\n✓ ALL SCENARIOS PASSED (variant C)")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--scenario", default="all")
    a = p.parse_args()
    if a.scenario == "all":
        all_scenarios()
        return
    log_dir = Path(os.environ.get("DEMO_LOG_DIR", LOG_ROOT))
    log_dir.mkdir(parents=True, exist_ok=True)
    {
        "basic": scenario_basic, "retry": scenario_retry,
        "cancel": scenario_cancel, "subworkflow": scenario_subworkflow,
        "crash": scenario_crash, "resume": scenario_resume,
        "introspect": scenario_static_introspect,
    }[a.scenario](log_dir)


if __name__ == "__main__":
    main()
