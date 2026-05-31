"""Variant A — declarative YAML scenario driver.

A scenario is a `scenario.yaml` file with these top-level keys:

  workflow:      one of the example-workflow names from `_engine.examples`
  run_id:        string identifying the run
  inputs:        dict, passed to engine.run()
  agent_scripts: {agent_name: [reply_dict, ...]}     # ordered FIFO
  subworkflow_agent_scripts:                          # optional
                 {child_workflow_name: {agent_name: [reply_dict, ...]}}
  gates:         {gate_name: {value: chosen_option, additional_input: {...}}}
                 OR {gate_name: "chosen_option"}      # string shorthand
  chaos:         {kill_after_event: {type: NodeCompleted, node: load}}  # optional
  resume:        true  # if set, the driver expects a prior partial run
                       # to be on disk and resumes it
  expect:
    terminal:    completed                            # required
    events:      [{type: RunStarted}, ...]             # ordered subseq
    agents_invoked: {architect: 2}                     # exact call counts
    retries:     {flaky: 2}                            # by-node retry count

The driver is ~120 lines of Python; the appeal is that "writing a
scenario" only ever touches YAML.

Limits surface clearly: anything that needs a Python predicate
(e.g. "the merge node's payload has a SHA that matches /^[0-9a-f]{40}$/")
forces a YAML schema extension. That's the trade-off.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

# Allow running as `python driver.py scenarios/foo.yaml` from variant dir.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _engine import (  # noqa: E402
    ChaosHook,
    EventLog,
    FakeProvider,
    KillRequested,
    WorkflowEngine,
    examples,
)

WORKFLOWS = {
    "tiny_three_node": lambda: examples.tiny_three_node(),
    "transient_failure_workflow": lambda: examples.transient_failure_workflow(fail_times=2),
    "gated_workflow": lambda: examples.gated_workflow(),
    "parent_with_subworkflow": lambda: examples.parent_with_subworkflow(),
}


def _build_provider(scripts: dict[str, list[dict]]) -> FakeProvider:
    fp = FakeProvider()
    for agent, replies in (scripts or {}).items():
        fp.script(agent, *replies)
    return fp


def _build_gate_handler(gates: dict[str, Any] | None):
    gates = gates or {}

    def handler(gate_name: str, options: list[str]):
        if gate_name not in gates:
            raise RuntimeError(f"Scenario has no scripted answer for gate '{gate_name}'")
        spec = gates[gate_name]
        if isinstance(spec, str):
            return spec, {}
        return spec["value"], spec.get("additional_input", {})

    return handler


def _build_chaos(chaos: dict[str, Any] | None) -> ChaosHook:
    if not chaos:
        return ChaosHook()
    kill = chaos.get("kill_after_event")
    if kill:
        def on_event(evt):
            if evt.type == kill["type"] and (kill.get("node") in (None, evt.node)):
                raise KillRequested(f"chaos: {kill}")
        return ChaosHook(on_event=on_event)
    return ChaosHook()


def run_scenario(scenario_path: Path, log_path: Path | None = None) -> dict[str, Any]:
    spec = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))

    workflow_key = spec["workflow"]
    built = WORKFLOWS[workflow_key]()
    workflow, attempts = built if isinstance(built, tuple) else (built, None)

    log_path = log_path or (scenario_path.parent / f".{spec.get('run_id', scenario_path.stem)}.events.jsonl")
    if not spec.get("resume", False) and log_path.exists():
        log_path.unlink()

    log = EventLog(log_path)
    parent_provider = _build_provider(spec.get("agent_scripts", {}))
    sub_scripts = spec.get("subworkflow_agent_scripts", {})
    sub_providers = {name: _build_provider(scripts) for name, scripts in sub_scripts.items()}

    engine = WorkflowEngine(
        workflow=workflow,
        provider=parent_provider,
        event_log=log,
        gate_handler=_build_gate_handler(spec.get("gates")),
        subworkflow_provider_for=lambda name: sub_providers.get(name, parent_provider),
        chaos=_build_chaos(spec.get("chaos")),
    )

    try:
        terminal = engine.run(run_id=spec.get("run_id", "scenario"), inputs=spec.get("inputs"))
        crashed = False
    except KillRequested as exc:
        terminal = None
        crashed = True

    _assert_expectations(spec.get("expect", {}), terminal, log, parent_provider, sub_providers, attempts, crashed)
    return {"terminal": terminal, "log_path": str(log_path), "crashed": crashed}


def _assert_expectations(expect, terminal, log: EventLog, parent: FakeProvider,
                         subs: dict[str, FakeProvider], attempts, crashed: bool):
    if expect.get("crashed") is not None:
        assert crashed == expect["crashed"], f"crashed={crashed} but expected {expect['crashed']}"
    if "terminal" in expect:
        assert terminal == expect["terminal"], f"terminal={terminal!r} expected {expect['terminal']!r}"

    if "events" in expect:
        all_events = list(log)
        idx = 0
        for want in expect["events"]:
            while idx < len(all_events):
                evt = all_events[idx]
                idx += 1
                if evt.type != want["type"]:
                    continue
                if "node" in want and evt.node != want["node"]:
                    continue
                if "payload_contains" in want:
                    if not all(evt.payload.get(k) == v for k, v in want["payload_contains"].items()):
                        continue
                break
            else:
                raise AssertionError(f"event {want} not found in order in log")

    for agent, n in (expect.get("agents_invoked") or {}).items():
        # Search both parent and child providers.
        found = parent.call_count(agent)
        for sub in subs.values():
            found += sub.call_count(agent)
        assert found == n, f"agent {agent!r} invoked {found}× expected {n}×"

    for node_id, n in (expect.get("retries") or {}).items():
        retries = [e for e in log if e.type == "RetryAttempted" and e.node == node_id]
        assert len(retries) == n, f"node {node_id!r} retried {len(retries)}× expected {n}×"


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if target is None:
        # Run all scenarios in scenarios/
        here = Path(__file__).resolve().parent / "scenarios"
        for s in sorted(here.glob("*.yaml")):
            print(f"=== {s.name} ===")
            print(run_scenario(s))
    else:
        print(run_scenario(target))
