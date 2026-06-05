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
