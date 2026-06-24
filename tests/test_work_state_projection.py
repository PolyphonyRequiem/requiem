"""Tests for requiem.projections.work_state — R4 read-only projection.

Coverage:

* Leaf-at-root (no children) — the simplest tree shape.
* Nested Scenario → Deliverable → Task — depth-first walk with parent
  linkage threaded through.
* Missing dates — None fields, not errors.
* PR not yet open — impl_branch exists, leaf_pr_number is None.
* PR open — find_open_pr_for_branch surfaces the active PR.
* PR merged — find_open_pr_for_branch returns empty; leaf-pr-map
  fallback discovers the PR via pr_view.
* No repo_client — projection still produces the tree; impl_branch is
  still set (it's purely local) but leaf_pr_* stays None.
* Best-effort repo errors — an exception from the repo client doesn't
  torch the whole projection.
* Clock seam — computed_at takes the injected clock.
* JSON serialisation round-trip via WorkStateProjection.to_dict.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from requiem.clients.repo import RepoPullRequest
from requiem.clients.twig import TwigItem
from requiem.projections import (
    WorkItemNode,
    WorkStateProjection,
    compute_work_state,
)


# ---- in-memory test doubles --------------------------------------------


class _FakeTwig:
    """Async TwigClient stand-in: serves canned TwigItems by id.

    Tests seed ``items`` with the full ADO-shaped ``raw`` payload — the
    projection reads dates from ``raw["fields"]`` (or the camelCase
    fallback) and children from ``raw["children"]``.
    """

    def __init__(self, items: dict[int, TwigItem]) -> None:
        self._items = items
        # forensic log so tests can assert call shape if they care
        self.calls: list[int] = []

    async def show_async(self, item_id: int) -> TwigItem:
        self.calls.append(item_id)
        if item_id not in self._items:
            raise KeyError(f"_FakeTwig has no item {item_id}")
        return self._items[item_id]


def _item(
    *,
    id_: int,
    title: str,
    item_type: str,
    state: str,
    parent_id: int | None = None,
    children_ids: tuple[int, ...] = (),
    start_date: str | None = None,
    target_date: str | None = None,
    finish_date: str | None = None,
    use_camel: bool = False,
) -> TwigItem:
    """Build a TwigItem with a faithful raw payload.

    By default, dates go under ``raw["fields"]`` using the canonical
    Microsoft VSTS reference names (the wire shape). Pass
    ``use_camel=True`` to instead put them at the top level under
    twig's camelCase aliases — the projection must handle both.
    """
    children = [{"id": cid, "title": f"item {cid}"} for cid in children_ids]
    raw: dict[str, Any] = {
        "id": id_,
        "title": title,
        "type": item_type,
        "state": state,
        "areaPath": "Project\\Area",
        "children": children,
    }
    if parent_id is not None:
        raw["parentId"] = parent_id
    if use_camel:
        if start_date is not None:
            raw["startDate"] = start_date
        if target_date is not None:
            raw["targetDate"] = target_date
        if finish_date is not None:
            raw["finishDate"] = finish_date
    else:
        fields = {}
        if start_date is not None:
            fields["Microsoft.VSTS.Scheduling.StartDate"] = start_date
        if target_date is not None:
            fields["Microsoft.VSTS.Scheduling.TargetDate"] = target_date
        if finish_date is not None:
            fields["Microsoft.VSTS.Scheduling.FinishDate"] = finish_date
        if fields:
            raw["fields"] = fields
    return TwigItem(
        id=id_,
        title=title,
        state=state,
        area_path="Project\\Area",
        work_item_type=item_type,
        parent_id=parent_id,
        raw=raw,
    )


class _FakeRepoClient:
    """Minimal RepoPlatform stand-in for projection tests.

    Tracks every call so tests can assert the projection asked the
    right questions in the right order. Open PRs are filtered by
    head; pr_view returns whatever was seeded by number.
    """

    def __init__(
        self,
        *,
        open_prs: list[RepoPullRequest] | None = None,
        prs_by_number: dict[int, RepoPullRequest] | None = None,
        raise_on_search: bool = False,
        raise_on_view: bool = False,
    ) -> None:
        self._open_prs = list(open_prs or [])
        self._prs_by_number = dict(prs_by_number or {})
        self._raise_search = raise_on_search
        self._raise_view = raise_on_view
        self.search_calls: list[tuple[str, str]] = []
        self.view_calls: list[tuple[str, int]] = []

    async def find_open_pr_for_branch(
        self, repo: str, *, head: str, limit: int = 30,
    ) -> list[RepoPullRequest]:
        self.search_calls.append((repo, head))
        if self._raise_search:
            raise RuntimeError("simulated repo search failure")
        return [pr for pr in self._open_prs if pr.head == head][:limit]

    async def pr_view(self, repo: str, number: int) -> RepoPullRequest:
        self.view_calls.append((repo, number))
        if self._raise_view:
            raise RuntimeError("simulated repo view failure")
        if number not in self._prs_by_number:
            raise KeyError(f"PR {number} not seeded")
        return self._prs_by_number[number]


def _fixed_clock(when: str) -> Any:
    dt = datetime.fromisoformat(when)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return lambda: dt


# ---- happy path: leaf-only -----------------------------------------------


def test_leaf_only_at_root_produces_one_node_tree():
    twig = _FakeTwig({
        100: _item(
            id_=100, title="ship it", item_type="Task",
            state="Active",
            start_date="2026-06-01T00:00:00Z",
            target_date="2026-06-15T00:00:00Z",
        ),
    })
    proj = asyncio.run(compute_work_state(
        root_item_id=100,
        twig=twig,
        clock=_fixed_clock("2026-06-22T12:00:00+00:00"),
    ))
    assert isinstance(proj, WorkStateProjection)
    assert proj.root_item_id == 100
    assert proj.computed_at == "2026-06-22T12:00:00+00:00"
    assert proj.tree.children == []
    assert proj.tree.item_id == 100
    assert proj.tree.title == "ship it"
    assert proj.tree.work_item_type == "Task"
    assert proj.tree.state == "Active"
    assert proj.tree.start_date == "2026-06-01T00:00:00Z"
    assert proj.tree.target_date == "2026-06-15T00:00:00Z"
    assert proj.tree.finish_date is None
    assert proj.tree.parent_id is None


def test_nested_scenario_to_deliverable_to_task_is_threaded():
    """Three-level walk: 1 → [2, 3] where 2 has child 4."""
    twig = _FakeTwig({
        1: _item(
            id_=1, title="Scenario A", item_type="Scenario",
            state="Active",
            children_ids=(2, 3),
        ),
        2: _item(
            id_=2, title="Deliverable A.1", item_type="Deliverable",
            state="Active", parent_id=1,
            children_ids=(4,),
            target_date="2026-08-01T00:00:00Z",
        ),
        3: _item(
            id_=3, title="Deliverable A.2", item_type="Deliverable",
            state="Proposed", parent_id=1,
        ),
        4: _item(
            id_=4, title="Task 4", item_type="Task",
            state="Resolved", parent_id=2,
            finish_date="2026-07-15T00:00:00Z",
        ),
    })
    proj = asyncio.run(compute_work_state(root_item_id=1, twig=twig))
    root = proj.tree
    assert [c.item_id for c in root.children] == [2, 3]
    assert root.children[0].children[0].item_id == 4
    assert root.children[0].target_date == "2026-08-01T00:00:00Z"
    assert root.children[0].children[0].finish_date == "2026-07-15T00:00:00Z"
    assert root.children[0].children[0].parent_id == 2
    # Twig was visited once per id (no duplicate fetches).
    assert sorted(twig.calls) == [1, 2, 3, 4]


def test_missing_dates_return_none_not_error():
    twig = _FakeTwig({
        50: _item(id_=50, title="no dates", item_type="Task", state="Active"),
    })
    proj = asyncio.run(compute_work_state(root_item_id=50, twig=twig))
    assert proj.tree.start_date is None
    assert proj.tree.target_date is None
    assert proj.tree.finish_date is None


def test_camel_case_dates_are_picked_up_too():
    """Twig sometimes pre-projects fields as camelCase top-level keys;
    the projection must accept either shape."""
    twig = _FakeTwig({
        77: _item(
            id_=77, title="camel item", item_type="Task", state="Active",
            start_date="2026-06-02T00:00:00Z",
            target_date="2026-06-30T00:00:00Z",
            use_camel=True,
        ),
    })
    proj = asyncio.run(compute_work_state(root_item_id=77, twig=twig))
    assert proj.tree.start_date == "2026-06-02T00:00:00Z"
    assert proj.tree.target_date == "2026-06-30T00:00:00Z"


# ---- PR linkage ----------------------------------------------------------


def test_no_repo_client_leaves_impl_branch_set_but_pr_fields_none():
    """impl_branch is purely local (deterministic from item id) — always
    computable. PR fields require a repo_client and stay None without
    one."""
    twig = _FakeTwig({
        42: _item(id_=42, title="atomic", item_type="Task", state="Active"),
    })
    proj = asyncio.run(compute_work_state(root_item_id=42, twig=twig))
    assert proj.tree.impl_branch == "impl/42-42"
    assert proj.tree.leaf_pr_number is None
    assert proj.tree.leaf_pr_url is None
    assert proj.tree.leaf_pr_state is None


def test_impl_branch_present_but_no_pr_yet_keeps_pr_fields_none():
    """The leaf has an impl branch (we computed it locally) but no PR
    is open and no leaf-pr-map entry exists — pr fields are None."""
    twig = _FakeTwig({
        99: _item(id_=99, title="not yet PR", item_type="Task", state="Active"),
    })
    repo = _FakeRepoClient()  # no open PRs, no merged map
    proj = asyncio.run(compute_work_state(
        root_item_id=99, twig=twig, repo_client=repo,
        github_repo="Owner/Repo",
    ))
    assert proj.tree.impl_branch == "impl/99-99"
    assert proj.tree.leaf_pr_number is None
    # Verify the projection actually consulted the repo client.
    assert repo.search_calls == [("Owner/Repo", "impl/99-99")]
    assert repo.view_calls == []


def test_open_pr_is_surfaced_from_active_search():
    twig = _FakeTwig({
        7: _item(id_=7, title="leaf 7", item_type="Task", state="Active"),
    })
    open_pr = RepoPullRequest(
        number=123, title="impl 7", state="open", merged_at=None,
        head="impl/7-7", base="feature/7",
        url="https://example.test/pull/123",
    )
    repo = _FakeRepoClient(open_prs=[open_pr])
    proj = asyncio.run(compute_work_state(
        root_item_id=7, twig=twig, repo_client=repo,
        github_repo="Owner/Repo",
    ))
    assert proj.tree.leaf_pr_number == 123
    assert proj.tree.leaf_pr_url == "https://example.test/pull/123"
    assert proj.tree.leaf_pr_state == "open"


def test_merged_pr_is_surfaced_via_leaf_pr_map_artifact(tmp_path: Path):
    """An open-only search can't witness a merged PR; the leaf-pr-map
    artifact carries the surviving PR number so pr_view can fetch the
    merged state."""
    twig = _FakeTwig({
        500: _item(id_=500, title="merged leaf", item_type="Task", state="Resolved"),
    })
    merged_pr = RepoPullRequest(
        number=42, title="impl 500", state="merged",
        merged_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
        head="impl/500-500", base="feature/500",
        url="https://example.test/pull/42",
    )
    repo = _FakeRepoClient(prs_by_number={42: merged_pr})
    # Seed the leaf-pr-map artifact.
    log_dir = tmp_path / "runs"
    log_dir.mkdir()
    map_path = log_dir / "leaf-pr-map-500.json"
    map_path.write_text(json.dumps({
        "item_id": 500,
        "leaves": [{"leaf_id": 500, "pr_number": 42}],
    }), encoding="utf-8")

    proj = asyncio.run(compute_work_state(
        root_item_id=500, twig=twig, repo_client=repo,
        github_repo="Owner/Repo", log_dir=log_dir,
    ))
    assert proj.tree.leaf_pr_number == 42
    assert proj.tree.leaf_pr_state == "merged"
    assert proj.tree.leaf_pr_url == "https://example.test/pull/42"
    # We searched first (open-only), then fell back to pr_view.
    assert repo.search_calls == [("Owner/Repo", "impl/500-500")]
    assert repo.view_calls == [("Owner/Repo", 42)]


def test_missing_leaf_pr_map_is_silently_ignored(tmp_path: Path):
    """No artifact present — projection is happy, no fallback fires."""
    twig = _FakeTwig({
        1: _item(id_=1, title="x", item_type="Task", state="Active"),
    })
    repo = _FakeRepoClient()
    proj = asyncio.run(compute_work_state(
        root_item_id=1, twig=twig, repo_client=repo,
        github_repo="Owner/Repo", log_dir=tmp_path,  # empty dir
    ))
    assert proj.tree.leaf_pr_number is None
    assert repo.view_calls == []


def test_malformed_leaf_pr_map_is_silently_ignored(tmp_path: Path):
    """A torn JSON file MUST NOT crash the projection."""
    twig = _FakeTwig({
        1: _item(id_=1, title="x", item_type="Task", state="Active"),
    })
    log_dir = tmp_path / "runs"
    log_dir.mkdir()
    (log_dir / "leaf-pr-map-1.json").write_text("{this isn't JSON", encoding="utf-8")
    repo = _FakeRepoClient()
    proj = asyncio.run(compute_work_state(
        root_item_id=1, twig=twig, repo_client=repo,
        github_repo="Owner/Repo", log_dir=log_dir,
    ))
    assert proj.tree.leaf_pr_number is None


def test_repo_client_errors_degrade_to_no_pr_not_a_crash():
    """A transient repo-client error MUST NOT torch a whole-tree fetch.
    The projection's repo linkage is best-effort by design."""
    twig = _FakeTwig({
        1: _item(id_=1, title="x", item_type="Task", state="Active"),
    })
    repo = _FakeRepoClient(raise_on_search=True)
    proj = asyncio.run(compute_work_state(
        root_item_id=1, twig=twig, repo_client=repo,
        github_repo="Owner/Repo",
    ))
    # impl_branch still set (computed locally); PR fields None.
    assert proj.tree.impl_branch == "impl/1-1"
    assert proj.tree.leaf_pr_number is None


def test_pr_view_error_surfaces_number_but_state_unknown(tmp_path: Path):
    """If we found a number in leaf-pr-map but pr_view fails, surface
    the number so consumers know it exists; state stays unknown."""
    twig = _FakeTwig({
        9: _item(id_=9, title="x", item_type="Task", state="Resolved"),
    })
    log_dir = tmp_path / "runs"
    log_dir.mkdir()
    (log_dir / "leaf-pr-map-9.json").write_text(
        json.dumps({"item_id": 9, "leaves": [{"leaf_id": 9, "pr_number": 77}]}),
        encoding="utf-8",
    )
    repo = _FakeRepoClient(raise_on_view=True)
    proj = asyncio.run(compute_work_state(
        root_item_id=9, twig=twig, repo_client=repo,
        github_repo="Owner/Repo", log_dir=log_dir,
    ))
    assert proj.tree.leaf_pr_number == 77
    assert proj.tree.leaf_pr_state is None
    assert proj.tree.leaf_pr_url is None


# ---- serialisation -------------------------------------------------------


def test_to_dict_round_trip_via_json():
    """The projection serialises cleanly to JSON (the CLI + dashboard
    paths both rely on this)."""
    twig = _FakeTwig({
        1: _item(
            id_=1, title="root", item_type="Scenario", state="Active",
            children_ids=(2,),
            start_date="2026-06-01T00:00:00Z",
        ),
        2: _item(
            id_=2, title="leaf", item_type="Task", state="Active",
            parent_id=1,
        ),
    })
    proj = asyncio.run(compute_work_state(
        root_item_id=1, twig=twig,
        clock=_fixed_clock("2026-06-22T12:00:00+00:00"),
    ))
    payload = proj.to_dict()
    # Round-trips through json.
    rendered = json.dumps(payload)
    parsed = json.loads(rendered)
    assert parsed["root_item_id"] == 1
    assert parsed["computed_at"] == "2026-06-22T12:00:00+00:00"
    assert parsed["tree"]["item_id"] == 1
    assert parsed["tree"]["children"][0]["item_id"] == 2
    assert parsed["tree"]["start_date"] == "2026-06-01T00:00:00Z"
    # The leaf node carries its (None) date fields explicitly — consumers
    # rely on the key existing.
    assert "target_date" in parsed["tree"]["children"][0]
    assert parsed["tree"]["children"][0]["target_date"] is None


def test_workitemnode_is_a_dataclass_with_full_field_set():
    """Catch accidental field removals in the public dataclass — the
    R3 / dashboard consumers depend on this exact set."""
    node = WorkItemNode(
        item_id=1, title="t", work_item_type="Task", state="Active",
        start_date=None, target_date=None, finish_date=None,
        parent_id=None, children=[],
        impl_branch=None, leaf_pr_number=None, leaf_pr_url=None,
        leaf_pr_state=None,
    )
    d = node.to_dict()
    assert set(d) == {
        "item_id", "title", "work_item_type", "state",
        "start_date", "target_date", "finish_date",
        "parent_id", "impl_branch",
        "leaf_pr_number", "leaf_pr_url", "leaf_pr_state",
        "children",
    }


# ---- error handling on the twig side -------------------------------------


def test_unknown_item_id_propagates_from_twig():
    """A missing root id is a programming error, not a "show empty
    tree" situation — let the underlying KeyError / TwigNotFound
    surface so the CLI can render a helpful message."""
    twig = _FakeTwig({})
    with pytest.raises(KeyError):
        asyncio.run(compute_work_state(root_item_id=999, twig=twig))


def test_ado_repo_is_used_when_github_repo_unset():
    """The projection accepts either repo identifier; ado_repo wins
    when github_repo is None."""
    twig = _FakeTwig({
        1: _item(id_=1, title="x", item_type="Task", state="Active"),
    })
    repo = _FakeRepoClient()
    asyncio.run(compute_work_state(
        root_item_id=1, twig=twig, repo_client=repo,
        ado_repo="org/proj/repo",
    ))
    assert repo.search_calls == [("org/proj/repo", "impl/1-1")]


def test_github_repo_wins_when_both_are_set():
    """Mirrors the rest of the codebase: github_repo takes precedence
    when an operator (or test) passes both."""
    twig = _FakeTwig({
        1: _item(id_=1, title="x", item_type="Task", state="Active"),
    })
    repo = _FakeRepoClient()
    asyncio.run(compute_work_state(
        root_item_id=1, twig=twig, repo_client=repo,
        github_repo="Owner/Repo", ado_repo="org/proj/repo",
    ))
    assert repo.search_calls == [("Owner/Repo", "impl/1-1")]
