"""Harness-style topology tests for variant B."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from decorators import route, verb, workflow  # noqa: E402
from core import Success, next_node  # noqa: E402
from demo import CloseOut  # noqa: E402


def test_verify_success_goes_to_human_approve():
    assert next_node(CloseOut, "verify", "success") == "human_approve"


def test_verify_failure_goes_to_end():
    assert next_node(CloseOut, "verify", "failure") == "$end"


def test_human_approve_failure_goes_to_end():
    assert next_node(CloseOut, "human_approve", "failure") == "$end"


def test_archive_wildcard_route_goes_to_end():
    assert next_node(CloseOut, "archive", "success") == "$end"


def test_typo_route_raises_at_class_decoration():
    with pytest.raises(ValueError, match="unknown node 'nope'"):
        @workflow("bad", entry="a")
        class Bad:
            @verb
            @staticmethod
            def a(ctx):
                return Success()

            routes = [route("a", on="success", to="nope")]
