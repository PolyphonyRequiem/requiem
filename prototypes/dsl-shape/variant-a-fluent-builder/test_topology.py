"""Harness-style topology tests for variant A.

Asserts `from X on Y, transition to Z` WITHOUT running the workflow.
This is the path-coverage primitive the harness needs to enumerate
scenarios cheaply.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from builder import WorkflowBuilder  # noqa: E402
from core import next_node, verify_done  # noqa: E402
from demo import build_close_out  # noqa: E402


def test_verify_success_goes_to_human_approve():
    wf = build_close_out()
    assert next_node(wf, "verify", "success") == "human_approve"


def test_verify_failure_goes_to_end():
    wf = build_close_out()
    assert next_node(wf, "verify", "failure") == "$end"


def test_human_approve_failure_goes_to_end():
    wf = build_close_out()
    assert next_node(wf, "human_approve", "failure") == "$end"


def test_archive_wildcard_route_goes_to_end():
    wf = build_close_out()
    assert next_node(wf, "archive", "success") == "$end"


def test_typo_route_raises_at_build():
    with pytest.raises(ValueError, match="unknown node 'nope'"):
        (
            WorkflowBuilder("bad")
            .entry("a")
            .verb("a", verify_done)
            .route("a", on="success", to="nope")
            .build()
        )
