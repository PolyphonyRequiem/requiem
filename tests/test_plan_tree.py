"""Tests for the committed-plan leaf resolver (``requiem.plan_tree``).

Pure-function tests: build plan-tree + committed-manifest dicts in a tmp dir
and assert the enumerated leaves (and the failure taxonomy). No subprocesses,
no engine — this is the spec's ``load_committed`` contract in isolation.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from requiem.plan_tree import (
    PlanArtifactError,
    ResolvedLeaf,
    load_committed_leaves,
)


def _write(p: Path, obj: dict) -> Path:
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


def _tree(**over) -> dict:
    """A two-level approved tree: root(7000) → [child A 700001 decomposable
    into two leaf grandchildren, child B 700002 leaf]. Synth ids follow
    ``parent*100 + index + 1``.
    """
    base = {
        "schema_version": 2,
        "plan_id": "plan-7000",
        "item_id": 7000,
        "decomposable": True,
        "verdict": "approved",
        "proposals": [
            {"title": "Child A", "description": "decomposes", "work_item_type": "Story"},
            {"title": "Child B", "description": "a leaf", "work_item_type": "Task",
             "review_group": "rg1"},
        ],
        "children": [
            {
                "item_id": 700001, "plan_id": "p", "decomposable": True,
                "proposals": [
                    {"title": "Leaf A1", "description": "first", "work_item_type": "Task"},
                    {"title": "Leaf A2", "description": "second", "work_item_type": "Task"},
                ],
                "children": [
                    {"item_id": 70000101, "decomposable": False, "proposals": [], "children": []},
                    {"item_id": 70000102, "decomposable": False, "proposals": [], "children": []},
                ],
            },
            {"item_id": 700002, "decomposable": False, "proposals": [], "children": []},
        ],
    }
    base.update(over)
    return base


def _committed(id_map: dict[int, int] | None = None, **over) -> dict:
    base = {
        "schema_version": 1,
        "plan_id": "plan-7000",
        "root_item_id": 7000,
        "dry_run": False,
        "id_map": {str(k): v for k, v in (id_map or {
            700001: 8001, 700002: 8002, 70000101: 8101, 70000102: 8102,
        }).items()},
    }
    base.update(over)
    return base


def test_enumerates_decomposable_false_leaves_depth_first(tmp_path):
    leaves = load_committed_leaves(
        _write(tmp_path / "t.json", _tree()),
        _write(tmp_path / "c.json", _committed()),
    )
    # The two grandchildren (under decomposable Child A) then Child B.
    assert [l.real_id for l in leaves] == [8101, 8102, 8002]
    assert all(isinstance(l, ResolvedLeaf) for l in leaves)


def test_leaf_metadata_comes_from_parent_proposal(tmp_path):
    leaves = load_committed_leaves(
        _write(tmp_path / "t.json", _tree()),
        _write(tmp_path / "c.json", _committed()),
    )
    by_real = {l.real_id: l for l in leaves}
    # Grandchild metadata from Child A's proposals[0]/[1].
    assert by_real[8101].title == "Leaf A1"
    assert by_real[8101].body == "first"
    assert by_real[8101].work_item_type == "Task"
    # Child B metadata + review_group from root proposals[1].
    assert by_real[8002].title == "Child B"
    assert by_real[8002].review_group == "rg1"


def test_pinned_leaf_uses_its_own_real_id(tmp_path):
    tree = _tree()
    # Pin Child B to an existing ADO id; it should not need an id_map entry.
    tree["proposals"][1]["item_id"] = 9999
    tree["children"][1]["item_id"] = 9999
    leaves = load_committed_leaves(
        _write(tmp_path / "t.json", tree),
        _write(tmp_path / "c.json", _committed(id_map={
            700001: 8001, 70000101: 8101, 70000102: 8102,
        })),
    )
    assert {l.real_id for l in leaves} == {8101, 8102, 9999}


@pytest.mark.parametrize("mutate,kind", [
    (lambda t, c: t.__setitem__("verdict", "needs_human"), "not_approved"),
    (lambda t, c: t.__setitem__("schema_version", 1), "unsupported_schema"),
    (lambda t, c: t.__setitem__("decomposable", False), "leaf_root"),
    (lambda t, c: c.__setitem__("dry_run", True), "dry_run"),
    (lambda t, c: c.__setitem__("root_item_id", 999), "root_mismatch"),
])
def test_header_and_manifest_failures(tmp_path, mutate, kind):
    tree, committed = _tree(), _committed()
    mutate(tree, committed)
    with pytest.raises(PlanArtifactError) as ei:
        load_committed_leaves(
            _write(tmp_path / "t.json", tree),
            _write(tmp_path / "c.json", committed),
        )
    assert ei.value.kind == kind


def test_missing_id_map_entry_for_leaf_is_unmapped(tmp_path):
    with pytest.raises(PlanArtifactError) as ei:
        load_committed_leaves(
            _write(tmp_path / "t.json", _tree()),
            # Drop the mapping for grandchild 70000102.
            _write(tmp_path / "c.json", _committed(id_map={
                700001: 8001, 700002: 8002, 70000101: 8101,
            })),
        )
    assert ei.value.kind == "unmapped_leaf"


def test_misaligned_proposals_vs_children(tmp_path):
    tree = _tree()
    tree["children"][0]["children"].pop()  # 2 proposals, 1 child
    with pytest.raises(PlanArtifactError) as ei:
        load_committed_leaves(
            _write(tmp_path / "t.json", tree),
            _write(tmp_path / "c.json", _committed()),
        )
    assert ei.value.kind == "misaligned"


def test_duplicate_real_id_rejected(tmp_path):
    with pytest.raises(PlanArtifactError) as ei:
        load_committed_leaves(
            _write(tmp_path / "t.json", _tree()),
            _write(tmp_path / "c.json", _committed(id_map={
                700001: 8001, 700002: 8101, 70000101: 8101, 70000102: 8102,
            })),
        )
    assert ei.value.kind == "duplicate_real_id"


def test_missing_artifact(tmp_path):
    with pytest.raises(PlanArtifactError) as ei:
        load_committed_leaves(tmp_path / "nope.json", tmp_path / "also.json")
    assert ei.value.kind == "missing"


def test_bad_json(tmp_path):
    (tmp_path / "t.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "c.json").write_text("{}", encoding="utf-8")
    with pytest.raises(PlanArtifactError) as ei:
        load_committed_leaves(tmp_path / "t.json", tmp_path / "c.json")
    assert ei.value.kind == "bad_json"


def test_non_boolean_decomposable_is_bad_node(tmp_path):
    tree = _tree()
    # A grandchild with a missing `decomposable` must fail loud, not be read
    # as a leaf (it could be a truncated/half-written decomposable node).
    del tree["children"][0]["children"][0]["decomposable"]
    with pytest.raises(PlanArtifactError) as ei:
        load_committed_leaves(
            _write(tmp_path / "t.json", tree),
            _write(tmp_path / "c.json", _committed()),
        )
    assert ei.value.kind == "bad_node"


def test_plan_id_mismatch_between_tree_and_manifest(tmp_path):
    with pytest.raises(PlanArtifactError) as ei:
        load_committed_leaves(
            _write(tmp_path / "t.json", _tree()),
            _write(tmp_path / "c.json", _committed(plan_id="plan-OTHER")),
        )
    assert ei.value.kind == "plan_mismatch"


def test_pinned_leaf_disagreeing_with_id_map_is_misaligned(tmp_path):
    tree = _tree()
    tree["proposals"][1]["item_id"] = 9999
    tree["children"][1]["item_id"] = 9999
    # id_map maps the pinned synth (9999) to a *different* real id → conflict.
    with pytest.raises(PlanArtifactError) as ei:
        load_committed_leaves(
            _write(tmp_path / "t.json", tree),
            _write(tmp_path / "c.json", _committed(id_map={
                700001: 8001, 9999: 5555, 70000101: 8101, 70000102: 8102,
            })),
        )
    assert ei.value.kind == "misaligned"

