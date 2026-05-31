"""Harness-style topology tests for variant C."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from core import Node, NodeKind, Route, Workflow, next_node, verify_done  # noqa: E402
from demo import CLOSE_OUT  # noqa: E402


def test_verify_success_goes_to_human_approve():
    assert next_node(CLOSE_OUT, "verify", "success") == "human_approve"


def test_verify_failure_goes_to_end():
    assert next_node(CLOSE_OUT, "verify", "failure") == "$end"


def test_human_approve_failure_goes_to_end():
    assert next_node(CLOSE_OUT, "human_approve", "failure") == "$end"


def test_archive_wildcard_route_goes_to_end():
    assert next_node(CLOSE_OUT, "archive", "success") == "$end"


def test_typo_route_raises_at_construction():
    with pytest.raises(Exception, match="unknown node 'nope'"):
        Workflow(
            name="bad",
            entry="a",
            nodes=[Node(name="a", kind=NodeKind.VERB, verb=verify_done)],
            routes=[Route(from_node="a", when="success", to_node="nope")],
        )
