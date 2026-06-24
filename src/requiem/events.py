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
    "cancel_requested",
    "subworkflow_started",
    "subworkflow_completed",
    "subworkflow_cancelled",
    "context_pack_truncated",
    "run_completed",
    "agent_call_started",
    "run_cost_summary",
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

    def emit_run_started(
        self,
        workflow: str,
        *,
        workflow_module: str | None = None,
        workflow_version: str = "0",
    ) -> None:
        """Emit ``run_started`` with workflow identity.

        ``workflow`` is the workflow's display name (``Workflow.name``).
        ``workflow_module`` is the importable module path that produced the
        workflow (e.g. ``requiem.workflows.code_review_demo``) — recorded so
        post-hoc tools like ``requiem events <run_id>`` can re-import the
        module and recover its humanize map without an explicit flag.
        ``workflow_version`` honours ADR 0004 §4.7 (version-pinned replay).
        """
        self._emit(
            "run_started",
            workflow=workflow,
            workflow_module=workflow_module,
            workflow_version=workflow_version,
        )

    def emit_node_entered(self, node_id: str, attempt: int = 1) -> None:
        self._emit("node_entered", node_id=node_id, attempt=attempt)

    def emit_verb_invoked(self, node_id: str, verb: str) -> None:
        self._emit("verb_invoked", node_id=node_id, verb=verb)

    def emit_verb_completed(self, node_id: str, outcome: dict[str, Any]) -> None:
        self._emit("verb_completed", node_id=node_id, outcome=outcome)

    def emit_retry_attempted(
        self,
        node_id: str,
        attempt: int,
        next_attempt: int,
        reason: str,
        after: float | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "node_id": node_id,
            "attempt": attempt,
            "next_attempt": next_attempt,
            "reason": reason,
        }
        if after is not None:
            payload["after"] = after
        self._emit("retry_attempted", **payload)

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

    def emit_agent_call_started(
        self,
        node_id: str,
        agent_id: str,
        role: str | None,
        provider: str | None,
        model: str,
        *,
        attempt: int = 1,
    ) -> None:
        """Record the model routing decision for one agent invocation
        (ADR-0030 §2 / §Idempotency).

        Emitted from the kernel BEFORE the provider.invoke call so a
        kill+resume between this event and the verb_completed can
        recover the resolved provider/model from the log rather than
        re-running the resolver against a possibly-edited config.

        ``provider`` is ``None`` when no policy override applied (caller
        is using the default provider + the AgentSpec.model literal).
        ``model`` is always populated — it's the literal that was (or
        will be) passed to the provider.
        """
        self._emit(
            "agent_call_started",
            node_id=node_id,
            agent_id=agent_id,
            role=role,
            provider=provider,
            model=model,
            attempt=attempt,
        )

    def emit_run_cost_summary(self, payload: dict[str, Any]) -> None:
        """Emit the per-run token/latency rollup (ADR-0030 §3a).

        Emitted exactly once per terminal disposition, right after the
        corresponding ``run_completed`` event. Resume idempotency is
        enforced kernel-side (see Engine._emit_cost_summary_once).
        ``payload`` should carry ``totals``, ``per_role``, and
        ``per_model`` keys; passed through verbatim into the envelope.
        """
        self._emit("run_cost_summary", **payload)

    def emit_subworkflow_started(
        self,
        node_id: str,
        *,
        sub_run_id: str,
        sub_workflow_module: str,
        inputs_summary: dict[str, Any] | None = None,
    ) -> None:
        """Parent-side marker that a child workflow is being invoked.

        The child writes its own ``{sub_run_id}.events.jsonl`` (Bach A
        purity); this event lives in the parent's log only.
        ``inputs_summary`` is the dict returned by the optional
        ``inputs_verb`` — recorded verbatim for observability.
        """
        self._emit(
            "subworkflow_started",
            node_id=node_id,
            sub_run_id=sub_run_id,
            sub_workflow_module=sub_workflow_module,
            inputs_summary=inputs_summary or {},
        )

    def emit_subworkflow_completed(
        self,
        node_id: str,
        *,
        sub_run_id: str,
        disposition: str,
        outcome: dict[str, Any],
        outcome_summary: dict[str, Any] | None = None,
    ) -> None:
        """Parent-side marker that a child workflow finished.

        ``disposition`` is the human-readable status (``completed``,
        ``failed``, ``cancelled``, ``needs_human``). ``outcome`` is the
        full verb-outcome dict the parent's router will consume — stored
        so a crash between this event and the parent's ``verb_completed``
        can be resumed without re-invoking the (now-finished) child.
        """
        self._emit(
            "subworkflow_completed",
            node_id=node_id,
            sub_run_id=sub_run_id,
            disposition=disposition,
            outcome=outcome,
            outcome_summary=outcome_summary or {},
        )

    def emit_subworkflow_cancelled(
        self,
        node_id: str,
        *,
        sub_run_id: str,
        reason: str = "parent_cancelled",
    ) -> None:
        """Parent-side marker that a child was cancelled (e.g. parent cancel propagated).

        Distinct from ``subworkflow_completed`` because the child may never
        have produced an outcome — propagation can write directly into the
        child's log before any of its loop iterations run.
        """
        self._emit(
            "subworkflow_cancelled",
            node_id=node_id,
            sub_run_id=sub_run_id,
            reason=reason,
        )

    def emit_context_pack_truncated(
        self,
        *,
        leaf_id: str,
        leaf_branch: str,
        plan_hash: str,
        cap_bytes: int,
        node_id: str | None = None,
    ) -> None:
        """Doctrine slice for a leaf's context pack exceeded the cap.

        Emitted by the orchestrator (fanout / kanban executor) after
        :func:`requiem.context_pack.commit_context_pack` returns a receipt
        whose ``doctrine_truncated`` flag is True. Observability only —
        the truncation is benign (we slice at a section boundary), but
        operators want to know the agent received less than the full
        matching doctrine so they can grow the cap or trim the doctrine.
        """
        self._emit(
            "context_pack_truncated",
            node_id=node_id,
            leaf_id=leaf_id,
            leaf_branch=leaf_branch,
            plan_hash=plan_hash,
            cap_bytes=cap_bytes,
        )

    def emit_cancel_requested(
        self, *, reason: str = "operator", requested_by: str = "cli"
    ) -> None:
        """External cancel signal written into the log.

        Per INV-CANCEL-SHORT-CIRCUITS-RETRY this event causes the engine to
        terminate the run at the next safe yield point without consulting
        ``retry_max``. The CLI's ``requiem cancel <run_id>`` writes this
        event; the next time the run is resumed (or on the next loop tick
        of an in-process run) the engine emits ``run_completed("cancelled")``
        and exits.
        """
        self._emit(
            "cancel_requested",
            node_id=None,
            reason=reason,
            requested_by=requested_by,
        )


def parse_envelope(raw: dict[str, Any]) -> Event:
    return Event.model_validate(raw)
