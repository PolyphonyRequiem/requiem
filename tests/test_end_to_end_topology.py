"""Tests for the ADR-0018 step-4 trunk-topology wiring in ``requiem.end_to_end``.

The driver wires three landed workflows around the executor when (and only when)
a ``github_repo`` is threaded:

    trunk_bootstrap  (BEFORE dispatch)
      → kanban_executor (existing fan-out)
      → leaf_pr        (AFTER delivery, persists the {leaf_id: pr_number} map)
      → [human/pr_lifecycle merges the leaf PRs into the trunk]
      → feature_pr     (separate integrate_pipeline invocation)

Like the existing driver tests, these inject *stub* engine factories that write a
minimal durable log and return a ``Completed`` — no LLM/ADO/Hermes/gh. The three
landed workflows have their own contract tests against fake gh clients; here we
exercise only the driver's orchestration: ordering, the github_repo gate,
fail-closed behaviour, the persisted leaf-PR map, and live⇒dry_run threading.
"""
from __future__ import annotations

import json
from pathlib import Path

from requiem.end_to_end import (
    IntegrationResult,
    integrate_pipeline,
    load_leaf_pr_map,
    run_pipeline,
)
from requiem.kernel import Completed
from requiem.workflows.feature_pr import LeafPr


def _write_log(log_dir: Path, run_id: str, events: list[tuple[str, dict]]) -> None:
    path = log_dir / f"{run_id}.events.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for node_id, outcome in events:
            fh.write(json.dumps({
                "kind": "verb_completed", "node_id": node_id,
                "payload": {"outcome": outcome},
            }) + "\n")


def _record_plan(*, decomposable: bool, verdict: str = "approved",
                 item_id: int = 700) -> dict:
    return {
        "kind": "success",
        "value": {
            "item_id": item_id, "item_title": f"item {item_id}",
            "summary": "do the thing", "decomposable": decomposable,
            "final_verdict": verdict,
            "plan_artifact": f"/logs/plan-{item_id}.plan.tree.json",
        },
    }


class _Calls:
    def __init__(self) -> None:
        self.planning = 0
        self.commit = 0
        self.executor = 0
        self.trunk = 0
        self.leaf_pr = 0
        self.feature_pr = 0
        self.order: list[str] = []
        self.trunk_inputs = None
        self.leaf_pr_inputs = None
        self.feature_pr_inputs = None


def _topology_factories(
    calls: _Calls, *,
    decomposable: bool = False,
    item_id: int = 700,
    exec_final: str = "end",
    exec_leaf_ids: tuple[str, ...] = ("700",),
    trunk_final: str = "end_success",
    trunk_created: bool = True,
    leaf_pr_final: str = "end_success",
    leaf_pr_numbers: dict[str, int] | None = None,
):
    """Stub factories for all five engines, recording call order + inputs."""
    leaf_pr_numbers = leaf_pr_numbers or {lid: 9000 + i for i, lid in enumerate(exec_leaf_ids)}

    def planning_factory(log_dir, *, item_id=item_id, twig=None, provider=None,
                         gate_handler=None, process_config=None):
        calls.planning += 1
        calls.order.append("planning")

        class _E:
            async def run(self, run_id):
                _write_log(log_dir, run_id, [
                    ("record_plan", _record_plan(decomposable=decomposable, item_id=item_id)),
                ])
                return Completed(run_id, "completed", "end", {})
        return _E()

    def commit_factory(log_dir, *, plan_tree_path=None, dry_run=None, twig=None,
                       manifest_path=None, gate_handler=None):
        calls.commit += 1
        calls.order.append("commit")

        class _E:
            async def run(self, run_id):
                _write_log(log_dir, run_id, [
                    ("write_manifest", {"kind": "success",
                                        "value": {"manifest_path": str(manifest_path)}}),
                ])
                return Completed(run_id, "completed", "end", {})
        return _E()

    def trunk_factory(log_dir, *, inputs=None, toolbelt=None, gate_handler=None):
        calls.trunk += 1
        calls.order.append("trunk_bootstrap")
        calls.trunk_inputs = inputs

        class _E:
            async def run(self, run_id):
                _write_log(log_dir, run_id, [
                    ("start", {"kind": "success", "value": {
                        "root_item_id": inputs.root_item_id,
                        "trunk_branch": inputs.trunk_branch,
                        "base_branch": inputs.base_branch,
                        "dry_run": inputs.dry_run,
                    }}),
                    ("ensure_trunk", {"kind": "success", "value": {
                        "trunk_branch": inputs.trunk_branch,
                        "base_branch": inputs.base_branch,
                        "base_sha": "basesha000",
                        "created": trunk_created,
                        "exists": not trunk_created,
                        "dry_run": inputs.dry_run,
                    }}),
                ])
                disp = "completed" if trunk_final == "end_success" else "failed"
                return Completed(run_id, disp, trunk_final, {})
        return _E()

    def executor_factory(log_dir, *, inputs=None, toolbelt=None, gate_handler=None):
        calls.executor += 1
        calls.order.append("executor")
        leaves = [{"leaf_id": lid} for lid in exec_leaf_ids]

        class _E:
            async def run(self, run_id):
                _write_log(log_dir, run_id, [
                    ("resolve_leaves", {"kind": "success", "value": {"leaves": leaves}}),
                ])
                disp = "completed" if exec_final == "end" else "failed"
                return Completed(run_id, disp, exec_final, {})
        return _E()

    def leaf_pr_factory(log_dir, *, inputs=None, toolbelt=None, gate_handler=None):
        calls.leaf_pr += 1
        calls.order.append("leaf_pr")
        calls.leaf_pr_inputs = inputs
        leaves = [
            {"leaf_id": lid, "pr_number": (None if inputs.dry_run
                                           else leaf_pr_numbers.get(lid))}
            for lid in inputs.leaf_ids
        ]

        class _E:
            async def run(self, run_id):
                _write_log(log_dir, run_id, [
                    ("start", {"kind": "success", "value": {
                        "root_item_id": inputs.root_item_id,
                        "trunk_branch": inputs.trunk_branch,
                        "leaves_total": len(inputs.leaf_ids),
                        "dry_run": inputs.dry_run,
                    }}),
                    ("open_leaf_prs", {"kind": "success", "value": {
                        "leaves": leaves,
                        "opened": 0 if inputs.dry_run else len(leaves),
                        "reused": 0,
                    }}),
                ])
                disp = "completed" if leaf_pr_final == "end_success" else "failed"
                return Completed(run_id, disp, leaf_pr_final, {})
        return _E()

    return (planning_factory, commit_factory, executor_factory,
            trunk_factory, leaf_pr_factory)


# ---- the github_repo gate: legacy path is untouched ------------------------


async def test_no_github_repo_skips_topology_entirely(tmp_path):
    """Without github_repo the driver behaves exactly as the legacy pipeline."""
    calls = _Calls()
    pf, cf, ef, tf, lf = _topology_factories(calls)
    result = await run_pipeline(
        700, log_dir=tmp_path, board="requiem-700", assignee="w", live=True,
        planning_factory=pf, commit_factory=cf, executor_factory=ef,
        trunk_bootstrap_factory=tf, leaf_pr_factory=lf,
    )
    assert result.status == "delivered"
    # No topology engine ran; no github fields populated.
    assert calls.trunk == 0 and calls.leaf_pr == 0
    assert result.github_repo is None
    assert result.trunk_branch is None
    assert result.leaf_pr_map == ()
    assert result.leaf_pr_map_path is None
    assert "all implementable leaves dispatched" in result.detail


# ---- ordering: bootstrap BEFORE dispatch, leaf_pr AFTER --------------------


async def test_topology_runs_in_correct_order(tmp_path):
    calls = _Calls()
    pf, cf, ef, tf, lf = _topology_factories(
        calls, exec_leaf_ids=("700",))
    result = await run_pipeline(
        700, log_dir=tmp_path, board="requiem-700", assignee="w", live=True,
        github_repo="Owner/Repo", base_branch="main",
        planning_factory=pf, commit_factory=cf, executor_factory=ef,
        trunk_bootstrap_factory=tf, leaf_pr_factory=lf,
    )
    assert result.status == "delivered"
    # trunk_bootstrap strictly before executor; leaf_pr strictly after.
    assert calls.order == ["planning", "trunk_bootstrap", "executor", "leaf_pr"]
    assert result.github_repo == "Owner/Repo"
    assert result.trunk_branch == "feature/700"
    assert result.trunk_verdict == "created"
    assert result.leaf_pr_verdict == "opened"


# ---- the persisted leaf-PR map (briefing's explicit requirement) -----------


async def test_leaf_pr_map_is_persisted_and_rehydratable(tmp_path):
    calls = _Calls()
    pf, cf, ef, tf, lf = _topology_factories(
        calls, exec_leaf_ids=("700", "701"),
        leaf_pr_numbers={"700": 11, "701": 12})
    result = await run_pipeline(
        700, log_dir=tmp_path, board="requiem-700", assignee="w", live=True,
        github_repo="Owner/Repo", base_branch="main",
        planning_factory=pf, commit_factory=cf, executor_factory=ef,
        trunk_bootstrap_factory=tf, leaf_pr_factory=lf,
    )
    assert result.leaf_pr_map == (("700", 11), ("701", 12))
    # The map is persisted to disk...
    assert result.leaf_pr_map_path is not None
    map_path = Path(result.leaf_pr_map_path)
    assert map_path.exists()
    # ...and re-hydrates into feature_pr's input element type.
    leaves = load_leaf_pr_map(map_path)
    assert leaves == (LeafPr("700", 11), LeafPr("701", 12))


# ---- live=False stays genuinely dry (dry_run threaded through) -------------


async def test_dry_run_threads_to_every_topology_step(tmp_path):
    calls = _Calls()
    pf, cf, ef, tf, lf = _topology_factories(
        calls, exec_leaf_ids=("700",), trunk_created=False)
    result = await run_pipeline(
        700, log_dir=tmp_path, board="requiem-700", assignee="w", live=False,
        github_repo="Owner/Repo", base_branch="main",
        planning_factory=pf, commit_factory=cf, executor_factory=ef,
        trunk_bootstrap_factory=tf, leaf_pr_factory=lf,
    )
    # live=False ⇒ dry_run=True on every topology engine's inputs.
    assert calls.trunk_inputs.dry_run is True
    assert calls.leaf_pr_inputs.dry_run is True
    # Dry-run leaf PRs have no numbers (nothing opened).
    assert result.leaf_pr_verdict == "previewed"
    assert result.leaf_pr_map == (("700", None),)


# ---- fail-closed: a failed bootstrap never dispatches ----------------------


async def test_failed_bootstrap_does_not_dispatch(tmp_path):
    calls = _Calls()
    pf, cf, ef, tf, lf = _topology_factories(
        calls, trunk_final="end_failed")
    result = await run_pipeline(
        700, log_dir=tmp_path, board="requiem-700", assignee="w", live=True,
        github_repo="Owner/Repo", base_branch="main",
        planning_factory=pf, commit_factory=cf, executor_factory=ef,
        trunk_bootstrap_factory=tf, leaf_pr_factory=lf,
    )
    assert result.stage == "trunk_bootstrap"
    assert result.status == "paused"
    # Trunk-before-fan-out: the executor and leaf_pr never ran.
    assert calls.executor == 0 and calls.leaf_pr == 0
    assert result.trunk_verdict == "failed"


# ---- a leaf_pr gate (needs_human/failed) surfaces, doesn't crash ----------


async def test_leaf_pr_needs_human_pauses_with_persisted_map(tmp_path):
    calls = _Calls()
    pf, cf, ef, tf, lf = _topology_factories(
        calls, exec_leaf_ids=("700", "701"),
        leaf_pr_final="end_human")
    result = await run_pipeline(
        700, log_dir=tmp_path, board="requiem-700", assignee="w", live=True,
        github_repo="Owner/Repo", base_branch="main",
        planning_factory=pf, commit_factory=cf, executor_factory=ef,
        trunk_bootstrap_factory=tf, leaf_pr_factory=lf,
    )
    assert result.stage == "leaf_pr"
    assert result.status == "paused"
    # Even on a pause, the partial map is persisted for inspection / re-run.
    assert result.leaf_pr_map_path is not None
    assert Path(result.leaf_pr_map_path).exists()


# ---- the executor pausing skips leaf_pr but keeps trunk projection --------


async def test_executor_pause_skips_leaf_pr(tmp_path):
    calls = _Calls()
    pf, cf, ef, tf, lf = _topology_factories(
        calls, exec_final="fail_end")
    result = await run_pipeline(
        700, log_dir=tmp_path, board="requiem-700", assignee="w", live=True,
        github_repo="Owner/Repo", base_branch="main",
        planning_factory=pf, commit_factory=cf, executor_factory=ef,
        trunk_bootstrap_factory=tf, leaf_pr_factory=lf,
    )
    assert result.stage == "executor"
    assert result.status == "paused"
    assert result.executor_final_node == "fail_end"
    # Bootstrap ran (trunk projected) but leaf_pr did not.
    assert calls.trunk == 1 and calls.leaf_pr == 0
    assert result.trunk_branch == "feature/700"


# ---- integrate_pipeline (phase 5): feature_pr after the human merges -------


def _feature_pr_factory(calls: _Calls, *, final: str = "end_success",
                        pr_number: int = 555, leaves_ready: int = 2,
                        leaves_total: int = 2):
    def factory(log_dir, *, inputs=None, toolbelt=None, gate_handler=None):
        calls.feature_pr += 1
        calls.feature_pr_inputs = inputs

        class _E:
            async def run(self, run_id):
                events = [
                    ("start", {"kind": "success", "value": {
                        "root_item_id": inputs.root_item_id,
                        "trunk_branch": inputs.trunk_branch,
                        "base_branch": inputs.base_branch,
                        "leaves_total": leaves_total,
                        "dry_run": inputs.dry_run,
                    }}),
                    ("verify_readiness", {"kind": "success", "value": {
                        "leaves_ready": leaves_ready,
                    }}),
                ]
                # In the real workflow open_pr only runs when readiness passed;
                # an end_human gate stops at verify_readiness (no open_pr value).
                if final == "end_success":
                    events.append(("open_pr", {"kind": "success", "value": {
                        "pr_number": pr_number,
                        "pr_url": f"https://github.com/{inputs.repo}/pull/{pr_number}",
                        "reused_existing": False,
                    }}))
                _write_log(log_dir, run_id, events)
                disp = "completed" if final == "end_success" else "failed"
                return Completed(run_id, disp, final, {})
        return _E()
    return factory


async def test_integrate_reads_persisted_map_and_opens_pr(tmp_path):
    # Seed a persisted map exactly as run_pipeline would have written it.
    map_path = tmp_path / "leaf-pr-map-700.json"
    map_path.write_text(json.dumps({
        "item_id": 700,
        "leaves": [{"leaf_id": "700", "pr_number": 11},
                   {"leaf_id": "701", "pr_number": 12}],
    }), encoding="utf-8")

    calls = _Calls()
    result = await integrate_pipeline(
        700, log_dir=tmp_path, github_repo="Owner/Repo",
        leaf_pr_map_path=map_path, base_branch="main", live=True,
        feature_pr_factory=_feature_pr_factory(calls),
    )
    assert isinstance(result, IntegrationResult)
    assert result.status == "opened"
    assert result.feature_pr_number == 555
    assert result.feature_pr_url.endswith("/pull/555")
    # feature_pr received the re-hydrated expected-leaf set from the map.
    assert calls.feature_pr_inputs.leaves == (LeafPr("700", 11), LeafPr("701", 12))
    assert calls.feature_pr_inputs.dry_run is False


async def test_integrate_not_ready_surfaces_to_human(tmp_path):
    map_path = tmp_path / "leaf-pr-map-700.json"
    map_path.write_text(json.dumps({
        "item_id": 700,
        "leaves": [{"leaf_id": "700", "pr_number": 11},
                   {"leaf_id": "701", "pr_number": 12}],
    }), encoding="utf-8")

    calls = _Calls()
    result = await integrate_pipeline(
        700, log_dir=tmp_path, github_repo="Owner/Repo",
        leaf_pr_map_path=map_path, base_branch="main", live=True,
        feature_pr_factory=_feature_pr_factory(
            calls, final="end_human", leaves_ready=1, leaves_total=2),
    )
    # The drift/laggard path: gate returns needs_human → not_ready, no crash.
    assert result.status == "not_ready"
    assert result.leaves_ready == 1
    assert result.leaves_total == 2
    assert result.feature_pr_number is None


async def test_integrate_requires_a_map_source(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        await integrate_pipeline(
            700, log_dir=tmp_path, github_repo="Owner/Repo",
            # neither leaves nor leaf_pr_map_path
        )
