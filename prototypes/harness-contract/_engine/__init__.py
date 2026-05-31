"""Shared stub engine for the three harness-contract variants.

This is NOT the real Requiem engine. It is the smallest thing that
gives the variants something to test:

  * `Workflow` / `Node` primitives (script / agent / human_gate /
    subworkflow / terminal)
  * a discriminated `Outcome` union (Success / RetryableFailure /
    PermanentFailure / NeedsHuman / Cancelled) — INV-DISCRIMINATED-OUTCOMES
  * `WorkflowEngine.run()` that walks the node graph, retries on
    RetryableFailure, and appends events to a JSONL `EventLog`
  * resumability — kill the engine, point it at the same `EventLog`
    file, and it picks up after the last NodeCompleted event
    (INV-RESTART, INV-EVENT-LOG-AUTHORITATIVE)
  * a `ChaosHook` seam so scenarios can inject failures / kills

Real Requiem will have a far richer engine; for seam-shaping this is
all three variants need to differentiate themselves.
"""

from .outcomes import (
    Outcome,
    Success,
    RetryableFailure,
    PermanentFailure,
    NeedsHuman,
    Cancelled,
)
from .events import Event, EventLog
from .workflow import Node, Workflow
from .provider import AgentProvider, FakeProvider, ScriptedAgent
from .engine import WorkflowEngine, ChaosHook, KillRequested
from .examples import (
    tiny_three_node,
    transient_failure_workflow,
    gated_workflow,
    parent_with_subworkflow,
)

__all__ = [
    "Outcome",
    "Success",
    "RetryableFailure",
    "PermanentFailure",
    "NeedsHuman",
    "Cancelled",
    "Event",
    "EventLog",
    "Node",
    "Workflow",
    "AgentProvider",
    "FakeProvider",
    "ScriptedAgent",
    "WorkflowEngine",
    "ChaosHook",
    "KillRequested",
    "tiny_three_node",
    "transient_failure_workflow",
    "gated_workflow",
    "parent_with_subworkflow",
]
