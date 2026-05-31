"""Variant B kernel: every node is an `async def` returning an Outcome.

The engine is an `async def loop`. Node bodies can await I/O directly
(real LLM HTTP, file ops, etc.) without blocking the engine. Sub-
workflow calls and parallel composition are native asyncio idioms.

Suspension model: the engine writes `human_gate_presented`, returns
`Suspended` to the awaiter. The operator calls `resolve_gate`. The
next `await engine.run(...)` replays the log and continues.

Cancellation model: cancellation is a durable event AND an
`asyncio.Event` the engine checks at every node boundary. Cooperative
node bodies can also await the event; any `await` point in a node
body can be cancelled via `asyncio.CancelledError`, which the engine
converts to a `Cancelled` outcome.

INV-RESTART is achieved exactly as variant A: kill the process, the
event log on disk lets the next `run()` reconstruct the resume point.
Coroutine state is NOT durable — verbs must be idempotent.
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from outcomes import (
    Outcome, Success, RetryableFailure, PermanentFailure, NeedsHuman, Cancelled,
)
from events import EventLog


# ----- Run results -----


@dataclass
class Suspended:
    run_id: str
    node_id: str
    prompt: str
    options: list[str]


@dataclass
class Completed:
    run_id: str
    disposition: str
    final_node: str


@dataclass
class RunCancelled:
    run_id: str
    reason: str


@dataclass
class RunFailed:
    run_id: str
    node_id: str
    reason: str
    error_kind: str


RunResult = Suspended | Completed | RunCancelled | RunFailed


# ----- Node context -----


@dataclass
class NodeContext:
    run_id: str
    workflow_id: str
    node_id: str
    inputs: dict[str, Any]
    completed: dict[str, dict[str, Any]]
    attempt: int
    cancel_event: asyncio.Event


# A node is just an async callable: NodeContext -> Outcome.
NodeFn = Callable[[NodeContext], Awaitable[Outcome]]


# ----- Workflow shape -----


@dataclass
class Node:
    node_id: str
    fn: NodeFn
    retry_max: int = 0
    # If kind == "human_gate" the engine treats the node's NeedsHuman
    # outcome as a suspension point and uses option labels for routing.
    kind: str = "regular"   # regular | human_gate | subworkflow | terminate | route
    # for subworkflow nodes:
    target_workflow: str | None = None
    # for terminate nodes:
    disposition: str | None = None


@dataclass
class Workflow:
    workflow_id: str
    start: str
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: dict[tuple[str, str], str] = field(default_factory=dict)

    def add(self, n: Node) -> "Workflow":
        self.nodes[n.node_id] = n
        return self

    def edge(self, from_id: str, outcome_key: str, to_id: str) -> "Workflow":
        self.edges[(from_id, outcome_key)] = to_id
        return self

    def route(self, from_id: str, label: str, to_id: str) -> "Workflow":
        return self.edge(from_id, f"success:{label}", to_id)

    def transition_for(self, from_id: str, key: str) -> str | None:
        return self.edges.get((from_id, key))


# ----- Engine -----


@dataclass
class Engine:
    workflows: dict[str, Workflow]
    log_dir: Path
    _cancel_events: dict[str, asyncio.Event] = field(default_factory=dict)

    def log_path(self, run_id: str) -> Path:
        return self.log_dir / f"{run_id}.events.jsonl"

    def _cancel_event(self, run_id: str) -> asyncio.Event:
        e = self._cancel_events.get(run_id)
        if e is None:
            e = asyncio.Event()
            self._cancel_events[run_id] = e
        return e

    def cancel(self, run_id: str, reason: str = "operator") -> None:
        self._cancel_event(run_id).set()
        EventLog(self.log_path(run_id)).append(
            "cancel_received", run_id=run_id, reason=reason
        )

    def resolve_gate(self, run_id: str, choice: str) -> None:
        EventLog(self.log_path(run_id)).append(
            "human_gate_resolved", run_id=run_id, choice=choice
        )

    async def run(
        self, workflow_id: str, run_id: str,
        inputs: dict[str, Any] | None = None,
    ) -> RunResult:
        wf = self.workflows[workflow_id]
        log = EventLog(self.log_path(run_id))
        replay = log.replay()

        has_started = any(e["type"] == "workflow_started" for e in replay)
        has_term = any(e["type"] == "workflow_terminated" for e in replay)
        has_cancel = (
            any(e["type"] == "cancel_received" for e in replay)
            or self._cancel_event(run_id).is_set()
        )
        if not has_term and has_cancel:
            if not has_started:
                log.append("workflow_started", run_id=run_id,
                           workflow=workflow_id, inputs=inputs or {}, scope=[])
            return self._terminate_cancelled(log, run_id, "<pre-start>",
                                             "cancel received before run started")

        if not replay:
            log.append("workflow_started", run_id=run_id,
                       workflow=workflow_id, inputs=inputs or {}, scope=[])
            current = wf.start
            completed: dict[str, dict[str, Any]] = {}
            attempt = 1
        else:
            r = self._reconstruct(wf, replay, run_id)
            if isinstance(r, (Suspended, Completed, RunCancelled, RunFailed)):
                return r
            current, completed, attempt = r

        return await self._loop(
            wf=wf, log=log, run_id=run_id, inputs=inputs or {},
            current=current, completed=completed, attempt=attempt, scope=[],
        )

    def _reconstruct(
        self, wf: Workflow, replay: list[dict[str, Any]], run_id: str,
    ) -> RunResult | tuple[str, dict[str, dict[str, Any]], int]:
        completed: dict[str, dict[str, Any]] = {}
        last_entered: str | None = None
        last_entered_attempt = 1
        last_completed_node: str | None = None
        last_gate_node: str | None = None
        last_choice: str | None = None
        terminated: dict[str, Any] | None = None

        for evt in replay:
            if evt.get("scope"):
                continue
            t = evt["type"]
            if t == "node_entered":
                last_entered = evt["node_id"]
                last_entered_attempt = evt.get("attempt", 1)
            elif t == "node_completed":
                completed[evt["node_id"]] = evt["outcome"]
                last_completed_node = evt["node_id"]
            elif t == "human_gate_presented":
                last_gate_node = evt["node_id"]
                last_choice = None
            elif t == "human_gate_resolved":
                last_choice = evt["choice"]
            elif t == "workflow_terminated":
                terminated = evt

        if terminated is not None:
            disp = terminated.get("disposition", "completed")
            if disp == "cancelled":
                return RunCancelled(run_id=run_id, reason=terminated.get("reason", ""))
            if disp == "failed":
                return RunFailed(run_id=run_id,
                                 node_id=terminated.get("node_id", ""),
                                 reason=terminated.get("reason", ""),
                                 error_kind=terminated.get("error_kind", "unknown"))
            return Completed(run_id=run_id, disposition=disp,
                             final_node=terminated.get("node_id", ""))

        if last_gate_node is not None and last_choice is not None:
            key = f"needs_human:{last_choice}"
            nxt = wf.transition_for(last_gate_node, key) \
                or wf.transition_for(last_gate_node, "needs_human")
            if nxt is None:
                raise RuntimeError(f"no transition from {last_gate_node!r} for {key!r}")
            return (nxt, completed, 1)
        if last_gate_node is not None and last_choice is None:
            return Suspended(run_id=run_id, node_id=last_gate_node,
                             prompt="", options=[])
        if last_entered is not None and last_entered != last_completed_node:
            return (last_entered, completed, last_entered_attempt)
        if last_completed_node is not None:
            n = wf.nodes[last_completed_node]
            key = self._outcome_key(completed[last_completed_node], n)
            nxt = wf.transition_for(last_completed_node, key)
            if nxt is None:
                raise RuntimeError(
                    f"no transition from {last_completed_node!r} for {key!r}"
                )
            return (nxt, completed, 1)
        raise RuntimeError("event log malformed; cannot reconstruct")

    async def _loop(
        self, *, wf: Workflow, log: EventLog, run_id: str,
        inputs: dict[str, Any], current: str,
        completed: dict[str, dict[str, Any]], attempt: int, scope: list[str],
    ) -> RunResult:
        cancel = self._cancel_event(run_id)
        while True:
            node = wf.nodes[current]
            if cancel.is_set() or self._durable_cancel(log):
                return self._terminate_cancelled(log, run_id, current)

            log.append("node_entered", run_id=run_id, node_id=current,
                       attempt=attempt, scope=scope)

            if node.kind == "subworkflow":
                sub_id = node.target_workflow
                sub_run_id = f"{run_id}__{current}"
                ctx = NodeContext(
                    run_id=run_id, workflow_id=wf.workflow_id, node_id=current,
                    inputs=inputs, completed=completed, attempt=attempt,
                    cancel_event=cancel,
                )
                sub_inputs = await node.fn(ctx)  # fn returns inputs as Success.value
                if not isinstance(sub_inputs, Success):
                    outcome: Outcome = sub_inputs  # propagate failure as-is
                else:
                    log.append("subworkflow_started", run_id=run_id,
                               parent_node=current, child_workflow=sub_id,
                               child_run_id=sub_run_id, scope=scope)
                    sub_result = await self.run(sub_id, sub_run_id, sub_inputs.value)
                    log.append("subworkflow_completed", run_id=run_id,
                               parent_node=current, child_workflow=sub_id,
                               child_run_id=sub_run_id,
                               result=_ser(sub_result), scope=scope)
                    if isinstance(sub_result, RunCancelled):
                        outcome = Cancelled(reason=sub_result.reason)
                    elif isinstance(sub_result, RunFailed):
                        outcome = PermanentFailure(
                            reason=sub_result.reason,
                            error_kind=f"subworkflow.{sub_id}.{sub_result.error_kind}",
                        )
                    elif isinstance(sub_result, Suspended):
                        # bubble up
                        return sub_result
                    else:  # Completed
                        outcome = Success(value={"disposition": sub_result.disposition,
                                                 "child_run_id": sub_run_id})
            else:
                ctx = NodeContext(
                    run_id=run_id, workflow_id=wf.workflow_id, node_id=current,
                    inputs=inputs, completed=completed, attempt=attempt,
                    cancel_event=cancel,
                )
                try:
                    outcome = await node.fn(ctx)
                except asyncio.CancelledError:
                    outcome = Cancelled(reason="task cancelled")
                except Exception as e:
                    outcome = PermanentFailure(
                        reason=f"verb crashed: {e!r}", error_kind="verb.crash",
                    )

            log.append("node_completed", run_id=run_id, node_id=current,
                       outcome=outcome.model_dump(), scope=scope)
            completed[current] = outcome.model_dump()

            # Branch on outcome.
            if isinstance(outcome, RetryableFailure):
                if cancel.is_set() or self._durable_cancel(log):
                    return self._terminate_cancelled(log, run_id, current)
                if attempt <= node.retry_max:
                    log.append("retry_attempted", run_id=run_id, node_id=current,
                               attempt=attempt, next_attempt=attempt + 1,
                               retry_max=node.retry_max, reason=outcome.reason,
                               error_kind=outcome.error_kind, scope=scope)
                    attempt += 1
                    continue
                nxt = wf.transition_for(current, "retry_exhausted")
                if nxt is None:
                    return self._terminate(log, run_id, current, "failed",
                                           reason=f"retry exhausted: {outcome.reason}",
                                           error_kind=outcome.error_kind)
                log.append("route_taken", run_id=run_id, from_node=current,
                           key="retry_exhausted", to_node=nxt, scope=scope)
                current = nxt; attempt = 1; continue

            if isinstance(outcome, Cancelled):
                return self._terminate_cancelled(log, run_id, current,
                                                 reason=outcome.reason)

            if isinstance(outcome, PermanentFailure):
                for key in (f"permanent_failure:{outcome.error_kind}",
                            "permanent_failure"):
                    nxt = wf.transition_for(current, key)
                    if nxt is not None:
                        log.append("route_taken", run_id=run_id,
                                   from_node=current, key=key, to_node=nxt, scope=scope)
                        current = nxt; attempt = 1; break
                else:
                    return self._terminate(log, run_id, current, "failed",
                                           reason=outcome.reason,
                                           error_kind=outcome.error_kind)
                continue

            if isinstance(outcome, NeedsHuman):
                log.append("human_gate_presented", run_id=run_id,
                           node_id=current, prompt=outcome.prompt,
                           options=outcome.options, scope=scope)
                return Suspended(run_id=run_id, node_id=current,
                                 prompt=outcome.prompt, options=list(outcome.options))

            # Success
            if node.kind == "terminate":
                return self._terminate(log, run_id, current,
                                       node.disposition or "completed")
            key = self._outcome_key(outcome.model_dump(), node)
            nxt = wf.transition_for(current, key) or wf.transition_for(current, "success")
            if nxt is None:
                raise RuntimeError(f"no transition from {current!r} for {key!r}")
            log.append("route_taken", run_id=run_id, from_node=current,
                       key=key, to_node=nxt, scope=scope)
            current = nxt; attempt = 1

    def _outcome_key(self, d: dict[str, Any], node: Node) -> str:
        kind = d["kind"]
        if node.kind == "route" and kind == "success":
            return f"success:{d.get('value', {}).get('route', 'default')}"
        return kind

    def _durable_cancel(self, log: EventLog) -> bool:
        for e in reversed(log.replay()):
            if e["type"] == "cancel_received":
                return True
            if e["type"] == "workflow_terminated":
                return False
        return False

    def _terminate(self, log, run_id, node_id, disposition,
                   reason="", error_kind=""):
        log.append("workflow_terminated", run_id=run_id, node_id=node_id,
                   disposition=disposition, reason=reason, error_kind=error_kind)
        if disposition == "cancelled":
            return RunCancelled(run_id=run_id, reason=reason)
        if disposition == "failed":
            return RunFailed(run_id=run_id, node_id=node_id,
                             reason=reason, error_kind=error_kind)
        return Completed(run_id=run_id, disposition=disposition, final_node=node_id)

    def _terminate_cancelled(self, log, run_id, node_id, reason="cancelled"):
        return self._terminate(log, run_id, node_id, "cancelled", reason=reason)


def _ser(r: RunResult) -> dict[str, Any]:
    if isinstance(r, Completed):
        return {"kind": "completed", "disposition": r.disposition,
                "final_node": r.final_node}
    if isinstance(r, Suspended):
        return {"kind": "suspended", "node_id": r.node_id}
    if isinstance(r, RunCancelled):
        return {"kind": "cancelled", "reason": r.reason}
    if isinstance(r, RunFailed):
        return {"kind": "failed", "node_id": r.node_id,
                "reason": r.reason, "error_kind": r.error_kind}
    raise TypeError(r)


# ----- Author-facing sugar -----


def agent(node_id: str, fn: NodeFn, retry_max: int = 0) -> Node:
    return Node(node_id=node_id, fn=fn, retry_max=retry_max, kind="regular")


def human_gate(node_id: str, prompt: str, options: list[str]) -> Node:
    async def fn(ctx: NodeContext) -> Outcome:
        return NeedsHuman(prompt=prompt, options=options)
    return Node(node_id=node_id, fn=fn, kind="human_gate")


def route(node_id: str, chooser: Callable[[NodeContext], str]) -> Node:
    async def fn(ctx: NodeContext) -> Outcome:
        r = chooser(ctx)
        if inspect.iscoroutine(r):
            r = await r
        return Success(value={"route": r})
    return Node(node_id=node_id, fn=fn, kind="route")


def subworkflow(node_id: str, target: str,
                inputs_fn: Callable[[NodeContext], dict[str, Any]]) -> Node:
    async def fn(ctx: NodeContext) -> Outcome:
        return Success(value=inputs_fn(ctx))
    return Node(node_id=node_id, fn=fn, kind="subworkflow", target_workflow=target)


def terminate(node_id: str, disposition: str = "completed") -> Node:
    async def fn(ctx: NodeContext) -> Outcome:
        return Success(value={"disposition": disposition})
    return Node(node_id=node_id, fn=fn, kind="terminate", disposition=disposition)
