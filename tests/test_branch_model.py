"""branch_model tests — the Option-D merge-group topology authority (ADR-0006).

Pins: the four ref-class constructors, the two-delimiter discipline, fail-closed
rejection of unsafe ids, and round-trip parse fidelity for attribution.
"""
from __future__ import annotations

import pytest

from requiem import branch_model as bm


# ---- constructors ------------------------------------------------------


def test_feature_trunk():
    assert bm.feature_trunk(500) == "feature/500"
    assert bm.feature_trunk("demo") == "feature/demo"


def test_plan_branch():
    assert bm.plan_branch(7000) == "plan/7000"


def test_impl_branch():
    assert bm.impl_branch(500, 501) == "impl/500-501"
    assert bm.impl_branch("demo", 22001) == "impl/demo-22001"


def test_evidence_branch():
    assert bm.evidence_branch(500, 501) == "evidence/500-501"


def test_only_two_delimiters_no_underscore_no_mg():
    # Option D collapses the recursive mg/<root>_<path> layer: the only
    # delimiters that appear are '/' (ref-class) and '-' (root-item payload).
    name = bm.impl_branch(12, 34)
    assert "_" not in name
    assert not name.startswith("mg/")
    assert name.count("/") == 1
    assert name.split("/", 1)[1].count("-") == 1


# ---- fail-closed validation -------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["a-b", "a/b", "a_b", "a b", "", "a.b", "a~b", "a:b", "feature/x"],
)
def test_unsafe_root_rejected(bad):
    with pytest.raises(bm.BranchModelError):
        bm.feature_trunk(bad)


@pytest.mark.parametrize("bad", ["a-b", "x/y", "p_q", " ", ""])
def test_unsafe_item_rejected(bad):
    with pytest.raises(bm.BranchModelError):
        bm.impl_branch("500", bad)
    # A dashed root would make the <root>-<item> payload ambiguous.
    with pytest.raises(bm.BranchModelError):
        bm.impl_branch(bad, "501")


# ---- parse / round-trip ------------------------------------------------


def test_parse_feature():
    ref = bm.parse_branch("feature/500")
    assert ref == bm.BranchRef(ref_class="feature", root="500", item=None)
    assert ref.is_leaf_ref is False
    assert ref.rebuild() == "feature/500"


def test_parse_plan():
    ref = bm.parse_branch("plan/7000")
    assert ref.ref_class == "plan"
    assert ref.root == "7000"
    assert ref.item is None


def test_parse_impl():
    ref = bm.parse_branch("impl/500-501")
    assert ref == bm.BranchRef(ref_class="impl", root="500", item="501")
    assert ref.is_leaf_ref is True
    assert ref.rebuild() == "impl/500-501"


def test_parse_evidence():
    ref = bm.parse_branch("evidence/demo-22001")
    assert ref.ref_class == "evidence"
    assert ref.root == "demo"
    assert ref.item == "22001"


@pytest.mark.parametrize(
    "name",
    [
        "main",
        "feature/a/b",          # too many segments
        "mg/500_1",             # collapsed ref-class
        "impl/500",             # missing item payload
        "impl/500-",            # empty item
        "impl/-501",            # empty root
        "impl/a-b-c",           # ambiguous double dash
        "wip/500",              # unknown ref-class
        "feature/a-b",          # root-only class can't carry a dash payload
    ],
)
def test_parse_rejects_non_requiem_refs(name):
    assert bm.parse_branch(name) is None


def test_round_trip_all_classes():
    for ctor, name in (
        (bm.feature_trunk, "feature/42"),
        (bm.plan_branch, "plan/42"),
    ):
        assert bm.parse_branch(ctor(42)).rebuild() == name
    for ctor, name in (
        (bm.impl_branch, "impl/42-7"),
        (bm.evidence_branch, "evidence/42-7"),
    ):
        assert bm.parse_branch(ctor(42, 7)).rebuild() == name
