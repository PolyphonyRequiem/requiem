"""End-to-end integration test for the code-review demo workflow.

Promoted from the walking-skeleton α suite. Adds a fifth test —
INV-RESTART end-to-end — that the walking-skeleton ran in `demo_resume.py`
but not under pytest.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from requiem.kernel import Completed, Suspended
from requiem.persistence import replay
from requiem.workflows.code_review_demo import build_engine, scripted_provider


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs"
    d.mkdir()
    return d


def _silent_approve(_node_id: str, _prompt: str, _opts: tuple[str, ...]) -> str:
    return "approve"


def _make(log_dir: Path):
    return build_engine(log_dir, gate_handler=_silent_approve)


async def test_full_run_completes(log_dir: Path):
    """Happy path: every recommended variant fires once."""
    engine = _make(log_dir)
    result = await engine.run("smoke")
    assert isinstance(result, Completed), result
    assert result.disposition == "completed"
    assert result.final_node == "end"

    events = list(replay(log_dir / "smoke.events.jsonl"))
    kinds = [e["kind"] for e in events]
    assert "verb_completed" in kinds
    assert "team_dispatched" in kinds
    assert kinds.count("team_branch_completed") == 3

    # Bach A: monotonic ordering.
    ids = [e["event_id"] for e in events]
    assert ids == sorted(ids)

    # Brahms B: envelope-loose shape.
    for e in events:
        assert "kind" in e and "payload" in e and "run_id" in e


async def test_retry_then_succeeds(log_dir: Path):
    await _make(log_dir).run("retry")
    events = list(replay(log_dir / "retry.events.jsonl"))
    retries = [e for e in events if e["kind"] == "retry_attempted"]
    assert len(retries) == 1
    assert retries[0]["payload"]["next_attempt"] == 2


async def test_human_gate_suspends_without_handler(log_dir: Path):
    engine = _make(log_dir)
    engine.gate_handler = None
    result = await engine.run("gate")
    assert isinstance(result, Suspended), result
    assert result.node_id == "human_gate"
    assert "approve" in result.options


async def test_invariant_event_log_authoritative(log_dir: Path):
    """The log alone reconstructs the run state — INV-EVENT-LOG-AUTHORITATIVE."""
    await _make(log_dir).run("auth")
    log_path = log_dir / "auth.events.jsonl"
    assert log_path.exists()
    # No sidecar manifest.
    assert not any(p.name.endswith(".manifest.yaml") for p in log_dir.iterdir())
    from requiem.kernel import _projection
    proj = _projection(list(replay(log_path)))
    assert proj["terminal"] == "completed"
    assert proj["team_branches_completed"] == 3


async def test_inv_restart_resume_skips_completed_nodes(log_dir: Path):
    """INV-RESTART: truncate the log mid-workflow, then resume with the
    same run_id. The reviewer scripts are one-shot — if the engine
    re-ran them, the second call would return `fake.exhausted`."""
    run_id = "restart"
    await _make(log_dir).run(run_id)
    log_path = log_dir / f"{run_id}.events.jsonl"

    # truncate to just-after-review_team-completed (before synthesize)
    lines = log_path.read_text(encoding="utf-8").splitlines()
    keep: list[str] = []
    for raw in lines:
        keep.append(raw)
        ev = json.loads(raw)
        if ev["kind"] == "verb_completed" and ev.get("node_id") == "review_team":
            break
    log_path.write_text("\n".join(keep) + "\n", encoding="utf-8")

    # second engine; reviewer scripts have already been consumed once
    # (a fresh provider here would also work — but our engine ctor calls
    # `scripted_provider()`, so the scripts come fresh; the test relies
    # on the engine NOT calling the reviewer agents on resume).
    engine2 = _make(log_dir)
    result = await engine2.run(run_id)
    assert isinstance(result, Completed), result
    enters = [e["node_id"] for e in replay(log_path) if e["kind"] == "node_entered"]
    counts = {n: enters.count(n) for n in
              {"read_snippet", "flaky_lint", "review_team", "synthesize", "archive"}}
    assert counts["read_snippet"] == 1, enters
    assert counts["review_team"] == 1, enters
    assert counts["synthesize"] == 1, enters
    assert counts["archive"] == 1, enters

    # The provider ran only the synthesizer (not the reviewers).
    calls = [c["agent"] for c in engine2.provider.calls]
    assert calls == ["synthesizer"]


def test_scripted_provider_constructs_cleanly():
    p = scripted_provider()
    assert "style_reviewer" in p.scripts
    assert "synthesizer" in p.scripts
