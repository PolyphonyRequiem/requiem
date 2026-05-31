"""Beethoven C — data-driven interpreter.

The kernel switches on `node.kind` and looks up verbs by name. It does
not know any user code by Python type. The same engine runs scripts,
agents, gates, and parallel-fork teams; the only thing that differs is
the dispatch arm in `_execute`.

INV-RESTART: `_reconstruct` rebuilds (current_node, completed, attempt)
from the event log alone. Resume picks up at the next undecided edge.

INV-CANCEL-SHORT-CIRCUITS-RETRY: `Cancelled` outcomes terminate the run
without consulting `retry_max`.

INV-DISCRIMINATED-OUTCOMES: every dispatch arm pattern-matches on the
sealed outcome union; mypy --strict would catch a missing arm via
`assert_never` (skipped here to keep the demo light).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Union

from engine.agent import AgentCall, AgentProvider
from engine.dsl import (
    AgentNode, AgentRegistry, Edge, HumanGateNode, ScriptNode, TeamNode,
    TerminateNode, VerbRegistry, Workflow,
)
from engine.events import EventEmitter
from engine.outcomes import (
    Cancelled, NeedsHuman, Outcome, PermanentFailure, RetryableFailure,
    Success, outcome_from_dict, outcome_to_dict,
)
from engine.persistence import EventStore, replay
from engine.toolbelt import Toolbelt


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


# ---- gate handler (autoresolved in the demo) -------------------------


GateHandler = Callable[[str, str, tuple[str, ...]], str]  # (node_id, prompt, options) -> choice


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
    _state: dict[str, str] = field(default_factory=dict)  # run_id -> "pending"|"done"

    def log_path(self, run_id: str) -> Path:
        return self.log_dir / f"{run_id}.events.jsonl"

    async def run(self, run_id: str) -> RunResult:
        wf = self.workflow
        errs = wf.validate_topology()
        if errs:
            raise ValueError(f"workflow invalid: {errs}")
        store = EventStore(self.log_path(run_id))
        emitter = EventEmitter(run_id, store.append)

        nm: dict[str, Any] = {n.node_id: n for n in wf.nodes}
        em: dict[tuple[str, str], str] = {(e.from_node, e.on): e.to_node for e in wf.edges}

        replayed = list(replay(self.log_path(run_id)))
        if not replayed:
            emitter.emit_run_started(wf.name)
            current: str | None = wf.entry
            completed: dict[str, dict[str, Any]] = {}
            attempt = 1
        else:
            current, completed, attempt = _reconstruct(replayed, nm, em)
            if current is None:
                # already terminated; surface from log
                terminal = next(
                    (r["payload"].get("terminal") for r in replayed if r["kind"] == "run_completed"),
                    "completed",
                )
                final = next(
                    (r["payload"].get("final_node") for r in replayed if r["kind"] == "run_completed"),
                    "?",
                )
                return Completed(run_id, terminal, final, _projection(replayed))

        # Did the replay leave us suspended at a gate that has no resolution?
        if current is not None and isinstance(nm[current], HumanGateNode) and _gate_open(replayed, current):
            if self.gate_handler is None:
                gate = nm[current]
                return Suspended(run_id, current, gate.prompt, tuple(gate.options))
            choice = self.gate_handler(current, nm[current].prompt, tuple(nm[current].options))
            emitter.emit_gate_resolved(current, choice)
            nxt = em.get((current, f"needs_human:{choice}")) or em.get((current, "needs_human"))
            if nxt is None:
                return Failed(run_id, current, "route.missing", f"no edge from {current} on {choice}")
            emitter.emit_route_taken(current, f"needs_human:{choice}", nxt)
            current = nxt
            attempt = 1

        while current is not None:
            node = nm[current]
            emitter.emit_node_entered(current, attempt=attempt)

            outcome = await self._execute(node, VerbContext(
                run_id=run_id, workflow=wf.name, node_id=current,
                attempt=attempt, completed=completed,
                toolbelt=self.toolbelt, emitter=emitter,
            ))

            emitter.emit_verb_completed(current, outcome_to_dict(outcome))
            completed[current] = outcome_to_dict(outcome)

            match outcome:
                case Cancelled(cause=cause, at_step=step):
                    # INV-CANCEL-SHORT-CIRCUITS-RETRY: no retry consultation.
                    emitter.emit_run_completed("cancelled", current)
                    return Failed(run_id, current, "cancelled", f"{cause} at {step}")

                case RetryableFailure(message=msg, error_kind=ek) if attempt <= getattr(node, "retry_max", 0):
                    emitter.emit_retry_attempted(current, attempt, attempt + 1, msg)
                    attempt += 1
                    continue

                case RetryableFailure(message=msg, error_kind=ek):
                    nxt = em.get((current, "retry_exhausted"))
                    if nxt is None:
                        emitter.emit_run_completed("failed", current)
                        return Failed(run_id, current, ek, f"retry exhausted: {msg}")
                    emitter.emit_route_taken(current, "retry_exhausted", nxt)
                    current = nxt; attempt = 1; continue

                case PermanentFailure(error_kind=ek, message=msg):
                    nxt = em.get((current, f"permanent_failure:{ek}")) or em.get((current, "permanent_failure"))
                    if nxt is None:
                        emitter.emit_run_completed("failed", current)
                        return Failed(run_id, current, ek, msg)
                    emitter.emit_route_taken(current, "permanent_failure", nxt)
                    current = nxt; attempt = 1; continue

                case NeedsHuman(gate=g, prompt=p, options=opts):
                    emitter.emit_gate_opened(current, p, list(opts))
                    if self.gate_handler is None:
                        return Suspended(run_id, current, p, opts)
                    choice = self.gate_handler(current, p, opts)
                    emitter.emit_gate_resolved(current, choice)
                    nxt = em.get((current, f"needs_human:{choice}")) or em.get((current, "needs_human"))
                    if nxt is None:
                        emitter.emit_run_completed("failed", current)
                        return Failed(run_id, current, "route.missing", f"no edge on {choice}")
                    emitter.emit_route_taken(current, f"needs_human:{choice}", nxt)
                    current = nxt; attempt = 1; continue

                case Success():
                    if isinstance(node, TerminateNode):
                        emitter.emit_run_completed(node.disposition, current)
                        return Completed(run_id, node.disposition, current,
                                         _projection(list(replay(self.log_path(run_id)))))
                    nxt = em.get((current, "success"))
                    if nxt is None:
                        emitter.emit_run_completed("failed", current)
                        return Failed(run_id, current, "route.missing",
                                      f"no success edge from {current}")
                    emitter.emit_route_taken(current, "success", nxt)
                    current = nxt; attempt = 1; continue

        emitter.emit_run_completed("failed", "<halted>")
        return Failed(run_id, "<halted>", "engine.halted", "ran off the end of the graph")

    # ---- per-node dispatch (data-driven interpreter) ----

    async def _execute(self, node: Any, ctx: VerbContext) -> Outcome:
        try:
            if isinstance(node, ScriptNode):
                fn = self.verbs.get(node.verb)
                result = fn(ctx)
                return await result if isinstance(result, Awaitable) else result  # type: ignore[arg-type]
            if isinstance(node, AgentNode):
                spec = self.agents.get(node.agent)
                prompt = self.verbs.get(node.prompt_verb)(ctx)
                call = AgentCall(spec=spec, user_message=prompt,
                                 retry_key=f"{ctx.run_id}:{ctx.node_id}#{ctx.attempt}")
                return await self.provider.invoke(call)
            if isinstance(node, TeamNode):
                return await self._run_team(node, ctx)
            if isinstance(node, HumanGateNode):
                return NeedsHuman(gate=node.node_id, prompt=node.prompt,
                                  options=tuple(node.options))
            if isinstance(node, TerminateNode):
                return Success(value={"disposition": node.disposition})
            raise TypeError(f"unknown node kind: {type(node).__name__}")
        except Exception as e:  # noqa: BLE001
            return PermanentFailure(error_kind="verb.crash",
                                    message=f"{type(e).__name__}: {e}")

    async def _run_team(self, node: TeamNode, ctx: VerbContext) -> Outcome:
        """parallel_fork: dispatch every branch concurrently, gather typed results."""
        ctx.emitter.emit_team_dispatched(
            node.node_id, node.team_id, [b.agent for b in node.branches]
        )

        async def _one(branch: Any) -> tuple[str, Outcome]:
            spec = self.agents.get(branch.agent)
            prompt = self.verbs.get(branch.prompt_verb)(ctx)
            call = AgentCall(spec=spec, user_message=prompt,
                             retry_key=f"{ctx.run_id}:{node.node_id}:{branch.agent}")
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


# ---- replay helpers --------------------------------------------------


def _gate_open(events: list[dict[str, Any]], node_id: str) -> bool:
    last_open = None
    last_resolved = None
    for e in events:
        if e["kind"] == "gate_opened" and e.get("node_id") == node_id:
            last_open = e["event_id"]
        elif e["kind"] == "gate_resolved" and e.get("node_id") == node_id:
            last_resolved = e["event_id"]
    return last_open is not None and (last_resolved is None or last_resolved < last_open)


def _reconstruct(events: list[dict[str, Any]],
                 nm: dict[str, Any],
                 em: dict[tuple[str, str], str]
                 ) -> tuple[str | None, dict[str, dict[str, Any]], int]:
    """Replay the log; return (next_node_to_enter, completed_map, attempt)."""
    completed: dict[str, dict[str, Any]] = {}
    last_entered: str | None = None
    last_attempt = 1
    last_completed: str | None = None
    last_route_target: str | None = None
    gate_resolved_target: str | None = None
    terminated = False

    for e in events:
        k = e["kind"]
        p = e["payload"]
        if k == "node_entered":
            last_entered = e.get("node_id")
            last_attempt = p.get("attempt", 1)
        elif k == "verb_completed":
            last_completed = e.get("node_id")
            completed[last_completed] = p["outcome"]
        elif k == "route_taken":
            last_route_target = p["to_node"]
        elif k == "gate_resolved":
            # the matching route_taken will follow if the engine was alive
            choice = p["choice"]
            node = e.get("node_id")
            gate_resolved_target = (
                em.get((node, f"needs_human:{choice}")) or em.get((node, "needs_human"))
            )
        elif k == "run_completed":
            terminated = True

    if terminated:
        return None, completed, 1

    # The last thing the engine actually committed:
    #   route_taken    → resume at route_target
    #   gate_resolved  → resume at the gate's chosen branch
    #   verb_completed → resume at successor (or stay if it was suspended)
    #   node_entered   → re-execute (idempotency contract)
    if last_route_target is not None and last_route_target in nm:
        # only honour if it was the *most recent* event
        last_route_event = next(
            (e for e in reversed(events) if e["kind"] == "route_taken"), None
        )
        if last_route_event and events.index(last_route_event) > _last_idx(events, "verb_completed"):
            return last_route_target, completed, 1

    if gate_resolved_target is not None and gate_resolved_target in nm:
        return gate_resolved_target, completed, 1

    if last_completed is not None and last_completed == last_entered:
        # verb finished; route was never taken — recompute
        out = outcome_from_dict(completed[last_completed])
        node = nm[last_completed]
        if isinstance(out, Success):
            if isinstance(node, TerminateNode):
                return None, completed, 1
            nxt = em.get((last_completed, "success"))
            return (nxt, completed, 1) if nxt else (None, completed, 1)
        if isinstance(out, RetryableFailure) and last_attempt <= getattr(node, "retry_max", 0):
            return last_completed, completed, last_attempt + 1
        return last_completed, completed, 1

    if last_entered is not None:
        # crash mid-verb; re-enter and re-execute (verbs MUST be idempotent).
        return last_entered, completed, last_attempt

    return None, completed, 1


def _last_idx(events: list[dict[str, Any]], kind: str) -> int:
    for i in range(len(events) - 1, -1, -1):
        if events[i]["kind"] == kind:
            return i
    return -1


def _projection(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Tiny derived view (Bach A) — proves the log is the truth."""
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
        "nodes_entered": nodes_entered, "verbs_completed": verbs_done,
        "retries": retries, "team_branches_completed": team_branches,
        "terminal": terminal, "total_events": len(events),
    }
