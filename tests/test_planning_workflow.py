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
from requiem.outcomes import BadOutput
from requiem.persistence import replay
from requiem.workflows.planning import (
    FakeTwigClient,
    ITER_CAP,
    PlanResult,
    build_engine,
    build_workflow,
    completed_from_log,
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


async def test_three_revisions_escalates_to_human(log_dir: Path):
    """Reviewer revises every iteration → router_3 escalates → human gate."""
    provider = FakeProvider(
        scripts={
            "planner": [_leaf_planner_output()] * ITER_CAP,
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


async def test_three_revisions_then_operator_aborts(log_dir: Path):
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


# ---- workflow topology smoke ------------------------------------------


def test_workflow_topology_validates():
    """`.build()` already runs topology validation; spot-check explicitly."""
    wf = build_workflow()
    assert wf.name == "planning"
    assert wf.entry == "start"
    errs = wf.validate_topology()
    assert errs == [], errs

    node_ids = {n.node_id for n in wf.nodes}
    # Three planner/reviewer/router triples.
    for i in range(1, ITER_CAP + 1):
        assert f"planner_{i}" in node_ids
        assert f"reviewer_{i}" in node_ids
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
