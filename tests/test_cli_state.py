"""Tests for ``requiem state`` — R4 read-only projection CLI (ADR-0031).

Strategy: monkeypatch the lazy imports inside :func:`cmd_state` so we
exercise the real argparse plumbing + the real rendering paths against
an in-memory projection — no live ADO, no live repo, no subprocess.

Coverage:
* JSON output round-trips through ``WorkStateProjection.to_dict``.
* Tree output is produced (we assert key substrings, not exact format).
* Mutually-exclusive ``--ado-repo`` / ``--github-repo`` flags.
* Default (neither repo arg) is allowed and yields a projection with
  ``leaf_pr_*`` unset.
"""
from __future__ import annotations

import json
import sys as _sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import requiem.cli.main  # ensure the submodule is registered

cli_main = _sys.modules["requiem.cli.main"]

from requiem.clients.twig import TwigItem
from requiem.projections import compute_work_state


# ---- in-memory doubles -------------------------------------------------


class _FakeTwig:
    """Async TwigClient stand-in: serves canned items by id."""

    def __init__(self, items: dict[int, TwigItem]) -> None:
        self._items = items

    async def show_async(self, item_id: int) -> TwigItem:
        if item_id not in self._items:
            raise KeyError(f"_FakeTwig has no item {item_id}")
        return self._items[item_id]


def _item(
    id_: int, title: str, state: str = "Active",
    children_ids: tuple[int, ...] = (),
    item_type: str = "Task",
    parent_id: int | None = None,
    start: str | None = None, target: str | None = None,
    finish: str | None = None,
) -> TwigItem:
    raw: dict = {
        "id": id_,
        "title": title,
        "type": item_type,
        "state": state,
        "areaPath": "Project\\Area",
        "children": [{"id": c, "title": f"item {c}"} for c in children_ids],
    }
    if parent_id is not None:
        raw["parentId"] = parent_id
    fields = {}
    if start:
        fields["Microsoft.VSTS.Scheduling.StartDate"] = start
    if target:
        fields["Microsoft.VSTS.Scheduling.TargetDate"] = target
    if finish:
        fields["Microsoft.VSTS.Scheduling.FinishDate"] = finish
    if fields:
        raw["fields"] = fields
    return TwigItem(
        id=id_, title=title, state=state, area_path="Project\\Area",
        work_item_type=item_type, parent_id=parent_id, raw=raw,
    )


@pytest.fixture
def patched_state(monkeypatch):
    """Replace TwigClient + _resolve_repo_target inside cmd_state.

    Returns the FakeTwig (so individual tests can swap its items dict
    if they want a different shape).
    """
    fake_twig = _FakeTwig({
        100: _item(
            100, "root scenario", state="Active", item_type="Scenario",
            children_ids=(101, 102),
            start="2026-06-01T00:00:00Z",
            target="2026-06-30T00:00:00Z",
        ),
        101: _item(101, "deliverable 1", state="Resolved",
                   item_type="Deliverable", parent_id=100,
                   finish="2026-06-15T00:00:00Z"),
        102: _item(102, "deliverable 2", state="Proposed",
                   item_type="Deliverable", parent_id=100),
    })

    # Swap the lazy imports in cmd_state. The targets are the *modules*
    # cmd_state imports inside the function body — patch the names on
    # those modules so the local `from … import …` resolves to fakes.
    import requiem.clients.twig as twig_mod
    import requiem.end_to_end as end_to_end_mod

    monkeypatch.setattr(twig_mod, "TwigClient", lambda *a, **kw: fake_twig)
    monkeypatch.setattr(
        end_to_end_mod, "_resolve_repo_target",
        lambda **kw: (None, None),
    )
    return fake_twig


# ---- happy path: JSON ---------------------------------------------------


def test_state_json_emits_full_projection(
    patched_state, capsys: pytest.CaptureFixture[str], tmp_path: Path,
):
    rc = cli_main.main([
        "state", "--item", "100",
        "--log-dir", str(tmp_path),
        "--json",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["root_item_id"] == 100
    assert payload["tree"]["item_id"] == 100
    assert payload["tree"]["title"] == "root scenario"
    assert payload["tree"]["state"] == "Active"
    assert payload["tree"]["start_date"] == "2026-06-01T00:00:00Z"
    assert payload["tree"]["target_date"] == "2026-06-30T00:00:00Z"
    # Children threaded through.
    child_ids = [c["item_id"] for c in payload["tree"]["children"]]
    assert child_ids == [101, 102]
    # No repo target → no leaf_pr_*.
    assert all(c["leaf_pr_number"] is None for c in payload["tree"]["children"])
    # impl_branch is still computed locally (purely a branch_model call).
    assert payload["tree"]["children"][0]["impl_branch"] is not None


def test_state_default_renders_tree_with_titles(
    patched_state, capsys: pytest.CaptureFixture[str], tmp_path: Path,
):
    rc = cli_main.main([
        "state", "--item", "100",
        "--log-dir", str(tmp_path),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "requiem state — item 100" in out
    assert "root scenario" in out
    assert "deliverable 1" in out
    assert "deliverable 2" in out
    # The Resolved child surfaces its state in the line.
    assert "Resolved" in out


# ---- mutual exclusion --------------------------------------------------


def test_state_rejects_both_repo_flags(
    patched_state, tmp_path: Path, capsys: pytest.CaptureFixture[str],
):
    """argparse's mutually_exclusive_group rejects passing both."""
    with pytest.raises(SystemExit) as ei:
        cli_main.main([
            "state", "--item", "100",
            "--log-dir", str(tmp_path),
            "--ado-repo", "org/proj/repo",
            "--github-repo", "Owner/Repo",
        ])
    assert ei.value.code != 0


# ---- unknown item ------------------------------------------------------


def test_state_unknown_item_propagates_keyerror(
    patched_state, tmp_path: Path, capsys: pytest.CaptureFixture[str],
):
    """An item the fake twig doesn't know about surfaces as a non-zero rc
    (the outer try-except in :func:`main` catches the KeyError raised
    deep in the projection walk and renders it as a failure)."""
    rc = cli_main.main([
        "state", "--item", "999",
        "--log-dir", str(tmp_path),
        "--json",
    ])
    # main() catches the KeyError and returns EXIT_CODE_FAILED.
    assert rc != 0


# ---- argparse plumbing --------------------------------------------------


def test_state_parser_requires_item():
    """Missing --item is a parser error (exit 2)."""
    with pytest.raises(SystemExit) as ei:
        cli_main.main(["state"])
    assert ei.value.code != 0
