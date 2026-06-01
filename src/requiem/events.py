"""Execution events — Brahms B (envelope-loose + typed emit helpers).

On the wire: one `Event` envelope. Everyone reads it. Per-kind payloads
opt into validation via a registry. The typed `EventEmitter.emit_*`
methods give authors compile-time-friendly call sites without forcing
the envelope into a closed union.

Honours `INV-EVENT-LOG-AUTHORITATIVE`: every observable transition the
engine makes is one `emit_*` call.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1


EVENT_KINDS: frozenset[str] = frozenset({
    "run_started",
    "node_entered",
    "verb_invoked",
    "verb_completed",
    "retry_attempted",
    "route_taken",
    "team_dispatched",
    "team_branch_completed",
    "gate_opened",
    "gate_resolved",
    "run_completed",
})
"""Sealed catalogue of kinds the kernel emits.

The renderer-registry exhaustiveness test (`tests/test_renderer_registry.py`)
treats this as the source of truth: every kind here must have a renderer,
and every renderer key must appear here.
"""


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: int
    run_id: str
    ts: datetime
    kind: str
    schema_version: int = SCHEMA_VERSION
    node_id: str | None = None
    team_id: str | None = None
    agent_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


AppendFn = Callable[[dict[str, Any]], int]


class EventEmitter:
    """Typed emit-helpers over the loose envelope."""

    def __init__(self, run_id: str, append: AppendFn) -> None:
        self.run_id = run_id
        self._append = append

    def _emit(
        self,
        kind: str,
        *,
        node_id: str | None = None,
        team_id: str | None = None,
        agent_id: str | None = None,
        **payload: Any,
    ) -> int:
        envelope = {
            "run_id": self.run_id,
            "ts": _now().isoformat(),
            "kind": kind,
            "schema_version": SCHEMA_VERSION,
            "node_id": node_id,
            "team_id": team_id,
            "agent_id": agent_id,
            "payload": payload,
        }
        return self._append(envelope)

    def emit_run_started(self, workflow: str) -> None:
        self._emit("run_started", workflow=workflow)

    def emit_node_entered(self, node_id: str, attempt: int = 1) -> None:
        self._emit("node_entered", node_id=node_id, attempt=attempt)

    def emit_verb_invoked(self, node_id: str, verb: str) -> None:
        self._emit("verb_invoked", node_id=node_id, verb=verb)

    def emit_verb_completed(self, node_id: str, outcome: dict[str, Any]) -> None:
        self._emit("verb_completed", node_id=node_id, outcome=outcome)

    def emit_retry_attempted(
        self, node_id: str, attempt: int, next_attempt: int, reason: str
    ) -> None:
        self._emit(
            "retry_attempted",
            node_id=node_id,
            attempt=attempt,
            next_attempt=next_attempt,
            reason=reason,
        )

    def emit_route_taken(self, from_node: str, key: str, to_node: str) -> None:
        self._emit("route_taken", node_id=from_node, key=key, to_node=to_node)

    def emit_team_dispatched(
        self, node_id: str, team_id: str, branches: list[str]
    ) -> None:
        self._emit(
            "team_dispatched", node_id=node_id, team_id=team_id, branches=branches
        )

    def emit_team_branch_completed(
        self,
        node_id: str,
        team_id: str,
        agent_id: str,
        outcome: dict[str, Any],
    ) -> None:
        self._emit(
            "team_branch_completed",
            node_id=node_id,
            team_id=team_id,
            agent_id=agent_id,
            outcome=outcome,
        )

    def emit_gate_opened(
        self, node_id: str, prompt: str, options: list[str],
        *, context: dict[str, Any] | None = None, auto: bool = False,
    ) -> None:
        self._emit(
            "gate_opened",
            node_id=node_id,
            prompt=prompt,
            options=options,
            context=context or {},
            auto=auto,
        )

    def emit_gate_resolved(
        self, node_id: str, choice: str, *, auto: bool = False
    ) -> None:
        self._emit("gate_resolved", node_id=node_id, choice=choice, auto=auto)

    def emit_run_completed(self, terminal: str, final_node: str) -> None:
        self._emit("run_completed", terminal=terminal, final_node=final_node)


def parse_envelope(raw: dict[str, Any]) -> Event:
    return Event.model_validate(raw)
