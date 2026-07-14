"""Planning workflow tests — Fauré (Phase C).

Covers the topology requirements in the Phase C brief:

* happy-path leaf (planner returns decomposable=False, reviewer approves first)
* decomposable path with 3 children (no recursion in v0 — flat only)
* revise loop: reviewer revises once, planner re-runs, approved on iter 2
* revise loop exhaustion: 3 revisions → escalate → human gate
* `BadOutput`: planner returns invalid output → routes to needs_human gate
* `max_depth` exceeded → needs_human gate
* INV-RESTART: kill mid-plan, resume to same terminal state.

The tests construct a `FakeProvider` per scenario so each scripts both
the planner and the reviewer deterministically. They use the workflow's
own `FakeTwigClient` to avoid the real subprocess seam.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from requiem.agent import FakeProvider
from requiem.clients.twig import TwigItem
from requiem.kernel import Completed, Failed
from requiem.outcomes import BadOutput, RetryableFailure
from requiem.plan_lineage import format_commit_marker
from requiem.persistence import replay
from requiem.workflows.planning import (
    FakeTwigClient,
    ITER_CAP,
    PlanResult,
    VALID_ESCALATION_POLICIES,
    _pin_validation_errors,
    build_engine,
    build_workflow,
    completed_from_log,
    make_escalation_policy_handler,
    project_plan_result,
    verdict_card,
)


# ---- fixtures ----------------------------------------------------------


ITEM_ID = 12345


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs"
    d.mkdir()
    return d


def _twig() -> FakeTwigClient:
    return FakeTwigClient(
        items={
            ITEM_ID: TwigItem(
                id=ITEM_ID,
                title="Refactor outcome dispatch",
                state="Active",
                area_path="Polyphony\\Engine",
                work_item_type="User Story",
                parent_id=None,
                raw={},
            ),
        }
    )


def _proceed_handler(node_id: str, prompt: str, options: tuple[str, ...]) -> str:
    return "proceed" if "proceed" in options else options[0]


_proceed_handler.__requiem_auto__ = True  # type: ignore[attr-defined]


def _abort_handler(node_id: str, prompt: str, options: tuple[str, ...]) -> str:
    return "abort" if "abort" in options else options[-1]


_abort_handler.__requiem_auto__ = True  # type: ignore[attr-defined]


def _leaf_planner_output() -> dict:
    return {
        "summary": "Single atomic refactor; no children.",
        "decomposable": False,
        "children": [],
        "estimated_complexity": "small",
        "rationale": "The change is localised to one module.",
    }


def _planner_token_exhaustion(
    retry_key: str = "planner#1",
) -> RetryableFailure:
    error = (
        "network_timeout: session input tokens (90578) exceeded "
        "max_cumulative_input_tokens=80000"
    )
    return RetryableFailure(
        retry_key=retry_key,
        error_kind="network_timeout",
        message=error,
        receipts=({
            "kind": "llm_call",
            "model": "claude-sonnet-5",
            "input_tokens": 90578,
            "output_tokens": 152,
            "latency_ms": 240096,
            "request_id": "run-47",
            "error": error,
        },),
    )


def _reviewer_request_body_timeout(
    retry_key: str = "reviewer#1",
) -> RetryableFailure:
    error = (
        "copilot session error: CAPIError: 408 "
        '{"error":{"message":"Timed out reading request body. Try again, '
        'or use a smaller request size.","code":"user_request_timeout"}}'
    )
    return RetryableFailure(
        retry_key=retry_key,
        error_kind="provider_unavailable",
        message=error,
        receipts=({
            "kind": "llm_call",
            "model": "claude-sonnet-5",
            "input_tokens": 37732,
            "output_tokens": 342,
            "latency_ms": 378164,
            "request_id": "run-48b-reviewer",
            "error": error,
        },),
    )


def _decomposable_planner_output() -> dict:
    return {
        "summary": "Three sub-tasks separable along clear seams.",
        "decomposable": True,
        "children": [
            {
                "title": "Define ErrorKind enum",
                "description": "Lift the closed taxonomy from ADR 0004.",
                "work_item_type": "Task",
            },
            {
                "title": "Migrate verbs to ErrorKind",
                "description": "Update each verb to construct outcomes with the enum.",
                "work_item_type": "Task",
            },
            {
                "title": "Update tests",
                "description": "Tighten outcome assertions to use the enum.",
                "work_item_type": "Task",
            },
        ],
        "estimated_complexity": "medium",
        "rationale": "Three independent, distinct sub-tasks.",
    }


# ---- happy paths -------------------------------------------------------


async def test_happy_path_leaf(log_dir: Path):
    """Planner returns leaf; reviewer approves on iteration 1."""
    provider = FakeProvider(
        scripts={
            "planner": [_leaf_planner_output()],
            "plan_reviewer": [{"verdict": "approve", "feedback": "LGTM."}],
        }
    )
    engine = build_engine(
        log_dir,
        item_id=ITEM_ID,
        twig=_twig(),
        provider=provider,
    )
    result = await engine.run("leaf")
    assert isinstance(result, Completed), result
    assert result.final_node == "end"

    completed = completed_from_log(engine.log_path("leaf"))
    plan = project_plan_result(completed)
    assert isinstance(plan, PlanResult)
    assert plan.item_id == ITEM_ID
    assert plan.decomposable is False
    assert plan.review_iterations == 1
    assert plan.final_verdict == "approved"
    assert plan.children == []  # leaf

    # Sidecar artifact lives where the verdict card promises it does.
    md = log_dir / "leaf.plan.md"
    assert md.exists(), f"plan.md missing; dir={list(log_dir.iterdir())}"
    body = md.read_text(encoding="utf-8")
    assert f"AB#{ITEM_ID}" in body
    assert "Refactor outcome dispatch" in body
    assert "approved" in body.lower()

    # Verdict card renders cleanly.
    card = verdict_card(completed)
    assert card is not None
    assert f"AB#{ITEM_ID}" in card
    assert "leaf" in card
    assert "Refactor outcome dispatch" in card


async def test_fetch_item_inventories_scenario_parentage_and_lineage(
    log_dir: Path,
):
    twig = _twig()
    direct_id = 7001
    nested_id = 7002
    twig.items[direct_id] = TwigItem(
        id=direct_id,
        title="Existing direct child",
        state="Active",
        area_path="Polyphony\\Engine",
        work_item_type="Task",
        parent_id=ITEM_ID,
        raw={
            "description": format_commit_marker(
                f"plan-{ITEM_ID}-prior", ITEM_ID * 100 + 1
            )
        },
    )
    twig.items[nested_id] = TwigItem(
        id=nested_id,
        title="Unmarked nested child",
        state="Active",
        area_path="Polyphony\\Engine",
        work_item_type="Task",
        parent_id=direct_id,
        raw={},
    )
    provider = FakeProvider(
        scripts={
            "planner": [_leaf_planner_output()],
            "plan_reviewer": [{"verdict": "approve", "feedback": "LGTM."}],
        }
    )
    engine = build_engine(
        log_dir, item_id=ITEM_ID, twig=twig, provider=provider
    )

    await engine.run("inventory")

    completed = completed_from_log(engine.log_path("inventory"))
    fetched = completed["fetch_item"]["value"]
    assert fetched["existing_work_complete"] is True
    by_id = {
        entry["item_id"]: entry for entry in fetched["existing_work"]
    }
    assert by_id[direct_id]["parent_id"] == ITEM_ID
    assert by_id[direct_id]["same_scenario_lineage"] is True
    assert by_id[nested_id]["path"] == [ITEM_ID, direct_id, nested_id]
    assert by_id[nested_id]["same_scenario_lineage"] is False


async def test_invalid_overlap_pin_is_revised_into_aligned_approved_tree(
    log_dir: Path,
):
    existing_id = 7777
    twig = _twig()
    twig.items[existing_id] = TwigItem(
        id=existing_id,
        title="Existing observability",
        state="Active",
        area_path="Polyphony\\Engine",
        work_item_type="Task",
        parent_id=ITEM_ID,
        raw={
            "description": format_commit_marker(
                f"plan-{ITEM_ID}-prior", ITEM_ID * 100 + 1
            )
        },
    )
    invalid = {
        **_decomposable_planner_output(),
        "children": [
            {
                "title": "Wrong title",
                "description": "overlap",
                "work_item_type": "Task",
                "item_id": existing_id,
            }
        ],
    }
    reconciled = {
        **_decomposable_planner_output(),
        "children": [
            {
                "title": "Existing observability",
                "description": "overlap",
                "work_item_type": "Task",
                "item_id": existing_id,
            }
        ],
    }
    provider = FakeProvider(
        scripts={
            "planner": [invalid, reconciled, _leaf_planner_output()],
            "plan_reviewer": [
                {"verdict": "approve", "feedback": "Exact reuse is sound."},
                {"verdict": "approve", "feedback": "Atomic child."},
            ],
        }
    )
    engine = build_engine(
        log_dir, item_id=ITEM_ID, twig=twig, provider=provider
    )

    result = await engine.run("pin-reconcile")

    assert isinstance(result, Completed), result
    assert result.final_node == "end"
    completed = completed_from_log(engine.log_path("pin-reconcile"))
    assert completed["pin_validator_1"]["error_kind"] == "pin_reconcile"
    assert completed["pin_validator_2"]["kind"] == "success"
    tree = json.loads(
        (log_dir / "pin-reconcile.plan.tree.json").read_text(encoding="utf-8")
    )
    assert tree["verdict"] == "approved"
    assert tree["proposals"][0]["item_id"] == existing_id
    assert tree["children"][0]["item_id"] == existing_id
    assert tree["children"][0]["final_verdict"] == "approved"


async def test_nested_omitted_exact_durable_pin_is_revised_before_review(
    log_dir: Path,
):
    deliverable_id = 7001
    task_ids = (7101, 7102, 7103)
    task_titles = ("Existing task one", "Existing task two", "Existing task three")
    twig = _twig()
    twig.items[deliverable_id] = TwigItem(
        id=deliverable_id,
        title="Existing deliverable",
        state="Active",
        area_path="Polyphony\\Engine",
        work_item_type="Feature",
        parent_id=ITEM_ID,
        raw={
            "description": format_commit_marker(
                f"plan-{ITEM_ID}-prior", ITEM_ID * 100 + 1
            )
        },
    )
    for index, (task_id, title) in enumerate(
        zip(task_ids, task_titles, strict=True),
        start=1,
    ):
        twig.items[task_id] = TwigItem(
            id=task_id,
            title=title,
            state="Active",
            area_path="Polyphony\\Engine",
            work_item_type="Task",
            parent_id=deliverable_id,
            raw={
                "description": format_commit_marker(
                    f"plan-{ITEM_ID}-prior",
                    deliverable_id * 100 + index,
                )
            },
        )

    root_plan = {
        "summary": "Reuse the existing deliverable.",
        "decomposable": True,
        "children": [
            {
                "title": "Existing deliverable",
                "description": "Existing durable work.",
                "work_item_type": "Feature",
                "item_id": deliverable_id,
            }
        ],
        "estimated_complexity": "medium",
        "rationale": "The exact durable child already exists.",
    }

    def nested_plan(*, include_all_pins: bool) -> dict:
        children = [
            {
                "title": title,
                "description": f"Reuse {title}.",
                "work_item_type": "Task",
                "item_id": task_id,
            }
            for task_id, title in zip(task_ids, task_titles, strict=True)
        ]
        if not include_all_pins:
            children[1].pop("item_id")
        return {
            "summary": "Reuse all three existing tasks.",
            "decomposable": True,
            "children": children,
            "estimated_complexity": "medium",
            "rationale": "Each task is an exact durable child.",
        }

    provider = FakeProvider(
        scripts={
            "planner": [
                root_plan,
                nested_plan(include_all_pins=False),
                nested_plan(include_all_pins=True),
                _leaf_planner_output(),
                _leaf_planner_output(),
                _leaf_planner_output(),
            ],
            "plan_reviewer": [
                {"verdict": "approve", "feedback": "Reuse the deliverable."},
                {"verdict": "approve", "feedback": "All tasks are reconciled."},
                {"verdict": "approve", "feedback": "Atomic task."},
                {"verdict": "approve", "feedback": "Atomic task."},
                {"verdict": "approve", "feedback": "Atomic task."},
            ],
        }
    )
    engine = build_engine(
        log_dir,
        item_id=ITEM_ID,
        twig=twig,
        provider=provider,
    )

    result = await engine.run("nested-pin-reconcile")

    assert isinstance(result, Completed), result
    child_log = log_dir / "nested-pin-reconcile__child_1.events.jsonl"
    child_completed = completed_from_log(child_log)
    assert child_completed["pin_validator_1"]["error_kind"] == "pin_reconcile"
    assert f"must pin AB#{task_ids[1]}" in child_completed[
        "pin_validator_1"
    ]["details"]["feedback"]
    assert "reviewer_1" not in child_completed
    assert child_completed["pin_validator_2"]["kind"] == "success"

    tree = json.loads(
        (log_dir / "nested-pin-reconcile.plan.tree.json").read_text(
            encoding="utf-8"
        )
    )
    nested = tree["children"][0]
    assert [proposal["item_id"] for proposal in nested["proposals"]] == list(
        task_ids
    )
    assert [child["item_id"] for child in nested["children"]] == list(task_ids)


@pytest.mark.parametrize(
    ("existing_work_complete", "inventory", "expected_fragment"),
    [
        (False, [], "inventory is incomplete"),
        (
            True,
            [
                {
                    "item_id": 8001,
                    "title": "Exact task",
                    "work_item_type": "Task",
                    "parent_id": ITEM_ID,
                    "same_scenario_lineage": False,
                }
            ],
            "lacks durable Requiem lineage",
        ),
        (
            True,
            [
                {
                    "item_id": 8001,
                    "title": "Exact task",
                    "work_item_type": "Task",
                    "parent_id": ITEM_ID,
                    "same_scenario_lineage": True,
                },
                {
                    "item_id": 8002,
                    "title": "Exact task",
                    "work_item_type": "Task",
                    "parent_id": ITEM_ID,
                    "same_scenario_lineage": True,
                },
            ],
            "reuse is ambiguous",
        ),
        (
            True,
            [
                {
                    "item_id": 8001,
                    "title": "Exact task",
                    "work_item_type": "Task",
                    "parent_id": ITEM_ID + 1,
                    "same_scenario_lineage": True,
                }
            ],
            "silently reparent or duplicate",
        ),
    ],
)
def test_unpinned_existing_work_fails_closed(
    existing_work_complete: bool,
    inventory: list[dict],
    expected_fragment: str,
):
    planner = {
        "children": [
            {
                "title": "Exact task",
                "description": "Proposed work.",
                "work_item_type": "Task",
            }
        ]
    }
    item = {
        "item_id": ITEM_ID,
        "scenario_item_id": ITEM_ID,
        "existing_work": inventory,
        "existing_work_complete": existing_work_complete,
    }

    errors = _pin_validation_errors(
        planner,
        item,
        ancestor_item_ids=(),
    )

    assert errors
    assert expected_fragment in errors[0]


def test_unpinned_genuinely_new_work_passes_reconciliation():
    errors = _pin_validation_errors(
        {
            "children": [
                {
                    "title": "New task",
                    "description": "No exact existing work.",
                    "work_item_type": "Task",
                }
            ]
        },
        {
            "item_id": ITEM_ID,
            "scenario_item_id": ITEM_ID,
            "existing_work": [
                {
                    "item_id": 8001,
                    "title": "Different task",
                    "work_item_type": "Task",
                    "parent_id": ITEM_ID,
                    "same_scenario_lineage": True,
                }
            ],
            "existing_work_complete": True,
        },
        ancestor_item_ids=(),
    )

    assert errors == []


async def test_unresolved_overlap_pin_routes_to_lineage_gate(log_dir: Path):
    existing_id = 8888
    twig = _twig()
    for candidate_id in (existing_id, existing_id + 1):
        twig.items[candidate_id] = TwigItem(
            id=candidate_id,
            title="Ambiguous overlap",
            state="Active",
            area_path="Polyphony\\Engine",
            work_item_type="Task",
            parent_id=ITEM_ID,
            raw={
                "description": format_commit_marker(
                    f"plan-{ITEM_ID}-prior",
                    ITEM_ID * 100 + candidate_id - existing_id + 1,
                )
            },
        )
    unsafe = {
        **_decomposable_planner_output(),
        "children": [
            {
                "title": "Ambiguous overlap",
                "description": "cannot prove ownership",
                "work_item_type": "Task",
                "item_id": existing_id,
            }
        ],
    }
    provider = FakeProvider(
        scripts={
            "planner": [unsafe] * ITER_CAP,
            "plan_reviewer": [],
        }
    )
    engine = build_engine(
        log_dir,
        item_id=ITEM_ID,
        twig=twig,
        provider=provider,
        gate_handler=_proceed_handler,
    )

    result = await engine.run("pin-unresolved")

    assert isinstance(result, Completed), result
    assert result.final_node == "end_needs_human"
    completed = completed_from_log(engine.log_path("pin-unresolved"))
    assert f"pin_validator_{ITER_CAP}" in completed
    assert all(
        f"reviewer_{iteration}" not in completed
        for iteration in range(1, ITER_CAP + 1)
    )
    events = list(replay(engine.log_path("pin-unresolved")))
    assert any(
        event.get("kind") == "gate_opened"
        and event.get("node_id") == "lineage_gate"
        for event in events
    )


async def test_decomposable_three_children(log_dir: Path):
    """Planner returns decomposable=True with 3 leaf children → recursion fires.

    After Fauré seat 2 the workflow spawns one sub-workflow per proposed
    child. Each child's own planning run returns a leaf, so the
    aggregated plan has 3 leaf PlanResult children. The flat sidecar
    JSON now carries the recursive tree (each child rendered as a
    serialised PlanResult, not a raw planner proposal).
    """
    provider = FakeProvider(
        scripts={
            # Root planner + 3 child planners.
            "planner": [
                _decomposable_planner_output(),
                _leaf_planner_output(),
                _leaf_planner_output(),
                _leaf_planner_output(),
            ],
            # Root reviewer + 3 child reviewers.
            "plan_reviewer": [
                {"verdict": "approve", "feedback": "Good cuts."},
                {"verdict": "approve", "feedback": "ok."},
                {"verdict": "approve", "feedback": "ok."},
                {"verdict": "approve", "feedback": "ok."},
            ],
        }
    )
    # Pre-register synthesised child item ids in the fake twig so each
    # child's fetch_item succeeds. Synthesised id is parent*100 + (i+1).
    twig = _twig()
    for slot in (1, 2, 3):
        child_id = ITEM_ID * 100 + slot
        twig.items[child_id] = TwigItem(
            id=child_id,
            title=f"Child slot {slot}",
            state="New",
            area_path="Polyphony\\Engine",
            work_item_type="Task",
            parent_id=ITEM_ID,
            raw={},
        )
    engine = build_engine(log_dir, item_id=ITEM_ID, twig=twig, provider=provider)
    result = await engine.run("decomp")
    assert isinstance(result, Completed), result

    completed = completed_from_log(engine.log_path("decomp"))
    plan = project_plan_result(completed)
    assert plan is not None
    assert plan.decomposable is True
    assert plan.review_iterations == 1
    assert len(plan.children) == 3
    assert all(c.decomposable is False for c in plan.children)
    expected_ids = {ITEM_ID * 100 + 1, ITEM_ID * 100 + 2, ITEM_ID * 100 + 3}
    assert {c.item_id for c in plan.children} == expected_ids

    tree = log_dir / "decomp.plan.tree.json"
    assert tree.exists(), f"plan.tree.json missing; dir={list(log_dir.iterdir())}"
    payload = json.loads(tree.read_text(encoding="utf-8"))
    assert payload["item_id"] == ITEM_ID
    assert payload["decomposable"] is True
    # `children` is now the recursive PlanResult tree; `proposals` keeps
    # the raw planner output.
    assert len(payload["children"]) == 3
    assert len(payload["proposals"]) == 3
    assert {c["title"] for c in payload["proposals"]} == {
        "Define ErrorKind enum",
        "Migrate verbs to ErrorKind",
        "Update tests",
    }

    # Three child sub-run logs exist (INV-SUBWORKFLOW-LOG-ISOLATION
    # gives each its own file).
    child_logs = sorted(
        p.name
        for p in log_dir.glob("decomp__child_*.events.jsonl")
    )
    assert child_logs == [
        "decomp__child_1.events.jsonl",
        "decomp__child_2.events.jsonl",
        "decomp__child_3.events.jsonl",
    ]


# ---- Q7: optional review_group label (ADR-0006 §Q7) --------------------


def test_child_plan_review_group_optional_and_unvalidated():
    """`review_group` defaults to None, accepts any free-form string (no
    closed enum), and legacy planner JSON omitting the field still
    validates — the forward-compatibility the Q7 decision turns on."""
    from requiem.workflows.planning import ChildPlan, PlannerOutput

    bare = ChildPlan(title="t", description="d", work_item_type="Task")
    assert bare.review_group is None

    labelled = ChildPlan(
        title="t",
        description="d",
        work_item_type="Task",
        review_group="data-layer",
    )
    assert labelled.review_group == "data-layer"

    # Deliberately not constrained to a closed enum in v0.
    odd = ChildPlan(
        title="t",
        description="d",
        work_item_type="Task",
        review_group="anything-goes 123",
    )
    assert odd.review_group == "anything-goes 123"

    # Legacy planner output (field absent everywhere) still validates.
    legacy = PlannerOutput.model_validate(
        {
            "summary": "s",
            "decomposable": True,
            "children": [
                {"title": "t", "description": "d", "work_item_type": "Task"}
            ],
            "estimated_complexity": "small",
            "rationale": "r",
        }
    )
    assert legacy.children[0].review_group is None


async def test_review_group_round_trips_into_plan_tree(log_dir: Path):
    """A planner-assigned `review_group` survives into the
    `.plan.tree.json` proposals; an unlabelled sibling carries None.

    This is the dashboard render hint surfacing end-to-end (ADR-0006 §Q7)
    with no branch-topology involvement.
    """
    planner_output = _decomposable_planner_output()
    planner_output["children"][0]["review_group"] = "enum-layer"
    planner_output["children"][1]["review_group"] = "enum-layer"
    # Third child intentionally left unlabelled to prove optionality.

    provider = FakeProvider(
        scripts={
            "planner": [
                planner_output,
                _leaf_planner_output(),
                _leaf_planner_output(),
                _leaf_planner_output(),
            ],
            "plan_reviewer": [
                {"verdict": "approve", "feedback": "Good cuts."},
                {"verdict": "approve", "feedback": "ok."},
                {"verdict": "approve", "feedback": "ok."},
                {"verdict": "approve", "feedback": "ok."},
            ],
        }
    )
    twig = _twig()
    for slot in (1, 2, 3):
        child_id = ITEM_ID * 100 + slot
        twig.items[child_id] = TwigItem(
            id=child_id,
            title=f"Child slot {slot}",
            state="New",
            area_path="Polyphony\\Engine",
            work_item_type="Task",
            parent_id=ITEM_ID,
            raw={},
        )
    engine = build_engine(log_dir, item_id=ITEM_ID, twig=twig, provider=provider)
    result = await engine.run("rg")
    assert isinstance(result, Completed), result

    tree = log_dir / "rg.plan.tree.json"
    assert tree.exists(), f"plan.tree.json missing; dir={list(log_dir.iterdir())}"
    payload = json.loads(tree.read_text(encoding="utf-8"))
    groups = {p["title"]: p.get("review_group") for p in payload["proposals"]}
    assert groups["Define ErrorKind enum"] == "enum-layer"
    assert groups["Migrate verbs to ErrorKind"] == "enum-layer"
    assert groups["Update tests"] is None


# ---- inter-leaf depends_on (run #36 fanout follow-up) -------------------


def test_child_plan_depends_on_optional_and_unvalidated():
    """`depends_on` defaults to None, accepts a list of 0-based sibling
    slot indices with no closed-schema validation at this layer (real
    validation — range/self-ref/leaf-only — happens in plan_tree at
    resolution time, where the full sibling list is known), and legacy
    planner JSON omitting the field still validates."""
    from requiem.workflows.planning import ChildPlan, PlannerOutput

    bare = ChildPlan(title="t", description="d", work_item_type="Task")
    assert bare.depends_on is None

    dependent = ChildPlan(
        title="t", description="d", work_item_type="Task", depends_on=[0, 2],
    )
    assert dependent.depends_on == [0, 2]

    legacy = PlannerOutput.model_validate(
        {
            "summary": "s",
            "decomposable": True,
            "children": [
                {"title": "t", "description": "d", "work_item_type": "Task"}
            ],
            "estimated_complexity": "small",
            "rationale": "r",
        }
    )
    assert legacy.children[0].depends_on is None


async def test_depends_on_round_trips_into_plan_tree(log_dir: Path):
    """A planner-declared `depends_on` survives into the `.plan.tree.json`
    proposals unchanged (plan_tree.py, not planning.py, resolves slot
    indices to real ids) — an undeclared sibling carries None."""
    planner_output = _decomposable_planner_output()
    planner_output["children"][1]["depends_on"] = [0]
    # Third child intentionally left unlabelled to prove optionality.

    provider = FakeProvider(
        scripts={
            "planner": [
                planner_output,
                _leaf_planner_output(),
                _leaf_planner_output(),
                _leaf_planner_output(),
            ],
            "plan_reviewer": [
                {"verdict": "approve", "feedback": "Good cuts."},
                {"verdict": "approve", "feedback": "ok."},
                {"verdict": "approve", "feedback": "ok."},
                {"verdict": "approve", "feedback": "ok."},
            ],
        }
    )
    twig = _twig()
    for slot in (1, 2, 3):
        child_id = ITEM_ID * 100 + slot
        twig.items[child_id] = TwigItem(
            id=child_id,
            title=f"Child slot {slot}",
            state="New",
            area_path="Polyphony\\Engine",
            work_item_type="Task",
            parent_id=ITEM_ID,
            raw={},
        )
    engine = build_engine(log_dir, item_id=ITEM_ID, twig=twig, provider=provider)
    result = await engine.run("dep")
    assert isinstance(result, Completed), result

    tree = log_dir / "dep.plan.tree.json"
    payload = json.loads(tree.read_text(encoding="utf-8"))
    deps = {p["title"]: p.get("depends_on") for p in payload["proposals"]}
    assert deps["Migrate verbs to ErrorKind"] == [0]
    assert deps["Define ErrorKind enum"] is None
    assert deps["Update tests"] is None


def test_planner_prompt_mentions_depends_on_guidance():
    """The planner is told about depends_on so it has a way to express a
    real build-time prerequisite instead of leaving it as unenforceable
    prose (run #36: 3/4 needs_human leaves were exactly this gap)."""
    from requiem.workflows.planning import build_verb_registry

    verbs = build_verb_registry(
        item_id=ITEM_ID, parent_plan_id=None, max_depth=3, current_depth=0,
        ancestor_item_ids=(), twig=_twig(), log_dir=Path("."),
    )

    class _Ctx:
        completed = {
            "fetch_item": {
                "value": {
                    "item_id": ITEM_ID, "title": "t", "work_item_type": "Feature",
                    "state": "New", "description": "",
                }
            },
        }

    prompt = verbs.get("planner_prompt_1")(_Ctx())
    assert "depends_on" in prompt
    assert "0-based" in prompt


def test_reviewer_prompt_renders_declared_depends_on():
    """The reviewer sees each child's slot index and any declared deps, so
    it can sanity-check a dependency before approving the plan."""
    from requiem.workflows.planning import build_verb_registry

    verbs = build_verb_registry(
        item_id=ITEM_ID, parent_plan_id=None, max_depth=3, current_depth=0,
        ancestor_item_ids=(), twig=_twig(), log_dir=Path("."),
    )

    class _Ctx:
        completed = {
            "fetch_item": {
                "value": {
                    "item_id": ITEM_ID, "title": "t", "work_item_type": "Feature",
                    "state": "New", "description": "",
                }
            },
            "planner_1": {
                "value": {
                    "parsed": {
                        "summary": "s", "decomposable": True,
                        "estimated_complexity": "small", "rationale": "r",
                        "children": [
                            {"title": "A", "description": "", "work_item_type": "Task"},
                            {"title": "B", "description": "", "work_item_type": "Task",
                             "depends_on": [0]},
                        ],
                    }
                }
            },
        }

    prompt = verbs.get("reviewer_prompt_1")(_Ctx())
    assert "slot 0" in prompt
    assert "slot 1" in prompt
    assert "depends_on: slot(s) [0]" in prompt


# ---- revise loop -------------------------------------------------------


async def test_revise_then_approve_iteration_2(log_dir: Path):
    """Reviewer revises iter 1; planner re-runs; reviewer approves iter 2."""
    provider = FakeProvider(
        scripts={
            "planner": [
                _leaf_planner_output(),
                {**_leaf_planner_output(),
                 "summary": "Tightened per reviewer feedback."},
            ],
            "plan_reviewer": [
                {"verdict": "revise", "feedback": "Tighten the summary."},
                {"verdict": "approve", "feedback": "Better."},
            ],
        }
    )
    engine = build_engine(log_dir, item_id=ITEM_ID, twig=_twig(), provider=provider)
    result = await engine.run("revise")
    assert isinstance(result, Completed), result

    completed = completed_from_log(engine.log_path("revise"))
    plan = project_plan_result(completed)
    assert plan is not None
    assert plan.review_iterations == 2
    assert plan.final_verdict == "approved"

    # planner_1 + planner_2 ran; planner_3 did not.
    assert "planner_1" in completed
    assert "planner_2" in completed
    assert "planner_3" not in completed
    assert completed["planner_2"]["value"]["parsed"]["summary"].startswith("Tightened")


async def test_stalled_revision_escalates_to_human(log_dir: Path):
    """A revision pass that makes no planner progress escalates immediately."""
    provider = FakeProvider(
        scripts={
            "planner": [
                _leaf_planner_output(),
                _leaf_planner_output(),
            ],
            "plan_reviewer": [
                {"verdict": "revise", "feedback": "Tighten the summary."},
                {"verdict": "revise", "feedback": "Tighten the summary."},
            ],
        }
    )
    engine = build_engine(
        log_dir,
        item_id=ITEM_ID,
        twig=_twig(),
        provider=provider,
        gate_handler=_proceed_handler,
    )
    result = await engine.run("stalled")
    assert isinstance(result, Completed), result
    assert result.final_node == "end_needs_human"

    completed = completed_from_log(engine.log_path("stalled"))
    plan = project_plan_result(completed)
    assert plan is not None
    assert plan.final_verdict == "needs_human"
    assert "planner_1" in completed
    assert "planner_2" in completed
    assert "planner_3" not in completed
    assert completed["router_2"]["kind"] == "permanent_failure"
    assert completed["router_2"]["error_kind"] == "escalate"


async def test_max_revisions_escalates_to_human(log_dir: Path):
    """Reviewer revises every iteration → router_3 escalates → human gate."""
    provider = FakeProvider(
        scripts={
            "planner": [
                {**_leaf_planner_output(), "summary": f"Revision {i}"}
                for i in range(1, ITER_CAP + 1)
            ],
            "plan_reviewer": [
                {"verdict": "revise", "feedback": "not yet"},
            ]
            * ITER_CAP,
        }
    )
    engine = build_engine(
        log_dir,
        item_id=ITEM_ID,
        twig=_twig(),
        provider=provider,
        gate_handler=_proceed_handler,
    )
    result = await engine.run("escalate")
    assert isinstance(result, Completed), result
    # `proceed` routes to record_needs_human → end_needs_human.
    assert result.final_node == "end_needs_human"

    completed = completed_from_log(engine.log_path("escalate"))
    plan = project_plan_result(completed)
    assert plan is not None
    assert plan.final_verdict == "needs_human"
    # All three planner/reviewer iterations ran.
    for i in range(1, ITER_CAP + 1):
        assert f"planner_{i}" in completed
        assert f"reviewer_{i}" in completed
        assert f"router_{i}" in completed
    # Last router returned a permanent_failure:escalate (not a Success).
    last_router = completed[f"router_{ITER_CAP}"]
    assert last_router["kind"] == "permanent_failure"
    assert last_router["error_kind"] == "escalate"

    # The escalation_gate event fired exactly once.
    events = list(replay(engine.log_path("escalate")))
    gate_opens = [
        e for e in events
        if e["kind"] == "gate_opened" and e.get("node_id") == "escalation_gate"
    ]
    assert len(gate_opens) == 1


async def test_max_revisions_then_operator_aborts(log_dir: Path):
    """Cap is hit; operator picks `abort` at the escalation gate."""
    provider = FakeProvider(
        scripts={
            "planner": [_leaf_planner_output()] * ITER_CAP,
            "plan_reviewer": [{"verdict": "revise", "feedback": "x"}] * ITER_CAP,
        }
    )
    engine = build_engine(
        log_dir,
        item_id=ITEM_ID,
        twig=_twig(),
        provider=provider,
        gate_handler=_abort_handler,
    )
    result = await engine.run("escalate-abort")
    assert isinstance(result, Completed), result
    assert result.disposition == "failed"
    assert result.final_node == "fail_end"


# ---- BadOutput contract ------------------------------------------------


async def test_planner_bad_output_routes_to_human_no_retry(log_dir: Path):
    """Planner returns invalid output → BadOutput → bad_output_gate, NOT retry."""
    invalid_planner_payload = {
        # Missing required `decomposable`, wrong `estimated_complexity` value.
        "summary": "broken",
        "children": [],
        "estimated_complexity": "epic",  # not in Literal
        "rationale": "missing decomposable field",
    }
    provider = FakeProvider(
        scripts={
            # Only ONE planner script entry — if the engine auto-retried,
            # the FakeProvider would return `fake.exhausted` and the run
            # would crash differently. Single entry == one call assertion.
            "planner": [invalid_planner_payload],
            # No reviewer script; the workflow shouldn't reach the reviewer.
            "plan_reviewer": [],
        }
    )
    engine = build_engine(
        log_dir,
        item_id=ITEM_ID,
        twig=_twig(),
        provider=provider,
        gate_handler=_abort_handler,  # bad_output_gate only offers `abort`
    )
    result = await engine.run("badoutput")
    assert isinstance(result, Completed), result
    assert result.disposition == "failed"
    assert result.final_node == "fail_end"

    # Only one planner call was made.
    planner_calls = [c for c in engine.provider.calls if c["agent"] == "planner"]
    assert len(planner_calls) == 1, planner_calls
    # And the reviewer was never invoked.
    reviewer_calls = [c for c in engine.provider.calls if c["agent"] == "plan_reviewer"]
    assert reviewer_calls == []

    # The bad_output route was actually taken.
    events = list(replay(engine.log_path("badoutput")))
    bo_routes = [
        e for e in events
        if e["kind"] == "route_taken"
        and e["payload"].get("key") == "bad_output"
    ]
    assert len(bo_routes) == 1
    # And the BadOutput outcome carried `error_kind=schema_mismatch`.
    bo_outcomes = [
        e for e in events
        if e["kind"] == "verb_completed"
        and (e["payload"].get("outcome") or {}).get("kind") == "bad_output"
    ]
    assert len(bo_outcomes) == 1
    assert (
        bo_outcomes[0]["payload"]["outcome"]["error_kind"] == "schema_mismatch"
    )


# ---- bounded planner token recovery ------------------------------------


async def test_recursive_planner_token_exhaustion_uses_one_evidence_only_retry(
    log_dir: Path,
):
    root_plan = {
        **_decomposable_planner_output(),
        "summary": "One deliverable needs recursive planning.",
        "children": [{
            "title": "Preferred + fallback VMSS SKU config schema",
            "description": (
                "Define the preferred SKU, ordered fallbacks, and hard "
                "eligibility constraints using the existing override layering."
            ),
            "work_item_type": "Deliverable",
        }],
    }
    provider = FakeProvider(
        scripts={
            "planner": [root_plan, _planner_token_exhaustion()],
            "bounded_planner": [{
                **_leaf_planner_output(),
                "summary": "A single bounded schema change.",
            }],
            "plan_reviewer": [
                {"verdict": "approve", "feedback": "Root is sound."},
                {"verdict": "approve", "feedback": "Recovered child is sound."},
            ],
        }
    )
    engine = build_engine(
        log_dir,
        item_id=ITEM_ID,
        twig=_twig(),
        provider=provider,
    )

    result = await engine.run("recursive-token-recovery")

    assert isinstance(result, Completed), result
    assert result.final_node == "end"
    plan = project_plan_result(completed_from_log(engine.log_path(result.run_id)))
    assert plan is not None
    assert plan.final_verdict == "approved"
    assert len(plan.children) == 1
    assert plan.children[0].summary == "A single bounded schema change."

    bounded_calls = [
        call for call in provider.calls if call["agent"] == "bounded_planner"
    ]
    assert len(bounded_calls) == 1
    assert bounded_calls[0]["model_options"] == {"disable_repo_tools": True}
    assert "single bounded recovery attempt" in bounded_calls[0]["user_message"]
    assert "Do not call tools" in bounded_calls[0]["user_message"]
    assert "Preferred + fallback VMSS SKU config schema" in (
        bounded_calls[0]["user_message"]
    )

    child_completed = completed_from_log(
        log_dir / "recursive-token-recovery__child_1.events.jsonl"
    )
    recovery = child_completed["recover_planner_token_exhaustion_1"]
    assert recovery["kind"] == "success"
    assert recovery["value"]["trigger"]["input_tokens"] == 90578
    assert child_completed["planner_recovery_1"]["kind"] == "success"


async def test_non_token_planner_failure_does_not_use_bounded_retry(
    log_dir: Path,
):
    provider = FakeProvider(
        scripts={
            "planner": [RetryableFailure(
                retry_key="planner#1",
                error_kind="provider_unavailable",
                message="temporary upstream outage",
            )],
            "bounded_planner": [],
            "plan_reviewer": [],
        }
    )
    engine = build_engine(log_dir, item_id=ITEM_ID, twig=_twig(), provider=provider)

    result = await engine.run("planner-runtime-failure")

    assert isinstance(result, Completed), result
    assert result.final_node == "fail_end_crash"
    completed = completed_from_log(engine.log_path(result.run_id))
    assert (
        completed["recover_planner_token_exhaustion_1"]["error_kind"]
        == "planner.runtime_failure"
    )
    assert all(call["agent"] != "bounded_planner" for call in provider.calls)


async def test_bounded_planner_failure_is_not_retried_again(log_dir: Path):
    provider = FakeProvider(
        scripts={
            "planner": [_planner_token_exhaustion("primary#1")],
            "bounded_planner": [_planner_token_exhaustion("bounded#1")],
            "plan_reviewer": [],
        }
    )
    engine = build_engine(log_dir, item_id=ITEM_ID, twig=_twig(), provider=provider)

    result = await engine.run("planner-bounded-failure")

    assert isinstance(result, Completed), result
    assert result.final_node == "fail_end_crash"
    completed = completed_from_log(engine.log_path(result.run_id))
    assert (
        completed["finalize_planner_recovery_failure_1"]["error_kind"]
        == "planner.bounded_retry_failed"
    )
    bounded_calls = [
        call for call in provider.calls if call["agent"] == "bounded_planner"
    ]
    assert len(bounded_calls) == 1


# ---- bounded reviewer request-body recovery ----------------------------


async def test_recursive_reviewer_request_timeout_uses_one_evidence_only_retry(
    log_dir: Path,
):
    root_plan = {
        **_decomposable_planner_output(),
        "summary": "One deliverable needs recursive planning.",
        "children": [{
            "title": "Typed picked-SKU fallback consumption",
            "description": (
                "Plan the typed fallback-chain consumption across the Cluster "
                "Bicep and Ev2 wiring."
            ),
            "work_item_type": "Deliverable",
        }],
    }
    child_plan = {
        **_leaf_planner_output(),
        "summary": "The typed fallback consumption is one bounded implementation.",
    }
    provider = FakeProvider(
        scripts={
            "planner": [root_plan, child_plan],
            "plan_reviewer": [
                {"verdict": "approve", "feedback": "Root is sound."},
                _reviewer_request_body_timeout(),
            ],
            "bounded_plan_reviewer": [{
                "verdict": "approve",
                "feedback": "The complete evidence supports this child plan.",
            }],
        }
    )
    engine = build_engine(
        log_dir,
        item_id=ITEM_ID,
        twig=_twig(),
        provider=provider,
    )

    result = await engine.run("recursive-reviewer-recovery")

    assert isinstance(result, Completed), result
    assert result.final_node == "end"
    plan = project_plan_result(completed_from_log(engine.log_path(result.run_id)))
    assert plan is not None
    assert plan.final_verdict == "approved"
    assert len(plan.children) == 1
    assert plan.children[0].final_verdict == "approved"

    bounded_calls = [
        call for call in provider.calls
        if call["agent"] == "bounded_plan_reviewer"
    ]
    assert len(bounded_calls) == 1
    assert bounded_calls[0]["model_options"] == {"disable_repo_tools": True}
    assert "single bounded recovery attempt" in bounded_calls[0]["user_message"]
    assert "Do not call tools" in bounded_calls[0]["user_message"]
    assert "Typed picked-SKU fallback consumption" in (
        bounded_calls[0]["user_message"]
    )

    child_completed = completed_from_log(
        log_dir / "recursive-reviewer-recovery__child_1.events.jsonl"
    )
    recovery = child_completed["recover_reviewer_request_body_timeout_1"]
    assert recovery["kind"] == "success"
    assert recovery["value"]["trigger"]["input_tokens"] == 37732
    assert child_completed["reviewer_recovery_1"]["kind"] == "success"
    assert child_completed["router_1"]["kind"] == "success"


async def test_non_request_body_reviewer_failure_does_not_use_bounded_retry(
    log_dir: Path,
):
    provider = FakeProvider(
        scripts={
            "planner": [_leaf_planner_output()],
            "plan_reviewer": [RetryableFailure(
                retry_key="reviewer#1",
                error_kind="provider_unavailable",
                message="temporary upstream outage",
            )],
            "bounded_plan_reviewer": [],
        }
    )
    engine = build_engine(log_dir, item_id=ITEM_ID, twig=_twig(), provider=provider)

    result = await engine.run("reviewer-runtime-failure")

    assert isinstance(result, Completed), result
    assert result.final_node == "fail_end_crash"
    completed = completed_from_log(engine.log_path(result.run_id))
    assert (
        completed["recover_reviewer_request_body_timeout_1"]["error_kind"]
        == "reviewer.runtime_failure"
    )
    assert all(
        call["agent"] != "bounded_plan_reviewer" for call in provider.calls
    )


async def test_bounded_reviewer_failure_is_not_retried_again(log_dir: Path):
    provider = FakeProvider(
        scripts={
            "planner": [_leaf_planner_output()],
            "plan_reviewer": [_reviewer_request_body_timeout("primary#1")],
            "bounded_plan_reviewer": [
                _reviewer_request_body_timeout("bounded#1")
            ],
        }
    )
    engine = build_engine(log_dir, item_id=ITEM_ID, twig=_twig(), provider=provider)

    result = await engine.run("reviewer-bounded-failure")

    assert isinstance(result, Completed), result
    assert result.final_node == "fail_end_crash"
    completed = completed_from_log(engine.log_path(result.run_id))
    assert (
        completed["finalize_reviewer_recovery_failure_1"]["error_kind"]
        == "reviewer.bounded_retry_failed"
    )
    bounded_calls = [
        call for call in provider.calls
        if call["agent"] == "bounded_plan_reviewer"
    ]
    assert len(bounded_calls) == 1


# ---- depth guard -------------------------------------------------------


async def test_max_depth_exceeded_routes_to_human(log_dir: Path):
    """`current_depth > max_depth` returns PermanentFailure → depth_gate."""
    # Choose depth that violates the guard but lets the operator pick `abort`.
    provider = FakeProvider(scripts={"planner": [], "plan_reviewer": []})
    engine = build_engine(
        log_dir,
        item_id=ITEM_ID,
        twig=_twig(),
        provider=provider,
        max_depth=2,
        current_depth=3,
        gate_handler=_abort_handler,
    )
    result = await engine.run("deep")
    assert isinstance(result, Completed), result
    assert result.disposition == "failed"
    assert result.final_node == "fail_end"

    events = list(replay(engine.log_path("deep")))
    # The depth_gate fired.
    gates = [
        e for e in events
        if e["kind"] == "gate_opened" and e.get("node_id") == "depth_gate"
    ]
    assert len(gates) == 1
    # No planner ran.
    planner_entries = [
        e for e in events
        if e["kind"] == "node_entered" and (e.get("node_id") or "").startswith("planner_")
    ]
    assert planner_entries == []


# ---- INV-RESTART -------------------------------------------------------


async def test_inv_restart_resume_to_same_terminal(log_dir: Path):
    """Kill mid-plan (leaf scenario); resume to identical terminal state.

    Uses a leaf planner output so this seat-1 test focuses on the
    planner→reviewer→record iteration-cap restart contract. Multi-level
    recursion's INV-RESTART is covered in
    ``tests/test_planning_recursion.py``.

    Strategy mirrors the code-review demo's restart test: run once, truncate
    the log to just after `reviewer_1.verb_completed`, then resume with a
    fresh engine sharing the same run id. Each LLM agent has exactly the
    number of scripted responses needed for the WHOLE run; if the engine
    re-ran a completed node, the FakeProvider would return `fake.exhausted`
    and the test would fail.
    """
    run_id = "restart"

    # ---- first run: full happy path (leaf, no recursion)
    provider1 = FakeProvider(
        scripts={
            "planner": [_leaf_planner_output()],
            "plan_reviewer": [{"verdict": "approve", "feedback": "ok"}],
        }
    )
    engine1 = build_engine(
        log_dir, item_id=ITEM_ID, twig=_twig(), provider=provider1
    )
    result1 = await engine1.run(run_id)
    assert isinstance(result1, Completed)
    assert result1.final_node == "end"

    log_path = engine1.log_path(run_id)
    completed1 = completed_from_log(log_path)
    plan1 = project_plan_result(completed1)
    assert plan1 is not None

    # ---- truncate the log to just after reviewer_1's verb_completed
    lines = log_path.read_text(encoding="utf-8").splitlines()
    keep: list[str] = []
    for raw in lines:
        keep.append(raw)
        ev = json.loads(raw)
        if (
            ev["kind"] == "verb_completed"
            and ev.get("node_id") == "reviewer_1"
        ):
            break
    else:
        pytest.fail("never saw reviewer_1 verb_completed; cannot truncate")
    log_path.write_text("\n".join(keep) + "\n", encoding="utf-8")

    # Delete any sidecar artefacts written by the first run so we can
    # verify the resume rewrote them deterministically.
    for p in list(log_dir.iterdir()):
        if p.suffix in {".md", ".json"} and p.name.startswith(run_id):
            p.unlink()

    # ---- resume: planner and reviewer were already consumed; if the
    # engine re-runs them, FakeProvider exhausts and fails loudly.
    provider2 = FakeProvider(
        scripts={
            "planner": [],         # zero entries — must not be called
            "plan_reviewer": [],
        }
    )
    engine2 = build_engine(
        log_dir, item_id=ITEM_ID, twig=_twig(), provider=provider2
    )
    result2 = await engine2.run(run_id)
    assert isinstance(result2, Completed), result2
    assert result2.final_node == result1.final_node
    assert result2.disposition == result1.disposition

    # Provider was not consulted at all on resume.
    assert provider2.calls == []

    completed2 = completed_from_log(log_path)
    plan2 = project_plan_result(completed2)
    assert plan2 == plan1, (plan1, plan2)

    # Sidecar artefact reappeared (leaf scenario → markdown).
    md = log_dir / f"{run_id}.plan.md"
    assert md.exists()


async def test_planner_recovery_resume_does_not_repeat_bounded_call(
    log_dir: Path,
):
    run_id = "planner-recovery-restart"
    provider1 = FakeProvider(
        scripts={
            "planner": [_planner_token_exhaustion()],
            "bounded_planner": [_leaf_planner_output()],
            "plan_reviewer": [{"verdict": "approve", "feedback": "ok"}],
        }
    )
    engine1 = build_engine(
        log_dir, item_id=ITEM_ID, twig=_twig(), provider=provider1
    )
    result1 = await engine1.run(run_id)
    assert isinstance(result1, Completed), result1

    log_path = engine1.log_path(run_id)
    lines = log_path.read_text(encoding="utf-8").splitlines()
    keep: list[str] = []
    for raw in lines:
        keep.append(raw)
        ev = json.loads(raw)
        if (
            ev["kind"] == "verb_completed"
            and ev.get("node_id") == "planner_recovery_1"
        ):
            break
    else:
        pytest.fail("never saw planner_recovery_1 verb_completed; cannot truncate")
    log_path.write_text("\n".join(keep) + "\n", encoding="utf-8")
    for path in log_dir.glob(f"{run_id}.plan.*"):
        path.unlink()

    provider2 = FakeProvider(
        scripts={
            "planner": [],
            "bounded_planner": [],
            "plan_reviewer": [{"verdict": "approve", "feedback": "ok"}],
        }
    )
    engine2 = build_engine(
        log_dir, item_id=ITEM_ID, twig=_twig(), provider=provider2
    )

    result2 = await engine2.run(run_id)

    assert isinstance(result2, Completed), result2
    assert result2.final_node == result1.final_node
    assert [call["agent"] for call in provider2.calls] == ["plan_reviewer"]


async def test_reviewer_recovery_resume_does_not_repeat_bounded_call(
    log_dir: Path,
):
    run_id = "reviewer-recovery-restart"
    provider1 = FakeProvider(
        scripts={
            "planner": [_leaf_planner_output()],
            "plan_reviewer": [_reviewer_request_body_timeout()],
            "bounded_plan_reviewer": [{
                "verdict": "approve",
                "feedback": "Complete evidence supports approval.",
            }],
        }
    )
    engine1 = build_engine(
        log_dir, item_id=ITEM_ID, twig=_twig(), provider=provider1
    )
    result1 = await engine1.run(run_id)
    assert isinstance(result1, Completed), result1

    log_path = engine1.log_path(run_id)
    lines = log_path.read_text(encoding="utf-8").splitlines()
    keep: list[str] = []
    for raw in lines:
        keep.append(raw)
        ev = json.loads(raw)
        if (
            ev["kind"] == "verb_completed"
            and ev.get("node_id") == "reviewer_recovery_1"
        ):
            break
    else:
        pytest.fail("never saw reviewer_recovery_1 verb_completed; cannot truncate")
    log_path.write_text("\n".join(keep) + "\n", encoding="utf-8")
    for path in log_dir.glob(f"{run_id}.plan.*"):
        path.unlink()

    provider2 = FakeProvider(
        scripts={
            "planner": [],
            "plan_reviewer": [],
            "bounded_plan_reviewer": [],
        }
    )
    engine2 = build_engine(
        log_dir, item_id=ITEM_ID, twig=_twig(), provider=provider2
    )

    result2 = await engine2.run(run_id)

    assert isinstance(result2, Completed), result2
    assert result2.final_node == result1.final_node
    assert provider2.calls == []


# ---- workflow topology smoke ------------------------------------------


def test_workflow_topology_validates():
    """`.build()` already runs topology validation; spot-check explicitly."""
    wf = build_workflow()
    assert wf.name == "planning"
    assert wf.entry == "start"
    errs = wf.validate_topology()
    assert errs == [], errs

    node_ids = {n.node_id for n in wf.nodes}
    # Planner/pin-validator/reviewer/router chains.
    for i in range(1, ITER_CAP + 1):
        assert f"planner_{i}" in node_ids
        assert f"recover_planner_token_exhaustion_{i}" in node_ids
        assert f"planner_recovery_{i}" in node_ids
        assert f"finalize_planner_recovery_failure_{i}" in node_ids
        assert f"pin_validator_{i}" in node_ids
        assert f"reviewer_{i}" in node_ids
        assert f"recover_reviewer_request_body_timeout_{i}" in node_ids
        assert f"reviewer_recovery_{i}" in node_ids
        assert f"finalize_reviewer_recovery_failure_{i}" in node_ids
        assert f"router_{i}" in node_ids
    # Recursion-shaped scaffolding for the not-yet-shipped sub-workflow.
    assert {"record_plan", "record_needs_human", "branch_decomposable"} <= node_ids
    # Every gate offers either proceed/abort or abort-only.
    gates = {n.node_id: n for n in wf.nodes if getattr(n, "kind", "") == "human_gate"}
    for gate in gates.values():
        assert "abort" in gate.options


def test_bad_output_is_distinct_from_permanent_failure():
    """Sanity: the BadOutput outcome variant survives the encode/decode cycle.

    Guards against accidental schema drift that would let a planner's
    invalid output be routed as `permanent_failure` (and therefore be
    auto-retried in some future branch).
    """
    bo = BadOutput(
        error_kind="schema_mismatch",
        validation_errors=("missing field 'decomposable'",),
        raw_output="{}",
    )
    assert bo.error_kind == "schema_mismatch"
    assert isinstance(bo.validation_errors, tuple)


# ---- issue #31: catch-all crash narration -----------------------------


class _CrashingTwig:
    """Twig fake whose `show_async` raises an unexpected exception.

    Not a ``TwigClientError`` subclass — the verb's ``except
    TwigClientError`` won't catch it, so the kernel's ``_execute``
    catches it instead and produces ``PermanentFailure(error_kind=
    "verb.crash", message=...)``. This is the exact shape Tchaikovsky
    saw in the BUG #1 asyncio collision pre-fix (kernel.py:460-464).
    """

    async def show_async(self, item_id: int):  # noqa: ARG002
        raise RuntimeError("simulated planning-seat verb crash")


async def test_planning_verb_crash_routes_to_narrated_terminal(log_dir: Path):
    """A verb crash narrates via the verdict card instead of route.missing.

    Issue #31: pre-fix, a ``verb.crash`` in any planning verb stranded
    the run with ``Failed(error_kind='route.missing')`` and no verdict
    card. Post-fix, the catch-all ``permanent_failure`` edge from every
    script/agent verb routes the crash to the ``fail_end_crash``
    terminate node so the run completes with a narrated card.
    """
    # No planner script — the run crashes inside fetch_item before any
    # agent is invoked.
    provider = FakeProvider(scripts={"planner": [], "plan_reviewer": []})
    engine = build_engine(
        log_dir,
        item_id=ITEM_ID,
        twig=_CrashingTwig(),
        provider=provider,
    )
    result = await engine.run("crash")

    # The run terminated cleanly (Completed with failed disposition),
    # not Failed(route.missing). That alone is the issue #31 fix.
    assert isinstance(result, Completed), result
    assert result.disposition == "failed"
    assert result.final_node == "fail_end_crash"

    # The crash outcome was recorded against the crashing verb.
    completed = completed_from_log(engine.log_path("crash"))
    fetch_outcome = completed.get("fetch_item")
    assert fetch_outcome is not None
    assert fetch_outcome["kind"] == "permanent_failure"
    assert fetch_outcome["error_kind"] == "verb.crash"
    assert "RuntimeError" in fetch_outcome["message"]
    assert "simulated planning-seat verb crash" in fetch_outcome["message"]

    # The verdict card narrates the crash — naming the verb and the
    # error_kind — instead of returning None (the pre-fix behaviour).
    card = verdict_card(completed)
    assert card is not None
    assert "Did not plan" in card
    assert "fetch_item" in card
    assert "verb.crash" in card
    assert "RuntimeError" in card

    # The route_taken event for the catch-all `permanent_failure` edge
    # fired — guards against future refactors that drop the edge.
    events = list(replay(engine.log_path("crash")))
    crash_routes = [
        e for e in events
        if e["kind"] == "route_taken"
        and e.get("node_id") == "fetch_item"
        and e["payload"].get("key") == "permanent_failure"
        and e["payload"].get("to_node") == "fail_end_crash"
    ]
    assert len(crash_routes) == 1, crash_routes


async def test_planner_agent_crash_also_narrates(log_dir: Path):
    """The same catch-all wiring covers agent nodes (not just scripts).

    Exercises the `planner_1` catch-all edge by handing the FakeProvider
    a script entry shape it can't dispatch — its `invoke` raises
    `TypeError`, the kernel converts it to `verb.crash`, and the new
    catch-all edge routes to `fail_end_crash`.
    """
    # 42 is neither an Outcome nor a dict, so FakeProvider raises
    # TypeError on dispatch.
    provider = FakeProvider(scripts={"planner": [42], "plan_reviewer": []})
    engine = build_engine(
        log_dir,
        item_id=ITEM_ID,
        twig=_twig(),
        provider=provider,
    )
    result = await engine.run("agent-crash")

    assert isinstance(result, Completed), result
    assert result.disposition == "failed"
    assert result.final_node == "fail_end_crash"

    completed = completed_from_log(engine.log_path("agent-crash"))
    planner_outcome = completed.get("planner_1")
    assert planner_outcome is not None
    assert planner_outcome["kind"] == "permanent_failure"
    assert planner_outcome["error_kind"] == "verb.crash"

    card = verdict_card(completed)
    assert card is not None
    assert "planner_1" in card
    assert "verb.crash" in card


# ============================================================
# ADR-0027: reviewer escalation handling
# ============================================================


def test_make_escalation_policy_handler_rejects_invalid_policy():
    """Policy names are validated at construction time, not at fire time."""
    import pytest
    with pytest.raises(ValueError, match="invalid escalation policy"):
        make_escalation_policy_handler("hallucinated-policy")


def test_make_escalation_policy_handler_accepts_all_valid_policies():
    """All three documented policies must construct successfully."""
    for policy in VALID_ESCALATION_POLICIES:
        h = make_escalation_policy_handler(policy)
        assert callable(h)
        assert getattr(h, "__requiem_auto__", False) is True
        assert getattr(h, "__requiem_escalation_policy__", None) == policy


def test_escalation_policy_handler_only_intercepts_escalation_gate():
    """Other gates must fall through to the fallback handler. This is
    the safety property that lets ADR-0027 ship without affecting
    bad_output_gate, type_policy_gate, recursion_depth_gate, etc."""
    fallback_calls: list[tuple[str, str, tuple[str, ...]]] = []

    def fallback(node_id, prompt, options):
        fallback_calls.append((node_id, prompt, options))
        return "abort"

    h = make_escalation_policy_handler("accept-last", fallback=fallback)
    # escalation_gate is intercepted with proceed.
    assert h("escalation_gate", "p", ("proceed", "abort")) == "proceed"
    assert fallback_calls == []
    # Other gates delegate.
    assert h("bad_output_gate", "p", ("abort",)) == "abort"
    assert fallback_calls == [("bad_output_gate", "p", ("abort",))]


def test_escalation_policy_escalate_is_pass_through():
    """policy=escalate (default) delegates EVERY gate to fallback —
    behavior is byte-identical to today's pre-ADR-0027 code path."""
    calls: list[str] = []

    def fallback(node_id, prompt, options):
        calls.append(node_id)
        return "proceed" if "proceed" in options else "abort"

    h = make_escalation_policy_handler("escalate", fallback=fallback)
    h("escalation_gate", "p", ("proceed", "abort"))
    h("bad_output_gate", "p", ("abort",))
    assert calls == ["escalation_gate", "bad_output_gate"]


def test_escalation_policy_abort_auto_aborts_escalation_only():
    """policy=abort: escalation_gate auto-aborts; other gates fall through."""
    fallback_calls: list[str] = []

    def fallback(node_id, prompt, options):
        fallback_calls.append(node_id)
        return "abort"

    h = make_escalation_policy_handler("abort", fallback=fallback)
    assert h("escalation_gate", "p", ("proceed", "abort")) == "abort"
    assert fallback_calls == []  # didn't fall through
    h("bad_output_gate", "p", ("abort",))
    assert fallback_calls == ["bad_output_gate"]


def test_escalation_policy_defensive_when_option_set_changes():
    """If the gate's option set ever changes such that 'proceed' is no
    longer offered, accept-last must NOT silently pick something else;
    it falls through to the fallback handler instead."""
    fallback_calls: list[str] = []

    def fallback(node_id, prompt, options):
        fallback_calls.append(node_id)
        return "abort"

    h = make_escalation_policy_handler("accept-last", fallback=fallback)
    # Gate that doesn't offer 'proceed' — must delegate.
    assert h("escalation_gate", "p", ("abort", "alternative")) == "abort"
    assert fallback_calls == ["escalation_gate"]


async def test_escalation_writes_sidecar_with_reviewer_feedback(log_dir: Path):
    """ADR-0027 Shape B: the escalation-feedback sidecar is written by
    record_needs_human whenever it fires from an escalation_gate route.
    Content must include the reviewer's last verdict + feedback so the
    operator has the open questions in durable markdown form."""
    REVIEWER_FEEDBACK = (
        "Plan is mostly fine but Task #3 needs explicit dependency on "
        "Task #1 — clarify before shipping."
    )
    provider = FakeProvider(
        scripts={
            "planner": [
                {**_leaf_planner_output(), "summary": f"Revision {i}"}
                for i in range(1, ITER_CAP + 1)
            ],
            "plan_reviewer": [
                {"verdict": "revise", "feedback": REVIEWER_FEEDBACK},
            ]
            * ITER_CAP,
        }
    )
    engine = build_engine(
        log_dir,
        item_id=ITEM_ID,
        twig=_twig(),
        provider=provider,
        gate_handler=_proceed_handler,
    )
    result = await engine.run("escalate-sidecar")
    assert isinstance(result, Completed), result

    # Sidecar file exists at the documented path.
    sidecar = log_dir / "escalate-sidecar.escalation-feedback.md"
    assert sidecar.exists(), (
        f"escalation-feedback sidecar should be written when "
        f"record_needs_human fires; expected at {sidecar}"
    )
    body = sidecar.read_text(encoding="utf-8")

    # Must contain the reviewer's actual feedback text.
    assert REVIEWER_FEEDBACK in body, (
        f"sidecar body should embed the reviewer's last feedback; "
        f"body was:\n{body}"
    )
    # Must name the run and the iteration cap.
    assert "escalate-sidecar" in body
    assert f"iteration {ITER_CAP}" in body
    # Must include the "what to do next" guidance.
    assert "What to do next" in body
    assert "approved, structurally aligned" in body
    assert "cannot seed ADO work or enter fanout" in body


async def test_escalation_sidecar_listed_in_plan_record(log_dir: Path):
    """The plan record returned by record_needs_human MUST reference
    the sidecar path so callers (end_to_end.py, dashboards) can find
    it without re-scanning log_dir."""
    provider = FakeProvider(
        scripts={
            "planner": [_leaf_planner_output()] * ITER_CAP,
            "plan_reviewer": [
                {"verdict": "revise", "feedback": "needs more"},
            ]
            * ITER_CAP,
        }
    )
    engine = build_engine(
        log_dir,
        item_id=ITEM_ID,
        twig=_twig(),
        provider=provider,
        gate_handler=_proceed_handler,
    )
    await engine.run("sidecar-in-record")
    completed = completed_from_log(engine.log_path("sidecar-in-record"))
    record = completed["record_needs_human"]["value"]
    assert "escalation_artifact" in record
    assert record["escalation_artifact"] is not None
    assert "escalation-feedback.md" in record["escalation_artifact"]


# ---- ADR-0030 §3a integration ----------------------------------------


async def test_run_emits_run_cost_summary_on_terminal_disposition(log_dir: Path):
    """A complete planning run produces exactly ONE run_cost_summary event
    after the run_completed event (ADR-0030 §3a). The summary aggregates
    receipts attached to every verb_completed outcome.

    This is the integration pin: the kernel hooks (`_emit_cost_summary_once`
    on every emit_run_completed site) actually fire end-to-end against a
    real planner+reviewer workflow, not just in isolation.
    """
    provider = FakeProvider(
        scripts={
            "planner": [_leaf_planner_output()],
            "plan_reviewer": [{"verdict": "approve", "feedback": "LGTM."}],
        }
    )
    engine = build_engine(
        log_dir, item_id=ITEM_ID, twig=_twig(), provider=provider,
    )
    await engine.run("cost-pin")

    from requiem.persistence import replay
    events = list(replay(engine.log_path("cost-pin")))
    summaries = [e for e in events if e["kind"] == "run_cost_summary"]
    assert len(summaries) == 1, (
        f"expected exactly one run_cost_summary; got {len(summaries)}. "
        f"kinds: {[e['kind'] for e in events[-5:]]}"
    )
    # run_cost_summary comes AFTER run_completed (terminal then telemetry).
    rc_idx = next(i for i, e in enumerate(events) if e["kind"] == "run_completed")
    cs_idx = next(i for i, e in enumerate(events) if e["kind"] == "run_cost_summary")
    assert cs_idx > rc_idx, "cost summary must follow run_completed"

    # Payload shape contract.
    payload = summaries[0]["payload"]
    assert "totals" in payload
    assert "per_role" in payload
    assert "per_model" in payload
    totals = payload["totals"]
    assert "input_tokens" in totals
    assert "output_tokens" in totals
    assert "agent_call_count" in totals
    assert "total_latency_ms" in totals
    assert "retry_count" in totals
