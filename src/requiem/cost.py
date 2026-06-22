"""Cost rollup — ADR-0030 §3a.

Pure projection over an event log. Given the list of envelopes the
``EventStore`` already persisted, :func:`summarize_costs` produces a
:class:`CostSummary` carrying totals, per-role, and per-model
aggregations of every receipt the kernel/providers attached to verb
outcomes.

The function is intentionally I/O-free — callers (the kernel's terminal-
disposition path, the dashboard, the eval rubrics) read events from
wherever they live and hand the list in. The kernel emits the result as
one ``run_cost_summary`` event right after ``run_completed``; the
``requiem events`` CLI re-runs this projection at render time to print
the cost block.

Receipt source per ADR-0004 §4.4: every outcome variant (Success,
RetryableFailure, PermanentFailure, BadOutput, NeedsHuman, Cancelled)
carries a peer ``receipts: tuple[dict, ...]`` field. Each receipt today
follows the ``make_receipt`` shape from
``requiem.providers._common`` — ``{kind, model, input_tokens,
output_tokens, latency_ms, request_id, error}``. We tolerate the legacy
location ``outcome.value["receipts"]`` for backwards compat with
in-tree tests that pre-date the peer field migration.

Role attribution: the kernel emits an ``agent_call_started`` event
BEFORE every provider.invoke, carrying ``role`` / ``provider`` /
``model``. We match each ``verb_completed`` (the outcome carrier) to
the most-recent prior ``agent_call_started`` on the same ``node_id``
(team branches use the team-node id but distinguish via ``agent_id``).
A ``verb_completed`` with no matching start event attributes to
role=``"unknown"`` — defensive against scripted/legacy logs that don't
emit the start event yet.

This module is the single source of truth for the rollup shape. The
kernel/CLI/dashboard all consume :class:`CostSummary` directly so the
field set evolves in one place.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


_UNKNOWN_ROLE = "unknown"
_UNKNOWN_MODEL = "unknown"


@dataclass(frozen=True, slots=True)
class CostSummary:
    """Per-run rollup of tokens, latency, and call counts.

    All counters are ``int`` (latency rounded to ms); the JSON
    serializer doesn't need to special-case floats. Empty event logs
    produce all-zero ``totals`` and empty ``per_role`` / ``per_model``
    dicts — the caller still emits the event for shape consistency.
    """

    totals: dict[str, int] = field(default_factory=dict)
    per_role: dict[str, dict[str, int]] = field(default_factory=dict)
    per_model: dict[str, dict[str, int]] = field(default_factory=dict)


def _zero_totals() -> dict[str, int]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "agent_call_count": 0,
        "total_latency_ms": 0,
        "retry_count": 0,
    }


def _zero_role_bucket() -> dict[str, int]:
    return {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "latency_ms": 0,
    }


def _zero_model_bucket() -> dict[str, int]:
    return {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }


def _outcome_receipts(outcome: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the receipts list from an outcome dict, handling both the
    canonical peer-field shape (``outcome.receipts``) and the legacy
    in-``value`` shape (``outcome.value.receipts``) for backwards compat.

    Returns the empty list when neither location carries receipts.
    """
    out: list[dict[str, Any]] = []
    peer = outcome.get("receipts") or ()
    if isinstance(peer, (list, tuple)):
        out.extend(r for r in peer if isinstance(r, dict))
    value = outcome.get("value") or {}
    if isinstance(value, dict):
        legacy = value.get("receipts") or ()
        if isinstance(legacy, (list, tuple)):
            # Only add legacy receipts the peer field didn't already cover.
            # Dedupe by JSON-serialized fingerprint — receipts can contain
            # nested lists/dicts (e.g. ``inspected_artifacts``) which are
            # not hashable directly. ``sort_keys=True`` makes the dump
            # stable across dict-ordering variants.
            import json as _json
            def _fingerprint(r: dict[str, Any]) -> str:
                try:
                    return _json.dumps(r, sort_keys=True, default=str)
                except (TypeError, ValueError):
                    return repr(sorted(r.items()))
            seen = {_fingerprint(r) for r in out}
            for r in legacy:
                if not isinstance(r, dict):
                    continue
                key = _fingerprint(r)
                if key in seen:
                    continue
                seen.add(key)
                out.append(r)
    return out


def summarize_costs(events: Iterable[dict[str, Any]]) -> CostSummary:
    """Project the event log into a :class:`CostSummary`.

    Pure / deterministic / idempotent: running it twice on the same
    sequence yields equal :class:`CostSummary` instances. Resume safety
    flows from this — the kernel's ``_emit_cost_summary_once`` guard
    prevents a SECOND emission, but a resume that re-runs this
    projection produces the same value either way.
    """
    events = list(events)

    totals = _zero_totals()
    per_role: dict[str, dict[str, int]] = {}
    per_model: dict[str, dict[str, int]] = {}

    # Build (node_id, agent_id) → most-recent agent_call_started payload
    # map. Walk the log forward; later starts overwrite earlier ones for
    # the same key (the per-attempt model resolution is the one we want
    # for the verb_completed that immediately follows).
    pending_start: dict[tuple[str, str], dict[str, Any]] = {}

    for ev in events:
        kind = ev.get("kind")
        payload = ev.get("payload") or {}
        node_id = ev.get("node_id") or ""

        if kind == "agent_call_started":
            # Key by (node_id, agent_id) so parallel-fork team branches
            # don't trample each other — the team node_id is shared but
            # the agent_id differs per branch.
            agent_id = ev.get("agent_id") or ""
            pending_start[(node_id, agent_id)] = payload
            # Also stash a (node_id, "") fallback so single-agent
            # verb_completed events (which carry no agent_id today) can
            # still find the most recent start.
            pending_start[(node_id, "")] = payload
            continue

        if kind == "retry_attempted":
            totals["retry_count"] = totals["retry_count"] + 1
            continue

        if kind not in ("verb_completed", "team_branch_completed"):
            continue

        outcome = payload.get("outcome") or {}
        receipts = _outcome_receipts(outcome)
        if not receipts:
            continue

        # Resolve role/model attribution.
        agent_id = ev.get("agent_id") or ""
        start = (
            pending_start.get((node_id, agent_id))
            or pending_start.get((node_id, ""))
            or {}
        )
        role = start.get("role") or _UNKNOWN_ROLE
        # Provider+model key in per_model uses "<provider>/<model>" when
        # a provider override applied, else the bare model literal. This
        # matches the dashboard rendering convention without forcing the
        # CLI to glue them together itself.
        provider_hint = start.get("provider")

        for r in receipts:
            input_tokens = int(r.get("input_tokens") or 0)
            output_tokens = int(r.get("output_tokens") or 0)
            latency_ms = int(r.get("latency_ms") or 0)
            receipt_model = str(r.get("model") or start.get("model") or _UNKNOWN_MODEL)

            totals["input_tokens"] = totals["input_tokens"] + input_tokens
            totals["output_tokens"] = totals["output_tokens"] + output_tokens
            totals["total_latency_ms"] = totals["total_latency_ms"] + latency_ms
            totals["agent_call_count"] = totals["agent_call_count"] + 1

            role_bucket = per_role.setdefault(role, _zero_role_bucket())
            role_bucket["calls"] += 1
            role_bucket["input_tokens"] += input_tokens
            role_bucket["output_tokens"] += output_tokens
            role_bucket["latency_ms"] += latency_ms

            if provider_hint:
                model_key = f"{provider_hint}/{receipt_model}"
            else:
                model_key = receipt_model
            model_bucket = per_model.setdefault(model_key, _zero_model_bucket())
            model_bucket["calls"] += 1
            model_bucket["input_tokens"] += input_tokens
            model_bucket["output_tokens"] += output_tokens

    return CostSummary(totals=totals, per_role=per_role, per_model=per_model)


__all__ = ["CostSummary", "summarize_costs"]
