"""Focused kernel tests.

Cover (a) the simplified `_reconstruct` cursor fold, (b) the new
BadOutput routing arm, and (c) INV-CANCEL-SHORT-CIRCUITS-RETRY.

End-to-end coverage lives in `test_integration_code_review.py`.
"""
from pathlib import Path

import pytest

from requiem.agent import AgentSpec, FakeProvider
from requiem.dsl import AgentRegistry, VerbRegistry, WorkflowBuilder
from requiem.kernel import (
    Completed,
    Engine,
    Failed,
    _AtNode,
    _AwaitingGate,
    _AwaitingRoute,
    _RouteAfterGate,
    _Terminated,
    _reconstruct,
)
from requiem.outcomes import (
    BadOutput,
    Cancelled,
    PermanentFailure,
    RetryableFailure,
    Success,
)
from requiem.toolbelt import Toolbelt


# ---- _reconstruct fold ---------------------------------------------


def _ev(kind, **kw):
    payload = kw.pop("payload", {})
    return {
        "event_id": kw.pop("event_id", 0),
        "run_id": "r",
        "ts": "2026-05-31T00:00:00+00:00",
        "kind": kind,
        "schema_version": 1,
        "node_id": kw.pop("node_id", None),
        "team_id": None,
        "agent_id": None,
        "payload": payload,
    }


def test_reconstruct_empty_log_starts_at_entry():
    cursor, completed = _reconstruct([], entry="start")
    assert cursor == _AtNode("start", 1)
    assert completed == {}


def test_reconstruct_after_node_entered_resumes_in_node():
    events = [
        _ev("run_started", payload={"workflow": "w"}),
        _ev("node_entered", node_id="a", payload={"attempt": 1}),
    ]
    cursor, _ = _reconstruct(events, entry="start")
    assert cursor == _AtNode("a", 1)


def test_reconstruct_after_verb_completed_resumes_at_route():
    events = [
        _ev("node_entered", node_id="a", payload={"attempt": 1}),
        _ev("verb_completed", node_id="a", payload={"outcome": {"kind": "success"}}),
    ]
    cursor, completed = _reconstruct(events, entry="start")
    assert isinstance(cursor, _AwaitingRoute)
    assert cursor.node_id == "a"
    assert completed["a"] == {"kind": "success"}


def test_reconstruct_after_retry_attempted_bumps_attempt():
    events = [
        _ev("node_entered", node_id="a", payload={"attempt": 1}),
        _ev("verb_completed", node_id="a",
            payload={"outcome": {"kind": "retryable_failure",
                                 "retry_key": "k", "error_kind": "t",
                                 "message": "m", "attempt": 1}}),
        _ev("retry_attempted", node_id="a",
            payload={"attempt": 1, "next_attempt": 2, "reason": "m"}),
    ]
    cursor, _ = _reconstruct(events, entry="start")
    assert cursor == _AtNode("a", 2)


def test_reconstruct_after_route_taken_advances():
    events = [
        _ev("node_entered", node_id="a", payload={"attempt": 1}),
        _ev("verb_completed", node_id="a", payload={"outcome": {"kind": "success"}}),
        _ev("route_taken", node_id="a", payload={"key": "success", "to_node": "b"}),
    ]
    cursor, _ = _reconstruct(events, entry="start")
    assert cursor == _AtNode("b", 1)


def test_reconstruct_after_gate_opened_awaits_gate():
    events = [
        _ev("node_entered", node_id="g"),
        _ev("verb_completed", node_id="g",
            payload={"outcome": {"kind": "needs_human", "gate": "g",
                                 "prompt": "?", "options": ("y", "n"),
                                 "context": {}}}),
        _ev("gate_opened", node_id="g",
            payload={"prompt": "?", "options": ["y", "n"]}),
    ]
    cursor, _ = _reconstruct(events, entry="start")
    assert cursor == _AwaitingGate("g", prompt="?", options=("y", "n"))


def test_reconstruct_after_gate_resolved_routes_gate():
    events = [
        _ev("gate_opened", node_id="g", payload={"prompt": "?", "options": ["y"]}),
        _ev("gate_resolved", node_id="g", payload={"choice": "y"}),
    ]
    cursor, _ = _reconstruct(events, entry="start")
    assert cursor == _RouteAfterGate("g", "y")


def test_reconstruct_terminated_recorded():
    events = [
        _ev("run_completed", node_id="end",
            payload={"terminal": "completed", "final_node": "end"}),
    ]
    cursor, _ = _reconstruct(events, entry="start")
    assert cursor == _Terminated("completed", "end")


# ---- BadOutput routing ---------------------------------------------


def _engine_with(workflow, *, scripts, log_dir):
    spec = AgentSpec(name="agent_x", charter="c",
                     response_model=__import__("pydantic").BaseModel)
    agents = AgentRegistry()
    agents.register(spec)
    return Engine(
        workflow=workflow,
        verbs=VerbRegistry(),
        agents=agents,
        provider=FakeProvider(scripts=scripts),
        toolbelt=Toolbelt.real(),
        log_dir=log_dir,
    )


async def test_bad_output_routes_to_bad_output_edge_when_present(tmp_path: Path):
    wf = (
        WorkflowBuilder("w").entry("a")
            .agent("a", agent="agent_x", prompt_verb="prompt")
                .edge("a", on="success",     to="end")
                .edge("a", on="bad_output",  to="remediate")
                .edge("a", on="permanent_failure", to="fail_end")
            .script("remediate", verb="noop")
                .edge("remediate", on="success", to="end")
            .terminate("end")
            .terminate("fail_end", disposition="failed")
            .build()
    )
    bad = BadOutput(error_kind="schema_mismatch",
                    validation_errors=("missing field x",))
    engine = _engine_with(wf, scripts={"agent_x": [bad]}, log_dir=tmp_path)
    engine.verbs.register("prompt")(lambda ctx: "p")
    engine.verbs.register("noop")(lambda ctx: Success(value={"remediated": True}))
    result = await engine.run("bad-out-1")
    assert isinstance(result, Completed)
    assert "remediate" in result.projection["nodes_entered"]


async def test_bad_output_falls_through_to_permanent_failure_edge(tmp_path: Path):
    wf = (
        WorkflowBuilder("w").entry("a")
            .agent("a", agent="agent_x", prompt_verb="prompt")
                .edge("a", on="success",            to="end")
                .edge("a", on="permanent_failure",  to="fail_end")
            .terminate("end")
            .terminate("fail_end", disposition="failed")
            .build()
    )
    bad = BadOutput(error_kind="schema_mismatch", validation_errors=("oops",))
    engine = _engine_with(wf, scripts={"agent_x": [bad]}, log_dir=tmp_path)
    engine.verbs.register("prompt")(lambda ctx: "p")
    result = await engine.run("bad-out-2")
    assert isinstance(result, Completed)
    assert result.disposition == "failed"


async def test_bad_output_with_no_edges_fails(tmp_path: Path):
    wf = (
        WorkflowBuilder("w").entry("a")
            .agent("a", agent="agent_x", prompt_verb="prompt")
                .edge("a", on="success", to="end")
            .terminate("end").build()
    )
    bad = BadOutput(error_kind="schema_mismatch", validation_errors=("oops",))
    engine = _engine_with(wf, scripts={"agent_x": [bad]}, log_dir=tmp_path)
    engine.verbs.register("prompt")(lambda ctx: "p")
    result = await engine.run("bad-out-3")
    assert isinstance(result, Failed)
    assert result.error_kind == "schema_mismatch"


# ---- INV-CANCEL-SHORT-CIRCUITS-RETRY ------------------------------


async def test_cancel_short_circuits_retry_budget(tmp_path: Path):
    """A Cancelled outcome must abort the run even when retry_max remains."""
    wf = (
        WorkflowBuilder("w").entry("a")
            .script("a", verb="cancel_me", retry_max=99)
                .edge("a", on="retry_exhausted", to="fail_end")
            .terminate("fail_end", disposition="failed").build()
    )
    engine = _engine_with(wf, scripts={}, log_dir=tmp_path)
    engine.verbs.register("cancel_me")(
        lambda ctx: Cancelled(cause="operator", at_step="a")
    )
    result = await engine.run("cancel-1")
    assert isinstance(result, Failed)
    assert result.error_kind == "cancelled"
    # The verb was invoked exactly once — no retry attempt was made.
    from requiem.persistence import replay
    events = list(replay(tmp_path / "cancel-1.events.jsonl"))
    enters = [e for e in events if e["kind"] == "node_entered"]
    retries = [e for e in events if e["kind"] == "retry_attempted"]
    assert len(enters) == 1
    assert len(retries) == 0
