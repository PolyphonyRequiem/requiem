"""The engine — variant A.

A loop. Pick current node, execute, take its outcome, look up
transition, advance. Retries are a tight inner loop. Cancellation
short-circuits. Sub-workflows are nested engine.run calls.

INV-RESTART: any run can be killed at any time. On the next call to
`run()` with the same run_id, the engine replays the event log,
rebuilds completed[], finds the last in-flight node, and resumes.

INV-CANCEL-SHORT-CIRCUITS-RETRY: a Cancelled outcome or a cancel
token tripped between retries terminates the retry loop immediately.

INV-NO-ENGINE-ABANDONMENT: the engine never picks 'abandoned' on its
own. Retry exhaustion routes to a *retry_exhausted* outcome key,
which the workflow author must route somewhere — usually to a
HumanGate.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from outcomes import (
    Outcome,
    Success,
    RetryableFailure,
    PermanentFailure,
    NeedsHuman,
    Cancelled,
)
from events import EventLog
from nodes import Node, NodeContext, HumanGate, SubworkflowCall, Terminate, Route
from workflow import Workflow


# ----- Run results returned to the caller -----


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


# ----- Engine -----


@dataclass
class Engine:
    workflows: dict[str, Workflow]
    log_dir: Path
    # Per-run cooperative cancel flags (in-memory; durable cancel
    # would be a `cancel_received` event before `run()` is called).
    _cancel_flags: dict[str, bool] = field(default_factory=dict)

    def log_path(self, run_id: str) -> Path:
        return self.log_dir / f"{run_id}.events.jsonl"

    def cancel(self, run_id: str, reason: str = "operator") -> None:
        """Mark a run cancelled. Durable: appends an event the next
        engine pass will observe even if invoked between process
        restarts."""
        self._cancel_flags[run_id] = True
        log = EventLog(self.log_path(run_id))
        log.append("cancel_received", run_id=run_id, reason=reason)

    def resolve_gate(self, run_id: str, choice: str) -> None:
        log = EventLog(self.log_path(run_id))
        log.append("human_gate_resolved", run_id=run_id, choice=choice)

    # ---- public entrypoint ----

    def run(
        self,
        workflow_id: str,
        run_id: str,
        inputs: dict[str, Any] | None = None,
    ) -> RunResult:
        wf = self.workflows[workflow_id]
        wf.validate()
        log = EventLog(self.log_path(run_id))
        replay = log.replay()

        # Short-circuit: cancel arrived (durable or in-memory) before
        # the workflow ever entered a node. Honour it immediately.
        has_started = any(e["type"] == "workflow_started" for e in replay)
        has_terminated = any(e["type"] == "workflow_terminated" for e in replay)
        has_cancel = (
            any(e["type"] == "cancel_received" for e in replay)
            or self._cancel_flags.get(run_id, False)
        )
        if not has_terminated and has_cancel:
            if not has_started:
                log.append(
                    "workflow_started",
                    run_id=run_id,
                    workflow=workflow_id,
                    inputs=inputs or {},
                    scope=[],
                )
            return self._terminate_cancelled(
                log, run_id, node_id="<pre-start>",
                reason="cancel received before run started",
            )

        if not replay:
            log.append(
                "workflow_started",
                run_id=run_id,
                workflow=workflow_id,
                inputs=inputs or {},
                scope=[],
            )
            current_node = wf.start
            completed: dict[str, dict[str, Any]] = {}
            attempt = 1
        else:
            current_node, completed, attempt, terminal = self._reconstruct(
                wf, replay
            )
            if terminal is not None:
                return terminal

        return self._loop(
            wf=wf,
            log=log,
            run_id=run_id,
            inputs=inputs or {},
            current_node=current_node,
            completed=completed,
            attempt=attempt,
            scope=[],
        )

    # ---- reconstruction (INV-RESTART) ----

    def _reconstruct(
        self, wf: Workflow, replay: list[dict[str, Any]]
    ) -> tuple[str, dict[str, dict[str, Any]], int, RunResult | None]:
        """Walk the event log; figure out the resume point.

        Strategy:
          - completed[node_id] = outcome of any node that has both
            `node_entered` and `node_completed`.
          - if last `node_entered` has no `node_completed`, resume by
            re-executing that node (idempotency is the verb's contract).
          - if the most recent uncompleted entry was a HumanGate AND
            we have a `human_gate_resolved` after the gate's
            `human_gate_presented`, follow the gate's transition.
          - if `workflow_terminated` present, return the terminal result.
        """
        completed: dict[str, dict[str, Any]] = {}
        last_entered: str | None = None
        last_entered_attempt = 1
        last_completed_kind: str | None = None
        last_completed_node: str | None = None
        last_gate_presented_node: str | None = None
        last_gate_choice: str | None = None
        terminated: dict[str, Any] | None = None

        for evt in replay:
            t = evt["type"]
            scope = evt.get("scope", [])
            # Only consider root-scope events for top-level reconstruction.
            if scope:
                continue
            if t == "node_entered":
                last_entered = evt["node_id"]
                last_entered_attempt = evt.get("attempt", 1)
            elif t == "node_completed":
                completed[evt["node_id"]] = evt["outcome"]
                last_completed_kind = evt["outcome"]["kind"]
                last_completed_node = evt["node_id"]
            elif t == "retry_attempted":
                # Next entry will be a fresh node_entered with attempt++.
                pass
            elif t == "human_gate_presented":
                last_gate_presented_node = evt["node_id"]
                last_gate_choice = None
            elif t == "human_gate_resolved":
                last_gate_choice = evt["choice"]
            elif t == "workflow_terminated":
                terminated = evt

        if terminated is not None:
            disp = terminated.get("disposition", "completed")
            if disp == "cancelled":
                return ("", {}, 1, RunCancelled(
                    run_id=terminated["run_id"], reason=terminated.get("reason", "")
                ))
            if disp == "failed":
                return ("", {}, 1, RunFailed(
                    run_id=terminated["run_id"],
                    node_id=terminated.get("node_id", ""),
                    reason=terminated.get("reason", ""),
                    error_kind=terminated.get("error_kind", "unknown"),
                ))
            return ("", {}, 1, Completed(
                run_id=terminated["run_id"],
                disposition=disp,
                final_node=terminated.get("node_id", ""),
            ))

        # Resume points (in priority order):

        # 1. A gate was presented and resolved. Transition on the
        #    operator's choice. This case wins even if a node_completed
        #    for the gate was also written (it is — engine writes it
        #    before suspending).
        if (
            last_gate_presented_node is not None
            and last_gate_choice is not None
        ):
            key = f"needs_human:{last_gate_choice}"
            nxt = wf.transition_for(last_gate_presented_node, key)
            if nxt is None:
                nxt = wf.transition_for(last_gate_presented_node, "needs_human")
            if nxt is None:
                raise RuntimeError(
                    f"no transition from {last_gate_presented_node!r} "
                    f"for key {key!r}"
                )
            return (nxt, completed, 1, None)

        # 2. A gate was presented but NOT yet resolved → still suspended.
        if (
            last_gate_presented_node is not None
            and last_gate_choice is None
        ):
            # Re-emit Suspended so the caller can resolve and retry.
            # We can't return RunResult here from _reconstruct without
            # widening the type; encode as a special terminal sentinel:
            # the engine will detect on entry to _loop and just return.
            # Simpler: surface as a Suspended via the terminal slot.
            return (
                last_gate_presented_node, completed, 1,
                Suspended(
                    run_id=replay[0].get("run_id", ""),
                    node_id=last_gate_presented_node,
                    prompt="",
                    options=[],
                ),
            )

        # 3. A node was entered after its last completion → in-flight
        #    crash. Re-execute (verb idempotency contract).
        if last_entered is not None and last_entered != last_completed_node:
            return (last_entered, completed, last_entered_attempt, None)

        # 4. Last node completed cleanly; pick its transition.
        if last_completed_node is not None:
            key = self._outcome_key(
                completed[last_completed_node], wf.nodes[last_completed_node]
            )
            nxt = wf.transition_for(last_completed_node, key)
            if nxt is None:
                raise RuntimeError(
                    f"no transition from {last_completed_node!r} for key {key!r}"
                )
            return (nxt, completed, 1, None)

        raise RuntimeError("event log is malformed; cannot reconstruct")

    # ---- the loop ----

    def _loop(
        self,
        *,
        wf: Workflow,
        log: EventLog,
        run_id: str,
        inputs: dict[str, Any],
        current_node: str,
        completed: dict[str, dict[str, Any]],
        attempt: int,
        scope: list[str],
    ) -> RunResult:
        while True:
            node = wf.nodes[current_node]

            # Cancellation observed BEFORE entering the node.
            if self._is_cancel_pending(run_id, log):
                return self._terminate_cancelled(log, run_id, current_node)

            # Sub-workflow: recurse before emitting node_entered? No —
            # we DO emit node_entered so the topology view sees it.
            log.append(
                "node_entered",
                run_id=run_id,
                node_id=current_node,
                attempt=attempt,
                scope=scope,
            )

            if isinstance(node, SubworkflowCall):
                sub_id = node.target_workflow
                sub_run_id = f"{run_id}__{current_node}"
                sub_inputs = node.inputs_from(
                    self._make_ctx(run_id, wf, current_node, inputs, completed, attempt)
                )
                log.append(
                    "subworkflow_started",
                    run_id=run_id,
                    parent_node=current_node,
                    child_workflow=sub_id,
                    child_run_id=sub_run_id,
                    scope=scope,
                )
                sub_result = self.run(sub_id, sub_run_id, sub_inputs)
                log.append(
                    "subworkflow_completed",
                    run_id=run_id,
                    parent_node=current_node,
                    child_workflow=sub_id,
                    child_run_id=sub_run_id,
                    result=_serialize_result(sub_result),
                    scope=scope,
                )
                if isinstance(sub_result, RunCancelled):
                    outcome: Outcome = Cancelled(reason=sub_result.reason)
                elif isinstance(sub_result, RunFailed):
                    outcome = PermanentFailure(
                        reason=sub_result.reason,
                        error_kind=f"subworkflow.{sub_id}.{sub_result.error_kind}",
                    )
                elif isinstance(sub_result, Suspended):
                    # Sub-workflow suspended on a gate; bubble up unchanged.
                    # Parent's node_completed not emitted; we exit the loop.
                    return sub_result
                else:  # Completed
                    outcome = Success(
                        value={"disposition": sub_result.disposition,
                               "child_run_id": sub_run_id}
                    )
            else:
                ctx = self._make_ctx(
                    run_id, wf, current_node, inputs, completed, attempt
                )
                try:
                    outcome = node.execute(ctx)
                except Exception as e:
                    # Verb crash -> PermanentFailure variant.
                    outcome = PermanentFailure(
                        reason=f"verb crashed: {e!r}",
                        error_kind="verb.crash",
                    )

            log.append(
                "node_completed",
                run_id=run_id,
                node_id=current_node,
                outcome=outcome.model_dump(),
                scope=scope,
            )
            completed[current_node] = outcome.model_dump()

            # Branch on outcome kind.
            if isinstance(outcome, RetryableFailure):
                # Cancel observed between attempts? Short-circuit (INV).
                if self._is_cancel_pending(run_id, log):
                    return self._terminate_cancelled(log, run_id, current_node)
                if attempt <= node.retry_max:
                    log.append(
                        "retry_attempted",
                        run_id=run_id,
                        node_id=current_node,
                        attempt=attempt,
                        next_attempt=attempt + 1,
                        retry_max=node.retry_max,
                        reason=outcome.reason,
                        error_kind=outcome.error_kind,
                        scope=scope,
                    )
                    attempt += 1
                    continue
                # Exhausted. INV-NO-ENGINE-ABANDONMENT: don't pick
                # abandoned. Route on `retry_exhausted` key; if author
                # didn't wire it, fail loudly.
                key = "retry_exhausted"
                nxt = wf.transition_for(current_node, key)
                if nxt is None:
                    return self._terminate(
                        log, run_id, current_node, "failed",
                        reason=f"retry exhausted: {outcome.reason}",
                        error_kind=outcome.error_kind,
                    )
                log.append(
                    "route_taken",
                    run_id=run_id,
                    from_node=current_node,
                    key=key,
                    to_node=nxt,
                    scope=scope,
                )
                current_node = nxt
                attempt = 1
                continue

            if isinstance(outcome, Cancelled):
                return self._terminate_cancelled(
                    log, run_id, current_node, reason=outcome.reason
                )

            if isinstance(outcome, PermanentFailure):
                # Route on permanent_failure[:error_kind] then bare
                # permanent_failure. If neither wired, terminate failed.
                for key in (f"permanent_failure:{outcome.error_kind}",
                            "permanent_failure"):
                    nxt = wf.transition_for(current_node, key)
                    if nxt is not None:
                        log.append(
                            "route_taken",
                            run_id=run_id,
                            from_node=current_node,
                            key=key,
                            to_node=nxt,
                            scope=scope,
                        )
                        current_node = nxt
                        attempt = 1
                        break
                else:
                    return self._terminate(
                        log, run_id, current_node, "failed",
                        reason=outcome.reason,
                        error_kind=outcome.error_kind,
                    )
                continue

            if isinstance(outcome, NeedsHuman):
                log.append(
                    "human_gate_presented",
                    run_id=run_id,
                    node_id=current_node,
                    prompt=outcome.prompt,
                    options=outcome.options,
                    scope=scope,
                )
                return Suspended(
                    run_id=run_id,
                    node_id=current_node,
                    prompt=outcome.prompt,
                    options=list(outcome.options),
                )

            # Success.
            if isinstance(node, Terminate):
                return self._terminate(
                    log, run_id, current_node, node.disposition,
                )

            key = self._outcome_key(outcome.model_dump(), node)
            nxt = wf.transition_for(current_node, key)
            if nxt is None:
                # Try bare "success" as fallback.
                nxt = wf.transition_for(current_node, "success")
            if nxt is None:
                raise RuntimeError(
                    f"no transition from {current_node!r} for key {key!r}"
                )
            log.append(
                "route_taken",
                run_id=run_id,
                from_node=current_node,
                key=key,
                to_node=nxt,
                scope=scope,
            )
            current_node = nxt
            attempt = 1

    # ---- helpers ----

    def _make_ctx(
        self,
        run_id: str,
        wf: Workflow,
        node_id: str,
        inputs: dict[str, Any],
        completed: dict[str, dict[str, Any]],
        attempt: int,
    ) -> NodeContext:
        return NodeContext(
            run_id=run_id,
            workflow_id=wf.workflow_id,
            node_id=node_id,
            inputs=inputs,
            completed=completed,
            attempt=attempt,
            cancel_requested=lambda: self._cancel_flags.get(run_id, False),
        )

    def _outcome_key(self, outcome_dict: dict[str, Any], node: Node) -> str:
        kind = outcome_dict["kind"]
        if isinstance(node, Route) and kind == "success":
            return f"success:{outcome_dict.get('value', {}).get('route', 'default')}"
        return kind

    def _is_cancel_pending(self, run_id: str, log: EventLog) -> bool:
        if self._cancel_flags.get(run_id):
            return True
        # Durable cancel detection: a cancel_received event without
        # a matching workflow_terminated.
        for evt in reversed(log.replay()):
            if evt["type"] == "cancel_received":
                self._cancel_flags[run_id] = True
                return True
            if evt["type"] == "workflow_terminated":
                return False
        return False

    def _terminate(
        self,
        log: EventLog,
        run_id: str,
        node_id: str,
        disposition: str,
        reason: str = "",
        error_kind: str = "",
    ) -> RunResult:
        log.append(
            "workflow_terminated",
            run_id=run_id,
            node_id=node_id,
            disposition=disposition,
            reason=reason,
            error_kind=error_kind,
        )
        if disposition == "cancelled":
            return RunCancelled(run_id=run_id, reason=reason)
        if disposition == "failed":
            return RunFailed(
                run_id=run_id, node_id=node_id,
                reason=reason, error_kind=error_kind,
            )
        return Completed(run_id=run_id, disposition=disposition, final_node=node_id)

    def _terminate_cancelled(
        self, log: EventLog, run_id: str, node_id: str, reason: str = "cancelled"
    ) -> RunResult:
        return self._terminate(log, run_id, node_id, "cancelled", reason=reason)


def _serialize_result(r: RunResult) -> dict[str, Any]:
    if isinstance(r, Completed):
        return {"kind": "completed", "disposition": r.disposition,
                "final_node": r.final_node}
    if isinstance(r, Suspended):
        return {"kind": "suspended", "node_id": r.node_id,
                "prompt": r.prompt, "options": r.options}
    if isinstance(r, RunCancelled):
        return {"kind": "cancelled", "reason": r.reason}
    if isinstance(r, RunFailed):
        return {"kind": "failed", "node_id": r.node_id,
                "reason": r.reason, "error_kind": r.error_kind}
    raise TypeError(f"unknown result {r!r}")
