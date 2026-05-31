"""Variant C — record-side.

Wraps a real `AgentProvider`, a real `gate_handler`, and an `EventLog`
subscriber. Every agent call, gate decision, and event is captured
into a YAML "recording" file. The same recording can later be replayed
by `replayer.py` without any real LLM/operator.

Recording format (YAML):

    workflow: tiny_three_node
    run_id: tiny-happy
    inputs: {...}
    agent_calls:
      - agent: architect
        reply: {plan: "ok"}                # ordered FIFO during replay
    gate_decisions:
      - gate: gate
        value: abort
        additional_input: {reason: x}
    events:
      - {event_id: 1, type: RunStarted, ...}
      - ...
    expect:
      terminal: completed
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _engine import AgentProvider, EventLog, FakeProvider, WorkflowEngine  # noqa: E402


@dataclass
class RecordingProvider:
    """Wraps any AgentProvider; records every (agent, reply) pair."""

    inner: AgentProvider
    log: list[dict[str, Any]] = field(default_factory=list)

    def invoke(self, agent_name: str, prompt_context: dict[str, Any]) -> dict[str, Any]:
        reply = self.inner.invoke(agent_name, prompt_context)
        self.log.append({"agent": agent_name, "reply": reply})
        return reply


@dataclass
class RecordingGateHandler:
    inner: Callable[[str, list[str]], tuple[str, dict[str, Any]]]
    log: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, gate_name: str, options: list[str]):
        value, extra = self.inner(gate_name, options)
        self.log.append({"gate": gate_name, "value": value, "additional_input": extra})
        return value, extra


def record_run(
    workflow_name: str,
    workflow,
    *,
    provider: AgentProvider,
    gate_handler: Callable | None = None,
    subworkflow_provider_for: Callable | None = None,
    inputs: dict[str, Any] | None = None,
    run_id: str = "recorded-run",
    recording_path: Path,
    log_path: Path,
) -> dict[str, Any]:
    """Run the workflow once with real-ish inputs; capture everything to YAML."""
    rec_provider = RecordingProvider(inner=provider)
    rec_gate = RecordingGateHandler(inner=gate_handler) if gate_handler else None

    # Wrap every child provider so its agent calls land in the SAME log list.
    def wrap_child(name: str) -> AgentProvider:
        child = subworkflow_provider_for(name) if subworkflow_provider_for else provider
        # Reuse the parent's recording log to keep call order globally correct.
        return RecordingProvider(inner=child, log=rec_provider.log)

    events_captured: list[dict[str, Any]] = []
    log = EventLog(log_path)
    log.subscribe(lambda e: events_captured.append(e.model_dump()))

    engine = WorkflowEngine(
        workflow=workflow,
        provider=rec_provider,
        event_log=log,
        gate_handler=rec_gate,
        subworkflow_provider_for=wrap_child if subworkflow_provider_for else None,
    )
    terminal = engine.run(run_id=run_id, inputs=inputs)

    recording = {
        "workflow": workflow_name,
        "run_id": run_id,
        "inputs": inputs or {},
        "agent_calls": rec_provider.log,
        "gate_decisions": rec_gate.log if rec_gate else [],
        "events": events_captured,
        "expect": {"terminal": terminal},
    }
    recording_path.parent.mkdir(parents=True, exist_ok=True)
    recording_path.write_text(yaml.safe_dump(recording, sort_keys=False), encoding="utf-8")
    return recording


# ---------------------------------------------------------------------------
# Convenience: a "fake real" run used to bootstrap a recording in CI.
# In real life the inner provider would be a Copilot/Claude client; here we
# use a FakeProvider seeded by the caller so the recording demo is hermetic.
# ---------------------------------------------------------------------------

def make_seeded_provider(**agents) -> FakeProvider:
    fp = FakeProvider()
    for name, replies in agents.items():
        if isinstance(replies, dict):
            replies = [replies]
        fp.script(name, *replies)
    return fp
