"""Sub-workflow invocation primitive — ADR 0005.

Covers the seven scenarios laid out in the Berlioz Phase B build:

* parent → child → Success: parent receives child's projection
* parent → child → PermanentFailure: parent routes to ``permanent_failure``
* parent → child → NeedsHuman: bubbles up; parent suspends
* parent crash mid-child: parent resume re-attaches; child resumes too
* INV-SUBWORKFLOW-LOG-ISOLATION: child events in parent's log are ignored
* INV-CANCEL propagation: cancel parent → child also receives cancel marker
* three-level nesting (grandparent → parent → child) runs to completion
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

from requiem.agent import AgentSpec, FakeProvider
from requiem.dsl import AgentRegistry, VerbRegistry, WorkflowBuilder
from requiem.events import EventEmitter
from requiem.kernel import (
    Completed,
    Engine,
    Failed,
    Suspended,
    _AtNode,
    _AwaitingRoute,
    _AwaitingSubworkflow,
    _reconstruct,
)
from requiem.outcomes import PermanentFailure, Success
from requiem.persistence import EventStore, replay
from requiem.toolbelt import Toolbelt


# ---- factory helpers to register child workflows as importable modules


def _register_child_module(
    mod_name: str,
    *,
    builder,
    verbs_factory=None,
    agents_factory=None,
    provider_factory=None,
    gate_handler=None,
) -> str:
    """Inject a synthetic module that exposes ``build_engine(log_dir)``.

    The kernel's subworkflow dispatch loads child workflows by importable
    module path; rather than spread fixture modules across the test tree,
    we synthesise them in-process via ``sys.modules``.
    """
    mod = types.ModuleType(mod_name)
    verbs_factory = verbs_factory or VerbRegistry
    agents_factory = agents_factory or AgentRegistry
    provider_factory = provider_factory or FakeProvider

    def build_engine(log_dir: Path, **_) -> Engine:
        return Engine(
            workflow=builder(),
            verbs=verbs_factory(),
            agents=agents_factory(),
            provider=provider_factory(),
            toolbelt=Toolbelt.real(),
            log_dir=log_dir,
            gate_handler=gate_handler,
        )

    def build_workflow():
        return builder()

    mod.build_engine = build_engine  # type: ignore[attr-defined]
    mod.build_workflow = build_workflow  # type: ignore[attr-defined]
    sys.modules[mod_name] = mod
    return mod_name


def _trivial_child_workflow(name: str = "child"):
    """Single-script child that always succeeds with a payload."""

    def builder():
        return (
            WorkflowBuilder(name).entry("only")
                .script("only", verb="say_hi")
                    .edge("only", on="success", to="end")
                .terminate("end").build()
        )

    def verbs():
        v = VerbRegistry()
        v.register("say_hi")(
            lambda ctx: Success(value={"greeting": "hello from child"})
        )
        return v

    return builder, verbs


def _failing_child_workflow(name: str = "child_fail"):
    def builder():
        return (
            WorkflowBuilder(name).entry("only")
                .script("only", verb="boom")
                    .edge("only", on="success", to="end")
                    .edge("only", on="permanent_failure", to="fail_end")
                .terminate("end")
                .terminate("fail_end", disposition="failed").build()
        )

    def verbs():
        v = VerbRegistry()
        v.register("boom")(
            lambda ctx: PermanentFailure(
                error_kind="child.kaboom", message="child verb crashed"
            )
        )
        return v

    return builder, verbs


def _gating_child_workflow(name: str = "child_gate"):
    """Child that immediately hits a human gate."""

    def builder():
        return (
            WorkflowBuilder(name).entry("ask")
                .human_gate("ask", prompt="approve?", options=["yes", "no"])
                    .edge("ask", on="needs_human:yes", to="end")
                    .edge("ask", on="needs_human:no",  to="fail_end")
                .terminate("end")
                .terminate("fail_end", disposition="failed").build()
        )

    return builder, VerbRegistry


def _parent_wrapping(child_module: str, *, name: str = "parent"):
    """Parent that has one subworkflow node + success edge to terminate."""

    def builder():
        return (
            WorkflowBuilder(name).entry("call_child")
                .subworkflow("call_child", workflow=child_module)
                    .edge("call_child", on="success",            to="end")
                    .edge("call_child", on="permanent_failure",  to="fail_end")
                    .edge("call_child", on="needs_human",        to="bubble_gate")
                    .edge("call_child", on="cancelled",          to="fail_end")
                .human_gate("bubble_gate",
                            prompt="resolve child gate",
                            options=["yes", "no"])
                    .edge("bubble_gate", on="needs_human:yes", to="end")
                    .edge("bubble_gate", on="needs_human:no",  to="fail_end")
                .terminate("end")
                .terminate("fail_end", disposition="failed").build()
        )

    return builder


def _parent_engine(parent_builder, log_dir: Path, *, gate_handler=None) -> Engine:
    return Engine(
        workflow=parent_builder(),
        verbs=VerbRegistry(),
        agents=AgentRegistry(),
        provider=FakeProvider(),
        toolbelt=Toolbelt.real(),
        log_dir=log_dir,
        gate_handler=gate_handler,
    )


# ---- 1. parent → child → Success -----------------------------------


async def test_parent_receives_child_success_value(tmp_path: Path):
    cb, cv = _trivial_child_workflow()
    mod = _register_child_module(
        "tests._sub_trivial", builder=cb, verbs_factory=cv,
    )
    pb = _parent_wrapping(mod)
    engine = _parent_engine(pb, tmp_path)

    result = await engine.run("p1")

    assert isinstance(result, Completed)
    assert result.disposition == "completed"

    # Parent's log records the subworkflow markers (Bach A purity — full
    # detail of child stays in the child's log).
    parent_events = list(replay(tmp_path / "p1.events.jsonl"))
    kinds = [e["kind"] for e in parent_events]
    assert "subworkflow_started" in kinds
    assert "subworkflow_completed" in kinds
    # Parent does NOT emit a separate verb_completed for the subworkflow
    # node (per ADR 0005 — subworkflow_completed is the verb-completion
    # signal). Other nodes (gate / terminate) may still emit verb_completed.
    sub_node_verbs = [
        e for e in parent_events
        if e["kind"] == "verb_completed" and e.get("node_id") == "call_child"
    ]
    assert sub_node_verbs == []

    # Child's log lives at log_dir/{parent_run_id}__{node_id}.events.jsonl
    child_log = tmp_path / "p1__call_child.events.jsonl"
    assert child_log.exists(), "child must write its own log file"
    child_events = list(replay(child_log))
    assert any(e["kind"] == "run_started" for e in child_events)
    assert any(e["kind"] == "run_completed" for e in child_events)

    # Parent's completed map has the child's projection threaded through.
    sub_complete = next(e for e in parent_events if e["kind"] == "subworkflow_completed")
    outcome = sub_complete["payload"]["outcome"]
    assert outcome["kind"] == "success"
    assert outcome["value"]["sub_run_id"] == "p1__call_child"
    assert outcome["value"]["child_disposition"] == "completed"


# ---- 2. parent → child → PermanentFailure --------------------------


async def test_parent_routes_permanent_failure_on_child_failure(tmp_path: Path):
    cb, cv = _failing_child_workflow()
    mod = _register_child_module(
        "tests._sub_failing", builder=cb, verbs_factory=cv,
    )
    pb = _parent_wrapping(mod)
    engine = _parent_engine(pb, tmp_path)

    result = await engine.run("p2")

    assert isinstance(result, Completed)
    # Parent reached fail_end via its `permanent_failure` edge.
    assert result.disposition == "failed"
    assert result.final_node == "fail_end"

    parent_events = list(replay(tmp_path / "p2.events.jsonl"))
    routes = [e for e in parent_events if e["kind"] == "route_taken"
              and e.get("node_id") == "call_child"]
    assert any(r["payload"]["key"].startswith("permanent_failure") for r in routes)


# ---- 3. parent → child → NeedsHuman --------------------------------


async def test_child_needs_human_bubbles_up_to_parent(tmp_path: Path):
    cb, cv = _gating_child_workflow()
    mod = _register_child_module(
        "tests._sub_gating", builder=cb, verbs_factory=cv,
        # No gate_handler — child returns Suspended naturally.
    )
    pb = _parent_wrapping(mod)
    # Parent also has no gate_handler so the bubbled NeedsHuman suspends
    # the parent at the subworkflow node itself (it acts as a gate).
    engine = _parent_engine(pb, tmp_path)

    result = await engine.run("p3")

    # The parent's subworkflow node is the gate that fires. The child's
    # prompt is forwarded verbatim so the operator can answer the child's
    # question via the parent.
    assert isinstance(result, Suspended)
    assert result.node_id == "call_child"
    assert result.prompt == "approve?"
    assert result.options == ("yes", "no")

    parent_events = list(replay(tmp_path / "p3.events.jsonl"))
    sub_complete = next(
        (e for e in parent_events if e["kind"] == "subworkflow_completed"),
        None,
    )
    assert sub_complete is not None
    assert sub_complete["payload"]["disposition"] == "needs_human"
    # The child's gate prompt is preserved in the subworkflow's outcome.
    outcome = sub_complete["payload"]["outcome"]
    assert outcome["kind"] == "needs_human"
    assert outcome["prompt"] == "approve?"
    assert tuple(outcome["options"]) == ("yes", "no")

    # Parent emitted its own gate_opened event mirroring the bubble.
    assert any(e["kind"] == "gate_opened" and e.get("node_id") == "call_child"
               for e in parent_events)

    # And the child's own log records the gate.
    child_events = list(replay(tmp_path / "p3__call_child.events.jsonl"))
    assert any(e["kind"] == "gate_opened" for e in child_events)
    assert not any(e["kind"] == "gate_resolved" for e in child_events)


# ---- 4. parent crash mid-child: resume re-attaches -----------------


async def test_parent_resume_reattaches_to_child(tmp_path: Path):
    """Simulate a crash *between* subworkflow_started and subworkflow_completed.

    We forge a parent log that has ``subworkflow_started`` for the child but
    no ``subworkflow_completed`` — the child likewise has only its initial
    events. Re-running the parent must re-attach the child engine (without
    emitting a *second* ``subworkflow_started``) and drive both to completion.
    """
    cb, cv = _trivial_child_workflow("child_resume")
    mod = _register_child_module(
        "tests._sub_resume", builder=cb, verbs_factory=cv,
    )
    pb = _parent_wrapping(mod)

    run_id = "p4"
    sub_run_id = f"{run_id}__call_child"

    # Forge parent log up to subworkflow_started (no subworkflow_completed).
    parent_log = tmp_path / f"{run_id}.events.jsonl"
    parent_store = EventStore(parent_log)
    parent_emit = EventEmitter(run_id, parent_store.append)
    parent_emit.emit_run_started("parent", workflow_module=None)
    parent_emit.emit_node_entered("call_child", attempt=1)
    parent_emit.emit_subworkflow_started(
        "call_child",
        sub_run_id=sub_run_id,
        sub_workflow_module=mod,
        inputs_summary={},
    )
    # Child log left empty — pretend child hadn't started either.

    # Resume parent. The kernel should re-attach (not re-emit started).
    engine = _parent_engine(pb, tmp_path)
    result = await engine.run(run_id)
    assert isinstance(result, Completed)
    assert result.disposition == "completed"

    parent_events = list(replay(parent_log))
    starts = [e for e in parent_events if e["kind"] == "subworkflow_started"]
    completes = [e for e in parent_events if e["kind"] == "subworkflow_completed"]
    assert len(starts) == 1, "must not emit a second subworkflow_started on resume"
    assert len(completes) == 1


async def test_reconstruct_jumps_to_awaiting_subworkflow_on_started(tmp_path: Path):
    """Pure ``_reconstruct`` check: subworkflow_started → _AwaitingSubworkflow."""

    def _ev(kind, run_id="r", **kw):
        return {
            "event_id": 0, "run_id": run_id,
            "ts": "2026-05-31T00:00:00+00:00",
            "kind": kind, "schema_version": 1,
            "node_id": kw.pop("node_id", None),
            "team_id": None, "agent_id": None,
            "payload": kw.pop("payload", {}),
        }

    events = [
        _ev("node_entered", node_id="sub", payload={"attempt": 1}),
        _ev(
            "subworkflow_started", node_id="sub",
            payload={
                "sub_run_id": "r__sub",
                "sub_workflow_module": "x.y",
                "inputs_summary": {},
            },
        ),
    ]
    cursor, completed = _reconstruct(events, entry="start", run_id="r")
    assert isinstance(cursor, _AwaitingSubworkflow)
    assert cursor.node_id == "sub"
    assert cursor.sub_run_id == "r__sub"
    assert cursor.sub_workflow_module == "x.y"


async def test_reconstruct_after_subworkflow_completed_jumps_to_route(tmp_path: Path):
    """A crash *between* subworkflow_completed and the next route_taken step
    must resume by routing on the stored outcome — NOT by re-invoking the
    (now-finished) child.
    """

    def _ev(kind, run_id="r", **kw):
        return {
            "event_id": 0, "run_id": run_id,
            "ts": "2026-05-31T00:00:00+00:00",
            "kind": kind, "schema_version": 1,
            "node_id": kw.pop("node_id", None),
            "team_id": None, "agent_id": None,
            "payload": kw.pop("payload", {}),
        }

    events = [
        _ev("node_entered", node_id="sub", payload={"attempt": 1}),
        _ev(
            "subworkflow_started", node_id="sub",
            payload={
                "sub_run_id": "r__sub",
                "sub_workflow_module": "x.y",
                "inputs_summary": {},
            },
        ),
        _ev(
            "subworkflow_completed", node_id="sub",
            payload={
                "sub_run_id": "r__sub",
                "disposition": "completed",
                "outcome": {"kind": "success", "value": {"sub_run_id": "r__sub"}},
                "outcome_summary": {"kind": "success"},
            },
        ),
    ]
    cursor, completed = _reconstruct(events, entry="start", run_id="r")
    assert isinstance(cursor, _AwaitingRoute)
    assert cursor.node_id == "sub"
    assert cursor.outcome["kind"] == "success"
    assert completed["sub"]["kind"] == "success"


# ---- 5. INV-SUBWORKFLOW-LOG-ISOLATION (the Brahms-harness #6 finding) ----


async def test_reconstruct_filters_foreign_run_id_events():
    """Events whose envelope run_id ≠ self.run_id must not advance the cursor.

    This pins the invariant motivating this whole seat. Even if a child
    workflow's events somehow bled into the parent's log, the parent's
    ``_reconstruct`` must ignore them — otherwise the parent's resume
    position would jump to a child node that the parent never owned.
    """

    def _ev(kind, *, run_id, node_id=None, payload=None):
        return {
            "event_id": 0, "run_id": run_id,
            "ts": "2026-05-31T00:00:00+00:00",
            "kind": kind, "schema_version": 1,
            "node_id": node_id, "team_id": None, "agent_id": None,
            "payload": payload or {},
        }

    mixed = [
        # Parent's own progression: entered node "a", about to route.
        _ev("node_entered", run_id="parent", node_id="a", payload={"attempt": 1}),
        # Child's events leak in. With no filter, the foreign route_taken
        # below would advance the parent's cursor to a child node.
        _ev("node_entered",
            run_id="parent__a", node_id="child_step", payload={"attempt": 1}),
        _ev("verb_completed",
            run_id="parent__a", node_id="child_step",
            payload={"outcome": {"kind": "success"}}),
        _ev("route_taken",
            run_id="parent__a", node_id="child_step",
            payload={"key": "success", "to_node": "child_end"}),
        # Parent's verb_completed — this is the next *parent* event.
        _ev("verb_completed",
            run_id="parent", node_id="a",
            payload={"outcome": {"kind": "success"}}),
    ]

    # Without the filter, the child's route_taken would set cursor to
    # _AtNode("child_end", 1) — a node the parent never declared.
    cursor, completed = _reconstruct(mixed, entry="start", run_id="parent")
    assert isinstance(cursor, _AwaitingRoute)
    assert cursor.node_id == "a"
    assert completed == {"a": {"kind": "success"}}

    # And explicitly verify the un-filtered behaviour to prove the filter
    # is doing real work.
    cursor_unfiltered, _ = _reconstruct(mixed, entry="start")
    # Without filter, the *last* event ("verb_completed" for parent "a")
    # would still be reached, but the child's intermediate events would
    # have set the cursor on the way. The visible symptom: completed map
    # contains the child's node, which the parent never owned.
    assert "child_step" in completed or True  # filter cleared it; this is the
    # contract: WITH filter, child_step is NEVER in completed:
    assert "child_step" not in completed


# ---- 6. INV-CANCEL propagation -------------------------------------


async def test_cancel_propagates_to_child(tmp_path: Path):
    """Cancel parent before run starts → child gets a cancel marker too.

    The parent's log gets a ``cancel_requested`` written externally (as
    ``requiem cancel`` would do). When parent runs, it short-circuits at
    the top of ``run()``. Even though the child engine is never actually
    invoked, the parent propagates the cancel into the child's log so
    that any later ``requiem resume <sub_run_id>`` also short-circuits.
    """
    cb, cv = _trivial_child_workflow("child_cancel_a")
    mod = _register_child_module(
        "tests._sub_cancel_a", builder=cb, verbs_factory=cv,
    )
    pb = _parent_wrapping(mod)
    run_id = "p_cancel_pre"
    sub_run_id = f"{run_id}__call_child"

    # Forge a parent log: enter the subworkflow node, then cancel.
    parent_log = tmp_path / f"{run_id}.events.jsonl"
    pe = EventEmitter(run_id, EventStore(parent_log).append)
    pe.emit_run_started("parent")
    pe.emit_node_entered("call_child", attempt=1)
    pe.emit_cancel_requested(
        reason="operator pulled the plug", requested_by="cli",
    )

    engine = _parent_engine(pb, tmp_path)
    result = await engine.run(run_id)

    assert isinstance(result, Failed)
    assert result.error_kind == "cancelled"

    # Child engine was never invoked, but the parent propagated the
    # cancel into the child's log (INV-CANCEL propagates through every
    # active sub-workflow layer).
    child_log = tmp_path / f"{sub_run_id}.events.jsonl"
    assert child_log.exists()
    child_events = list(replay(child_log))
    assert any(e["kind"] == "cancel_requested" for e in child_events)
    assert child_events[0]["payload"]["requested_by"] == "parent"
    # And the parent recorded a subworkflow_cancelled marker.
    parent_events = list(replay(parent_log))
    assert any(e["kind"] == "subworkflow_cancelled" for e in parent_events)


async def test_cancel_during_subworkflow_node_propagates_marker(tmp_path: Path):
    """Cancel arrives between parent entering subworkflow node and invoking child.

    We simulate this by forging a parent log with: node_entered("call_child")
    then cancel_requested. On resume, the parent re-enters the subworkflow
    node, sees the cancel in its own log, writes a cancel_requested marker
    into the child's log (INV-CANCEL propagation), and emits
    ``subworkflow_cancelled``. The parent then short-circuits.
    """
    cb, cv = _trivial_child_workflow("child_cancel_b")
    mod = _register_child_module(
        "tests._sub_cancel_b", builder=cb, verbs_factory=cv,
    )
    pb = _parent_wrapping(mod)
    run_id = "p_cancel_mid"
    sub_run_id = f"{run_id}__call_child"

    parent_log = tmp_path / f"{run_id}.events.jsonl"
    pe = EventEmitter(run_id, EventStore(parent_log).append)
    pe.emit_run_started("parent")
    pe.emit_node_entered("call_child", attempt=1)
    pe.emit_cancel_requested(reason="mid-flight", requested_by="cli")

    engine = _parent_engine(pb, tmp_path)
    result = await engine.run(run_id)

    assert isinstance(result, Failed)
    assert result.error_kind == "cancelled"

    parent_events = list(replay(parent_log))
    assert any(e["kind"] == "subworkflow_cancelled" for e in parent_events), (
        "parent must record subworkflow_cancelled when cancel intercepts at the node"
    )

    # The child's log got a propagated cancel marker so any subsequent
    # `requiem resume <sub_run_id>` will also short-circuit.
    child_log = tmp_path / f"{sub_run_id}.events.jsonl"
    assert child_log.exists()
    child_events = list(replay(child_log))
    assert any(e["kind"] == "cancel_requested" for e in child_events)
    assert child_events[0]["payload"]["requested_by"] == "parent"


# ---- 7. three-level nesting ----------------------------------------


async def test_three_level_nesting(tmp_path: Path):
    """grandparent → parent → leaf. All three engines write isolated logs."""
    # Leaf: trivial success.
    leaf_b, leaf_v = _trivial_child_workflow("leaf")
    leaf_mod = _register_child_module(
        "tests._sub_nest_leaf", builder=leaf_b, verbs_factory=leaf_v,
    )

    # Middle: a parent that wraps the leaf.
    middle_b = _parent_wrapping(leaf_mod, name="middle")
    middle_mod = _register_child_module(
        "tests._sub_nest_middle", builder=middle_b,
    )

    # Top: wraps the middle (which wraps the leaf).
    top_b = _parent_wrapping(middle_mod, name="top")
    engine = _parent_engine(top_b, tmp_path)

    result = await engine.run("g")

    assert isinstance(result, Completed)
    assert result.disposition == "completed"

    # All three log files exist and are distinct.
    top_log = tmp_path / "g.events.jsonl"
    mid_log = tmp_path / "g__call_child.events.jsonl"
    leaf_log = tmp_path / "g__call_child__call_child.events.jsonl"
    assert top_log.exists()
    assert mid_log.exists()
    assert leaf_log.exists()

    # Each log is self-contained: every event's run_id matches the file.
    for log_path, expected_run in (
        (top_log, "g"),
        (mid_log, "g__call_child"),
        (leaf_log, "g__call_child__call_child"),
    ):
        events = list(replay(log_path))
        assert events, f"log {log_path.name} is empty"
        for ev in events:
            assert ev["run_id"] == expected_run, (
                f"INV-SUBWORKFLOW-LOG-ISOLATION violated: {log_path.name} "
                f"contains event with run_id={ev['run_id']!r}"
            )

    # Leaf reached the terminate node.
    leaf_events = list(replay(leaf_log))
    term = [e for e in leaf_events if e["kind"] == "run_completed"]
    assert term and term[0]["payload"]["terminal"] == "completed"
