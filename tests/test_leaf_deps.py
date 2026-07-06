"""Unit tests for requiem.workflows.leaf_deps — the shared dependency-graph
validation and wave-scheduling module used by both fan-out backends."""

from __future__ import annotations

from requiem.workflows.leaf_deps import (
    compute_blocked,
    releasable_leaves,
    validate_dep_graph,
)


# ---- validate_dep_graph --------------------------------------------------


def test_validate_no_deps_all_ready():
    deps_of = {"a": (), "b": (), "c": ()}
    error, ready = validate_dep_graph(deps_of)
    assert error is None
    assert set(ready) == {"a", "b", "c"}


def test_validate_linear_chain_only_root_ready():
    deps_of = {"a": (), "b": ("a",), "c": ("b",)}
    error, ready = validate_dep_graph(deps_of)
    assert error is None
    assert ready == ("a",)


def test_validate_self_dep_rejected():
    deps_of = {"a": ("a",)}
    error, ready = validate_dep_graph(deps_of)
    assert error is not None
    assert "depends on itself" in error
    assert ready == ()


def test_validate_unknown_dep_rejected():
    deps_of = {"a": ("ghost",)}
    error, ready = validate_dep_graph(deps_of)
    assert error is not None
    assert "unknown leaf" in error
    assert ready == ()


def test_validate_cycle_rejected():
    deps_of = {"a": ("b",), "b": ("a",)}
    error, ready = validate_dep_graph(deps_of)
    assert error is not None
    assert "cycle" in error
    assert ready == ()


def test_validate_diamond_shape_ready_frontier():
    # a -> b, a -> c, b -> d, c -> d (d depends on both b and c)
    deps_of = {"a": (), "b": ("a",), "c": ("a",), "d": ("b", "c")}
    error, ready = validate_dep_graph(deps_of)
    assert error is None
    assert ready == ("a",)


# ---- compute_blocked ------------------------------------------------------


def test_compute_blocked_propagates_transitively():
    deps_of = {"a": (), "b": ("a",), "c": ("b",)}
    blocked = compute_blocked(
        deps_of, nondelivered={"a"}, settled={"a"},
    )
    assert blocked == {"b", "c"}


def test_compute_blocked_none_when_deps_delivered():
    deps_of = {"a": (), "b": ("a",)}
    blocked = compute_blocked(
        deps_of, nondelivered=set(), settled={"a"},
    )
    assert blocked == set()


def test_compute_blocked_respects_already_blocked():
    deps_of = {"a": (), "b": ("a",), "c": ("b",)}
    blocked = compute_blocked(
        deps_of, nondelivered=set(), settled=set(),
        already_blocked=frozenset({"a"}),
    )
    assert blocked == {"a", "b", "c"}


def test_compute_blocked_settled_leaf_never_marked():
    # b is nondelivered AND already settled — c (which depends on b) must
    # still be reported as blocked even though b itself isn't re-flagged.
    deps_of = {"b": (), "c": ("b",)}
    blocked = compute_blocked(
        deps_of, nondelivered={"b"}, settled={"b"},
    )
    assert blocked == {"c"}
    assert "b" not in blocked


# ---- releasable_leaves ----------------------------------------------------


def test_releasable_leaves_with_no_deps():
    deps_of = {"a": (), "b": ()}
    releasable = releasable_leaves(
        deps_of, delivered=set(), settled=set(), blocked=set(),
    )
    assert releasable == {"a", "b"}


def test_releasable_leaves_waits_for_delivery():
    deps_of = {"a": (), "b": ("a",)}
    releasable = releasable_leaves(
        deps_of, delivered=set(), settled=set(), blocked=set(),
    )
    assert releasable == {"a"}
    releasable_after = releasable_leaves(
        deps_of, delivered={"a"}, settled={"a"}, blocked=set(),
    )
    assert releasable_after == {"b"}


def test_releasable_leaves_excludes_settled_and_blocked():
    deps_of = {"a": (), "b": (), "c": ()}
    releasable = releasable_leaves(
        deps_of, delivered=set(), settled={"a"}, blocked={"b"},
    )
    assert releasable == {"c"}
