"""Recursive-planning tests — Fauré seat 2 (Phase C).

Pins the recursion topology added on top of the seat-1 flat workflow:

* single-level decomposable (root → 3 leaf children)
* two-level decomposable (root → 1 middle decomposable → 2 leaves)
* ``max_depth`` cap rejects depth-3 attempt → ``recursion_depth_gate``
* cycle detection when a child's item_id matches the root → ``cycle_gate``
* INV-SUBWORKFLOW-LOG-ISOLATION across 2 levels
* INV-RESTART across 2 levels: crash mid-grandchild, resume to identical
  terminal state and serialised plan tree.

Each scenario uses :class:`FakeProvider` so planner/reviewer outputs are
fully deterministic. The fake twig is pre-populated with every item id
the recursion will fetch (synth child ids follow the ``parent*100 +
slot`` convention from :func:`_synth_child_id`).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from requiem.agent import FakeProvider
from requiem.clients.twig import TwigItem
from requiem.kernel import Completed
from requiem.persistence import replay
from requiem.workflows.planning import (
    FakeTwigClient,
    PlanResult,
    build_engine,
    completed_from_log,
    project_plan_result,
    verdict_card,
)


# ---- fixtures ----------------------------------------------------------


ROOT_ID = 12345


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs"
    d.mkdir()
    return d


def _proceed_handler(node_id: str, prompt: str, options: tuple[str, ...]) -> str:
    return "proceed" if "proceed" in options else options[0]


_proceed_handler.__requiem_auto__ = True  # type: ignore[attr-defined]


def _twig_with(*item_ids: int) -> FakeTwigClient:
    """Build a FakeTwigClient that returns a stub TwigItem for each id."""
    items = {
        i: TwigItem(
            id=i,
            title=f"Item {i}",
            state="New",
            area_path="Polyphony\\Engine",
            work_item_type=("User Story" if i == ROOT_ID else "Task"),
            parent_id=None,
            raw={},
        )
        for i in item_ids
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


def _decomp(*titles_or_children: str | dict, summary: str = "decomposable") -> dict:
    """Build a decomposable planner output.

    Accepts either ``str`` (treated as a child title) or a full child
    dict (so callers can pin ``item_id`` for cycle scenarios).
    """
    children = []
    for entry in titles_or_children:
        if isinstance(entry, str):
            children.append({
                "title": entry,
                "description": f"{entry} description",
                "work_item_type": "Task",
            })
        else:
            children.append(entry)
    return {
        "summary": summary,
        "decomposable": True,
        "children": children,
        "estimated_complexity": "medium",
        "rationale": "separable seams",
    }


def _approve(feedback: str = "ok") -> dict:
    return {"verdict": "approve", "feedback": feedback}


# ---- scenarios ---------------------------------------------------------


async def test_single_level_three_children_all_leaves(log_dir: Path):
    """Root decomposable into 3 leaves; PlanResult has 3 leaf children."""
    child_ids = [ROOT_ID * 100 + i for i in (1, 2, 3)]
    provider = FakeProvider(
        scripts={
            "planner": [
                _decomp("A", "B", "C"),
                _leaf("a"),
                _leaf("b"),
                _leaf("c"),
            ],
            "plan_reviewer": [_approve(), _approve(), _approve(), _approve()],
        }
    )
    twig = _twig_with(ROOT_ID, *child_ids)
    engine = build_engine(
        log_dir, item_id=ROOT_ID, twig=twig, provider=provider
    )
    result = await engine.run("rec1")
    assert isinstance(result, Completed), result
    assert result.final_node == "end"

    plan = project_plan_result(completed_from_log(engine.log_path("rec1")))
    assert plan is not None
    assert plan.decomposable is True
    assert plan.review_iterations == 1
    assert {c.item_id for c in plan.children} == set(child_ids)
    assert all(c.decomposable is False for c in plan.children)
    assert all(c.children == [] for c in plan.children)

    # Three sub-run logs exist (INV-SUBWORKFLOW-LOG-ISOLATION).
    sub_logs = sorted(p.name for p in log_dir.glob("rec1__child_*.events.jsonl"))
    assert sub_logs == [
        "rec1__child_1.events.jsonl",
        "rec1__child_2.events.jsonl",
        "rec1__child_3.events.jsonl",
    ]


async def test_two_level_decomposable_grandchildren(log_dir: Path):
    """Root decomposable → middle child decomposable → 2 leaf grandchildren."""
    # Layer 1: root proposes one child (middle).
    middle_id = ROOT_ID * 100 + 1
    # Layer 2: middle proposes 2 grandchildren.
    grand_ids = [middle_id * 100 + 1, middle_id * 100 + 2]

    provider = FakeProvider(
        scripts={
            "planner": [
                _decomp("middle"),           # root planner
                _decomp("g1", "g2"),         # middle planner
                _leaf("g1"),                 # grandchild 1
                _leaf("g2"),                 # grandchild 2
            ],
            "plan_reviewer": [
                _approve(), _approve(), _approve(), _approve(),
            ],
        }
    )
    twig = _twig_with(ROOT_ID, middle_id, *grand_ids)
    engine = build_engine(log_dir, item_id=ROOT_ID, twig=twig, provider=provider)
    result = await engine.run("rec2")
    assert isinstance(result, Completed), result

    plan = project_plan_result(completed_from_log(engine.log_path("rec2")))
    assert plan is not None
    assert plan.decomposable is True
    assert len(plan.children) == 1
    middle = plan.children[0]
    assert middle.item_id == middle_id
    assert middle.decomposable is True
    assert len(middle.children) == 2
    assert {g.item_id for g in middle.children} == set(grand_ids)
    assert all(g.decomposable is False for g in middle.children)

    # Verdict card mentions the two-level tree.
    card = verdict_card(completed_from_log(engine.log_path("rec2")))
    assert card is not None
    assert f"AB#{ROOT_ID}" in card

    # Logs exist at every layer (parent + middle + 2 grandchildren = 4).
    all_logs = sorted(p.name for p in log_dir.glob("rec2*.events.jsonl"))
    # parent + 1 middle + 2 grand = 4
    assert len(all_logs) == 4, all_logs


async def test_max_depth_2_three_level_proposed_routes_to_human(log_dir: Path):
    """At depth 2 a decomposable proposal would push to depth 3.

    With ``max_depth=2``, ``branch_decomposable`` at depth 2 emits
    ``recursion_depth_exceeded`` and the workflow routes to
    ``recursion_depth_gate``; the proceed handler accepts as
    needs-human, and the run terminates at ``end_needs_human``.
    """
    middle_id = ROOT_ID * 100 + 1
    grand_id = middle_id * 100 + 1

    provider = FakeProvider(
        scripts={
            "planner": [
                _decomp("middle"),       # root (depth 0)
                _decomp("would-be-g"),   # middle (depth 1) → still allowed (depth 2 child)
                _decomp("would-be-gg"),  # grandchild (depth 2) → proposes depth 3
            ],
            "plan_reviewer": [_approve(), _approve(), _approve()],
        }
    )
    twig = _twig_with(ROOT_ID, middle_id, grand_id)
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        max_depth=2,
        gate_handler=_proceed_handler,
    )
    result = await engine.run("recdep")
    assert isinstance(result, Completed), result
    # The grandchild terminated at end_needs_human because its
    # branch_decomposable saw current_depth=2 + 1 > max_depth=2.
    # That permanent_failure bubbled up via the escalation_gate
    # in the parent layers — but since the proceed handler was
    # used at every gate, the root terminates cleanly too.
    plan = project_plan_result(completed_from_log(engine.log_path("recdep")))
    assert plan is not None
    # The grandchild's plan record was written via record_needs_human,
    # so its final_verdict is "needs_human".
    assert plan.children[0].children[0].final_verdict == "needs_human"


async def test_cycle_detection_child_proposes_ancestor_id(log_dir: Path):
    """A child whose pinned item_id matches the root triggers cycle_gate."""
    # The root proposes ONE child that explicitly pins the root's own id.
    cyclic_child = {
        "title": "Recursive self-reference",
        "description": "Pretend this work item depends on itself.",
        "work_item_type": "Task",
        "item_id": ROOT_ID,
    }
    provider = FakeProvider(
        scripts={
            "planner": [_decomp(cyclic_child)],
            "plan_reviewer": [_approve()],
        }
    )
    twig = _twig_with(ROOT_ID)
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        gate_handler=_proceed_handler,
    )
    result = await engine.run("reccyc")
    assert isinstance(result, Completed), result
    # Cycle was detected at prep_child_1 → cycle_gate → proceed →
    # record_needs_human → end_needs_human.
    assert result.final_node == "end_needs_human"

    # Verify the cycle_detected failure was actually recorded.
    events = list(replay(engine.log_path("reccyc")))
    saw_cycle = any(
        ev.get("kind") == "verb_completed"
        and ev.get("node_id") == "prep_child_1"
        and (
            ((ev.get("payload") or {}).get("outcome") or {}).get("error_kind")
            == "cycle_detected"
        )
        for ev in events
    )
    assert saw_cycle, "expected prep_child_1 to emit cycle_detected"

    # No sub-run was actually spawned for the cyclic child.
    sub_logs = list(log_dir.glob("reccyc__child_*.events.jsonl"))
    assert sub_logs == []


async def test_inv_subworkflow_log_isolation_2level(log_dir: Path):
    """Each layer's log only contains events for its own run_id.

    Pins INV-SUBWORKFLOW-LOG-ISOLATION across 2 levels of recursion: the
    root log has no events tagged with the middle's run_id, the middle's
    log has none from the grandchildren, and so on.
    """
    middle_id = ROOT_ID * 100 + 1
    grand_id = middle_id * 100 + 1

    provider = FakeProvider(
        scripts={
            "planner": [
                _decomp("middle"),
                _decomp("g1"),
                _leaf("g1"),
            ],
            "plan_reviewer": [_approve(), _approve(), _approve()],
        }
    )
    twig = _twig_with(ROOT_ID, middle_id, grand_id)
    engine = build_engine(log_dir, item_id=ROOT_ID, twig=twig, provider=provider)
    result = await engine.run("reciso")
    assert isinstance(result, Completed), result

    root_log = log_dir / "reciso.events.jsonl"
    middle_log = log_dir / "reciso__child_1.events.jsonl"
    grand_log = log_dir / "reciso__child_1__child_1.events.jsonl"
    for p in (root_log, middle_log, grand_log):
        assert p.exists(), f"missing layer log: {p.name}"

    def _run_ids(path: Path) -> set[str]:
        return {ev.get("run_id") for ev in replay(path) if ev.get("run_id")}

    root_ids = _run_ids(root_log)
    middle_ids = _run_ids(middle_log)
    grand_ids = _run_ids(grand_log)

    assert root_ids == {"reciso"}, root_ids
    assert middle_ids == {"reciso__child_1"}, middle_ids
    assert grand_ids == {"reciso__child_1__child_1"}, grand_ids


async def test_inv_restart_2level_crash_mid_grandchild(log_dir: Path):
    """Truncate the grandchild's log mid-run; resume to identical terminal.

    Pins INV-RESTART for 2-level recursion. Strategy:

    1. Run a 2-level happy path to completion with `FakeProvider`
       scripted with exactly the right number of LLM calls.
    2. Truncate the grandchild's log to just after its
       ``reviewer_1.verb_completed`` event.
    3. Resume with a fresh engine that has ZERO scripted responses —
       if any layer re-runs an LLM agent, FakeProvider exhausts and
       the test fails loudly.
    4. Assert terminal disposition and reconstructed PlanResult match.
    """
    middle_id = ROOT_ID * 100 + 1
    grand_id = middle_id * 100 + 1
    run_id = "recres"

    # ---- first run: full happy path
    provider1 = FakeProvider(
        scripts={
            "planner": [
                _decomp("middle"),
                _decomp("g1"),
                _leaf("g1"),
            ],
            "plan_reviewer": [_approve(), _approve(), _approve()],
        }
    )
    twig = _twig_with(ROOT_ID, middle_id, grand_id)
    engine1 = build_engine(log_dir, item_id=ROOT_ID, twig=twig, provider=provider1)
    result1 = await engine1.run(run_id)
    assert isinstance(result1, Completed), result1

    root_log = engine1.log_path(run_id)
    middle_log = log_dir / f"{run_id}__child_1.events.jsonl"
    grand_log = log_dir / f"{run_id}__child_1__child_1.events.jsonl"
    assert middle_log.exists() and grand_log.exists()

    plan1 = project_plan_result(completed_from_log(root_log))
    assert plan1 is not None

    # ---- truncate the grandchild's log to just after reviewer_1's
    # verb_completed. Anything written by the middle or root layer
    # AFTER the grandchild completed will also be invalid context, so
    # also truncate the middle and root logs back to the point the
    # grandchild's subworkflow was started (so the resume re-enters
    # the grandchild fresh from the middle's perspective).
    def _truncate_after(path: Path, predicate) -> int:
        lines = path.read_text(encoding="utf-8").splitlines()
        keep: list[str] = []
        for raw in lines:
            keep.append(raw)
            if predicate(json.loads(raw)):
                break
        else:
            pytest.fail(f"truncation predicate never matched in {path.name}")
        path.write_text("\n".join(keep) + "\n", encoding="utf-8")
        return len(keep)

    # Grandchild: keep through reviewer_1.verb_completed.
    _truncate_after(
        grand_log,
        lambda ev: (
            ev.get("kind") == "verb_completed"
            and ev.get("node_id") == "reviewer_1"
        ),
    )
    # Middle: keep through child_1.subworkflow_started (so on resume
    # the kernel re-enters child_1 and reads the truncated grandchild
    # log via its persisted cursor).
    _truncate_after(
        middle_log,
        lambda ev: (
            ev.get("kind") == "subworkflow_started"
            and ev.get("node_id") == "child_1"
        ),
    )
    # Root: keep through child_1.subworkflow_started.
    _truncate_after(
        root_log,
        lambda ev: (
            ev.get("kind") == "subworkflow_started"
            and ev.get("node_id") == "child_1"
        ),
    )

    # Delete sidecar artefacts so resume must rewrite them.
    for p in list(log_dir.iterdir()):
        if p.suffix in {".md", ".json"} and p.name.startswith(run_id):
            p.unlink()

    # ---- resume with ZERO scripted responses for already-completed
    # iterations. The grandchild only needs to run record_plan (which
    # doesn't call the LLM); the middle and root layers re-enter their
    # subworkflow nodes (which the kernel resumes from the cursor) and
    # then run aggregate_children + record_plan. NONE of these call the
    # provider, so an empty FakeProvider is sufficient.
    provider2 = FakeProvider(
        scripts={"planner": [], "plan_reviewer": []}
    )
    engine2 = build_engine(log_dir, item_id=ROOT_ID, twig=twig, provider=provider2)
    result2 = await engine2.run(run_id)
    assert isinstance(result2, Completed), result2
    assert result2.final_node == result1.final_node
    assert result2.disposition == result1.disposition
    assert provider2.calls == [], (
        f"resume must not consult the LLM; got calls: {provider2.calls}"
    )

    plan2 = project_plan_result(completed_from_log(root_log))
    assert plan2 is not None
    assert plan2 == plan1, (plan1, plan2)
