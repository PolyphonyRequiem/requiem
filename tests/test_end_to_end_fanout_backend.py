"""Tests for the in-process fan-out dispatch backend in ``run_pipeline`` (#4).

The driver gained ``dispatch_backend="fanout"`` (ADR-0021): instead of fanning
leaves out to an external Hermes worker via kanban_executor, it runs the
``requiem.workflows.fanout`` orchestrator in-process. These tests inject a stub
planning engine (no LLM/ADO) but drive the REAL fanout engine against a throwaway
git repo + a real implementation provider — so the wiring is exercised end to end
without any external creds. The default ``dispatch_backend="kanban"`` path is
covered (unchanged) by test_end_to_end.py / test_end_to_end_topology.py.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from requiem.agent import FakeProvider
from requiem.end_to_end import run_pipeline
from requiem.kernel import Completed


def _write_log(log_dir: Path, run_id: str, events: list[tuple[str, dict]]) -> None:
    path = log_dir / f"{run_id}.events.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for node_id, outcome in events:
            fh.write(json.dumps({
                "kind": "verb_completed", "node_id": node_id,
                "payload": {"outcome": outcome},
            }) + "\n")


def _atomic_plan(item_id: int) -> dict:
    """A planning record for an ATOMIC root (decomposable=False) — the driver
    dispatches the item itself as one inline leaf."""
    return {
        "kind": "success",
        "value": {
            "item_id": item_id, "item_title": f"item {item_id}",
            "summary": "implement the thing", "decomposable": False,
            "final_verdict": "approved",
            "plan_artifact": f"/logs/plan-{item_id}.plan.tree.json",
        },
    }


def _stub_planning(item_id: int):
    def planning_factory(log_dir, *, item_id=item_id, twig=None, provider=None,
                         gate_handler=None, process_config=None):
        class _E:
            async def run(self, run_id):
                _write_log(log_dir, run_id, [("record_plan", _atomic_plan(item_id))])
                return Completed(run_id, "completed", "end", {})
        return _E()
    return planning_factory


def _repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(cmd, cwd=r, check=True)
    (r / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=r, check=True)
    return r


def _happy_coder() -> FakeProvider:
    return FakeProvider(scripts={"coder": [{
        "intent_summary": "create marker",
        "file_changes": [{"path": "M.md", "operation": "create", "content": "x\n"}],
        "notes": "",
    }]})


async def test_fanout_backend_dispatches_atomic_root_in_process(tmp_path: Path):
    """dispatch_backend='fanout' runs the in-process orchestrator: the atomic
    root is dispatched as one leaf and lands (dry-run preview)."""
    repo = _repo(tmp_path)
    result = await run_pipeline(
        700, log_dir=tmp_path, board="requiem-test",
        dispatch_backend="fanout", repo_path=repo,
        provider=_happy_coder(), live=False,
        planning_factory=_stub_planning(700),
    )
    assert result.stage == "fanout"
    assert result.status == "delivered", result.detail
    assert result.fanout_verdict == "previewed"  # dry-run
    assert result.fanout_leaves_total == 1
    assert result.fanout_leaves_landed == 1
    assert result.leaf_ids == ("700",)
    # The in-process child wrote its own isolated log.
    assert (tmp_path / "fanout-700__leaf-700.events.jsonl").exists()


async def test_fanout_backend_requires_repo_path(tmp_path: Path):
    """Without repo_path the fanout backend fails closed (no working tree to
    mutate) — it never silently no-ops."""
    result = await run_pipeline(
        700, log_dir=tmp_path, board="requiem-test",
        dispatch_backend="fanout", repo_path=None,
        provider=_happy_coder(), live=False,
        planning_factory=_stub_planning(700),
    )
    assert result.status == "paused"
    assert "repo_path" in result.detail


async def test_fanout_backend_surfaces_surrendered_leaf(tmp_path: Path):
    """A leaf whose coder emits bad output surrenders; the driver reports the
    fan-out paused for a human (B2 roll-up), not delivered."""
    repo = _repo(tmp_path)
    bad_provider = FakeProvider(scripts={"coder": [{"garbage": "not a CoderOutput"}]})
    result = await run_pipeline(
        700, log_dir=tmp_path, board="requiem-test",
        dispatch_backend="fanout", repo_path=repo,
        provider=bad_provider, live=False,
        planning_factory=_stub_planning(700),
    )
    assert result.status == "paused"
    assert result.fanout_leaves_total == 1
    assert result.fanout_leaves_landed == 0
    assert "need a human" in result.detail


async def test_default_backend_is_kanban_unchanged(tmp_path: Path):
    """Omitting dispatch_backend keeps the legacy kanban path: with no
    github_repo and an atomic root, the executor stub path runs (proving the
    fanout branch is NOT taken by default)."""
    # A stub executor records that the kanban path was taken.
    taken = {"executor": False}

    def executor_factory(log_dir, *, inputs=None, toolbelt=None, gate_handler=None):
        taken["executor"] = True

        class _E:
            async def run(self, run_id):
                _write_log(log_dir, run_id, [
                    ("resolve_leaves", {"kind": "success", "value": {
                        "leaves": [{"leaf_id": "700"}]}}),
                ])
                return Completed(run_id, "completed", "end", {})
        return _E()

    result = await run_pipeline(
        700, log_dir=tmp_path, board="requiem-test",
        planning_factory=_stub_planning(700),
        executor_factory=executor_factory,
    )
    assert taken["executor"] is True
    assert result.stage == "executor"
    # The fanout-specific fields stay at their defaults on the kanban path.
    assert result.fanout_verdict is None
