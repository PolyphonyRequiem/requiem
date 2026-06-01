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
    """`NeedsHuman` was emitted; the kernel needs a handler choice."""
    node_id: str


@dataclass(frozen=True, slots=True)
class _RouteAfterGate:
    """Handler returned a choice; the kernel must take the matching edge."""
    node_id: str
    choice: str


@dataclass(frozen=True, slots=True)
class _Terminated:
    terminal: str
    final_node: str


_Cursor = Union[_AtNode, _AwaitingRoute, _AwaitingGate, _RouteAfterGate, _Terminated]


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
            emitter.emit_run_started(wf.name)
            cursor: _Cursor = _AtNode(wf.entry, 1)
            completed: dict[str, dict[str, Any]] = {}
        else:
            cursor, completed = _reconstruct(replayed, wf.entry)

        while True:
            match cursor:
                case _Terminated(terminal=t, final_node=f):
                    return Completed(
                        run_id, t, f, _projection(list(replay(self.log_path(run_id))))
                    )

                case _AtNode(node_id=nid, attempt=at):
                    node = nm[nid]
                    emitter.emit_node_entered(nid, attempt=at)
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

                case _AwaitingRoute(node_id=nid, outcome=odict, attempt=at):
                    nxt = self._route(nid, odict, at, em, nm, emitter, run_id)
                    if isinstance(nxt, _Halt):
                        return nxt.result
                    cursor = nxt

                case _AwaitingGate(node_id=nid):
                    gate_node = nm[nid]
                    if self.gate_handler is None:
                        return Suspended(
                            run_id, nid, gate_node.prompt, tuple(gate_node.options)
                        )
                    choice = self.gate_handler(
                        nid, gate_node.prompt, tuple(gate_node.options)
                    )
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
                return _AwaitingGate(node_id)

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


# ---- reconstruct: a pure fold over the event log -------------------


def _reconstruct(
    events: list[dict[str, Any]],
    entry: str,
) -> tuple[_Cursor, dict[str, dict[str, Any]]]:
    """Fold the event log into a single `_Cursor` describing where to resume.

    One match arm per event kind; no "look at the last route_taken vs the
    last verb_completed" reasoning. Each event either advances the cursor
    or records side state (`completed`, `last_attempt`).
    """
    cursor: _Cursor = _AtNode(entry, 1)
    completed: dict[str, dict[str, Any]] = {}
    last_attempt = 1

    for e in events:
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
                cursor = _AwaitingGate(node)
            case "gate_resolved":
                cursor = _RouteAfterGate(node, payload["choice"])
            case "run_completed":
                cursor = _Terminated(
                    payload.get("terminal", "completed"),
                    payload.get("final_node") or node or "?",
                )
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
        elif k == "run_completed":
            terminal = e["payload"].get("terminal")
    return {
        "nodes_entered": nodes_entered,
        "verbs_completed": verbs_done,
        "retries": retries,
        "team_branches_completed": team_branches,
        "terminal": terminal,
        "total_events": len(events),
    }
