"""Workflow path-coverage harness — Brahms-harness B (pytest fixtures over real engine).

Promoted from Phase A per ADR 0002. The harness drives the real
``requiem.kernel.Engine`` end-to-end, substituting only the LLM
boundary (``AgentProvider``) and the external-process boundary
(``Toolbelt``) with scripted fakes. Every node category and every
verb outcome variant becomes addressable from a scenario file that
fits comfortably in ~30 lines.

Public surface:

* :func:`scenario`            — Scenario factory (kwargs-only)
* :func:`run_scenario`        — one-shot runner; returns ScenarioResult
* :class:`Scenario`           — dataclass: workflow + inputs + scripts + expectations
* :class:`ScenarioResult`     — dataclass: raw result + events + assertion methods
* :class:`Harness`            — multi-run object with truncate-log + resume helpers
* :class:`FakeAgent`          — scripted AgentProvider (wraps FakeProvider)
* :class:`FakeToolbelt`       — scripted Toolbelt builder

Free-function assertion helpers (also methods on ScenarioResult):

* :func:`assert_completed`        — Completed with optional disposition
* :func:`assert_needs_human`      — Suspended at a (optionally named) gate
* :func:`assert_visited_nodes`    — every named node appears in node_entered events
* :func:`assert_no_retry`         — no retry_attempted events
* :func:`assert_cancelled`        — terminated cancelled
* :func:`assert_short_circuited`  — INV-CANCEL-SHORT-CIRCUITS-RETRY honoured
"""
from __future__ import annotations

from requiem.harness.assertions import (
    assert_cancelled,
    assert_completed,
    assert_needs_human,
    assert_no_retry,
    assert_short_circuited,
    assert_visited_nodes,
    assert_terminal_state_matches,
)
from requiem.harness.fakes import (
    FakeAgent,
    FakeGhClient,
    FakeGitClient,
    FakeToolbelt,
    FakeTwigClient,
)
from requiem.harness.harness import Harness
from requiem.harness.scenario import (
    Scenario,
    ScenarioResult,
    run_scenario,
    scenario,
)

__all__ = [
    # core
    "scenario",
    "run_scenario",
    "Scenario",
    "ScenarioResult",
    "Harness",
    # fakes
    "FakeAgent",
    "FakeToolbelt",
    "FakeGitClient",
    "FakeGhClient",
    "FakeTwigClient",
    # assertions
    "assert_completed",
    "assert_needs_human",
    "assert_visited_nodes",
    "assert_no_retry",
    "assert_cancelled",
    "assert_short_circuited",
    "assert_terminal_state_matches",
]
