"""Root-dispatch workflow tests — Haydn (Phase C).

Covers the Phase C brief's six required cases:

1. happy auto_plan=True → ``end_planned``; manifest carries plan record
2. auto_plan=False     → ``end_dispatched``; manifest written; no child log
3. not-a-root + reject → ``end_human``; no manifest, no child log
4. idempotent re-dispatch → second run reuses manifest, no clobber
5. dry_run=True        → no disk writes; verdict card still renders
6. INV-RESTART         → truncate parent log mid-subworkflow, resume cleanly
7. INV-SUBWORKFLOW-LOG-ISOLATION → parent log only carries parent run_id,
   child log only carries sub_run_id (no cross-write)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from requiem.agent import FakeProvider
from requiem.clients.twig import TwigItem
from requiem.kernel import Completed, Failed
from requiem.persistence import replay
from requiem.workflows import planning as _planning
from requiem.workflows.root_dispatch import (
    FakeTwigClient,
    RootDispatchInputs,
    RootDispatchResult,
    build_engine,
    build_workflow,
    completed_from_log,
    verdict_card,
)


# ---- fixtures ---------------------------------------------------------


ROOT_ITEM_ID = 4242
PARENT_FEATURE_ID = 4200
PARENT_TASK_ID = 4001  # NOT a root-tier parent (Task)


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs"
    d.mkdir()
    return d


def _root_item(item_id: int = ROOT_ITEM_ID, *, parent_id: int | None = None,
               work_item_type: str = "User Story") -> TwigItem:
    return TwigItem(
        id=item_id,
        title=f"Root work item {item_id}",
        state="Active",
        area_path="PolyphonyRequiem\\v0",
        work_item_type=work_item_type,
        parent_id=parent_id,
        raw={},
    )


def _feature_item(item_id: int = PARENT_FEATURE_ID) -> TwigItem:
    return TwigItem(
        id=item_id,
        title="Parent feature",
        state="Active",
        area_path="PolyphonyRequiem\\v0",
        work_item_type="Feature",
        parent_id=None,
        raw={},
    )


def _task_parent(item_id: int = PARENT_TASK_ID) -> TwigItem:
    return TwigItem(
        id=item_id,
        title="Mid-tier task — not a root parent",
        state="Active",
        area_path="PolyphonyRequiem\\v0",
        work_item_type="Task",
        parent_id=None,
        raw={},
    )


def _twig_one_root() -> FakeTwigClient:
    return FakeTwigClient(items={ROOT_ITEM_ID: _root_item()})


def _twig_with_feature_parent() -> FakeTwigClient:
    return FakeTwigClient(
        items={
            ROOT_ITEM_ID: _root_item(parent_id=PARENT_FEATURE_ID),
            PARENT_FEATURE_ID: _feature_item(),
        }
    )


def _twig_with_task_parent() -> FakeTwigClient:
    return FakeTwigClient(
        items={
            ROOT_ITEM_ID: _root_item(parent_id=PARENT_TASK_ID),
            PARENT_TASK_ID: _task_parent(),
        }
    )


def _planning_provider() -> FakeProvider:
    """Scripts the planner+reviewer for a happy-leaf plan."""
    return FakeProvider(
        scripts={
            "planner": [{
                "summary": "Atomic refactor; no children.",
                "decomposable": False,
                "children": [],
                "estimated_complexity": "small",
                "rationale": "Localised change.",
            }],
            "plan_reviewer": [{
                "verdict": "approve",
                "feedback": "Looks scoped correctly.",
            }],
        }
    )


def _reject_handler(node_id: str, prompt: str, options: tuple[str, ...]) -> str:
    return "reject" if "reject" in options else options[-1]


_reject_handler.__requiem_auto__ = True  # type: ignore[attr-defined]


def _force_root_handler(node_id: str, prompt: str,
                        options: tuple[str, ...]) -> str:
    return "force-root" if "force-root" in options else options[0]


_force_root_handler.__requiem_auto__ = True  # type: ignore[attr-defined]


def _proceed_handler(node_id: str, prompt: str,
                     options: tuple[str, ...]) -> str:
    """For the planning sub-workflow's own gates (proceed / abort)."""
    return "proceed" if "proceed" in options else options[0]


_proceed_handler.__requiem_auto__ = True  # type: ignore[attr-defined]


# ---- 1. happy auto_plan=True --------------------------------------


async def test_auto_plan_true_runs_planning_and_records_outcome(log_dir: Path):
    inputs = RootDispatchInputs(
        item_id=ROOT_ITEM_ID,
        repo="acme/widgets",
        repo_path=Path("."),
        base_branch="main",
        dry_run=False,
        auto_plan=True,
    )
    twig = _twig_one_root()
    engine = build_engine(
        log_dir,
        inputs=inputs,
        twig=twig,
        provider=_planning_provider(),
        gate_handler=_proceed_handler,
        today="2026-06-01",
    )
    run_id = "auto-plan-happy"
    result = await engine.run(run_id)
    assert isinstance(result, Completed), result
    assert result.disposition == "completed"
    assert result.final_node == "end_planned"

    log_path = engine.log_path(run_id)
    completed = completed_from_log(log_path)

    # Result projection populated.
    rdr = RootDispatchResult.from_completed(completed)
    assert rdr is not None
    assert rdr.item_id == ROOT_ITEM_ID
    assert rdr.run_id == f"root-{ROOT_ITEM_ID}-2026-06-01"
    assert rdr.plan_verdict == "approved"
    assert rdr.plan_tree_id and rdr.plan_tree_id.startswith("plan-")

    # Manifest file exists and contains plan record.
    manifest_path = log_dir / ".runs" / f"{rdr.run_id}.manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["item_id"] == ROOT_ITEM_ID
    assert manifest["repo"] == "acme/widgets"
    assert manifest["plan_tree_id"] == rdr.plan_tree_id
    assert manifest["plan_verdict"] == "approved"
    assert len(manifest["child_run_ids"]) == 1
    sub_run_id = manifest["child_run_ids"][0]

    # Child log was actually written by the sub-workflow.
    child_log = log_dir / f"{sub_run_id}.events.jsonl"
    assert child_log.exists()

    # Verdict card renders the planned variant.
    card = verdict_card(completed)
    assert card is not None
    assert "Dispatched + planned" in card
    assert "approved" in card


# ---- 2. auto_plan=False ------------------------------------------


async def test_auto_plan_false_terminates_at_dispatched(log_dir: Path):
    inputs = RootDispatchInputs(
        item_id=ROOT_ITEM_ID,
        repo_path=Path("."),
        dry_run=False,
        auto_plan=False,
    )
    engine = build_engine(
        log_dir,
        inputs=inputs,
        twig=_twig_one_root(),
        today="2026-06-02",
    )
    run_id = "no-auto-plan"
    result = await engine.run(run_id)
    assert isinstance(result, Completed)
    assert result.final_node == "end_dispatched"

    completed = completed_from_log(engine.log_path(run_id))
    rdr = RootDispatchResult.from_completed(completed)
    assert rdr is not None
    assert rdr.plan_tree_id is None  # planning never ran
    assert rdr.plan_verdict is None

    manifest_path = log_dir / ".runs" / f"{rdr.run_id}.manifest.json"
    assert manifest_path.exists()

    # No child log written.
    child_logs = list(log_dir.glob(f"{run_id}__*.events.jsonl"))
    assert child_logs == []

    # Verdict card renders dispatch-only variant.
    card = verdict_card(completed)
    assert card is not None
    assert "Dispatched (no auto-plan)" in card


# ---- 3. not-a-root + reject -------------------------------------


async def test_not_a_root_with_task_parent_routes_to_human_reject(
    log_dir: Path,
):
    inputs = RootDispatchInputs(
        item_id=ROOT_ITEM_ID,
        repo_path=Path("."),
        dry_run=False,
        auto_plan=False,
    )
    engine = build_engine(
        log_dir,
        inputs=inputs,
        twig=_twig_with_task_parent(),
        gate_handler=_reject_handler,
    )
    run_id = "not-a-root"
    result = await engine.run(run_id)
    assert isinstance(result, Completed)
    assert result.final_node == "end_human"

    completed = completed_from_log(engine.log_path(run_id))
    # validate_root surfaced a needs_human outcome.
    vr = completed["validate_root"]
    assert vr["kind"] == "needs_human"
    assert vr["gate"] == "not_root"
    # No manifest because compute_run_id never ran.
    assert "compute_run_id" not in completed
    assert "write_manifest" not in completed
    manifest_dir = log_dir / ".runs"
    if manifest_dir.exists():
        assert list(manifest_dir.glob("*.manifest.json")) == []

    card = verdict_card(completed)
    assert card is not None
    assert "Not a root item" in card


async def test_feature_parent_is_accepted_as_root(log_dir: Path):
    """Parent type ``Feature`` counts as root-tier — happy success edge."""
    inputs = RootDispatchInputs(
        item_id=ROOT_ITEM_ID, repo_path=Path("."), auto_plan=False,
    )
    engine = build_engine(
        log_dir,
        inputs=inputs,
        twig=_twig_with_feature_parent(),
        today="2026-06-03",
    )
    result = await engine.run("feature-parent")
    assert isinstance(result, Completed)
    assert result.final_node == "end_dispatched"


async def test_force_root_override_proceeds_past_human_gate(log_dir: Path):
    """Operator picks ``force-root`` → workflow proceeds to manifest."""
    inputs = RootDispatchInputs(
        item_id=ROOT_ITEM_ID, repo_path=Path("."), auto_plan=False,
    )
    engine = build_engine(
        log_dir,
        inputs=inputs,
        twig=_twig_with_task_parent(),
        gate_handler=_force_root_handler,
        today="2026-06-04",
    )
    result = await engine.run("force-root")
    assert isinstance(result, Completed)
    assert result.final_node == "end_dispatched"

    manifest_path = log_dir / ".runs" / f"root-{ROOT_ITEM_ID}-2026-06-04.manifest.json"
    assert manifest_path.exists()


# ---- 4. idempotent re-dispatch ------------------------------


async def test_redispatch_reuses_existing_manifest(log_dir: Path):
    inputs = RootDispatchInputs(
        item_id=ROOT_ITEM_ID, repo_path=Path("."), auto_plan=False,
    )

    engine1 = build_engine(
        log_dir,
        inputs=inputs,
        twig=_twig_one_root(),
        today="2026-06-05",
    )
    r1 = await engine1.run("first")
    assert isinstance(r1, Completed)

    manifest_path = log_dir / ".runs" / f"root-{ROOT_ITEM_ID}-2026-06-05.manifest.json"
    assert manifest_path.exists()
    first_bytes = manifest_path.read_bytes()

    # Different operator-run-id, different "today" — but manifest exists,
    # so compute_run_id reuses the recorded id, write_manifest no-ops.
    engine2 = build_engine(
        log_dir,
        inputs=inputs,
        twig=_twig_one_root(),
        today="2026-06-06",
    )
    r2 = await engine2.run("second")
    assert isinstance(r2, Completed)
    assert r2.final_node == "end_dispatched"

    second_bytes = manifest_path.read_bytes()
    assert first_bytes == second_bytes, "manifest must not be overwritten"

    completed2 = completed_from_log(engine2.log_path("second"))
    cri = completed2["compute_run_id"]["value"]
    assert cri["reused"] is True
    assert cri["run_id"] == f"root-{ROOT_ITEM_ID}-2026-06-05"

    wm = completed2["write_manifest"]["value"]
    assert wm["reused"] is True

    # Only one manifest file in total.
    all_manifests = sorted(
        (log_dir / ".runs").glob(f"root-{ROOT_ITEM_ID}-*.manifest.json")
    )
    assert len(all_manifests) == 1


# ---- 5. dry_run=True ---------------------------------------------


async def test_dry_run_writes_nothing_to_disk(log_dir: Path):
    inputs = RootDispatchInputs(
        item_id=ROOT_ITEM_ID,
        repo_path=Path("."),
        dry_run=True,
        auto_plan=False,
    )
    engine = build_engine(
        log_dir,
        inputs=inputs,
        twig=_twig_one_root(),
        today="2026-06-07",
    )
    result = await engine.run("dry")
    assert isinstance(result, Completed)
    assert result.final_node == "end_dispatched"

    # Manifest dir was never created (no `.mkdir()` in dry-run branch).
    manifest_dir = log_dir / ".runs"
    if manifest_dir.exists():
        assert list(manifest_dir.iterdir()) == []

    completed = completed_from_log(engine.log_path("dry"))
    wm = completed["write_manifest"]["value"]
    assert wm["dry_run"] is True
    assert "preview" in wm
    # Preview contains the manifest skeleton.
    assert wm["preview"]["item_id"] == ROOT_ITEM_ID

    # Verdict card still renders.
    card = verdict_card(completed)
    assert card is not None
    assert "Dispatched (no auto-plan)" in card


# ---- 6. INV-RESTART: truncate mid-subworkflow --------


async def test_inv_restart_truncate_mid_subworkflow_resumes_cleanly(
    log_dir: Path,
):
    """Truncate the parent log after ``subworkflow_started`` for
    ``spawn_planning`` (before ``subworkflow_completed``). Resume must
    re-attach to the planning child, finish it, and terminate at
    ``end_planned``."""
    inputs = RootDispatchInputs(
        item_id=ROOT_ITEM_ID,
        repo_path=Path("."),
        dry_run=False,
        auto_plan=True,
    )
    engine1 = build_engine(
        log_dir,
        inputs=inputs,
        twig=_twig_one_root(),
        provider=_planning_provider(),
        gate_handler=_proceed_handler,
        today="2026-06-08",
    )
    run_id = "resume-mid-sub"
    r1 = await engine1.run(run_id)
    assert isinstance(r1, Completed)
    assert r1.final_node == "end_planned"

    log_path = engine1.log_path(run_id)
    # Truncate after the first subworkflow_started event for spawn_planning
    # (drop subworkflow_completed + record_plan_outcome + end_planned).
    lines = log_path.read_text(encoding="utf-8").splitlines()
    keep: list[str] = []
    found_sw_start = False
    for raw in lines:
        ev = json.loads(raw)
        keep.append(raw)
        if (
            ev["kind"] == "subworkflow_started"
            and ev.get("node_id") == "spawn_planning"
        ):
            found_sw_start = True
            break
    assert found_sw_start, "test fixture should have emitted subworkflow_started"
    log_path.write_text("\n".join(keep) + "\n", encoding="utf-8")

    # The child log already exists from the first run — its replay is
    # idempotent so the resumed parent will get the same completion.
    # The parent provider can be empty: planning verbs/agents on the
    # child won't be re-invoked (child engine resumes from its own log).
    engine2 = build_engine(
        log_dir,
        inputs=inputs,
        twig=_twig_one_root(),
        provider=FakeProvider(scripts={"planner": [], "plan_reviewer": []}),
        gate_handler=_proceed_handler,
        today="2026-06-08",
    )
    r2 = await engine2.run(run_id)
    assert isinstance(r2, Completed), r2
    assert r2.disposition == r1.disposition
    assert r2.final_node == r1.final_node

    # Terminal state matches: manifest content is identical.
    manifest_path = log_dir / ".runs" / f"root-{ROOT_ITEM_ID}-2026-06-08.manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["plan_verdict"] == "approved"


# ---- 7. INV-SUBWORKFLOW-LOG-ISOLATION ---------------------------


async def test_subworkflow_log_isolation(log_dir: Path):
    """Parent log carries only the parent run_id envelope; child log
    carries only the sub_run_id envelope. No cross-writes."""
    inputs = RootDispatchInputs(
        item_id=ROOT_ITEM_ID, repo_path=Path("."), auto_plan=True,
    )
    engine = build_engine(
        log_dir,
        inputs=inputs,
        twig=_twig_one_root(),
        provider=_planning_provider(),
        gate_handler=_proceed_handler,
        today="2026-06-09",
    )
    run_id = "isolation"
    result = await engine.run(run_id)
    assert isinstance(result, Completed)
    assert result.final_node == "end_planned"

    parent_log = engine.log_path(run_id)
    parent_events = list(replay(parent_log))
    parent_run_ids = {e.get("run_id") for e in parent_events}
    assert parent_run_ids == {run_id}, parent_run_ids

    # Find the sub_run_id the kernel used.
    sw_started = [
        e for e in parent_events if e["kind"] == "subworkflow_started"
    ]
    assert len(sw_started) == 1
    sub_run_id = sw_started[0]["payload"]["sub_run_id"]
    assert sub_run_id != run_id  # distinct run id
    assert "__" in sub_run_id    # kernel's separator convention

    child_log = log_dir / f"{sub_run_id}.events.jsonl"
    assert child_log.exists()
    child_events = list(replay(child_log))
    child_run_ids = {e.get("run_id") for e in child_events}
    assert child_run_ids == {sub_run_id}, child_run_ids

    # No file mixes envelopes.
    assert run_id not in child_run_ids
    assert sub_run_id not in parent_run_ids


# ---- bonus: twig_not_found classification --------------------


async def test_twig_not_found_routes_to_failed(log_dir: Path):
    inputs = RootDispatchInputs(
        item_id=ROOT_ITEM_ID, repo_path=Path("."), auto_plan=False,
    )
    # Empty twig client → fetch_item raises TwigItemNotFoundError →
    # PermanentFailure(twig.not_found) → end_failed.
    engine = build_engine(
        log_dir,
        inputs=inputs,
        twig=FakeTwigClient(items={}),
    )
    result = await engine.run("missing")
    # The workflow routes twig.not_found → end_failed (a terminate node
    # with disposition="failed"), so the kernel returns Completed with
    # that disposition rather than a raw Failed.
    assert isinstance(result, Completed)
    assert result.disposition == "failed"
    assert result.final_node == "end_failed"

    completed = completed_from_log(engine.log_path("missing"))
    fi = completed["fetch_item"]
    assert fi["kind"] == "permanent_failure"
    assert fi["error_kind"] == "twig.not_found"


# ---- bonus: shim is re-registered on each build_engine ---------


def test_planning_shim_registered_on_build_engine(log_dir: Path):
    import sys
    inputs = RootDispatchInputs(
        item_id=ROOT_ITEM_ID, repo_path=Path("."), auto_plan=True,
    )
    mod_name = f"requiem.workflows._dispatch_planning_for_item_{ROOT_ITEM_ID}"
    # Clear any previous registration.
    sys.modules.pop(mod_name, None)
    build_engine(
        log_dir,
        inputs=inputs,
        twig=_twig_one_root(),
        provider=_planning_provider(),
    )
    assert mod_name in sys.modules
    mod = sys.modules[mod_name]
    assert hasattr(mod, "build_engine")
    assert hasattr(mod, "build_workflow")
