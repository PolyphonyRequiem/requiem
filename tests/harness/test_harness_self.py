"""Self-tests for the harness — promote-time validation.

What these tests cover (one bullet per brief item):

* trivial 3-node workflow scenario passes end-to-end
* `assert_visited` correctly detects skipped nodes
* `assert_no_retry` correctly catches a retry
* `cancel_after_event` produces a Cancelled outcome
* resume from a truncated log → identical terminal
* `truncate_log` rejects invalid event indices

Each test constructs the smallest workflow that exercises the seam under
test; the larger end-to-end test against `code_review_demo` lives in
``tests/test_integration_code_review.py``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel

from requiem.agent import AgentSpec
from requiem.dsl import AgentRegistry, VerbRegistry, WorkflowBuilder
from requiem.harness import (
    Harness,
    scenario,
)
from requiem.kernel import Engine
from requiem.outcomes import RetryableFailure, Success
from requiem.toolbelt import Toolbelt


# ---- a tiny workflow used by every test in this file -----------------


class _Greeting(BaseModel):
    text: str
    sentiment: Literal["happy", "sad"]


GREETER = AgentSpec(
    name="greeter",
    charter="say hello",
    response_model=_Greeting,
)


def _build_trivial_workflow():
    return (
        WorkflowBuilder(
            "trivial",
            module="tests.harness.workflow_trivial",
            version="0.0",
        )
        .entry("start")
        .script("start", verb="noop")
            .edge("start", on="success", to="greet")
        .agent("greet", agent="greeter", prompt_verb="greet_prompt")
            .edge("greet", on="success", to="end")
        .terminate("end", disposition="completed")
        .build()
    )


def _build_verbs():
    verbs = VerbRegistry()

    @verbs.register("noop")
    def _noop(ctx):
        return Success(value={"ok": True})

    @verbs.register("greet_prompt")
    def _greet_prompt(ctx):
        return "say hello"

    return verbs


def _build_agents():
    reg = AgentRegistry()
    reg.register(GREETER)
    return reg


def build_engine(log_dir: Path, **_: object) -> Engine:
    """Harness factory contract: build a runnable Engine.

    `provider` and `toolbelt` are placeholders — the harness overrides
    them. `gate_handler` is None because the workflow has no human gate.
    """
    from requiem.agent import FakeProvider

    return Engine(
        workflow=_build_trivial_workflow(),
        verbs=_build_verbs(),
        agents=_build_agents(),
        provider=FakeProvider(scripts={}),
        toolbelt=Toolbelt(git=None, files=None, gh=None),  # type: ignore[arg-type]
        log_dir=log_dir,
    )


# Register the in-module workflow under a stable import path so
# Scenario.workflow can name it. We add it to sys.modules so
# `importlib.import_module` finds it without a real package.
def _register_module_under(name: str) -> None:
    import sys
    import types

    mod = types.ModuleType(name)
    mod.build_engine = build_engine  # type: ignore[attr-defined]
    sys.modules[name] = mod


_register_module_under("tests.harness.workflow_trivial")


# ---- a slightly bigger workflow that retries + uses tool seam --------


class _Echo(BaseModel):
    body: str


ECHOER = AgentSpec(name="echoer", charter="echo", response_model=_Echo)


def _build_retry_workflow():
    return (
        WorkflowBuilder(
            "retrying",
            module="tests.harness.workflow_retry",
            version="0.0",
        )
        .entry("start")
        .script("start", verb="noop")
            .edge("start", on="success", to="read")
        .script("read", verb="read_file")
            .edge("read", on="success", to="lint")
            .edge("read", on="permanent_failure", to="fail")
        .script("lint", verb="flaky_lint", retry_max=3)
            .edge("lint", on="success", to="end")
            .edge("lint", on="retry_exhausted", to="fail")
        .terminate("end", disposition="completed")
        .terminate("fail", disposition="failed")
        .build()
    )


def _build_retry_verbs(target_path: Path):
    from requiem.toolbelt import FileMissing, FileRead

    verbs = VerbRegistry()

    @verbs.register("noop")
    def _noop(ctx):
        return Success(value={"ok": True})

    @verbs.register("read_file")
    def _read(ctx):
        outcome = ctx.toolbelt.files.read_text(target_path)
        match outcome:
            case FileRead(content=text):
                return Success(value={"len": len(text)})
            case FileMissing():
                from requiem.outcomes import PermanentFailure

                return PermanentFailure(error_kind="missing", message="no file")

    @verbs.register("flaky_lint")
    def _flaky(ctx):
        if ctx.attempt < 2:
            return RetryableFailure(
                retry_key=f"{ctx.run_id}:lint",
                error_kind="lint.transient",
                message="OOM",
                attempt=ctx.attempt,
            )
        return Success(value={"attempts_used": ctx.attempt})

    return verbs


def build_retry_engine(log_dir: Path, *, target_path: Path) -> Engine:
    from requiem.agent import FakeProvider

    return Engine(
        workflow=_build_retry_workflow(),
        verbs=_build_retry_verbs(target_path),
        agents=AgentRegistry(),
        provider=FakeProvider(scripts={}),
        toolbelt=Toolbelt(git=None, files=None, gh=None),  # type: ignore[arg-type]
        log_dir=log_dir,
    )


def _register_retry_module():
    import sys
    import types

    mod = types.ModuleType("tests.harness.workflow_retry")
    mod.build_engine = build_retry_engine  # type: ignore[attr-defined]
    sys.modules["tests.harness.workflow_retry"] = mod


_register_retry_module()


# ---- a workflow with one cancellable agent (for cancel_after_event) --


class _Long(BaseModel):
    result: str


LONG = AgentSpec(name="long", charter="long", response_model=_Long)


def _build_long_workflow():
    return (
        WorkflowBuilder(
            "longish",
            module="tests.harness.workflow_long",
            version="0.0",
        )
        .entry("start")
        .script("start", verb="noop")
            .edge("start", on="success", to="long")
        .agent("long", agent="long", prompt_verb="prompt", retry_max=3)
            .edge("long", on="success", to="end")
            .edge("long", on="retry_exhausted", to="fail")
        .terminate("end", disposition="completed")
        .terminate("fail", disposition="failed")
        .build()
    )


def build_long_engine(log_dir: Path, **_: object) -> Engine:
    from requiem.agent import FakeProvider

    verbs = VerbRegistry()

    @verbs.register("noop")
    def _noop(ctx):
        return Success(value={"ok": True})

    @verbs.register("prompt")
    def _prompt(ctx):
        return "go"

    agents = AgentRegistry()
    agents.register(LONG)
    return Engine(
        workflow=_build_long_workflow(),
        verbs=verbs,
        agents=agents,
        provider=FakeProvider(scripts={}),
        toolbelt=Toolbelt(git=None, files=None, gh=None),  # type: ignore[arg-type]
        log_dir=log_dir,
    )


def _register_long_module():
    import sys
    import types

    mod = types.ModuleType("tests.harness.workflow_long")
    mod.build_engine = build_long_engine  # type: ignore[attr-defined]
    sys.modules["tests.harness.workflow_long"] = mod


_register_long_module()


# ---- the tests --------------------------------------------------------


def test_trivial_three_node_scenario_passes(harness: Harness):
    scn = scenario(
        workflow="tests.harness.workflow_trivial",
        agent_outputs={"greeter": {"text": "hi", "sentiment": "happy"}},
        expected_terminal="completed",
    )
    result = harness.run(scn)
    result.assert_completed(disposition="completed", final_node="end")
    result.assert_visited(["start", "greet", "end"], exact=True)
    result.assert_no_retry()


def test_assert_visited_detects_skipped_nodes(harness: Harness):
    scn = scenario(
        workflow="tests.harness.workflow_trivial",
        agent_outputs={"greeter": {"text": "hi", "sentiment": "happy"}},
    )
    result = harness.run(scn)
    # `intermediate_node` was never in the workflow at all.
    with pytest.raises(AssertionError, match="ordered subsequence"):
        result.assert_visited(["start", "intermediate_node", "end"])


def test_assert_no_retry_catches_a_retry(harness: Harness, tmp_path: Path):
    target = tmp_path / "snippet.py"
    target.write_text("print('hi')\n", encoding="utf-8")
    scn = scenario(
        workflow="tests.harness.workflow_retry",
        extra_engine_kwargs={"target_path": target},
        tool_outputs={
            ("files", "read_text", str(target)): "print('hi')\n",
        },
    )
    result = harness.run(scn)
    result.assert_completed()
    assert result.retries == 1, result.retries
    with pytest.raises(AssertionError, match="expected zero retries"):
        result.assert_no_retry()


def test_cancel_after_event_produces_cancelled(harness: Harness):
    scn = scenario(
        workflow="tests.harness.workflow_long",
        # Script enough retries that the cancel must short-circuit them.
        agent_outputs={
            "long": [
                {"result": "first"},
                {"result": "second"},
                {"result": "third"},
                {"result": "fourth"},
            ],
        },
    )
    # The trivial workflow emits ~4 events before the agent call returns;
    # cancelling after 3 events guarantees the cancel fires before the
    # second loop tick, so the engine returns `Failed("cancelled")`.
    result = harness.run(scn, cancel_after_event=3)
    result.assert_cancelled()
    result.assert_short_circuited()


def test_resume_after_truncation_identical_terminal(harness: Harness, tmp_path: Path):
    target = tmp_path / "snippet.py"
    target.write_text("print('hi')\n", encoding="utf-8")
    scn = scenario(
        workflow="tests.harness.workflow_retry",
        extra_engine_kwargs={"target_path": target},
        tool_outputs={
            ("files", "read_text", str(target)): "print('hi')\n",
        },
        run_id="resume-me",
    )
    full = harness.run(scn)
    full.assert_completed()
    # Truncate the log to just after `read` completes — leaving `lint`
    # and `end` for the resume to discover.
    keep_until = -1
    for e in full.events:
        if e.get("kind") == "verb_completed" and e.get("node_id") == "read":
            keep_until = int(e["event_id"])
            break
    assert keep_until >= 0, "couldn't find read.verb_completed in event log"

    truncated = harness.truncate_log("resume-me", after_event=keep_until)
    resumed = harness.resume(scn, truncated)
    resumed.assert_completed()
    resumed.assert_terminal_state_matches(full)


def test_truncate_log_rejects_invalid_indices(harness: Harness):
    scn = scenario(
        workflow="tests.harness.workflow_trivial",
        agent_outputs={"greeter": {"text": "hi", "sentiment": "happy"}},
        run_id="trunc-me",
    )
    result = harness.run(scn)
    max_id = max(int(e["event_id"]) for e in result.events)

    with pytest.raises(ValueError, match="exceeds max event_id"):
        harness.truncate_log("trunc-me", after_event=max_id + 5)

    with pytest.raises(ValueError, match=">= 0"):
        harness.truncate_log("trunc-me", after_event=-1)

    with pytest.raises(ValueError, match="no log for run_id"):
        harness.truncate_log("never-ran", after_event=0)


# ---- bonus: assertion helpers as free functions also work -----------


def test_free_function_assertions(harness: Harness):
    from requiem.harness import (
        assert_completed,
        assert_no_retry,
        assert_visited_nodes,
    )

    scn = scenario(
        workflow="tests.harness.workflow_trivial",
        agent_outputs={"greeter": {"text": "hi", "sentiment": "happy"}},
    )
    r = harness.run(scn)
    assert_completed(r)
    assert_visited_nodes(r, ["start", "greet", "end"])
    assert_no_retry(r)


def test_scripted_agent_fixture_normalizes_inputs(scripted_agent):
    a = scripted_agent({"x": {"k": 1}, "y": [{"k": 2}, {"k": 3}]})
    assert a.scripts == {"x": [{"k": 1}], "y": [{"k": 2}, {"k": 3}]}


def test_scripted_toolbelt_fixture_lifts_strings(scripted_toolbelt, tmp_path: Path):
    p = tmp_path / "hello.txt"
    tb = scripted_toolbelt({("files", "read_text", str(p)): "hello"})
    from requiem.toolbelt import FileRead

    outcome = tb.files.read_text(p)
    assert isinstance(outcome, FileRead)
    assert outcome.content == "hello"
