"""Tests for ADR-0030 §3a run_cost_summary projection.

The kernel emits one ``run_cost_summary`` event after ``run_completed``
on every terminal disposition. It is a pure projection over the receipts
already attached to each ``verb_completed`` outcome, grouped by:

  * ``totals``   — aggregate (input_tokens, output_tokens, agent_call_count,
                   total_latency_ms, retry_count)
  * ``per_role`` — per-role rollup (calls, input_tokens, output_tokens,
                   latency_ms). Role attribution comes from the most-recent
                   ``agent_call_started`` matching the same node_id.
  * ``per_model``— per-(provider/)model rollup (calls, input_tokens, output_tokens).

Resume idempotency: ``_emit_cost_summary_once`` re-scans the log; if a
``run_cost_summary`` already exists, no re-emit.
"""
from __future__ import annotations

from requiem.cost import CostSummary, summarize_costs


# ---- helpers -----------------------------------------------------------


def _event(kind: str, **payload):
    """Minimal event envelope shape (matches replay() output)."""
    return {
        "kind": kind,
        "node_id": payload.pop("node_id", None),
        "agent_id": payload.pop("agent_id", None),
        "payload": payload,
    }


def _verb_completed(node_id: str, kind: str = "success", *, receipts=None, value=None):
    """A verb_completed envelope with optional peer + legacy receipts."""
    outcome = {"kind": kind}
    if receipts is not None:
        outcome["receipts"] = receipts
    if value is not None:
        outcome["value"] = value
    return _event("verb_completed", node_id=node_id, outcome=outcome)


def _agent_call_started(node_id: str, role: str, provider: str, model: str, *, attempt: int = 1):
    return _event(
        "agent_call_started", node_id=node_id, agent_id=role,
        role=role, provider=provider, model=model, attempt=attempt,
    )


def _receipt(model: str, *, input_tokens: int, output_tokens: int, latency_ms: int):
    return {
        "kind": "llm_call",
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "latency_ms": latency_ms,
    }


# ---- shape pins --------------------------------------------------------


def test_empty_log_yields_zero_totals_and_empty_maps() -> None:
    summary = summarize_costs([])
    assert summary == CostSummary(
        totals={
            "input_tokens": 0, "output_tokens": 0, "agent_call_count": 0,
            "total_latency_ms": 0, "retry_count": 0,
        },
        per_role={},
        per_model={},
    )


def test_single_success_with_peer_receipt_aggregates_into_totals_and_per_model() -> None:
    events = [
        _agent_call_started("planner_1", "planner", "anthropic", "claude-opus-4.7"),
        _verb_completed("planner_1", receipts=[
            _receipt("claude-opus-4.7", input_tokens=1000, output_tokens=500, latency_ms=4200),
        ]),
    ]
    s = summarize_costs(events)
    assert s.totals["input_tokens"] == 1000
    assert s.totals["output_tokens"] == 500
    assert s.totals["total_latency_ms"] == 4200
    assert s.totals["agent_call_count"] == 1
    assert s.totals["retry_count"] == 0
    assert "planner" in s.per_role
    assert s.per_role["planner"]["calls"] == 1
    assert s.per_role["planner"]["input_tokens"] == 1000


# ---- multi-role / multi-model ------------------------------------------


def test_per_role_and_per_model_aggregate_correctly_across_three_roles() -> None:
    events = [
        # planner: 1 call, claude-opus-4.7
        _agent_call_started("planner_1", "planner", "anthropic", "claude-opus-4.7"),
        _verb_completed("planner_1", receipts=[
            _receipt("claude-opus-4.7", input_tokens=4500, output_tokens=2100, latency_ms=32000),
        ]),
        # reviewer: 2 calls, claude-sonnet-4
        _agent_call_started("reviewer_1", "reviewer", "anthropic", "claude-sonnet-4"),
        _verb_completed("reviewer_1", receipts=[
            _receipt("claude-sonnet-4", input_tokens=2600, output_tokens=900, latency_ms=14000),
        ]),
        _agent_call_started("reviewer_2", "reviewer", "anthropic", "claude-sonnet-4"),
        _verb_completed("reviewer_2", receipts=[
            _receipt("claude-sonnet-4", input_tokens=2600, output_tokens=900, latency_ms=14000),
        ]),
        # closer: 1 call, gpt-4o-mini
        _agent_call_started("closer_1", "closer", "openai", "gpt-4o-mini"),
        _verb_completed("closer_1", receipts=[
            _receipt("gpt-4o-mini", input_tokens=500, output_tokens=200, latency_ms=2000),
        ]),
    ]
    s = summarize_costs(events)
    assert s.totals["agent_call_count"] == 4
    assert s.totals["input_tokens"] == 4500 + 2600 + 2600 + 500
    assert s.totals["output_tokens"] == 2100 + 900 + 900 + 200
    assert set(s.per_role.keys()) == {"planner", "reviewer", "closer"}
    assert s.per_role["reviewer"]["calls"] == 2
    assert s.per_role["reviewer"]["input_tokens"] == 5200
    # per_model may key by raw model or provider/model — accept either.
    model_keys = set(s.per_model.keys())
    assert any("claude-opus" in k for k in model_keys)
    assert any("claude-sonnet" in k for k in model_keys)
    assert any("gpt-4o-mini" in k for k in model_keys)


# ---- retries -----------------------------------------------------------


def test_retryable_failure_tokens_counted_and_retry_count_increments() -> None:
    """Retried calls still cost tokens; ``retry_count`` reflects retry events."""
    events = [
        _agent_call_started("flaky_1", "planner", "anthropic", "claude-opus-4.7"),
        _verb_completed("flaky_1", kind="retryable_failure", receipts=[
            _receipt("claude-opus-4.7", input_tokens=100, output_tokens=20, latency_ms=1000),
        ]),
        _event("retry_attempted", node_id="flaky_1"),
        _verb_completed("flaky_1", receipts=[
            _receipt("claude-opus-4.7", input_tokens=100, output_tokens=20, latency_ms=1000),
        ]),
    ]
    s = summarize_costs(events)
    assert s.totals["input_tokens"] == 200
    assert s.totals["retry_count"] == 1


# ---- legacy receipts ---------------------------------------------------


def test_legacy_value_receipts_are_deduped_against_peer() -> None:
    """``success_with`` copies receipts into ``outcome.value.receipts`` for
    back-compat. The projection must not double-count when both locations
    carry the same receipt."""
    same_receipt = _receipt("claude-opus-4.7", input_tokens=100, output_tokens=20, latency_ms=500)
    events = [
        _agent_call_started("n1", "planner", "anthropic", "claude-opus-4.7"),
        _verb_completed("n1", receipts=[same_receipt], value={"receipts": [same_receipt]}),
    ]
    s = summarize_costs(events)
    assert s.totals["input_tokens"] == 100  # not 200


def test_legacy_receipt_with_nested_list_does_not_crash_dedupe() -> None:
    """Receipts can carry nested lists (``inspected_artifacts``) which are
    not hashable; the dedupe key must serialize defensively."""
    receipt = {
        "kind": "llm_call",
        "model": "claude-opus-4.7",
        "input_tokens": 100,
        "output_tokens": 20,
        "latency_ms": 500,
        "inspected_artifacts": [{"path": "x.py"}, {"path": "y.py"}],
    }
    events = [
        _agent_call_started("n1", "planner", "anthropic", "claude-opus-4.7"),
        _verb_completed("n1", receipts=[receipt], value={"receipts": [receipt]}),
    ]
    s = summarize_costs(events)  # must not raise TypeError
    assert s.totals["input_tokens"] == 100


# ---- role fallback -----------------------------------------------------


def test_verb_completed_without_prior_agent_call_started_attributes_to_unknown_role() -> None:
    """Pre-ADR-0030 workflows have no ``agent_call_started`` events — the
    projection must still aggregate them; role attribution falls to
    ``\"unknown\"`` rather than failing."""
    events = [
        _verb_completed("legacy_node", receipts=[
            _receipt("claude-opus-4.7", input_tokens=100, output_tokens=20, latency_ms=500),
        ]),
    ]
    s = summarize_costs(events)
    assert s.totals["agent_call_count"] == 1
    assert s.totals["input_tokens"] == 100
    # Some attribution bucket exists — either "unknown" or empty per_role
    # depending on the implementation. Both are acceptable for v0; what
    # matters is the totals are correct.
    if s.per_role:
        assert any(v["input_tokens"] == 100 for v in s.per_role.values())


# ---- idempotency / determinism -----------------------------------------


def test_summarize_costs_is_deterministic_on_same_input() -> None:
    events = [
        _agent_call_started("n1", "planner", "anthropic", "claude-opus-4.7"),
        _verb_completed("n1", receipts=[
            _receipt("claude-opus-4.7", input_tokens=1000, output_tokens=500, latency_ms=4200),
        ]),
    ]
    s1 = summarize_costs(events)
    s2 = summarize_costs(events)
    assert s1 == s2


def test_summarize_costs_pure_does_not_mutate_input() -> None:
    """The projection is pure — must not mutate the events it reads."""
    events = [
        _agent_call_started("n1", "planner", "anthropic", "claude-opus-4.7"),
        _verb_completed("n1", receipts=[
            _receipt("claude-opus-4.7", input_tokens=1000, output_tokens=500, latency_ms=4200),
        ]),
    ]
    import copy
    snapshot = copy.deepcopy(events)
    summarize_costs(events)
    assert events == snapshot
