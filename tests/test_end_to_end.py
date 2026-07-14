"""Tests for the end-to-end driver (``requiem.end_to_end``).

The driver's job is orchestration: run planning → (atomic-root inline | commit
→ executor) as sequential top-level engines and branch on the plan shape. These
tests inject *stub* engine factories (no LLM/ADO/Hermes) that write a minimal
durable log and return a ``Completed``, so the branching logic is exercised in
isolation. The individual workflows have their own end-to-end tests.
"""
from __future__ import annotations

import json
from pathlib import Path

from requiem.end_to_end import _resolve_twig_cwd, run_pipeline
from requiem.kernel import Completed


def _write_log(log_dir: Path, run_id: str, events: list[tuple[str, dict]]) -> None:
    path = log_dir / f"{run_id}.events.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for node_id, outcome in events:
            fh.write(json.dumps({
                "kind": "verb_completed", "node_id": node_id,
                "payload": {"outcome": outcome},
            }) + "\n")


def _record_plan(*, decomposable: bool, verdict: str = "approved",
                 item_id: int = 500, plan_artifact: str | None = None) -> dict:
    return {
        "kind": "success",
        "value": {
            "item_id": item_id, "item_title": f"item {item_id}",
            "summary": "do the thing", "decomposable": decomposable,
            "final_verdict": verdict,
            "plan_artifact": plan_artifact or f"/logs/plan-{item_id}.plan.tree.json",
        },
    }


def test_resolve_twig_cwd_prefers_repo_root(tmp_path):
    repo_path = tmp_path / "fanout-worktree"
    repo_path.mkdir()
    fallback_repo = tmp_path / "repo-root"
    fallback_repo.mkdir()

    assert _resolve_twig_cwd(repo_path=repo_path, repo=fallback_repo) == fallback_repo.resolve()


def test_resolve_twig_cwd_falls_back_to_repo_path(tmp_path):
    fallback_repo = tmp_path / "repo-root"
    fallback_repo.mkdir()

    assert _resolve_twig_cwd(repo_path=fallback_repo, repo=None) == fallback_repo.resolve()


class _Calls:
    def __init__(self) -> None:
        self.planning = 0
        self.commit = 0
        self.executor = 0
        self.commit_dry_run: bool | None = None
        self.exec_inputs = None


def _factories(calls: _Calls, *, decomposable: bool, plan_verdict: str = "approved",
               exec_final: str = "end", item_id: int = 500):
    def planning_factory(log_dir, *, item_id=item_id, twig=None, provider=None,
                         gate_handler=None, process_config=None):
        calls.planning += 1
        node = "record_plan" if plan_verdict == "approved" else "record_needs_human"

        class _E:
            async def run(self, run_id):
                artifact = log_dir / f"plan-{item_id}.plan.tree.json"
                if decomposable and plan_verdict == "approved":
                    artifact.write_text(
                        json.dumps(
                            {
                                "schema_version": 2,
                                "plan_id": f"plan-{item_id}-test",
                                "item_id": item_id,
                                "decomposable": True,
                                "verdict": "approved",
                                "proposals": [
                                    {
                                        "title": "Leaf",
                                        "description": "body",
                                        "work_item_type": "Task",
                                    }
                                ],
                                "children": [
                                    {
                                        "item_id": item_id * 100 + 1,
                                        "plan_id": "leaf",
                                        "decomposable": False,
                                        "summary": "body",
                                        "review_iterations": 1,
                                        "final_verdict": "approved",
                                        "proposals": [],
                                        "children": [],
                                    }
                                ],
                            }
                        ),
                        encoding="utf-8",
                    )
                _write_log(log_dir, run_id, [
                    (node, _record_plan(decomposable=decomposable,
                                        verdict=plan_verdict, item_id=item_id,
                                        plan_artifact=str(artifact))),
                ])
                return Completed(run_id, "completed", "end", {})
        return _E()

    def commit_factory(log_dir, *, plan_tree_path=None, dry_run=None, twig=None,
                       manifest_path=None, gate_handler=None):
        calls.commit += 1
        calls.commit_dry_run = dry_run

        class _E:
            async def run(self, run_id):
                Path(manifest_path).write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "plan_id": f"plan-{item_id}-test",
                            "root_item_id": item_id,
                            "dry_run": False,
                            "id_map": {str(item_id * 100 + 1): 9001},
                        }
                    ),
                    encoding="utf-8",
                )
                _write_log(log_dir, run_id, [
                    ("write_manifest", {"kind": "success",
                                        "value": {"manifest_path": str(manifest_path)}}),
                ])
                return Completed(run_id, "completed", "end", {})
        return _E()

    def executor_factory(log_dir, *, inputs=None, toolbelt=None, gate_handler=None):
        calls.executor += 1
        calls.exec_inputs = inputs
        leaves = [{"leaf_id": l.leaf_id, "branch": l.branch} for l in inputs.leaves]

        class _E:
            async def run(self, run_id):
                _write_log(log_dir, run_id, [
                    ("resolve_leaves", {"kind": "success",
                                        "value": {"leaves": leaves}}),
                ])
                return Completed(run_id, "completed", exec_final, {})
        return _E()

    return planning_factory, commit_factory, executor_factory


async def test_run_pipeline_prefers_repo_root_for_twig_client(tmp_path, monkeypatch):
    repo_path = tmp_path / "worktree"
    repo_path.mkdir()
    repo_root = tmp_path / "repo-root"
    repo_root.mkdir()
    sentinel = object()
    observed: dict[str, Path | None] = {}

    def fake_make_twig_client(*, repo_path: Path | None, repo: Path | None) -> object:
        observed["repo_path"] = repo_path
        observed["repo"] = repo
        return sentinel

    monkeypatch.setattr("requiem.end_to_end._make_twig_client", fake_make_twig_client)

    calls = _Calls()
    pf, cf, ef = _factories(calls, decomposable=False, item_id=500)

    def planning_factory(log_dir, *, item_id=500, twig=None, provider=None,
                         gate_handler=None, process_config=None):
        assert twig is sentinel
        return pf(log_dir, item_id=item_id, twig=twig, provider=provider,
                  gate_handler=gate_handler, process_config=process_config)

    result = await run_pipeline(
        500, log_dir=tmp_path, board="requiem-500", assignee="w",
        live=True, repo_path=repo_path, repo=repo_root,
        planning_factory=planning_factory, commit_factory=cf, executor_factory=ef,
    )

    assert result.stage == "executor"
    assert result.status == "delivered"
    assert observed["repo_path"] is None
    assert observed["repo"] == repo_root


async def test_leaf_root_dispatches_root_as_single_leaf(tmp_path):
    calls = _Calls()
    pf, cf, ef = _factories(calls, decomposable=False, item_id=500)
    result = await run_pipeline(
        500, log_dir=tmp_path, board="requiem-500", assignee="w",
        live=True, planning_factory=pf, commit_factory=cf, executor_factory=ef,
    )
    assert result.stage == "executor"
    assert result.status == "delivered"
    assert result.decomposable is False
    assert result.leaf_ids == ("500",)
    assert result.committed_path is None
    # Atomic root never seeds children.
    assert calls.commit == 0
    # The single inline leaf is the root item, branch impl/<root>-<root>.
    assert calls.exec_inputs.leaves[0].leaf_id == "500"
    assert calls.exec_inputs.leaves[0].branch == "impl/500-500"


async def test_decomposable_without_commit_stops_planned(tmp_path):
    calls = _Calls()
    pf, cf, ef = _factories(calls, decomposable=True)
    result = await run_pipeline(
        500, log_dir=tmp_path, board="requiem-500",
        commit=False, planning_factory=pf, commit_factory=cf, executor_factory=ef,
    )
    assert result.stage == "planning"
    assert result.status == "planned"
    assert result.decomposable is True
    # No seeding, no dispatch without explicit commit.
    assert calls.commit == 0 and calls.executor == 0


async def test_decomposable_with_commit_seeds_then_dispatches(tmp_path):
    calls = _Calls()
    pf, cf, ef = _factories(calls, decomposable=True)
    result = await run_pipeline(
        500, log_dir=tmp_path, board="requiem-500", assignee="w",
        commit=True, live=True,
        planning_factory=pf, commit_factory=cf, executor_factory=ef,
    )
    assert result.stage == "executor"
    assert result.status == "delivered"
    assert calls.commit == 1 and calls.executor == 1
    # A faithful fan-out seeds for real, never a dry-run preview.
    assert calls.commit_dry_run is False
    # The executor is artifact-driven here (no inline leaves).
    assert calls.exec_inputs.leaves == ()
    assert calls.exec_inputs.plan_tree_path is not None
    assert calls.exec_inputs.committed_path is not None
    assert result.committed_path is not None


async def test_planning_not_approved_pauses(tmp_path):
    calls = _Calls()
    pf, cf, ef = _factories(calls, decomposable=True, plan_verdict="needs_human")
    result = await run_pipeline(
        500, log_dir=tmp_path, board="requiem-500",
        commit=True, planning_factory=pf, commit_factory=cf, executor_factory=ef,
    )
    assert result.stage == "planning"
    assert result.status == "paused"
    # Never proceeds past an unapproved plan.
    assert calls.commit == 0 and calls.executor == 0


async def test_accept_last_needs_human_still_pauses_before_mutation(tmp_path):
    calls = _Calls()
    pf, cf, ef = _factories(
        calls, decomposable=True, plan_verdict="needs_human"
    )
    result = await run_pipeline(
        500,
        log_dir=tmp_path,
        board="requiem-500",
        commit=True,
        escalation_policy="accept-last",
        planning_factory=pf,
        commit_factory=cf,
        executor_factory=ef,
    )
    assert result.stage == "planning"
    assert result.status == "paused"
    assert calls.commit == 0 and calls.executor == 0


async def test_executor_pause_surfaces(tmp_path):
    calls = _Calls()
    pf, cf, ef = _factories(calls, decomposable=False, exec_final="fail_end")
    result = await run_pipeline(
        500, log_dir=tmp_path, board="requiem-500", live=True,
        planning_factory=pf, commit_factory=cf, executor_factory=ef,
    )
    assert result.stage == "executor"
    assert result.status == "paused"
    assert result.executor_final_node == "fail_end"
