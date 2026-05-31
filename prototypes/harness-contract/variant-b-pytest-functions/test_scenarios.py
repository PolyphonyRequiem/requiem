"""Variant B — scenarios are pytest functions with shared fixtures.

Each function corresponds to one of the 6 mandated demos. Authoring
cost: ~5-15 lines per scenario. Authoring tool: any Python editor +
the type-hinted fixtures from `conftest.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _engine import (  # noqa: E402
    EventLog,
    FakeProvider,
    KillRequested,
    WorkflowEngine,
    gated_workflow,
    parent_with_subworkflow,
    tiny_three_node,
    transient_failure_workflow,
)


# 1) Tiny happy path.
def test_tiny_happy_path(fake_provider, make_engine, event_log):
    fake_provider.script("architect", {"plan": "ok"})
    engine = make_engine(tiny_three_node())

    terminal = engine.run(run_id="tiny-happy")

    assert terminal == "completed"
    assert [e.type for e in event_log if e.type == "NodeCompleted"] == [
        "NodeCompleted",
        "NodeCompleted",
        "NodeCompleted",
    ]
    assert fake_provider.call_count("architect") == 1


# 2) Transient failure — 2 retries then success.
def test_transient_failure_retries_twice(make_engine, event_log):
    workflow, attempts = transient_failure_workflow(fail_times=2)
    engine = make_engine(workflow)

    terminal = engine.run(run_id="flaky-retry")

    assert terminal == "completed"
    assert attempts["n"] == 3, "verb should run 3× (2 transient + 1 success)"
    retries = event_log.find("RetryAttempted", node="flaky")
    assert len(retries) == 2
    assert [r.payload["attempt"] for r in retries] == [1, 2]


# 3) Assert a SPECIFIC event was emitted.
def test_run_started_event_emitted(fake_provider, make_engine, event_log):
    fake_provider.script("architect", {"plan": "ok"})
    engine = make_engine(tiny_three_node())

    engine.run(run_id="event-assert-7", inputs={"work_item_id": 7})

    started = event_log.find("RunStarted")
    assert len(started) == 1
    assert started[0].run_id == "event-assert-7"
    assert started[0].payload["inputs"]["work_item_id"] == 7


# 4) INV-RESTART — kill mid-run, restart, assert resume.
def test_inv_restart_resumes_after_kill(fake_provider, make_engine, event_log, kill_after, tmp_path):
    fake_provider.script("architect", {"plan": "after-resume"})
    engine = make_engine(tiny_three_node(), chaos=kill_after("NodeCompleted", node="load"))

    with pytest.raises(KillRequested):
        engine.run(run_id="restart-demo")

    # Restart with a fresh engine, same log path, fresh provider script.
    log2 = EventLog(event_log.path)
    provider2 = FakeProvider().script("architect", {"plan": "after-resume"})
    engine2 = WorkflowEngine(workflow=tiny_three_node(), provider=provider2, event_log=log2)

    terminal = engine2.run(run_id="restart-demo")

    assert terminal == "completed"
    resumed = log2.find("RunResumed")
    assert len(resumed) == 1 and resumed[0].payload["after_node"] == "load"
    # We must not re-enter `load` after the resume.
    entered_after_resume = [e.node for e in log2 if e.type == "NodeEntered"]
    assert entered_after_resume.count("load") == 1
    assert "architect" in entered_after_resume


# 5) Human gate behaviour.
@pytest.mark.parametrize(
    "gate_value, expected_terminal",
    [("approve", "completed"), ("abort", "aborted")],
)
def test_human_gate_branches(fake_provider, make_engine, gate_answer, event_log,
                              gate_value, expected_terminal):
    fake_provider.script("architect", {"plan": "needs-review"})
    handler = gate_answer(gate={"value": gate_value, "reason": "operator-decision"})
    engine = make_engine(gated_workflow(), gate_handler=handler)

    terminal = engine.run(run_id=f"gate-{gate_value}")

    assert terminal == expected_terminal
    resolved = event_log.find("HumanGateResolved", chosen=gate_value)
    assert len(resolved) == 1
    assert resolved[0].payload["additional_input"]["reason"] == "operator-decision"


# 6) Sub-workflow scripting story.
def test_subworkflow_scripts_per_child(make_engine, event_log):
    parent_provider = FakeProvider().script("architect", {"plan": "parent-plan"})
    child_provider = FakeProvider().script("reviewer", {"verdict": "approve"})

    engine = make_engine(
        parent_with_subworkflow(),
        provider=parent_provider,
        subworkflow_provider_for=lambda name: child_provider if name == "child" else parent_provider,
    )

    terminal = engine.run(run_id="parent-with-child")

    assert terminal == "completed"
    assert parent_provider.call_count("architect") == 1
    assert child_provider.call_count("reviewer") == 1
    sub_done = event_log.find("SubworkflowCompleted", child="child", terminal="completed")
    assert len(sub_done) == 1
