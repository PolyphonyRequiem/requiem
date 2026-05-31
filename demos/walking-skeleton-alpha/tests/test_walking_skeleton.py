"""Brahms-harness B — pytest fixtures driving the in-process engine.

Scenarios are plain pytest functions; assertions are plain Python; the
seam injection points (provider, gate handler, toolbelt) are fixtures.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# put demo dir on path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.kernel import Completed, Engine, Failed, Suspended
from engine.persistence import replay
from engine.toolbelt import Toolbelt
from reviewers import scripted_provider
from workflow import build_agent_registry, build_verb_registry, build_workflow


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs"
    d.mkdir()
    return d


@pytest.fixture
def auto_approve():
    def _gate(_node_id: str, _prompt: str, _opts):
        return "approve"
    return _gate


@pytest.fixture
def make_engine(log_dir, auto_approve):
    def _factory(*, provider=None, gate=None):
        return Engine(
            workflow=build_workflow(),
            verbs=build_verb_registry(),
            agents=build_agent_registry(),
            provider=provider or scripted_provider(),
            toolbelt=Toolbelt.real(),
            log_dir=log_dir,
            gate_handler=gate or auto_approve,
        )
    return _factory


@pytest.mark.asyncio
async def test_full_run_completes(make_engine, log_dir):
    """The end-to-end happy path: every recommended variant fires once."""
    pytest.importorskip("pytest_asyncio")
    engine = make_engine()
    result = await engine.run("smoke")
    assert isinstance(result, Completed), result
    assert result.disposition == "completed"
    assert result.final_node == "end"

    events = list(replay(log_dir / "smoke.events.jsonl"))
    kinds = [e["kind"] for e in events]
    # Stravinsky outcomes ride in verb_completed payloads.
    assert "verb_completed" in kinds
    # Beethoven Q-K7 fired.
    assert "team_dispatched" in kinds
    assert kinds.count("team_branch_completed") == 3
    # Bach: every event has event_id and runs in order.
    ids = [e["event_id"] for e in events]
    assert ids == sorted(ids)
    # Brahms envelope-loose: a v2 reader could ingest a v3 event we don't
    # understand without crashing. (Just check the envelope shape.)
    for e in events:
        assert "kind" in e and "payload" in e and "run_id" in e


@pytest.mark.asyncio
async def test_retry_then_succeeds(make_engine, log_dir):
    """flaky_lint returns RetryableFailure on attempt 1, Success on attempt 2."""
    await make_engine().run("retry")
    events = list(replay(log_dir / "retry.events.jsonl"))
    retries = [e for e in events if e["kind"] == "retry_attempted"]
    assert len(retries) == 1, f"expected exactly one retry, got {retries}"
    assert retries[0]["payload"]["next_attempt"] == 2


@pytest.mark.asyncio
async def test_human_gate_suspends_without_handler(make_engine, log_dir):
    """No gate handler → the engine suspends (proves the gate is real)."""
    engine = make_engine(gate=None)
    # null out the handler explicitly
    engine.gate_handler = None
    result = await engine.run("gate")
    assert isinstance(result, Suspended), result
    assert result.node_id == "human_gate"
    assert "approve" in result.options


@pytest.mark.asyncio
async def test_invariant_event_log_authoritative(make_engine, log_dir):
    """The log alone reconstructs the run state — INV-EVENT-LOG-AUTHORITATIVE."""
    await make_engine().run("auth")
    events = list(replay(log_dir / "auth.events.jsonl"))
    # No sidecar manifest. The log is everything.
    assert (log_dir / "auth.events.jsonl").exists()
    assert not any(p.name.endswith(".manifest.yaml") for p in log_dir.iterdir())
    # Projection re-derived from the log alone:
    from engine.kernel import _projection
    proj = _projection(events)
    assert proj["terminal"] == "completed"
    assert proj["team_branches_completed"] == 3
