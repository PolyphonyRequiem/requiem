"""commit_plan workflow tests — the plan→ADO seeding slice (ADR-0011).

Covers the rubber-duck-hardened design:

* dry-run default previews, writes nothing
* real seed creates the full recursive tree with correct parent linkage + id_map
* idempotent re-run (marker match) creates nothing
* partial-seed recovery: a failed run leaves markers; re-run reuses + finishes
* marker survives a human rename of a seeded item
* pinned proposals are reused, never re-created
* artifact guards: missing / unsupported schema / not-approved / misaligned /
  oversized → end_failed
* ambiguous existing child (dup title, no marker) → human gate
* unclassified twig error → human gate (Ravel L-1)
* end-to-end from a REAL planning artifact (validates Part-1 enrichment at depth)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from requiem.clients.twig import TwigItem, TwigUnknownError
from requiem.kernel import Completed
from requiem.workflows import commit_plan as cp
from requiem.workflows.commit_plan import FakeTwigClient, build_engine
from requiem.workflows.planning import completed_from_log

pytestmark = pytest.mark.asyncio


ROOT = 4242


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs"
    d.mkdir()
    return d


def _write_tree(log_dir: Path, tree: dict | None = None, name: str = "t") -> Path:
    path = log_dir / f"{name}.plan.tree.json"
    path.write_text(json.dumps(tree or cp._demo_tree(ROOT)), encoding="utf-8")
    return path


def _twig_with_root(root_id: int = ROOT) -> FakeTwigClient:
    return FakeTwigClient(items={
        root_id: TwigItem(
            id=root_id, title="Demo root", state="Active",
            area_path="Area", work_item_type="User Story", parent_id=None, raw={},
        ),
    })


def _abort(node_id, prompt, options):
    return "abort" if "abort" in options else options[-1]


_abort.__requiem_auto__ = True  # type: ignore[attr-defined]


def _seed_value(engine, run_id: str) -> dict:
    completed = completed_from_log(engine.log_path(run_id))
    return (completed.get("seed_tree") or {}).get("value") or {}


# ---- dry-run default ----------------------------------------------------


async def test_dry_run_default_previews_and_writes_nothing(log_dir: Path):
    tree = _write_tree(log_dir)
    twig = _twig_with_root()
    engine = build_engine(log_dir, plan_tree_path=tree, twig=twig)  # dry_run defaults True
    result = await engine.run("dry")
    assert isinstance(result, Completed)
    assert result.final_node == "end_success"

    seed = _seed_value(engine, "dry")
    assert seed["dry_run"] is True
    assert seed["would_create_count"] == 4
    assert seed["created_count"] == 0
    assert all(r["status"] == "would_create" for r in seed["ledger"])
    # Nothing actually created in twig (only the pre-existing root remains).
    assert twig.created_titles == []
    assert len(twig.items) == 1


# ---- real seed ----------------------------------------------------------


async def test_real_seed_creates_full_tree_with_linkage_and_idmap(log_dir: Path):
    tree = _write_tree(log_dir)
    twig = _twig_with_root()
    engine = build_engine(log_dir, plan_tree_path=tree, dry_run=False, twig=twig)
    result = await engine.run("seed")
    assert isinstance(result, Completed)
    assert result.final_node == "end_success"

    seed = _seed_value(engine, "seed")
    assert seed["created_count"] == 4
    assert seed["reused_count"] == 0
    # depth-first order: child1, its 2 grandchildren, then child2.
    assert twig.created_titles == [
        "Data layer", "Define schema", "Write migration", "API layer",
    ]
    # id_map covers every synth id.
    assert set(seed["id_map"].keys()) == {"424201", "42420101", "42420102", "424202"}

    # Parent linkage: the two grandchildren hang off the real "Data layer" id.
    data_layer_real = seed["id_map"]["424201"]
    grandchildren = [it for it in twig.items.values() if it.parent_id == data_layer_real]
    assert {g.title for g in grandchildren} == {"Define schema", "Write migration"}
    # Both top-level children hang off the real root.
    top = [it for it in twig.items.values() if it.parent_id == ROOT]
    assert {t.title for t in top} == {"Data layer", "API layer"}

    # Manifest written.
    manifest_path = Path(
        (completed_from_log(engine.log_path("seed"))["write_manifest"]["value"]["manifest_path"])
    )
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["created_count"] == 4
    assert manifest["dry_run"] is False


async def test_idempotent_rerun_creates_nothing(log_dir: Path):
    tree = _write_tree(log_dir)
    twig = _twig_with_root()
    e1 = build_engine(log_dir, plan_tree_path=tree, dry_run=False, twig=twig)
    await e1.run("seed1")
    first = _seed_value(e1, "seed1")

    e2 = build_engine(log_dir, plan_tree_path=tree, dry_run=False, twig=twig)
    result = await e2.run("seed2")
    assert isinstance(result, Completed)
    second = _seed_value(e2, "seed2")
    assert second["created_count"] == 0
    assert second["reused_count"] == 4
    assert all(r["status"] == "reused" for r in second["ledger"])
    assert second["id_map"] == first["id_map"]
    assert len(twig.items) == 5  # root + 4, no duplicates


# ---- recovery / robustness ---------------------------------------------


async def test_partial_seed_then_recovery(log_dir: Path):
    """First run fails creating the LAST child after seeding the first three;
    a clean re-run reuses the three (by marker) and finishes the fourth."""
    tree = _write_tree(log_dir)
    twig = _twig_with_root()
    twig.fail_on_title["API layer"] = TwigUnknownError("boom", exit_code=1, stderr="weird")

    e1 = build_engine(log_dir, plan_tree_path=tree, dry_run=False, twig=twig, gate_handler=_abort)
    r1 = await e1.run("partial")
    assert isinstance(r1, Completed)
    assert r1.final_node == "end_human"
    # Three children already created and persisted in ADO.
    assert twig.created_titles == ["Data layer", "Define schema", "Write migration"]

    # Operator fixes the transient fault; re-run is idempotent + completes.
    del twig.fail_on_title["API layer"]
    e2 = build_engine(log_dir, plan_tree_path=tree, dry_run=False, twig=twig)
    r2 = await e2.run("recover")
    assert isinstance(r2, Completed)
    assert r2.final_node == "end_success"
    seed = _seed_value(e2, "recover")
    assert seed["created_count"] == 1          # only the previously-failed one
    assert seed["reused_count"] == 3           # the rest matched by marker
    assert twig.created_titles[-1] == "API layer"
    assert len([it for it in twig.items.values() if it.id != ROOT]) == 4  # no dups


async def test_marker_survives_human_rename(log_dir: Path):
    tree = _write_tree(log_dir)
    twig = _twig_with_root()
    e1 = build_engine(log_dir, plan_tree_path=tree, dry_run=False, twig=twig)
    await e1.run("seed")
    first = _seed_value(e1, "seed")
    data_layer_real = first["id_map"]["424201"]

    # A human renames the seeded item in ADO (title no longer matches the proposal).
    old = twig.items[data_layer_real]
    twig.items[data_layer_real] = TwigItem(
        id=old.id, title="Data layer (renamed by a human)", state=old.state,
        area_path=old.area_path, work_item_type=old.work_item_type,
        parent_id=old.parent_id, raw=old.raw,  # raw still carries the marker
    )

    e2 = build_engine(log_dir, plan_tree_path=tree, dry_run=False, twig=twig)
    await e2.run("reseed")
    second = _seed_value(e2, "reseed")
    # Marker match ignores the title change → reused, not duplicated.
    assert second["created_count"] == 0
    assert second["id_map"]["424201"] == data_layer_real


async def test_pinned_proposal_is_reused_not_created(log_dir: Path):
    # A flat tree with one pinned child (already exists in ADO) + one normal child.
    pinned_id = 7777
    tree = {
        "schema_version": 2, "plan_id": "plan-pin", "item_id": ROOT,
        "decomposable": True, "verdict": "approved",
        "proposals": [
            {"title": "Existing", "description": "d", "work_item_type": "Task", "item_id": pinned_id},
            {"title": "Fresh", "description": "d", "work_item_type": "Task"},
        ],
        "children": [
            {"item_id": pinned_id, "plan_id": "p", "decomposable": False,
             "summary": "", "review_iterations": 1, "final_verdict": "approved",
             "proposals": [], "children": []},
            {"item_id": ROOT * 100 + 2, "plan_id": "p", "decomposable": False,
             "summary": "", "review_iterations": 1, "final_verdict": "approved",
             "proposals": [], "children": []},
        ],
    }
    path = _write_tree(log_dir, tree, name="pin")
    twig = _twig_with_root()
    twig.items[pinned_id] = TwigItem(
        id=pinned_id, title="Existing", state="Active", area_path="Area",
        work_item_type="Task", parent_id=ROOT, raw={},
    )
    engine = build_engine(log_dir, plan_tree_path=path, dry_run=False, twig=twig)
    result = await engine.run("pin")
    assert isinstance(result, Completed)
    seed = _seed_value(engine, "pin")
    assert seed["created_count"] == 1                 # only "Fresh"
    assert seed["id_map"][str(pinned_id)] == pinned_id
    assert twig.created_titles == ["Fresh"]


# ---- artifact guards (load_tree) ---------------------------------------


async def test_policy_forced_leaf_child_passes_validation(log_dir: Path):
    """ADR-0025 Gap A* regression pin: when the planning workflow's
    short-circuit produces a child with `final_verdict='policy-forced-leaf'`,
    commit_plan's load_tree validator must accept it as terminal. Pre-fix,
    the validator only allowed None and 'approved', rejecting any
    short-circuited child as 'not approved' and aborting the entire
    commit_plan run (this is what broke the SKU-fallback dogfood run 6,
    2026-06-17 — top-level plan reached commit_plan but load_tree rejected
    every Gap-A-short-circuited Task child)."""
    tree = {
        "schema_version": 2, "plan_id": "plan-pf", "item_id": ROOT,
        "decomposable": True, "verdict": "approved",
        "proposals": [
            {"title": "Implementable Task", "description": "d", "work_item_type": "Task"},
        ],
        "children": [
            {
                "item_id": ROOT * 100 + 1, "plan_id": "p",
                "decomposable": False,
                "summary": "Implementable Task",
                "review_iterations": 0,
                # The synthetic verdict from the Gap A short-circuit.
                "final_verdict": "policy-forced-leaf",
                "proposals": [], "children": [],
            },
        ],
    }
    path = _write_tree(log_dir, tree, name="pf")
    engine = build_engine(
        log_dir, plan_tree_path=path, dry_run=False, twig=_twig_with_root()
    )
    result = await engine.run("pf")
    assert isinstance(result, Completed)
    # The whole point: don't fail at load_tree on validation_failed.
    assert result.final_node != "end_failed", (
        f"policy-forced-leaf must be accepted by load_tree; "
        f"final={result.final_node}"
    )


async def test_needs_human_decomposable_child_passes_validation(log_dir: Path):
    """ADR-0027 regression pin: when --on-escalate=accept-last fires on a
    decomposable child, the planning workflow ships the last planner output
    as final_verdict='needs_human'. The child carries the planner's
    proposals (e.g. 5 grandchildren the reviewer kept rejecting) but ZERO
    committed children — the workflow short-circuited before recursing.

    commit_plan's load_tree validator must:
      1. Accept 'needs_human' as terminal (don't reject as 'not approved')
      2. NOT descend into a needs_human child (its `children: []` is
         intentional, not 'missing 5 proposals'-misalignment)

    This is exactly what broke SKU-fallback dogfood run 21 (2026-06-21):
    one of the five Deliverables had reviewer escalate, accept-last fired,
    children remained empty, and load_tree errored with both
    \"final_verdict 'needs_human' is not approved\" AND
    \"0 children != 5 proposals — artifact misaligned\".
    """
    tree = {
        "schema_version": 2, "plan_id": "plan-nh", "item_id": ROOT,
        "decomposable": True, "verdict": "approved",
        "proposals": [
            # Two siblings: one cleanly approved, one needs_human.
            {"title": "Clean Deliverable", "description": "d",
             "work_item_type": "Deliverable"},
            {"title": "Contentious Deliverable", "description": "d",
             "work_item_type": "Deliverable"},
        ],
        "children": [
            {
                "item_id": ROOT * 100 + 1, "plan_id": "p1",
                "decomposable": True,
                "summary": "Clean Deliverable",
                "review_iterations": 1,
                "final_verdict": "approved",
                "proposals": [
                    {"title": "Task 1", "description": "d", "work_item_type": "Task"},
                ],
                "children": [
                    {
                        "item_id": (ROOT * 100 + 1) * 100 + 1, "plan_id": "p1c1",
                        "decomposable": False, "summary": "Task 1",
                        "review_iterations": 0, "final_verdict": "policy-forced-leaf",
                        "proposals": [], "children": [],
                    },
                ],
            },
            {
                "item_id": ROOT * 100 + 2, "plan_id": "p2",
                "decomposable": True,
                "summary": "Contentious Deliverable",
                "review_iterations": 8,  # hit ITER_CAP
                # The accept-last verdict written by ADR-0027.
                "final_verdict": "needs_human",
                # Planner's last output: 5 proposed Tasks the reviewer
                # kept escalating. NEVER expanded into children because
                # accept-last short-circuits before recursion.
                "proposals": [
                    {"title": f"Task {i}", "description": "d",
                     "work_item_type": "Task"}
                    for i in range(1, 6)
                ],
                "children": [],  # intentionally empty
            },
        ],
    }
    path = _write_tree(log_dir, tree, name="nh")
    engine = build_engine(
        log_dir, plan_tree_path=path, dry_run=False, twig=_twig_with_root()
    )
    result = await engine.run("nh")
    assert isinstance(result, Completed)
    # The whole point: don't fail at load_tree validation.
    assert result.final_node != "end_failed", (
        f"needs_human decomposable child must be accepted by load_tree "
        f"AND its empty children must NOT be flagged as misaligned; "
        f"final={result.final_node}"
    )


async def test_missing_artifact_routes_to_end_failed(log_dir: Path):
    engine = build_engine(log_dir, plan_tree_path=log_dir / "nope.json", dry_run=False, twig=_twig_with_root())
    result = await engine.run("missing")
    assert isinstance(result, Completed)
    assert result.final_node == "end_failed"


async def test_unsupported_schema_routes_to_end_failed(log_dir: Path):
    tree = cp._demo_tree(ROOT)
    tree["schema_version"] = 1  # pre-enrichment artifact
    path = _write_tree(log_dir, tree, name="old")
    engine = build_engine(log_dir, plan_tree_path=path, dry_run=False, twig=_twig_with_root())
    result = await engine.run("old")
    assert result.final_node == "end_failed"


async def test_unknown_verdict_routes_to_end_failed(log_dir: Path):
    """commit_plan accepts `verdict in {"approved", "needs_human"}` —
    the `needs_human` verdict shipped by ``--on-escalate accept-last``
    must NOT bounce here (the escalation policy already gated the
    workflow at the planning phase; commit_plan trusts that decision).

    Any OTHER verdict (e.g. ``"rejected"``, ``"abort"``, ``"unknown"``)
    is still a hard fail at load_tree."""
    tree = cp._demo_tree(ROOT)
    tree["verdict"] = "rejected"
    path = _write_tree(log_dir, tree, name="rejected")
    engine = build_engine(log_dir, plan_tree_path=path, dry_run=False, twig=_twig_with_root())
    result = await engine.run("rejected")
    assert result.final_node == "end_failed"


async def test_needs_human_verdict_proceeds_through_commit(log_dir: Path):
    """ADR-0027 (accept-last): a tree with ``verdict=needs_human`` is
    a valid commit_plan input. The operator's escalation policy already
    decided to ship; commit_plan honors that and proceeds to seed.

    This is the load-bearing path for the `--on-escalate accept-last`
    dogfood: the planner's last-good plan rides through with a
    needs_human verdict and commit_plan seeds ADO children as normal."""
    tree = cp._demo_tree(ROOT)
    tree["verdict"] = "needs_human"
    path = _write_tree(log_dir, tree, name="nh")
    engine = build_engine(log_dir, plan_tree_path=path, dry_run=False, twig=_twig_with_root())
    result = await engine.run("nh")
    assert result.final_node == "end_success"


async def test_misaligned_tree_fails_validation(log_dir: Path):
    tree = cp._demo_tree(ROOT)
    # Corrupt alignment: drop one child so len(children) != len(proposals).
    tree["children"] = tree["children"][:1]
    path = _write_tree(log_dir, tree, name="bad")
    engine = build_engine(log_dir, plan_tree_path=path, dry_run=False, twig=_twig_with_root())
    result = await engine.run("bad")
    assert result.final_node == "end_failed"


async def test_synth_id_mismatch_fails_validation(log_dir: Path):
    tree = cp._demo_tree(ROOT)
    tree["children"][0]["item_id"] = 999999  # no longer the expected synth id
    path = _write_tree(log_dir, tree, name="mismatch")
    engine = build_engine(log_dir, plan_tree_path=path, dry_run=False, twig=_twig_with_root())
    result = await engine.run("mismatch")
    assert result.final_node == "end_failed"


async def test_size_cap_refuses_oversized_tree(log_dir: Path):
    tree = _write_tree(log_dir)
    engine = build_engine(
        log_dir, plan_tree_path=tree, dry_run=False, twig=_twig_with_root(), max_creates=2
    )
    result = await engine.run("toobig")
    assert result.final_node == "end_failed"


# ---- runtime gates ------------------------------------------------------


async def test_ambiguous_existing_child_routes_to_human(log_dir: Path):
    tree = _write_tree(log_dir)
    twig = _twig_with_root()
    # Two un-markered children under root with the same (title, type) as proposal[0].
    for cid in (5001, 5002):
        twig.items[cid] = TwigItem(
            id=cid, title="Data layer", state="New", area_path="Area",
            work_item_type="Task", parent_id=ROOT, raw={"description": "hand-made"},
        )
    engine = build_engine(log_dir, plan_tree_path=tree, dry_run=False, twig=twig, gate_handler=_abort)
    result = await engine.run("ambig")
    assert isinstance(result, Completed)
    assert result.final_node == "end_human"


async def test_unclassified_twig_error_routes_to_human(log_dir: Path):
    tree = _write_tree(log_dir)
    twig = _twig_with_root()
    twig.fail_on_title["Data layer"] = TwigUnknownError("kaboom", exit_code=1, stderr="???")
    engine = build_engine(log_dir, plan_tree_path=tree, dry_run=False, twig=twig, gate_handler=_abort)
    result = await engine.run("unknown")
    assert isinstance(result, Completed)
    assert result.final_node == "end_human"


# ---- end-to-end from a real planning artifact (validates Part-1) --------


async def test_seeds_a_real_planning_artifact(log_dir: Path):
    """Produce a genuine `.plan.tree.json` via the planning workflow (2 levels
    deep), then commit it. This proves the Part-1 enrichment carried grandchild
    proposals through serialization — without it, the grandchildren could not be
    created from the artifact alone."""
    from requiem.agent import FakeProvider
    from requiem.workflows.planning import build_engine as plan_engine

    R = 12345
    A, B = R * 100 + 1, R * 100 + 2
    A1, A2 = A * 100 + 1, A * 100 + 2

    def leaf():
        return {"summary": "leaf", "decomposable": False, "children": [],
                "estimated_complexity": "small", "rationale": "atomic"}

    root_out = {
        "summary": "two parts", "decomposable": True,
        "children": [
            {"title": "Part A", "description": "decomposable", "work_item_type": "Task"},
            {"title": "Part B", "description": "leaf", "work_item_type": "Task"},
        ],
        "estimated_complexity": "medium", "rationale": "two distinct parts",
    }
    a_out = {
        "summary": "split A", "decomposable": True,
        "children": [
            {"title": "A-one", "description": "g1", "work_item_type": "Task"},
            {"title": "A-two", "description": "g2", "work_item_type": "Task"},
        ],
        "estimated_complexity": "small", "rationale": "two sub-parts",
    }
    provider = FakeProvider(scripts={
        "planner": [root_out, a_out, leaf(), leaf(), leaf()],
        "plan_reviewer": [{"verdict": "approve", "feedback": "ok"}] * 5,
    })

    # Pre-register the synthesised ids the recursion fetches.
    from requiem.workflows.planning import FakeTwigClient as PlanFakeTwig
    plan_twig = PlanFakeTwig(items={})
    for iid, title, parent in [
        (R, "Root", None), (A, "Part A", R), (B, "Part B", R),
        (A1, "A-one", A), (A2, "A-two", A),
    ]:
        plan_twig.items[iid] = TwigItem(
            id=iid, title=title, state="New", area_path="Area",
            work_item_type="Task", parent_id=parent, raw={},
        )

    pe = plan_engine(log_dir, item_id=R, twig=plan_twig, provider=provider)
    plan_result = await pe.run("planrun")
    assert isinstance(plan_result, Completed)
    artifact = log_dir / "planrun.plan.tree.json"
    assert artifact.exists()

    # Now commit that artifact into a fresh ADO (only the real root exists).
    commit_twig = _twig_with_root(R)
    ce = build_engine(log_dir, plan_tree_path=artifact, dry_run=False, twig=commit_twig)
    commit_result = await ce.run("commitrun")
    assert isinstance(commit_result, Completed)
    assert commit_result.final_node == "end_success"

    seed = _seed_value(ce, "commitrun")
    assert seed["created_count"] == 4
    # The two grandchildren only exist because their proposals survived into the
    # artifact at depth (Part-1 enrichment).
    assert set(commit_twig.created_titles) == {"Part A", "Part B", "A-one", "A-two"}
    part_a_real = seed["id_map"][str(A)]
    gkids = [it for it in commit_twig.items.values() if it.parent_id == part_a_real]
    assert {g.title for g in gkids} == {"A-one", "A-two"}
