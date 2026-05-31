"""Variant C engine — an interpreter over the WorkflowModel.

Same invariants as A and B. Same event log format. The DIFFERENCE is
that nodes are looked up by `kind` discriminator and verbs by name,
not by Python type — so the engine knows nothing about user code
beyond what the registry exposes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from outcomes import (
    Outcome, Success, RetryableFailure, PermanentFailure, NeedsHuman, Cancelled,
)
from events import EventLog
from model import (
    WorkflowModel, NodeModel,
    AgentNode, ScriptNode, HumanGateNode, RouteNode,
    SubworkflowNode, TerminateNode, VerbRegistry,
)


# ----- Run results -----


@dataclass
class Suspended:
    run_id: str; node_id: str; prompt: str; options: list[str]
@dataclass
class Completed:
    run_id: str; disposition: str; final_node: str
@dataclass
class RunCancelled:
    run_id: str; reason: str
@dataclass
class RunFailed:
    run_id: str; node_id: str; reason: str; error_kind: str


RunResult = Suspended | Completed | RunCancelled | RunFailed


# ----- Verb context (what verbs see) -----


@dataclass
class VerbContext:
    run_id: str; workflow_id: str; node_id: str
    inputs: dict[str, Any]
    completed: dict[str, dict[str, Any]]
    attempt: int
    cancel_requested: bool


# ----- Engine -----


@dataclass
class Engine:
    workflows: dict[str, WorkflowModel]
    verbs: VerbRegistry
    log_dir: Path
    _cancel_flags: dict[str, bool] = field(default_factory=dict)

    def log_path(self, run_id: str) -> Path:
        return self.log_dir / f"{run_id}.events.jsonl"

    def cancel(self, run_id: str, reason: str = "operator") -> None:
        self._cancel_flags[run_id] = True
        EventLog(self.log_path(run_id)).append(
            "cancel_received", run_id=run_id, reason=reason
        )

    def resolve_gate(self, run_id: str, choice: str) -> None:
        EventLog(self.log_path(run_id)).append(
            "human_gate_resolved", run_id=run_id, choice=choice
        )

    def validate(self, workflow_id: str) -> list[str]:
        return self.workflows[workflow_id].validate_topology()

    def run(self, workflow_id: str, run_id: str,
            inputs: dict[str, Any] | None = None) -> RunResult:
        wf = self.workflows[workflow_id]
        errs = wf.validate_topology()
        if errs:
            raise ValueError(f"workflow {workflow_id!r} invalid: {errs}")
        log = EventLog(self.log_path(run_id))
        replay = log.replay()

        has_started = any(e["type"] == "workflow_started" for e in replay)
        has_term = any(e["type"] == "workflow_terminated" for e in replay)
        has_cancel = (
            any(e["type"] == "cancel_received" for e in replay)
            or self._cancel_flags.get(run_id, False)
        )
        if not has_term and has_cancel:
            if not has_started:
                log.append("workflow_started", run_id=run_id,
                           workflow=workflow_id, inputs=inputs or {}, scope=[])
            return self._terminate_cancelled(log, run_id, "<pre-start>",
                                             "cancel received before run started")

        nm = wf.node_map(); em = wf.edge_map()

        if not replay:
            log.append("workflow_started", run_id=run_id,
                       workflow=workflow_id, inputs=inputs or {}, scope=[])
            current = wf.start
            completed: dict[str, dict[str, Any]] = {}
            attempt = 1
        else:
            r = self._reconstruct(nm, em, replay, run_id)
            if isinstance(r, (Suspended, Completed, RunCancelled, RunFailed)):
                return r
            current, completed, attempt = r

        return self._loop(wf=wf, nm=nm, em=em, log=log, run_id=run_id,
                          inputs=inputs or {}, current=current,
                          completed=completed, attempt=attempt, scope=[])

    def _reconstruct(
        self, nm: dict[str, NodeModel], em: dict[tuple[str, str], str],
        replay: list[dict[str, Any]], run_id: str,
    ) -> RunResult | tuple[str, dict[str, dict[str, Any]], int]:
        completed: dict[str, dict[str, Any]] = {}
        last_entered = None; last_entered_attempt = 1
        last_completed_node = None
        last_gate_node = None; last_choice = None
        terminated = None
        for evt in replay:
            if evt.get("scope"): continue
            t = evt["type"]
            if t == "node_entered":
                last_entered = evt["node_id"]
                last_entered_attempt = evt.get("attempt", 1)
            elif t == "node_completed":
                completed[evt["node_id"]] = evt["outcome"]
                last_completed_node = evt["node_id"]
            elif t == "human_gate_presented":
                last_gate_node = evt["node_id"]; last_choice = None
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
            nxt = em.get((last_gate_node, key)) or em.get((last_gate_node, "needs_human"))
            if nxt is None:
                raise RuntimeError(f"no transition from {last_gate_node!r} for {key!r}")
            return (nxt, completed, 1)
        if last_gate_node is not None and last_choice is None:
            return Suspended(run_id=run_id, node_id=last_gate_node,
                             prompt="", options=[])
        if last_entered is not None and last_entered != last_completed_node:
            return (last_entered, completed, last_entered_attempt)
        if last_completed_node is not None:
            n = nm[last_completed_node]
            key = self._outcome_key(completed[last_completed_node], n)
            nxt = em.get((last_completed_node, key))
            if nxt is None:
                raise RuntimeError(f"no transition from {last_completed_node!r} for {key!r}")
            return (nxt, completed, 1)
        raise RuntimeError("event log malformed")

    def _loop(self, *, wf: WorkflowModel,
              nm: dict[str, NodeModel], em: dict[tuple[str, str], str],
              log: EventLog, run_id: str, inputs: dict[str, Any],
              current: str, completed: dict[str, dict[str, Any]],
              attempt: int, scope: list[str]) -> RunResult:
        while True:
            node = nm[current]
            if self._is_cancel(run_id, log):
                return self._terminate_cancelled(log, run_id, current)
            log.append("node_entered", run_id=run_id, node_id=current,
                       attempt=attempt, scope=scope)

            outcome = self._execute(node, run_id, wf.workflow_id,
                                    inputs, completed, attempt, log, scope)
            # Special case: sub-workflow suspended bubbles up.
            if isinstance(outcome, _BubbleSuspended):
                return outcome.suspended

            log.append("node_completed", run_id=run_id, node_id=current,
                       outcome=outcome.model_dump(), scope=scope)
            completed[current] = outcome.model_dump()

            if isinstance(outcome, RetryableFailure):
                if self._is_cancel(run_id, log):
                    return self._terminate_cancelled(log, run_id, current)
                if attempt <= getattr(node, "retry_max", 0):
                    log.append("retry_attempted", run_id=run_id,
                               node_id=current, attempt=attempt,
                               next_attempt=attempt + 1,
                               retry_max=getattr(node, "retry_max", 0),
                               reason=outcome.reason,
                               error_kind=outcome.error_kind, scope=scope)
                    attempt += 1; continue
                nxt = em.get((current, "retry_exhausted"))
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
                    nxt = em.get((current, key))
                    if nxt is not None:
                        log.append("route_taken", run_id=run_id,
                                   from_node=current, key=key, to_node=nxt,
                                   scope=scope)
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
                                 prompt=outcome.prompt,
                                 options=list(outcome.options))

            # Success
            if isinstance(node, TerminateNode):
                return self._terminate(log, run_id, current, node.disposition)
            key = self._outcome_key(outcome.model_dump(), node)
            nxt = em.get((current, key)) or em.get((current, "success"))
            if nxt is None:
                raise RuntimeError(f"no transition from {current!r} for {key!r}")
            log.append("route_taken", run_id=run_id, from_node=current,
                       key=key, to_node=nxt, scope=scope)
            current = nxt; attempt = 1

    def _execute(self, node: NodeModel, run_id: str, workflow_id: str,
                 inputs: dict[str, Any],
                 completed: dict[str, dict[str, Any]],
                 attempt: int, log: EventLog, scope: list[str]) -> Outcome:
        ctx = VerbContext(
            run_id=run_id, workflow_id=workflow_id, node_id=node.node_id,
            inputs=inputs, completed=completed, attempt=attempt,
            cancel_requested=self._cancel_flags.get(run_id, False),
        )
        try:
            if isinstance(node, (AgentNode, ScriptNode)):
                fn = self.verbs.get(node.verb)
                return fn(ctx)
            if isinstance(node, HumanGateNode):
                return NeedsHuman(prompt=node.prompt, options=list(node.options))
            if isinstance(node, RouteNode):
                fn = self.verbs.get(node.chooser)
                choice = fn(ctx)
                return Success(value={"route": choice})
            if isinstance(node, SubworkflowNode):
                inputs_fn = self.verbs.get(node.inputs_from)
                sub_inputs = inputs_fn(ctx)
                sub_run_id = f"{run_id}__{node.node_id}"
                log.append("subworkflow_started", run_id=run_id,
                           parent_node=node.node_id,
                           child_workflow=node.target_workflow,
                           child_run_id=sub_run_id, scope=scope)
                sub_result = self.run(node.target_workflow, sub_run_id, sub_inputs)
                log.append("subworkflow_completed", run_id=run_id,
                           parent_node=node.node_id,
                           child_workflow=node.target_workflow,
                           child_run_id=sub_run_id,
                           result=_ser(sub_result), scope=scope)
                if isinstance(sub_result, RunCancelled):
                    return Cancelled(reason=sub_result.reason)
                if isinstance(sub_result, RunFailed):
                    return PermanentFailure(
                        reason=sub_result.reason,
                        error_kind=f"subworkflow.{node.target_workflow}.{sub_result.error_kind}",
                    )
                if isinstance(sub_result, Suspended):
                    # bubble up; _loop will detect and return.
                    return _BubbleSuspended(sub_result)  # type: ignore[return-value]
                return Success(value={"disposition": sub_result.disposition,
                                      "child_run_id": sub_run_id})
            if isinstance(node, TerminateNode):
                return Success(value={"disposition": node.disposition})
            raise TypeError(f"unknown node kind: {type(node).__name__}")
        except Exception as e:
            return PermanentFailure(reason=f"verb crashed: {e!r}",
                                    error_kind="verb.crash")

    def _outcome_key(self, d: dict[str, Any], node: NodeModel) -> str:
        kind = d["kind"]
        if isinstance(node, RouteNode) and kind == "success":
            return f"success:{d.get('value', {}).get('route', 'default')}"
        return kind

    def _is_cancel(self, run_id: str, log: EventLog) -> bool:
        if self._cancel_flags.get(run_id): return True
        for e in reversed(log.replay()):
            if e["type"] == "cancel_received":
                self._cancel_flags[run_id] = True
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


class _SubworkflowSuspension(Exception):
    def __init__(self, suspended: Suspended):
        self.suspended = suspended


@dataclass
class _BubbleSuspended:
    """Internal sentinel: not an Outcome, but smuggled through the
    execute → loop boundary to surface a sub-workflow suspension."""
    suspended: Suspended


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
