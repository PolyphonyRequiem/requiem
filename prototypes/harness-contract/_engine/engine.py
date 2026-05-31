"""WorkflowEngine — the minimum that exercises every invariant.

Single-process Python (INV-SINGLE-PROCESS); events written to disk
before routing decisions (INV-EVENT-LOG-AUTHORITATIVE); retries capped
at retry_max with discriminated outcomes (INV-DISCRIMINATED-OUTCOMES,
INV-CANCEL-SHORT-CIRCUITS-RETRY); supports restart from any kill point
that left the log consistent (INV-RESTART).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .events import EventLog
from .outcomes import (
    Cancelled,
    NeedsHuman,
    Outcome,
    PermanentFailure,
    RetryableFailure,
    Success,
)
from .provider import AgentProvider
from .workflow import Node, Workflow


class KillRequested(Exception):
    """Raised by a ChaosHook to simulate a hard engine kill."""


@dataclass
class ChaosHook:
    """A single hook callable invoked AFTER every event append and
    BEFORE the engine acts on it. Raise KillRequested to simulate a
    crash. Return None otherwise.

    Variants attach a hook to script `kill_after_event` chaos."""

    on_event: Callable[[Any], None] | None = None

    def fire(self, evt):
        if self.on_event is not None:
            self.on_event(evt)


GateHandler = Callable[[str, list[str]], tuple[str, dict[str, Any]]]
"""(gate_name, options) -> (chosen_option, additional_input)"""


@dataclass
class WorkflowEngine:
    workflow: Workflow
    provider: AgentProvider
    event_log: EventLog
    gate_handler: GateHandler | None = None
    subworkflow_provider_for: Callable[[str], AgentProvider] | None = None
    chaos: ChaosHook = field(default_factory=ChaosHook)

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def run(self, run_id: str, inputs: dict[str, Any] | None = None) -> str:
        """Run (or resume) until a terminal node. Returns terminal label."""
        inputs = inputs or {}
        resume_from = self.event_log.last_completed_node(run_id=run_id)
        if resume_from is None:
            self._emit("RunStarted", run_id, inputs=inputs)
            current = self.workflow.start
        else:
            self._emit("RunResumed", run_id, after_node=resume_from)
            current = self._route_after(resume_from, last_outcome=Success())

        while True:
            node = self.workflow.nodes[current]
            self._emit("NodeEntered", run_id, node=node.id)
            outcome = self._execute_node(node, run_id)

            if isinstance(outcome, Cancelled):
                # INV-CANCEL-SHORT-CIRCUITS-RETRY — no further attempts.
                self._emit("NodeCompleted", run_id, node=node.id, outcome="cancelled")
                self._emit("RunTerminated", run_id, terminal="cancelled")
                return "cancelled"

            if isinstance(outcome, PermanentFailure):
                self._emit("NodeCompleted", run_id, node=node.id, outcome="permanent_failure", reason=outcome.reason)
                self._emit("RunTerminated", run_id, terminal="permanent_failure")
                return "permanent_failure"

            self._emit("NodeCompleted", run_id, node=node.id, outcome=outcome.kind, payload=getattr(outcome, "payload", {}))

            if node.kind == "terminal":
                terminal = node.terminal_label or "completed"
                self._emit("RunTerminated", run_id, terminal=terminal)
                return terminal

            current = self._route_after(node.id, outcome)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _emit(self, type: str, run_id: str, *, node: str | None = None, **payload):
        evt = self.event_log.append(type, run_id, node=node, **payload)
        self.chaos.fire(evt)  # may raise KillRequested

    def _execute_node(self, node: Node, run_id: str) -> Outcome:
        if node.kind == "terminal":
            return Success()

        if node.kind == "script":
            return self._with_retry(node, run_id, node.verb)

        if node.kind == "agent":
            def call() -> Outcome:
                reply = self.provider.invoke(node.agent, {})
                return Success(payload=reply)
            return self._with_retry(node, run_id, call)

        if node.kind == "human_gate":
            if self.gate_handler is None:
                raise RuntimeError(f"Workflow has gate '{node.id}' but no gate_handler was supplied.")
            self._emit("HumanGatePresented", run_id, node=node.id, options=node.options)
            chosen, extra = self.gate_handler(node.id, node.options)
            if chosen not in node.options:
                raise RuntimeError(f"Gate '{node.id}' got unknown option '{chosen}' (allowed: {node.options})")
            self._emit("HumanGateResolved", run_id, node=node.id, chosen=chosen, additional_input=extra)
            return Success(payload={"chosen": chosen, **extra})

        if node.kind == "subworkflow":
            self._emit("SubworkflowStarted", run_id, node=node.id, child=node.subworkflow.name)
            sub_provider = (
                self.subworkflow_provider_for(node.subworkflow.name)
                if self.subworkflow_provider_for is not None
                else self.provider
            )
            sub_engine = WorkflowEngine(
                workflow=node.subworkflow,
                provider=sub_provider,
                event_log=self.event_log,
                gate_handler=self.gate_handler,
                subworkflow_provider_for=self.subworkflow_provider_for,
                chaos=self.chaos,
            )
            terminal = sub_engine.run(run_id=f"{run_id}::{node.subworkflow.name}")
            self._emit("SubworkflowCompleted", run_id, node=node.id, child=node.subworkflow.name, terminal=terminal)
            return Success(payload={"child_terminal": terminal})

        raise RuntimeError(f"Unknown node kind: {node.kind}")

    def _with_retry(self, node: Node, run_id: str, verb: Callable[[], Outcome]) -> Outcome:
        attempt = 0
        while True:
            outcome = verb()
            if not isinstance(outcome, RetryableFailure):
                return outcome
            attempt += 1
            if attempt > node.retry_max:
                return PermanentFailure(reason=f"retry exhausted after {attempt} attempts: {outcome.reason}")
            self._emit("RetryAttempted", run_id, node=node.id, attempt=attempt, reason=outcome.reason)

    def _route_after(self, node_id: str, last_outcome: Outcome) -> str:
        node = self.workflow.nodes[node_id]
        if node.next is not None:
            return node.next
        if node.routes:
            key = last_outcome.payload.get("chosen") if isinstance(last_outcome, Success) else None
            if key is None and isinstance(last_outcome, Success):
                # Fall back to a `route_key` field if the verb produced one.
                key = last_outcome.payload.get("route_key")
            if key not in node.routes:
                raise RuntimeError(f"Node '{node_id}' produced route key '{key}' not in routes {list(node.routes)}")
            return node.routes[key]
        raise RuntimeError(f"Node '{node_id}' has neither `next` nor `routes`")
