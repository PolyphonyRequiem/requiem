"""End-to-end integration test for the code-review demo workflow.

**Migrated to the Brahms-harness B fixture-based DSL** (Phase B / Handel).
This file replaces the ad-hoc test by the same name; it is the canonical
demonstration that the harness API is ergonomic enough to express every
shape the hand-written predecessor expressed.

For the line-level comparison see PR #20 (Handel) — the previous version
of this file lives at that PR's parent commit (`tests/test_integration_code_review.py`
@ 18e6b2b).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from requiem.harness import Harness, scenario


# A single scenario, reused across happy-path tests. Per the brief: a
# "scenario file" for a complex workflow should fit in ~30 LOC.
def _code_review_scenario(
    snippet_path: Path,
    *,
    gate_choices="approve",
    run_id: str = "smoke",
):
    return scenario(
        workflow="requiem.workflows.code_review_demo",
        extra_engine_kwargs={"snippet_path": snippet_path},
        agent_outputs={
            "style_reviewer": {
                "severity": "warn",
                "category": "style",
                "summary": "mutable default argument `cache={}` will leak state",
                "line_hint": 3,
            },
            "correctness_reviewer": {
                "severity": "blocking",
                "category": "correctness",
                "summary": "`int(x)` raises ValueError on bad input",
                "line_hint": 5,
            },
            "performance_reviewer": {
                "severity": "info",
                "category": "performance",
                "summary": "linear scan of `cache.keys()` could be O(1)",
                "line_hint": 7,
            },
            "synthesizer": {
                "recommend_merge": False,
                "rationale": "1 blocking + 1 warn; ValueError must be fixed",
                "top_finding": "unhandled ValueError on int(x)",
                "severity_seen": ["warn", "blocking", "info"],
            },
        },
        # Snippet content is delivered through the FakeToolbelt instead of
        # touching the real filesystem — keeps the test hermetic.
        tool_outputs={
            ("files", "read_text", str(snippet_path)): (
                "def lookup_or_compute(x, cache={}):\n"
                "    if x in cache.keys():\n"
                "        return cache[x]\n"
                "    value = int(x) * 2\n"
                "    cache[x] = value\n"
                "    return value\n"
            ),
        },
        gate_choices=gate_choices,
        expected_terminal="completed",
        run_id=run_id,
    )


# ---- happy path ------------------------------------------------------


def test_full_run_completes(harness: Harness, tmp_path: Path):
    """Happy path: every node enters once (except retried lint)."""
    snippet = tmp_path / "snippet.py"
    result = harness.run(_code_review_scenario(snippet, run_id="smoke"))

    result.assert_completed(disposition="completed", final_node="end")
    result.assert_visited(
        ["start", "read_snippet", "flaky_lint", "review_team",
         "synthesize", "human_gate", "archive", "end"],
    )

    # Bach A: monotonic ordering of event_ids.
    ids = [e["event_id"] for e in result.events]
    assert ids == sorted(ids)

    # Brahms B: envelope-loose shape — every event has the three core fields.
    for e in result.events:
        assert "kind" in e and "payload" in e and "run_id" in e

    # The team dispatch fired exactly the three reviewer branches.
    branches = [e for e in result.events if e["kind"] == "team_branch_completed"]
    assert len(branches) == 3


# ---- retry -----------------------------------------------------------


def test_retry_then_succeeds(harness: Harness, tmp_path: Path):
    """The flaky_lint verb retries exactly once before succeeding."""
    snippet = tmp_path / "snippet.py"
    result = harness.run(_code_review_scenario(snippet, run_id="retry"))

    result.assert_completed()
    assert result.retries == 1, result.retries
    retry_evt = next(e for e in result.events if e["kind"] == "retry_attempted")
    assert retry_evt["payload"]["next_attempt"] == 2


# ---- human gate suspension ------------------------------------------


def test_human_gate_suspends_without_handler(harness: Harness, tmp_path: Path):
    """gate_choices=None → kernel returns Suspended on first gate."""
    snippet = tmp_path / "snippet.py"
    scn = _code_review_scenario(snippet, gate_choices=None, run_id="gate")
    result = harness.run(scn)

    result.assert_needs_human(gate="human_gate")
    # Defensive: the suspended-shape advertises the options the workflow named.
    from requiem.kernel import Suspended

    assert isinstance(result.raw, Suspended)
    assert "approve" in result.raw.options
    assert "reject" in result.raw.options


# ---- INV-EVENT-LOG-AUTHORITATIVE ------------------------------------


def test_invariant_event_log_authoritative(harness: Harness, tmp_path: Path):
    """The log alone reconstructs the run state — no sidecar manifest."""
    snippet = tmp_path / "snippet.py"
    result = harness.run(_code_review_scenario(snippet, run_id="auth"))

    assert result.log_path.exists()
    sidecars = [p for p in harness.log_dir.iterdir() if p.suffix == ".yaml"]
    assert sidecars == [], f"unexpected sidecar manifests: {sidecars!r}"

    from requiem.kernel import _projection
    from requiem.persistence import replay

    proj = _projection(list(replay(result.log_path)))
    assert proj["terminal"] == "completed"
    assert proj["team_branches_completed"] == 3


# ---- INV-RESTART: truncate + resume -------------------------------


def test_inv_restart_resume_skips_completed_nodes(harness: Harness, tmp_path: Path):
    """Truncate the log mid-workflow; resume; verify the engine did not
    re-invoke verbs whose verb_completed event already exists."""
    snippet = tmp_path / "snippet.py"
    scn = _code_review_scenario(snippet, run_id="restart")
    full = harness.run(scn)
    full.assert_completed()

    # Truncate at review_team.verb_completed — synthesize / archive / end
    # are everything the resume should still do.
    keep_until = next(
        int(e["event_id"])
        for e in full.events
        if e["kind"] == "verb_completed" and e.get("node_id") == "review_team"
    )
    truncated = harness.truncate_log("restart", after_event=keep_until)
    resumed = harness.resume(scn, truncated)

    resumed.assert_completed()
    resumed.assert_terminal_state_matches(full)

    # The reviewer agents must NOT have been called on the resumed run —
    # only the synthesizer was still outstanding. (`agent_calls` reflects
    # the resumed engine's fake-provider counter, not the original run's.)
    assert resumed.agent_calls == ["synthesizer"], resumed.agent_calls

    # And `read_snippet`, `flaky_lint`, `review_team` did NOT re-enter
    # on the resumed run — the kernel skipped them because their
    # verb_completed events are already in the log.
    from requiem.persistence import replay

    enters_after_truncate = [
        e["node_id"]
        for e in replay(resumed.log_path)
        if e["kind"] == "node_entered" and int(e["event_id"]) > keep_until
    ]
    assert "review_team" not in enters_after_truncate
    assert "synthesize" in enters_after_truncate
    assert "archive" in enters_after_truncate


# ---- module-internal sanity (unchanged from the pre-harness file) --


def test_scripted_provider_constructs_cleanly():
    from requiem.workflows.code_review_demo import scripted_provider

    p = scripted_provider()
    assert "style_reviewer" in p.scripts
    assert "synthesizer" in p.scripts
