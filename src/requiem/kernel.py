"""Workflow kernel — Beethoven C (data-driven interpreter).

The kernel switches on `node.kind` and looks up verbs by name. It does
not know any user code by Python type. The same engine runs scripts,
agents, gates, and parallel-fork teams; the only thing that differs is
the dispatch arm in `_execute`.

Invariants:

* `INV-RESTART` — the event log is the only durable state. `_reconstruct`
  folds the log into a `_Cursor` describing exactly where to resume.
* `INV-CANCEL-SHORT-CIRCUITS-RETRY` — `Cancelled` outcomes terminate the
  run without consulting `retry_max`.
* `INV-DISCRIMINATED-OUTCOMES` — every dispatch arm pattern-matches on
  the sealed outcome union.

---

Resume model (cleanup of Verdi-1's `_reconstruct`):

Instead of dispatching on the last event kind through several `last_*`
trackers, we fold every event into a single `_Cursor` value. The cursor
*is* the resume position; the main loop dispatches on it whether the run
is fresh or resumed. This collapses the previous four "what was the last
committed action" special cases into a single `match`.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Union

from requiem.agent import AgentCall, AgentProvider
from requiem.dsl import (
    AgentNode,
    AgentRegistry,
    HumanGateNode,
    ScriptNode,
    SubWorkflowNode,
    TeamNode,
    TerminateNode,
    VerbRegistry,
    Workflow,
)
from requiem.events import EventEmitter
from requiem.outcomes import (
    BadOutput,
    Cancelled,
    NeedsHuman,
    Outcome,
    PermanentFailure,
    RetryableFailure,
    Success,
    outcome_from_dict,
    outcome_to_dict,
)
from requiem.persistence import EventStore, replay
from requiem.toolbelt import Toolbelt


# ---- run results ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Completed:
    run_id: str
    disposition: str
    final_node: str
    projection: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Suspended:
    run_id: str
    node_id: str
    prompt: str
    options: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Failed:
    run_id: str
    node_id: str
    error_kind: str
    message: str


RunResult = Union[Completed, Suspended, Failed]


# ---- verb context (what verbs see) -----------------------------------


@dataclass
class VerbContext:
    run_id: str
    workflow: str
    node_id: str
    attempt: int
    completed: dict[str, dict[str, Any]]
    toolbelt: Toolbelt
    emitter: EventEmitter


GateHandler = Callable[[str, str, tuple[str, ...]], str]


# ---- resume cursor (the entire resume protocol) ---------------------


@dataclass(frozen=True, slots=True)
class _AtNode:
    """About to enter (or re-enter) a node and execute its verb."""
    node_id: str
    attempt: int = 1


@dataclass(frozen=True, slots=True)
class _AwaitingRoute:
    """Verb finished; the kernel must decide the next edge."""
    node_id: str
    outcome: dict[str, Any]
    attempt: int


@dataclass(frozen=True, slots=True)
class _AwaitingGate:
    """`NeedsHuman` was emitted; the kernel needs a handler choice.

    Carries ``prompt`` + ``options`` directly so the arm doesn't have to
    look them up on the originating node. Two cases motivate this:

    1. Script-returned ``NeedsHuman``: the gate metadata only exists on
       the outcome, not on the (non-existent) ``HumanGateNode``.
    2. Sub-workflow bubble-up: the parent's node is a ``SubWorkflowNode``,
       which has no ``prompt`` / ``options``; the child's gate text comes
       through the outcome.

    Reconstructed across a kill+resume from the ``gate_opened`` event.
    """
    node_id: str
    prompt: str = ""
    options: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _RouteAfterGate:
    """Handler returned a choice; the kernel must take the matching edge."""
    node_id: str
    choice: str


@dataclass(frozen=True, slots=True)
class _AwaitingSubworkflow:
    """A `subworkflow_started` event is in the log; the kernel must
    (re-)attach the child engine and run it to completion.

    Used both for first-entry (no child yet) and for resume after a
    parent crash mid-child. Re-attach is idempotent: the kernel does
    NOT emit a second `subworkflow_started` on resume; the child engine
    handles its own resume via its own log per INV-RESTART.
    """
    node_id: str
    sub_run_id: str
    sub_workflow_module: str
    attempt: int = 1


@dataclass(frozen=True, slots=True)
class _Terminated:
    terminal: str
    final_node: str


@dataclass(frozen=True, slots=True)
class _CancelledByOperator:
    """A `cancel_requested` event was seen in the log before resume."""
    final_node: str
    reason: str


_Cursor = Union[
    _AtNode, _AwaitingRoute, _AwaitingGate, _RouteAfterGate,
    _AwaitingSubworkflow, _Terminated, _CancelledByOperator,
]


def _cursor_node(cursor: _Cursor) -> str | None:
    """Best-effort: which node would the engine be touching right now?"""
    if isinstance(cursor, (_AtNode, _AwaitingRoute, _AwaitingGate, _RouteAfterGate)):
        return cursor.node_id
    if isinstance(cursor, _AwaitingSubworkflow):
        return cursor.node_id
    if isinstance(cursor, _Terminated):
        return cursor.final_node
    if isinstance(cursor, _CancelledByOperator):
        return cursor.final_node
    return None


def _pending_cancel(log_path: Path) -> str | None:
    """Return the cancel reason if a `cancel_requested` event is in the log.

    Re-reads the log tail every call (cheap for v0 demo-scale logs). Returns
    ``None`` if no cancel is pending. The engine consults this at the top of
    each loop iteration to honour INV-CANCEL-SHORT-CIRCUITS-RETRY for
    externally-injected cancels.
    """
    if not log_path.exists():
        return None
    for ev in replay(log_path):
        if ev.get("kind") == "cancel_requested":
            return ev.get("payload", {}).get("reason", "operator")
    return None


# ---- engine ----------------------------------------------------------


@dataclass
class Engine:
    workflow: Workflow
    verbs: VerbRegistry
    agents: AgentRegistry
    provider: AgentProvider
    toolbelt: Toolbelt
    log_dir: Path
    gate_handler: GateHandler | None = None
    on_event: Callable[[dict[str, Any]], None] | None = None
    """Optional observer fired after each event is durably appended.

    The CLI uses this to stream live narration during `requiem run`. The
    callback receives the full envelope including `event_id`. Exceptions
    raised by the observer propagate (the kernel does not swallow renderer
    bugs — fail loud).
    """
    _state: dict[str, str] = field(default_factory=dict)

    def log_path(self, run_id: str) -> Path:
        return self.log_dir / f"{run_id}.events.jsonl"

    async def run(self, run_id: str) -> RunResult:
        wf = self.workflow
        errs = wf.validate_topology()
        if errs:
            raise ValueError(f"workflow invalid: {errs}")
        store = EventStore(self.log_path(run_id))
        append = store.append
        if self.on_event is not None:
            base_append = append
            observer = self.on_event

            def append_and_notify(envelope: dict[str, Any]) -> int:
                eid = base_append(envelope)
                observer({**envelope, "event_id": eid})
                return eid

            append = append_and_notify
        emitter = EventEmitter(run_id, append)

        nm: dict[str, Any] = {n.node_id: n for n in wf.nodes}
        em: dict[tuple[str, str], str] = {
            (e.from_node, e.on): e.to_node for e in wf.edges
        }

        replayed = list(replay(self.log_path(run_id)))
        if not replayed:
            emitter.emit_run_started(
                wf.name,
                workflow_module=wf.module,
                workflow_version=wf.version,
            )
            cursor: _Cursor = _AtNode(wf.entry, 1)
            completed: dict[str, dict[str, Any]] = {}
        else:
            cursor, completed = _reconstruct(replayed, wf.entry, run_id=run_id)

        # External-cancel check: a `cancel_requested` event in the log (written
        # by `requiem cancel <run_id>` or a peer process) short-circuits the
        # run per INV-CANCEL-SHORT-CIRCUITS-RETRY.
        if isinstance(cursor, _CancelledByOperator):
            final = cursor.final_node
            self._propagate_cancel_at(final, nm, emitter, run_id, cursor.reason)
            emitter.emit_run_completed("cancelled", final)
            return Failed(run_id, final, "cancelled", f"operator cancel: {cursor.reason}")

        while True:
            # Re-check the log tail for a cancel_requested that arrived after
            # we started looping (in-process cancel). Cheap: O(N) on log size,
            # called once per cursor step; the demo run is ~30 events.
            cancel = _pending_cancel(self.log_path(run_id))
            if cancel is not None:
                node_for_cancel = _cursor_node(cursor) or "—"
                self._propagate_cancel_at(
                    node_for_cancel, nm, emitter, run_id, cancel,
                )
                emitter.emit_run_completed("cancelled", node_for_cancel)
                return Failed(
                    run_id, node_for_cancel, "cancelled",
                    f"operator cancel: {cancel}",
                )
            match cursor:
                case _Terminated(terminal=t, final_node=f):
                    return Completed(
                        run_id, t, f, _projection(list(replay(self.log_path(run_id))))
                    )

                case _AtNode(node_id=nid, attempt=at):
                    node = nm[nid]
                    emitter.emit_node_entered(nid, attempt=at)
                    if isinstance(node, SubWorkflowNode):
                        sub_run_id = node.sub_run_id or f"{run_id}__{nid}"
                        inputs: dict[str, Any] = {}
                        if node.inputs_verb is not None:
                            inputs_fn = self.verbs.get(node.inputs_verb)
                            inputs_ctx = VerbContext(
                                run_id=run_id, workflow=wf.name, node_id=nid,
                                attempt=at, completed=completed,
                                toolbelt=self.toolbelt, emitter=emitter,
                            )
                            try:
                                produced = inputs_fn(inputs_ctx)
                                inputs = produced if isinstance(produced, dict) else {}
                            except Exception as e:  # noqa: BLE001
                                # An inputs_verb crash is a verb failure on the
                                # parent — record it like any other verb crash.
                                odict = outcome_to_dict(PermanentFailure(
                                    error_kind="subworkflow.inputs_crash",
                                    message=f"{type(e).__name__}: {e}",
                                ))
                                emitter.emit_verb_completed(nid, odict)
                                completed[nid] = odict
                                cursor = _AwaitingRoute(nid, odict, at)
                                continue
                        emitter.emit_subworkflow_started(
                            nid, sub_run_id=sub_run_id,
                            sub_workflow_module=node.workflow_module,
                            inputs_summary=inputs,
                        )
                        cursor = _AwaitingSubworkflow(
                            nid, sub_run_id, node.workflow_module, at
                        )
                        continue
                    outcome = await self._execute(
                        node,
                        VerbContext(
                            run_id=run_id,
                            workflow=wf.name,
                            node_id=nid,
                            attempt=at,
                            completed=completed,
                            toolbelt=self.toolbelt,
                            emitter=emitter,
                        ),
                    )
                    odict = outcome_to_dict(outcome)
                    emitter.emit_verb_completed(nid, odict)
                    completed[nid] = odict
                    cursor = _AwaitingRoute(nid, odict, at)

                case _AwaitingSubworkflow(
                    node_id=nid, sub_run_id=srid,
                    sub_workflow_module=mod, attempt=at,
                ):
                    node = nm[nid]
                    outcome = await self._run_subworkflow(
                        nid, srid, mod, node, run_id, completed, emitter,
                    )
                    odict = outcome_to_dict(outcome)
                    disposition = _disposition_for_outcome(outcome)
                    emitter.emit_subworkflow_completed(
                        nid, sub_run_id=srid, disposition=disposition,
                        outcome=odict,
                        outcome_summary=_summarise_outcome(odict),
                    )
                    completed[nid] = odict
                    cursor = _AwaitingRoute(nid, odict, at)

                case _AwaitingRoute(node_id=nid, outcome=odict, attempt=at):
                    nxt = self._route(nid, odict, at, em, nm, emitter, run_id)
                    if isinstance(nxt, _Halt):
                        return nxt.result
                    cursor = nxt

                case _AwaitingGate(node_id=nid, prompt=stored_prompt, options=stored_opts):
                    gate_node = nm.get(nid)
                    # Static `human_gate` nodes carry prompt/options on the
                    # node; script-returned `NeedsHuman` and sub-workflow
                    # bubble-ups carry them on the cursor. Prefer node attrs
                    # when present (non-empty); fall back to the cursor.
                    prompt = (
                        getattr(gate_node, "prompt", None) if gate_node is not None else None
                    ) or stored_prompt
                    options = tuple(
                        (getattr(gate_node, "options", ()) if gate_node is not None else ())
                        or stored_opts
                    )
                    if self.gate_handler is None:
                        return Suspended(run_id, nid, prompt, options)
                    choice = self.gate_handler(nid, prompt, options)
                    auto = bool(getattr(self.gate_handler, "__requiem_auto__", False))
                    emitter.emit_gate_resolved(nid, choice, auto=auto)
                    cursor = _RouteAfterGate(nid, choice)

                case _RouteAfterGate(node_id=nid, choice=c):
                    key = f"needs_human:{c}"
                    nxt_node = em.get((nid, key)) or em.get((nid, "needs_human"))
                    if nxt_node is None:
                        emitter.emit_run_completed("failed", nid)
                        return Failed(
                            run_id, nid, "route.missing",
                            f"no edge from {nid} on {c}",
                        )
                    emitter.emit_route_taken(nid, key, nxt_node)
                    cursor = _AtNode(nxt_node, 1)

    # ---- per-node dispatch (the data-driven interpreter) -----------

    async def _execute(self, node: Any, ctx: VerbContext) -> Outcome:
        try:
            if isinstance(node, ScriptNode):
                fn = self.verbs.get(node.verb)
                result = fn(ctx)
                return await result if isinstance(result, Awaitable) else result  # type: ignore[arg-type]
            if isinstance(node, AgentNode):
                spec = self.agents.get(node.agent)
                prompt = self.verbs.get(node.prompt_verb)(ctx)
                call = AgentCall(
                    spec=spec,
                    user_message=prompt,
                    retry_key=f"{ctx.run_id}:{ctx.node_id}#{ctx.attempt}",
                )
                return await self.provider.invoke(call)
            if isinstance(node, TeamNode):
                return await self._run_team(node, ctx)
            if isinstance(node, HumanGateNode):
                return NeedsHuman(
                    gate=node.node_id,
                    prompt=node.prompt,
                    options=tuple(node.options),
                )
            if isinstance(node, TerminateNode):
                return Success(value={"disposition": node.disposition})
            raise TypeError(f"unknown node kind: {type(node).__name__}")
        except Exception as e:  # noqa: BLE001
            return PermanentFailure(
                error_kind="verb.crash",
                message=f"{type(e).__name__}: {e}",
            )

    async def _run_team(self, node: TeamNode, ctx: VerbContext) -> Outcome:
        """`parallel_fork`: dispatch every branch concurrently."""
        ctx.emitter.emit_team_dispatched(
            node.node_id, node.team_id, [b.agent for b in node.branches]
        )

        async def _one(branch: Any) -> tuple[str, Outcome]:
            spec = self.agents.get(branch.agent)
            prompt = self.verbs.get(branch.prompt_verb)(ctx)
            call = AgentCall(
                spec=spec,
                user_message=prompt,
                retry_key=f"{ctx.run_id}:{node.node_id}:{branch.agent}",
            )
            return branch.agent, await self.provider.invoke(call)

        results = await asyncio.gather(*(_one(b) for b in node.branches))

        findings: list[dict[str, Any]] = []
        for agent_name, outcome in results:
            ctx.emitter.emit_team_branch_completed(
                node.node_id, node.team_id, agent_name, outcome_to_dict(outcome),
            )
            if isinstance(outcome, Success):
                findings.append({"agent": agent_name, "result": outcome.value})
            else:
                return PermanentFailure(
                    error_kind="team.branch_failed",
                    message=f"branch {agent_name} returned {type(outcome).__name__}",
                    details={"outcome": outcome_to_dict(outcome)},
                )
        return Success(value={"team_id": node.team_id, "findings": findings})

    # ---- sub-workflow dispatch (ADR 0005) -------------------------

    async def _run_subworkflow(
        self,
        node_id: str,
        sub_run_id: str,
        module_path: str,
        node: Any,
        parent_run_id: str,
        completed: dict[str, dict[str, Any]],
        emitter: EventEmitter,
    ) -> Outcome:
        """Spawn (or re-attach to) a child workflow and map its result.

        The child engine writes to its OWN ``{sub_run_id}.events.jsonl``
        (INV-SUBWORKFLOW-LOG-ISOLATION). Resume is automatic: the child's
        own ``run()`` does its own ``_reconstruct`` over its own log.
        """
        import importlib  # local to avoid top-level cost when unused
        import inspect

        try:
            mod = importlib.import_module(module_path)
        except Exception as e:  # noqa: BLE001
            return PermanentFailure(
                error_kind="subworkflow.import_failed",
                message=f"could not import {module_path!r}: {type(e).__name__}: {e}",
            )

        factory = getattr(mod, "build_engine", None)
        if factory is None:
            return PermanentFailure(
                error_kind="subworkflow.no_build_engine",
                message=f"module {module_path!r} has no build_engine(log_dir)",
            )

        # ADR 0005 addendum (Fauré seat 2): the kernel reads the recorded
        # `subworkflow_started.inputs_summary` from the parent's log and
        # forwards values to `build_engine` as kwargs, filtered by
        # `inspect.signature` so factories that don't accept them are
        # unaffected. Reading from the log (not from cursor state) means
        # resume after a crash recovers the same inputs the original
        # invocation used — the log is authoritative
        # (INV-EVENT-LOG-AUTHORITATIVE).
        recorded_inputs: dict[str, Any] = {}
        parent_log = self.log_path(parent_run_id)
        for ev in replay(parent_log):
            if (
                ev.get("kind") == "subworkflow_started"
                and (ev.get("payload") or {}).get("sub_run_id") == sub_run_id
            ):
                recorded_inputs = dict(
                    (ev.get("payload") or {}).get("inputs_summary") or {}
                )
                # Keep the last in case of duplicate (defensive — first-write
                # wins is the contract, but resume re-emit guards prevent
                # duplicates anyway).

        try:
            sig = inspect.signature(factory)
            kwargs: dict[str, Any] = {
                k: v for k, v in recorded_inputs.items() if k in sig.parameters
            }
            # The kernel hands the child the *same* log_dir; child's run_id
            # (sub_run_id) makes the filename distinct.
            if "log_dir" in sig.parameters:
                child_engine = factory(log_dir=self.log_dir, **kwargs)
            else:
                child_engine = factory(self.log_dir, **kwargs)
        except Exception as e:  # noqa: BLE001
            return PermanentFailure(
                error_kind="subworkflow.build_failed",
                message=f"{module_path}.build_engine raised "
                        f"{type(e).__name__}: {e}",
            )

        if not isinstance(child_engine, Engine):
            return PermanentFailure(
                error_kind="subworkflow.bad_factory",
                message=f"{module_path}.build_engine did not return an Engine",
            )

        try:
            result = await child_engine.run(sub_run_id)
        except Exception as e:  # noqa: BLE001
            return PermanentFailure(
                error_kind="subworkflow.run_crashed",
                message=f"child run crashed: {type(e).__name__}: {e}",
            )

        return _child_result_to_outcome(result, node_id, sub_run_id)

    def _propagate_cancel_to_child(
        self, sub_run_id: str, *, reason: str
    ) -> None:
        """Write a ``cancel_requested`` event into the child's log.

        Idempotent: if the child already has a cancel marker we don't add
        a second one (the child's first short-circuit is enough).
        """
        child_log = self.log_dir / f"{sub_run_id}.events.jsonl"
        for ev in replay(child_log):
            if ev.get("kind") == "cancel_requested":
                return
        store = EventStore(child_log)
        emitter = EventEmitter(sub_run_id, store.append)
        emitter.emit_cancel_requested(reason=reason, requested_by="parent")

    def _propagate_cancel_at(
        self,
        node_id: str | None,
        nm: dict[str, Any],
        emitter: EventEmitter,
        parent_run_id: str,
        reason: str,
    ) -> None:
        """If ``node_id`` is a SubWorkflowNode, propagate cancel to the child.

        Called from both the top-of-``run`` short-circuit and the in-loop
        cancel detection so cancellation is fan-out idempotent regardless
        of when the cancel arrives. INV-CANCEL propagates through every
        active sub-workflow layer.
        """
        if not node_id:
            return
        node = nm.get(node_id)
        if not isinstance(node, SubWorkflowNode):
            return
        sub_run_id = node.sub_run_id or f"{parent_run_id}__{node_id}"
        self._propagate_cancel_to_child(sub_run_id, reason=f"parent: {reason}")
        emitter.emit_subworkflow_cancelled(
            node_id, sub_run_id=sub_run_id, reason=f"parent: {reason}",
        )

    # ---- route dispatch (one match arm per outcome variant) --------

    def _route(
        self,
        node_id: str,
        outcome_dict: dict[str, Any],
        attempt: int,
        em: dict[tuple[str, str], str],
        nm: dict[str, Any],
        emitter: EventEmitter,
        run_id: str,
    ) -> "_Cursor | _Halt":
        node = nm[node_id]
        outcome = outcome_from_dict(outcome_dict)
        match outcome:
            case Cancelled(cause=cause, at_step=step):
                # INV-CANCEL-SHORT-CIRCUITS-RETRY: no retry consultation.
                emitter.emit_run_completed("cancelled", node_id)
                return _Halt(Failed(run_id, node_id, "cancelled", f"{cause} at {step}"))

            case RetryableFailure(error_kind=ek, message=msg):
                if attempt <= getattr(node, "retry_max", 0):
                    emitter.emit_retry_attempted(node_id, attempt, attempt + 1, msg)
                    return _AtNode(node_id, attempt + 1)
                nxt = em.get((node_id, "retry_exhausted"))
                if nxt is None:
                    emitter.emit_run_completed("failed", node_id)
                    return _Halt(Failed(run_id, node_id, ek, f"retry exhausted: {msg}"))
                emitter.emit_route_taken(node_id, "retry_exhausted", nxt)
                return _AtNode(nxt, 1)

            case BadOutput(error_kind=ek, validation_errors=ves):
                # Distinct from PermanentFailure: never network-retried.
                # Prefer a `bad_output` remediation edge; fall through to
                # `permanent_failure` if the author didn't wire one.
                if (node_id, "bad_output") in em:
                    nxt = em[(node_id, "bad_output")]
                    emitter.emit_route_taken(node_id, "bad_output", nxt)
                    return _AtNode(nxt, 1)
                if (node_id, "permanent_failure") in em:
                    nxt = em[(node_id, "permanent_failure")]
                    emitter.emit_route_taken(node_id, "permanent_failure", nxt)
                    return _AtNode(nxt, 1)
                emitter.emit_run_completed("failed", node_id)
                return _Halt(Failed(run_id, node_id, ek, f"bad output: {'; '.join(ves)}"))

            case PermanentFailure(error_kind=ek, message=msg):
                nxt = em.get((node_id, f"permanent_failure:{ek}")) or em.get(
                    (node_id, "permanent_failure")
                )
                if nxt is None:
                    emitter.emit_run_completed("failed", node_id)
                    return _Halt(Failed(run_id, node_id, ek, msg))
                emitter.emit_route_taken(node_id, "permanent_failure", nxt)
                return _AtNode(nxt, 1)

            case NeedsHuman(prompt=p, options=opts, context=ctx):
                auto = bool(getattr(self.gate_handler, "__requiem_auto__", False))
                emitter.emit_gate_opened(node_id, p, list(opts), context=ctx, auto=auto)
                return _AwaitingGate(node_id, prompt=p, options=tuple(opts))

            case Success():
                if isinstance(node, TerminateNode):
                    emitter.emit_run_completed(node.disposition, node_id)
                    return _Halt(
                        Completed(
                            run_id,
                            node.disposition,
                            node_id,
                            _projection(list(replay(self.log_path(run_id)))),
                        )
                    )
                nxt = em.get((node_id, "success"))
                if nxt is None:
                    emitter.emit_run_completed("failed", node_id)
                    return _Halt(
                        Failed(run_id, node_id, "route.missing",
                               f"no success edge from {node_id}")
                    )
                emitter.emit_route_taken(node_id, "success", nxt)
                return _AtNode(nxt, 1)


@dataclass(frozen=True, slots=True)
class _Halt:
    """Sentinel: the router decided the run is over; return `result`."""
    result: RunResult


# ---- sub-workflow helpers (ADR 0005) -------------------------------


def _child_result_to_outcome(
    result: RunResult, parent_node_id: str, sub_run_id: str
) -> Outcome:
    """Map a child ``RunResult`` to the parent's verb outcome.

    The parent's router then takes the standard edges (``success``,
    ``permanent_failure``, ``needs_human``, ``cancelled``) — no new edge
    keys, no special routing path.
    """
    if isinstance(result, Completed):
        if result.disposition == "completed":
            return Success(value={
                "sub_run_id": sub_run_id,
                "child_disposition": result.disposition,
                "child_final_node": result.final_node,
                "child_projection": result.projection,
            })
        if result.disposition == "cancelled":
            return Cancelled(cause="operator", at_step=parent_node_id)
        # Any other terminal disposition (e.g. ``failed``) — the child
        # voluntarily reached a `terminate(disposition="failed")` node, so
        # the parent treats it as a permanent failure.
        return PermanentFailure(
            error_kind=f"subworkflow.{result.disposition}",
            message=f"child workflow ended with disposition={result.disposition!r}",
            details={
                "sub_run_id": sub_run_id,
                "child_final_node": result.final_node,
            },
        )
    if isinstance(result, Suspended):
        return NeedsHuman(
            gate=f"{parent_node_id}/{result.node_id}",
            prompt=result.prompt,
            options=tuple(result.options),
            context={
                "sub_run_id": sub_run_id,
                "child_node_id": result.node_id,
            },
        )
    if isinstance(result, Failed):
        if result.error_kind == "cancelled":
            return Cancelled(cause="operator", at_step=parent_node_id)
        return PermanentFailure(
            error_kind=f"subworkflow.{result.error_kind}",
            message=result.message,
            details={
                "sub_run_id": sub_run_id,
                "child_node_id": result.node_id,
            },
        )
    return PermanentFailure(  # defensive: future RunResult variants
        error_kind="subworkflow.unknown_result",
        message=f"child returned unknown RunResult: {type(result).__name__}",
    )


def _disposition_for_outcome(outcome: Outcome) -> str:
    """Human-readable disposition tag for ``subworkflow_completed``."""
    if isinstance(outcome, Success):
        return "completed"
    if isinstance(outcome, NeedsHuman):
        return "needs_human"
    if isinstance(outcome, Cancelled):
        return "cancelled"
    return "failed"


def _summarise_outcome(odict: dict[str, Any]) -> dict[str, Any]:
    """Best-effort short summary for the event payload (full outcome stays in ``outcome``)."""
    kind = odict.get("kind", "?")
    if kind == "success":
        value = odict.get("value", {})
        return {
            "kind": kind,
            "sub_run_id": value.get("sub_run_id"),
            "child_final_node": value.get("child_final_node"),
        }
    if kind in ("permanent_failure", "bad_output"):
        return {"kind": kind, "error_kind": odict.get("error_kind")}
    if kind == "needs_human":
        return {"kind": kind, "gate": odict.get("gate")}
    if kind == "cancelled":
        return {"kind": kind, "cause": odict.get("cause")}
    return {"kind": kind}


# ---- reconstruct: a pure fold over the event log -------------------


def _reconstruct(
    events: list[dict[str, Any]],
    entry: str,
    *,
    run_id: str | None = None,
) -> tuple[_Cursor, dict[str, dict[str, Any]]]:
    """Fold the event log into a single `_Cursor` describing where to resume.

    One match arm per event kind; no "look at the last route_taken vs the
    last verb_completed" reasoning. Each event either advances the cursor
    or records side state (`completed`, `last_attempt`).

    If ``run_id`` is provided, events whose envelope ``run_id`` does not
    match are skipped — this enforces ``INV-SUBWORKFLOW-LOG-ISOLATION``
    (ADR 0005): a child workflow's events MUST NOT advance the parent's
    cursor, even in the (defensive) case where they bleed into the parent's
    log file. Brahms-harness PR #6's finding made law.
    """
    cursor: _Cursor = _AtNode(entry, 1)
    completed: dict[str, dict[str, Any]] = {}
    last_attempt = 1

    for e in events:
        if run_id is not None and e.get("run_id") != run_id:
            # INV-SUBWORKFLOW-LOG-ISOLATION: foreign-run event ignored.
            continue
        kind = e["kind"]
        payload = e["payload"]
        node = e.get("node_id")
        match kind:
            case "node_entered":
                last_attempt = payload.get("attempt", 1)
                cursor = _AtNode(node, last_attempt)
            case "verb_completed":
                completed[node] = payload["outcome"]
                cursor = _AwaitingRoute(node, payload["outcome"], last_attempt)
            case "retry_attempted":
                last_attempt = payload["next_attempt"]
                cursor = _AtNode(node, last_attempt)
            case "route_taken":
                last_attempt = 1
                cursor = _AtNode(payload["to_node"], 1)
            case "gate_opened":
                cursor = _AwaitingGate(
                    node,
                    prompt=payload.get("prompt", ""),
                    options=tuple(payload.get("options", ()) or ()),
                )
            case "gate_resolved":
                cursor = _RouteAfterGate(node, payload["choice"])
            case "subworkflow_started":
                cursor = _AwaitingSubworkflow(
                    node,
                    payload["sub_run_id"],
                    payload["sub_workflow_module"],
                    last_attempt,
                )
            case "subworkflow_completed":
                # The full outcome is preserved in the payload precisely so
                # that a crash between subworkflow_completed and the next
                # route step resumes without re-invoking the (now-finished)
                # child engine. The cursor jumps straight to routing.
                outcome = payload["outcome"]
                completed[node] = outcome
                cursor = _AwaitingRoute(node, outcome, last_attempt)
            case "subworkflow_cancelled":
                # Recorded for observability; cancel_requested (separately
                # written) drives the resume short-circuit.
                pass
            case "run_completed":
                cursor = _Terminated(
                    payload.get("terminal", "completed"),
                    payload.get("final_node") or node or "?",
                )
            case "cancel_requested":
                # Whatever the cursor *was*, an external cancel makes it
                # this. The engine will emit `run_completed("cancelled")`
                # and exit immediately.
                prev = _cursor_node(cursor) or "—"
                cursor = _CancelledByOperator(prev, payload.get("reason", "operator"))
            # run_started / verb_invoked / team_dispatched /
            # team_branch_completed are observable but do not change the
            # resume position — node_entered/verb_completed already do.
    return cursor, completed


# ---- projection (a tiny derived view; proves the log is the truth) --


def _projection(events: list[dict[str, Any]]) -> dict[str, Any]:
    nodes_entered: list[str] = []
    verbs_done = 0
    retries = 0
    team_branches = 0
    subworkflows_done = 0
    terminal: str | None = None
    for e in events:
        k = e["kind"]
        if k == "node_entered":
            nodes_entered.append(e.get("node_id"))
        elif k == "verb_completed":
            verbs_done += 1
        elif k == "retry_attempted":
            retries += 1
        elif k == "team_branch_completed":
            team_branches += 1
        elif k == "subworkflow_completed":
            # Subworkflow nodes don't emit `verb_completed` (the
            # subworkflow_completed event IS the verb-completion signal —
            # see ADR 0005); count them here so the projection still
            # totals every node that produced an outcome.
            verbs_done += 1
            subworkflows_done += 1
        elif k == "run_completed":
            terminal = e["payload"].get("terminal")
    return {
        "nodes_entered": nodes_entered,
        "verbs_completed": verbs_done,
        "retries": retries,
        "team_branches_completed": team_branches,
        "subworkflows_completed": subworkflows_done,
        "terminal": terminal,
        "total_events": len(events),
    }
