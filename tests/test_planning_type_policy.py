"""Process-config tier policy wired into the planning workflow (§9 #1).

The decompose-vs-leaf tier decision is *config-driven*, not purely LLM-driven:
``process.yaml``'s ``implementable_types`` / ``decomposable_types`` are
authoritative over the planner's own ``decomposable`` flag.

* implementable type → forced leaf (config overrides a planner that proposed
  children; the discarded count is recorded as a breadcrumb);
* decomposable type the planner left as a leaf → fail closed to
  ``type_policy_gate``;
* a configured policy with no ``work_item_type`` to classify → fail closed;
* empty tier sets (the default) → planner's decision stands unchanged;
* the effective config snapshot is threaded into recursive child inputs so a
  child tiers with the SAME config the parent started with (INV-RESTART).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from requiem.agent import FakeProvider
from requiem.clients.twig import TwigItem
from requiem.kernel import Completed
from requiem.process_config import ProcessConfig
from requiem.workflows.planning import (
    FakeTwigClient,
    build_engine,
    completed_from_log,
    project_plan_result,
)

ROOT_ID = 4242


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs"
    d.mkdir()
    return d


def _proceed_handler(node_id, prompt, options):
    return "proceed" if "proceed" in options else options[0]


_proceed_handler.__requiem_auto__ = True  # type: ignore[attr-defined]


def _abort_handler(node_id, prompt, options):
    return "abort" if "abort" in options else options[-1]


_abort_handler.__requiem_auto__ = True  # type: ignore[attr-defined]


def _twig(item_type_by_id: dict[int, str]) -> FakeTwigClient:
    items = {
        i: TwigItem(
            id=i,
            title=f"Item {i}",
            state="New",
            area_path="Polyphony\\Engine",
            work_item_type=t,
            parent_id=None,
            raw={},
        )
        for i, t in item_type_by_id.items()
    }
    return FakeTwigClient(items=items)


def _leaf(summary: str = "leaf") -> dict:
    return {
        "summary": summary,
        "decomposable": False,
        "children": [],
        "estimated_complexity": "small",
        "rationale": "atomic",
    }


def _decomp(*titles: str, summary: str = "decomposable") -> dict:
    return {
        "summary": summary,
        "decomposable": True,
        "children": [
            {"title": t, "description": f"{t} desc", "work_item_type": "Task"}
            for t in titles
        ],
        "estimated_complexity": "medium",
        "rationale": "separable",
    }


def _approve() -> dict:
    return {"verdict": "approve", "feedback": "ok"}


async def test_implementable_type_forces_leaf_over_planner(log_dir: Path):
    """Config says the root type is implementable → leaf, even though the
    planner proposed a decomposition. No children are spawned; the discard
    is recorded as a breadcrumb."""
    cfg = ProcessConfig(implementable_types=frozenset({"User Story"}))
    provider = FakeProvider(
        scripts={
            # Planner defies policy and proposes 2 children; config wins.
            "planner": [_decomp("A", "B")],
            "plan_reviewer": [_approve()],
        }
    )
    twig = _twig({ROOT_ID: "User Story"})
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
    )
    result = await engine.run("impl")
    assert isinstance(result, Completed), result
    assert result.final_node == "end"

    plan = project_plan_result(completed_from_log(engine.log_path("impl")))
    assert plan is not None
    assert plan.decomposable is False
    assert plan.children == []
    # No child sub-workflows were spawned.
    assert list(log_dir.glob("impl__child_*.events.jsonl")) == []


async def test_decomposable_type_planner_leaf_gates(log_dir: Path):
    """Config requires decomposition but the planner produced a leaf →
    fail closed to type_policy_gate. With the abort handler the run fails."""
    cfg = ProcessConfig(decomposable_types=frozenset({"Feature"}))
    provider = FakeProvider(
        scripts={"planner": [_leaf("oops")], "plan_reviewer": [_approve()]}
    )
    twig = _twig({ROOT_ID: "Feature"})
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
        gate_handler=_abort_handler,
    )
    result = await engine.run("dec_abort")
    assert isinstance(result, Completed), result
    assert result.final_node == "fail_end"


async def test_decomposable_type_planner_leaf_gate_proceed(log_dir: Path):
    """Same conflict, but the operator proceeds → recorded needs-human."""
    cfg = ProcessConfig(decomposable_types=frozenset({"Feature"}))
    provider = FakeProvider(
        scripts={"planner": [_leaf("oops")], "plan_reviewer": [_approve()]}
    )
    twig = _twig({ROOT_ID: "Feature"})
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
        gate_handler=_proceed_handler,
    )
    result = await engine.run("dec_proceed")
    assert isinstance(result, Completed), result
    assert result.final_node == "end_needs_human"


async def test_unspecified_type_preserves_planner_decision(log_dir: Path):
    """Empty tier sets → the planner's decomposition stands (legacy behavior)."""
    cfg = ProcessConfig()  # no tier policy
    child_ids = [ROOT_ID * 100 + i for i in (1, 2)]
    provider = FakeProvider(
        scripts={
            "planner": [_decomp("A", "B"), _leaf("a"), _leaf("b")],
            "plan_reviewer": [_approve(), _approve(), _approve()],
        }
    )
    twig = _twig({ROOT_ID: "User Story", child_ids[0]: "Task", child_ids[1]: "Task"})
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
    )
    result = await engine.run("unspec")
    assert isinstance(result, Completed), result
    plan = project_plan_result(completed_from_log(engine.log_path("unspec")))
    assert plan is not None
    assert plan.decomposable is True
    assert {c.item_id for c in plan.children} == set(child_ids)


async def test_missing_work_item_type_with_policy_gates(log_dir: Path):
    """A configured policy with no type to classify fails closed."""
    cfg = ProcessConfig(implementable_types=frozenset({"Task"}))
    provider = FakeProvider(
        scripts={"planner": [_leaf("x")], "plan_reviewer": [_approve()]}
    )
    twig = _twig({ROOT_ID: ""})  # blank work_item_type
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
        gate_handler=_abort_handler,
    )
    result = await engine.run("notype")
    assert isinstance(result, Completed), result
    assert result.final_node == "fail_end"


async def test_alias_normalized_type_forces_leaf(log_dir: Path):
    """An aliased type (Bug -> Task) classifies through implementable_types."""
    cfg = ProcessConfig(
        type_aliases={"Bug": "Task"},
        implementable_types=frozenset({"Task"}),
    )
    provider = FakeProvider(
        scripts={"planner": [_decomp("A")], "plan_reviewer": [_approve()]}
    )
    twig = _twig({ROOT_ID: "Bug"})
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
    )
    result = await engine.run("alias")
    assert isinstance(result, Completed), result
    plan = project_plan_result(completed_from_log(engine.log_path("alias")))
    assert plan is not None
    assert plan.decomposable is False


async def test_config_snapshot_threads_into_children(log_dir: Path):
    """A decomposable root whose children are an implementable type: the
    config snapshot reaches the child sub-workflows, which are forced to
    leaves — proving restart-safe config propagation through child inputs."""
    cfg = ProcessConfig(
        decomposable_types=frozenset({"User Story"}),
        implementable_types=frozenset({"Task"}),
    )
    child_ids = [ROOT_ID * 100 + i for i in (1, 2)]
    provider = FakeProvider(
        scripts={
            # Root decomposes (policy-compliant). Each child planner defies
            # policy by proposing a sub-decomposition; the threaded config
            # forces every child to a leaf, so those proposals are discarded.
            "planner": [_decomp("A", "B"), _decomp("a1"), _decomp("b1")],
            "plan_reviewer": [_approve(), _approve(), _approve()],
        }
    )
    twig = _twig(
        {ROOT_ID: "User Story", child_ids[0]: "Task", child_ids[1]: "Task"}
    )
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
    )
    result = await engine.run("thread")
    assert isinstance(result, Completed), result
    plan = project_plan_result(completed_from_log(engine.log_path("thread")))
    assert plan is not None
    assert plan.decomposable is True
    assert len(plan.children) == 2
    # Both children were forced to leaves by the threaded policy.
    assert all(c.decomposable is False for c in plan.children)
    assert all(c.children == [] for c in plan.children)


# ---- ADR-0025 Gap A: implementable types skip planner+reviewer entirely ----
#
# When the type policy already says "this is an implementable leaf, do
# not decompose," there is no useful work for the planner or reviewer to
# do. The pre-Gap-A flow ran them anyway and then overrode the verdict,
# paying for two LLM calls to produce a decision the config already
# made — AND surfacing reviewer escalations on leaves the reviewer has
# no authority to refine. The 2026-06-17 SKU-fallback dogfood failed
# exactly this way: 5/7 Task leaves planned cleanly, 2 escalated as
# "vague," cascade killed the whole tree, commit_plan never fired.
#
# Gap A's short-circuit: at the policy classifier stage, if
# tier_for_type == "implementable", route directly to record_plan with
# a synthesised leaf PlanResult. ZERO LLM calls. The breadcrumb
# inspected_artifact is `policy:implementable/<work_item_type>` to make
# the policy-driven nature legible in the event log.


async def test_implementable_type_skips_planner_and_reviewer_entirely(
    log_dir: Path,
):
    """ADR-0025 Gap A load-bearing pin. When the root's type is in
    implementable_types, NO planner call AND NO reviewer call is made;
    the plan goes straight to recorded-as-leaf."""
    cfg = ProcessConfig(implementable_types=frozenset({"Task"}))
    # Empty scripts on both agents — if either gets called we'll see
    # fake.exhausted PermanentFailure (which surfaces as a non-success
    # outcome and our assertion below will catch it loud).
    provider = FakeProvider(scripts={"planner": [], "plan_reviewer": []})
    twig = _twig({ROOT_ID: "Task"})
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
    )
    result = await engine.run("gap-a-impl")
    assert isinstance(result, Completed), result
    assert result.final_node == "end", (
        f"implementable-type root should reach 'end' without LLM calls; "
        f"got final_node={result.final_node!r}"
    )

    # Hard assertion: ZERO LLM calls of any kind.
    assert provider.calls == [], (
        f"implementable type should skip planner+reviewer entirely; "
        f"got calls: {provider.calls}"
    )

    # The plan record exists and is shaped correctly.
    plan = project_plan_result(completed_from_log(engine.log_path("gap-a-impl")))
    assert plan is not None
    assert plan.decomposable is False
    assert plan.children == []
    # The synthesised summary echoes the work-item title from twig.
    assert plan.summary == f"Item {ROOT_ID}"


async def test_implementable_type_skip_records_policy_artifact(
    log_dir: Path,
):
    """The short-circuit must leave a `policy:implementable/<type>`
    breadcrumb in inspected_artifacts so an operator reading the event
    log can tell this leaf was policy-driven, not planner-driven.
    Without this breadcrumb the audit story is broken — you can't
    distinguish 'planner said leaf' from 'policy forced leaf'."""
    cfg = ProcessConfig(implementable_types=frozenset({"Task"}))
    provider = FakeProvider(scripts={"planner": [], "plan_reviewer": []})
    twig = _twig({ROOT_ID: "Task"})
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
    )
    result = await engine.run("gap-a-artifact")
    assert isinstance(result, Completed)
    assert result.final_node == "end"

    # Walk the event log to find the policy:implementable artifact.
    from requiem.persistence import replay
    log_path = engine.log_path("gap-a-artifact")
    found_policy_artifact = False
    found_agent_artifact = False
    for ev in replay(log_path):
        if ev.get("kind") != "verb_completed":
            continue
        oc = (ev.get("payload") or {}).get("outcome", {}) or {}
        artifacts = oc.get("inspected_artifacts") or []
        for a in artifacts:
            if a.startswith("policy:implementable/"):
                found_policy_artifact = True
                assert "Task" in a, (
                    f"breadcrumb should name the work_item_type: {a!r}"
                )
            if a.startswith("agent:planner/") or a.startswith("agent:plan_reviewer/"):
                found_agent_artifact = True
    assert found_policy_artifact, (
        "implementable-type short-circuit should leave a "
        "policy:implementable/<type> breadcrumb in inspected_artifacts"
    )
    assert not found_agent_artifact, (
        "no agent artifacts should appear; we skipped planner+reviewer"
    )


async def test_decomposable_type_still_calls_planner_no_regression(
    log_dir: Path,
):
    """ADR-0025 Gap A guard against the short-circuit accidentally
    swallowing the decomposable path. When the root is a decomposable
    type (e.g. Scenario), the planner+reviewer pair DOES run exactly as
    before — the short-circuit is implementable-only."""
    cfg = ProcessConfig(
        decomposable_types=frozenset({"Scenario"}),
        implementable_types=frozenset({"Task"}),
    )
    provider = FakeProvider(
        scripts={
            "planner": [_decomp("childA", "childB")],
            "plan_reviewer": [_approve()],
        }
    )
    twig = _twig({ROOT_ID: "Scenario", ROOT_ID * 100 + 1: "Task", ROOT_ID * 100 + 2: "Task"})
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
    )
    result = await engine.run("gap-a-decomp")
    assert isinstance(result, Completed), result
    assert result.final_node == "end"

    # Both planner AND reviewer were called for the ROOT (Scenario).
    # Each child (Task) skips planner+reviewer per the same short-circuit,
    # so the call count is exactly 2 (one planner + one reviewer for root).
    agent_names = [c["agent"] for c in provider.calls]
    assert "planner" in agent_names, (
        f"decomposable root must call planner; got: {agent_names}"
    )
    assert "plan_reviewer" in agent_names, (
        f"decomposable root must call reviewer; got: {agent_names}"
    )
    # Exactly one of each — no recursion-driven LLM calls for the Task
    # children (they short-circuit per Gap A).
    assert agent_names.count("planner") == 1, agent_names
    assert agent_names.count("plan_reviewer") == 1, agent_names

    plan = project_plan_result(completed_from_log(engine.log_path("gap-a-decomp")))
    assert plan is not None
    assert plan.decomposable is True
    assert len(plan.children) == 2
    assert all(c.decomposable is False for c in plan.children), (
        "Task children must be policy-driven leaves"
    )


async def test_no_tier_policy_still_calls_planner_no_regression(
    log_dir: Path,
):
    """When no tier policy is configured (the polyphony default), the
    short-circuit must not fire — planner runs as before for ALL types.
    Pin against the short-circuit accidentally activating on default config."""
    cfg = ProcessConfig()  # no implementable_types, no decomposable_types
    provider = FakeProvider(
        scripts={
            "planner": [_leaf("atomic")],
            "plan_reviewer": [_approve()],
        }
    )
    twig = _twig({ROOT_ID: "Task"})  # Task root with NO policy
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
    )
    result = await engine.run("gap-a-no-policy")
    assert isinstance(result, Completed)
    assert result.final_node == "end"

    # planner + reviewer were both called (no short-circuit without policy).
    agent_names = [c["agent"] for c in provider.calls]
    assert "planner" in agent_names
    assert "plan_reviewer" in agent_names
