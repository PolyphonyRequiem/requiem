"""Tests for leaf self-merge wiring on the in-process fan-out backend.

Run #34 (dogfood) exposed a gap: ``dispatch_backend="fanout"`` opened every
leaf PR directly against the repo default branch (``main``) instead of the
bootstrapped trunk (``feature/<root>``), and never invoked ``leaf_lifecycle``
after a leaf landed — so the self-merge feature (wired only into the kanban
backend) was structurally unreachable on the actual live path. These tests
pin the fix: fanout leaves target the trunk, and each landed leaf is driven
through ``leaf_lifecycle`` exactly as the kanban path's ``leaf_pr`` leg does.

Like ``test_end_to_end_topology.py``, these inject *stub* engine factories
(no real git/gh/LLM) to exercise only the driver's orchestration.
"""
from __future__ import annotations

import json
from pathlib import Path

from requiem.clients.fs import FsGitError
from requiem.end_to_end import run_pipeline
from requiem.kernel import Completed

ROOT = 700
REPO = "Owner/Repo"


def _write_log(log_dir: Path, run_id: str, events: list[tuple[str, dict]]) -> None:
    path = log_dir / f"{run_id}.events.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for node_id, outcome in events:
            fh.write(json.dumps({
                "kind": "verb_completed", "node_id": node_id,
                "payload": {"outcome": outcome},
            }) + "\n")


def _atomic_plan(item_id: int) -> dict:
    return {
        "kind": "success",
        "value": {
            "item_id": item_id, "item_title": f"item {item_id}",
            "summary": "implement the thing", "decomposable": False,
            "final_verdict": "approved",
            "plan_artifact": f"/logs/plan-{item_id}.plan.tree.json",
        },
    }


def _stub_planning(item_id: int = ROOT):
    def planning_factory(log_dir, *, item_id=item_id, twig=None, provider=None,
                         gate_handler=None, process_config=None):
        class _E:
            async def run(self, run_id):
                _write_log(log_dir, run_id, [("record_plan", _atomic_plan(item_id))])
                return Completed(run_id, "completed", "end", {})
        return _E()
    return planning_factory


def _stub_trunk(*, created: bool = True):
    def trunk_factory(log_dir, *, inputs=None, toolbelt=None, gate_handler=None):
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
                        "created": created,
                        "exists": not created,
                        "dry_run": inputs.dry_run,
                    }}),
                ])
                return Completed(run_id, "completed", "end_success", {})
        return _E()
    return trunk_factory


class _FanoutCalls:
    def __init__(self) -> None:
        self.inputs: list[object] = []


def _stub_fanout(calls: _FanoutCalls, outcomes: list[dict]):
    """A stub ``fanout_factory`` that writes a fixed ``dispatch_leaves`` outcome
    (bypassing the real resolve/dispatch loop) and records the inputs it was
    built with — chiefly ``base_branch``, so tests can assert leaves are told
    to target the trunk rather than the repo default branch."""
    def fanout_factory(log_dir, *, inputs=None, toolbelt=None, provider=None,
                       gate_handler=None):
        calls.inputs.append(inputs)

        class _E:
            async def run(self, run_id):
                landed = sum(1 for o in outcomes if o["disposition"] == "completed")
                needs_human = sum(1 for o in outcomes if o["disposition"] == "needs_human")
                failed = sum(1 for o in outcomes
                             if o["disposition"] not in ("completed", "needs_human"))
                _write_log(log_dir, run_id, [
                    ("dispatch_leaves", {"kind": "success", "value": {
                        "leaves_total": len(outcomes),
                        "leaves_landed": landed,
                        "leaves_needs_human": needs_human,
                        "leaves_failed": failed,
                        "outcomes": outcomes,
                        "dry_run": inputs.dry_run,
                        "parallel": inputs.parallel,
                    }}),
                ])
                return Completed(run_id, "completed", "end_success", {})
        return _E()
    return fanout_factory


class _LifecycleCalls:
    def __init__(self) -> None:
        self.inputs: list[object] = []


async def _noop_trunk_sync(repo_path: Path, remote: str, branch: str) -> None:
    """Stub for the run #35 trunk-sync hook — these tests use `tmp_path` as
    `repo_path`, which isn't a real git repo, so the real `git fetch` must
    never run here."""


def _stub_leaf_lifecycle(calls: _LifecycleCalls, states: dict[str, str]):
    def leaf_lifecycle_factory(log_dir, *, inputs=None, toolbelt=None, provider=None,
                               gate_handler=None):
        calls.inputs.append(inputs)
        state = states[inputs.leaf_id]

        class _E:
            async def run(self, run_id):
                events = [
                    ("fetch_pr", {"kind": "success", "value": {
                        "number": inputs.pr_number,
                        "head": f"impl/{inputs.root_item_id}-{inputs.leaf_id}",
                        "base": f"feature/{inputs.root_item_id}",
                        "state": "open", "merged": False,
                    }}),
                ]
                if state == "merged":
                    events.append(("merge_pr", {"kind": "success", "value": {
                        "merged": True, "merge_sha": f"merge-{inputs.leaf_id}",
                        "strategy": "squash",
                    }}))
                    final, disp = "end_merged", "completed"
                else:
                    events.append(("check_tests_passed", {
                        "kind": "permanent_failure",
                        "error_kind": f"needs_human.{state}", "message": state,
                    }))
                    final, disp = "needs_human_end", "failed"
                _write_log(log_dir, run_id, events)
                return Completed(run_id, disp, final, {})
        return _E()
    return leaf_lifecycle_factory


async def test_fanout_leaves_target_trunk_not_base_branch(tmp_path: Path):
    """The run #34 gap: fanout leaves must fork from + PR against
    feature/<root> when a trunk exists, not the repo default branch."""
    fanout_calls = _FanoutCalls()
    lifecycle_calls = _LifecycleCalls()
    outcomes = [{
        "real_id": ROOT, "disposition": "completed", "final_node": "end",
        "child_run_id": f"fanout-{ROOT}__leaf-{ROOT}", "skipped": False,
        "pr_number": 9001, "branch_name": f"impl/{ROOT}-{ROOT}",
    }]
    result = await run_pipeline(
        ROOT, log_dir=tmp_path, board="requiem-test",
        dispatch_backend="fanout", repo_path=tmp_path, github_repo=REPO,
        base_branch="main", live=True,
        planning_factory=_stub_planning(ROOT),
        trunk_bootstrap_factory=_stub_trunk(),
        fanout_factory=_stub_fanout(fanout_calls, outcomes),
        leaf_lifecycle_factory=_stub_leaf_lifecycle(lifecycle_calls, {str(ROOT): "merged"}),
        trunk_sync=_noop_trunk_sync,
    )
    assert len(fanout_calls.inputs) == 1
    assert fanout_calls.inputs[0].base_branch == f"feature/{ROOT}"
    assert result.trunk_branch == f"feature/{ROOT}"
    assert result.status == "delivered", result.detail
    assert result.leaf_lifecycle_verdict == "merged"
    assert result.leaf_lifecycle_results == ((str(ROOT), "merged"),)
    assert result.stage == "leaf_lifecycle"


async def test_fanout_leaf_lifecycle_continues_past_a_blocked_leaf(tmp_path: Path):
    """Two landed leaves; the first blocks in leaf_lifecycle. Run #36
    postmortem: the old behaviour abandoned every remaining leaf the moment
    ONE bad merge was hit — 19 good PRs sat unmerged over 4 stragglers in
    that run. The second (mergeable) leaf must still get its own self-merge
    attempt."""
    fanout_calls = _FanoutCalls()
    lifecycle_calls = _LifecycleCalls()
    outcomes = [
        {"real_id": 1, "disposition": "completed", "final_node": "end",
         "child_run_id": "fanout-700__leaf-1", "skipped": False,
         "pr_number": 9001, "branch_name": "impl/700-1"},
        {"real_id": 2, "disposition": "completed", "final_node": "end",
         "child_run_id": "fanout-700__leaf-2", "skipped": False,
         "pr_number": 9002, "branch_name": "impl/700-2"},
    ]
    result = await run_pipeline(
        ROOT, log_dir=tmp_path, board="requiem-test",
        dispatch_backend="fanout", repo_path=tmp_path, github_repo=REPO,
        base_branch="main", live=True,
        planning_factory=_stub_planning(ROOT),
        trunk_bootstrap_factory=_stub_trunk(),
        fanout_factory=_stub_fanout(fanout_calls, outcomes),
        leaf_lifecycle_factory=_stub_leaf_lifecycle(
            lifecycle_calls, {"1": "blocked_by_ci", "2": "merged"},
        ),
        trunk_sync=_noop_trunk_sync,
    )
    assert result.status == "paused"
    assert result.stage == "leaf_lifecycle"
    assert len(lifecycle_calls.inputs) == 2  # leaf 2 IS dispatched
    assert {c.leaf_id for c in lifecycle_calls.inputs} == {"1", "2"}
    assert result.leaf_lifecycle_results == (("1", "needs_human"), ("2", "merged"))
    assert "1/2" in result.detail  # 1 of 2 landed leaves merged



async def test_fanout_needs_human_when_pr_number_missing(tmp_path: Path):
    """A landed leaf with no recoverable PR number pauses for a human instead
    of crashing or silently skipping self-merge."""
    fanout_calls = _FanoutCalls()
    lifecycle_calls = _LifecycleCalls()
    outcomes = [{
        "real_id": ROOT, "disposition": "completed", "final_node": "end",
        "child_run_id": f"fanout-{ROOT}__leaf-{ROOT}", "skipped": False,
        "pr_number": None, "branch_name": f"impl/{ROOT}-{ROOT}",
    }]
    result = await run_pipeline(
        ROOT, log_dir=tmp_path, board="requiem-test",
        dispatch_backend="fanout", repo_path=tmp_path, github_repo=REPO,
        base_branch="main", live=True,
        planning_factory=_stub_planning(ROOT),
        trunk_bootstrap_factory=_stub_trunk(),
        fanout_factory=_stub_fanout(fanout_calls, outcomes),
        leaf_lifecycle_factory=_stub_leaf_lifecycle(lifecycle_calls, {}),
        trunk_sync=_noop_trunk_sync,
    )
    assert result.status == "paused"
    assert result.stage == "leaf_lifecycle"
    assert "no PR number" in result.detail
    assert len(lifecycle_calls.inputs) == 0  # never dispatched


async def test_fanout_stragglers_do_not_block_self_merge_of_landed_leaves(
    tmp_path: Path,
):
    """Run #36: 19/23 leaves landed cleanly, 4 needed a human at the FAN-OUT
    stage — but self-merge never ran for ANY of the 19 good leaves because
    the loop was gated on zero needs_human/failed leaves across the whole
    fan-out. A leaf that needed a human at fan-out never reaches this loop
    at all (its disposition isn't "completed"); the leaves that DID land
    must still get their self-merge attempt."""
    fanout_calls = _FanoutCalls()
    lifecycle_calls = _LifecycleCalls()
    outcomes = [
        {"real_id": 1, "disposition": "completed", "final_node": "end",
         "child_run_id": "fanout-700__leaf-1", "skipped": False,
         "pr_number": 9001, "branch_name": "impl/700-1"},
        {"real_id": 2, "disposition": "completed", "final_node": "end",
         "child_run_id": "fanout-700__leaf-2", "skipped": False,
         "pr_number": 9002, "branch_name": "impl/700-2"},
        {"real_id": 3, "disposition": "needs_human", "final_node": "invoke_coder",
         "child_run_id": "fanout-700__leaf-3", "skipped": False,
         "pr_number": None, "branch_name": ""},
    ]
    result = await run_pipeline(
        ROOT, log_dir=tmp_path, board="requiem-test",
        dispatch_backend="fanout", repo_path=tmp_path, github_repo=REPO,
        base_branch="main", live=True,
        planning_factory=_stub_planning(ROOT),
        trunk_bootstrap_factory=_stub_trunk(),
        fanout_factory=_stub_fanout(fanout_calls, outcomes),
        leaf_lifecycle_factory=_stub_leaf_lifecycle(
            lifecycle_calls, {"1": "merged", "2": "merged"},
        ),
        trunk_sync=_noop_trunk_sync,
    )
    # Both landed leaves got their own self-merge attempt, both merged.
    assert len(lifecycle_calls.inputs) == 2
    assert {c.leaf_id for c in lifecycle_calls.inputs} == {"1", "2"}
    assert result.leaf_lifecycle_results == (("1", "merged"), ("2", "merged"))
    assert result.leaf_lifecycle_verdict == "merged"
    # Still paused overall (leaf 3 needs a human), but the detail reflects
    # that the landed leaves' merges succeeded, not an abandoned self-merge.
    assert result.status == "paused"
    assert "2/2" in result.detail


async def test_fanout_not_live_skips_self_merge(tmp_path: Path):
    """A dry-run (live=False) preview never drives leaf_lifecycle, mirroring
    the kanban path's dry-run behaviour."""
    fanout_calls = _FanoutCalls()
    lifecycle_calls = _LifecycleCalls()
    outcomes = [{
        "real_id": ROOT, "disposition": "completed", "final_node": "end",
        "child_run_id": f"fanout-{ROOT}__leaf-{ROOT}", "skipped": False,
        "pr_number": None, "branch_name": "",
    }]
    result = await run_pipeline(
        ROOT, log_dir=tmp_path, board="requiem-test",
        dispatch_backend="fanout", repo_path=tmp_path, github_repo=REPO,
        base_branch="main", live=False,
        planning_factory=_stub_planning(ROOT),
        trunk_bootstrap_factory=_stub_trunk(),
        fanout_factory=_stub_fanout(fanout_calls, outcomes),
        leaf_lifecycle_factory=_stub_leaf_lifecycle(lifecycle_calls, {}),
        trunk_sync=_noop_trunk_sync,
    )
    assert result.status == "delivered"
    assert len(lifecycle_calls.inputs) == 0
    assert result.leaf_lifecycle_verdict is None


async def test_fanout_syncs_trunk_branch_before_dispatch(tmp_path: Path):
    """Run #35 postmortem: trunk_bootstrap creates feature/<root> purely via
    the platform REST API (no working tree touched); the persistent local
    worktree fanout dispatches into never learns about that ref through
    ordinary git operations, so every leaf's `git checkout -b impl/<x>
    feature/<root>` failed with "'feature/<root>' is not a commit"
    (0/23 leaves landed). The driver must sync the trunk branch into the
    local repo_path exactly once, before fan-out, whenever live+trunk exist."""
    fanout_calls = _FanoutCalls()
    lifecycle_calls = _LifecycleCalls()
    sync_calls: list[tuple[Path, str, str]] = []

    async def _recording_trunk_sync(repo_path: Path, remote: str, branch: str) -> None:
        sync_calls.append((repo_path, remote, branch))

    outcomes = [{
        "real_id": ROOT, "disposition": "completed", "final_node": "end",
        "child_run_id": f"fanout-{ROOT}__leaf-{ROOT}", "skipped": False,
        "pr_number": 9001, "branch_name": f"impl/{ROOT}-{ROOT}",
    }]
    result = await run_pipeline(
        ROOT, log_dir=tmp_path, board="requiem-test",
        dispatch_backend="fanout", repo_path=tmp_path, github_repo=REPO,
        base_branch="main", live=True,
        planning_factory=_stub_planning(ROOT),
        trunk_bootstrap_factory=_stub_trunk(),
        fanout_factory=_stub_fanout(fanout_calls, outcomes),
        leaf_lifecycle_factory=_stub_leaf_lifecycle(lifecycle_calls, {str(ROOT): "merged"}),
        trunk_sync=_recording_trunk_sync,
    )
    assert result.status == "delivered", result.detail
    assert sync_calls == [(tmp_path, "origin", f"feature/{ROOT}")]


async def test_fanout_trunk_sync_failure_pauses_cleanly(tmp_path: Path):
    """If the trunk sync itself fails (e.g. the fetch can't reach the
    remote), fail closed to a paused PipelineResult with a clear detail —
    never let every leaf crash into the same git error independently."""
    fanout_calls = _FanoutCalls()
    lifecycle_calls = _LifecycleCalls()

    async def _failing_trunk_sync(repo_path: Path, remote: str, branch: str) -> None:
        raise FsGitError(["fetch", remote, branch], 1, "fatal: could not read from remote")

    outcomes = [{
        "real_id": ROOT, "disposition": "completed", "final_node": "end",
        "child_run_id": f"fanout-{ROOT}__leaf-{ROOT}", "skipped": False,
        "pr_number": 9001, "branch_name": f"impl/{ROOT}-{ROOT}",
    }]
    result = await run_pipeline(
        ROOT, log_dir=tmp_path, board="requiem-test",
        dispatch_backend="fanout", repo_path=tmp_path, github_repo=REPO,
        base_branch="main", live=True,
        planning_factory=_stub_planning(ROOT),
        trunk_bootstrap_factory=_stub_trunk(),
        fanout_factory=_stub_fanout(fanout_calls, outcomes),
        leaf_lifecycle_factory=_stub_leaf_lifecycle(lifecycle_calls, {}),
        trunk_sync=_failing_trunk_sync,
    )
    assert result.status == "paused"
    assert result.stage == "fanout"
    assert "could not sync trunk branch" in result.detail
    assert len(fanout_calls.inputs) == 0  # dispatch never even started
