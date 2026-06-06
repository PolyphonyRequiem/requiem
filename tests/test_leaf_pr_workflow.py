"""leaf_pr workflow tests — requiem-owned leaf-PR opening (ADR-0018 step 2).

leaf_pr touches only ``gh`` (pr_search / pr_create) — no git/fs/twig — so these
tests need no real repo, just a scriptable Fake gh client. They assert the
narrow mutation contract: idempotent reuse of an open leaf PR, fail-closed on a
wrong-base / ambiguous / errored leaf, and the ``{leaf_id: pr_number}`` map that
``feature_pr`` consumes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import requiem.workflows.leaf_pr as lp
from requiem import branch_model
from requiem.clients.gh import GhPullRequest, GhUnknownError
from requiem.kernel import Completed
from requiem.toolbelt import FakeFileClient, RealGitClient, Toolbelt
from requiem.workflows.feature_pr import LeafPr
from requiem.workflows.leaf_pr import LeafPrInputs
from requiem.workflows.planning import completed_from_log

ROOT = 700
REPO = "Owner/Repo"


# ---- fakes --------------------------------------------------------------


@dataclass
class FakeGh:
    next_pr_number: int = 9000
    open_prs: list[GhPullRequest] = field(default_factory=list)
    created: list[dict[str, Any]] = field(default_factory=list)
    raise_on_search: Exception | None = None
    raise_on_create: Exception | None = None

    async def pr_search(self, repo: str, query: str, limit: int = 30):
        if self.raise_on_search is not None:
            raise self.raise_on_search
        return list(self.open_prs)

    async def pr_create(self, repo: str, *, title: str, body: str, head: str, base: str):
        if self.raise_on_create is not None:
            raise self.raise_on_create
        n = self.next_pr_number
        self.next_pr_number += 1
        url = f"https://github.com/{repo}/pull/{n}"
        pr = GhPullRequest(
            number=n, title=title, state="OPEN", merged=False, merged_at=None,
            head=head, base=base, url=url, raw={"number": n, "url": url},
        )
        self.created.append({"title": title, "head": head, "base": base, "url": url})
        self.open_prs.append(pr)
        return pr


# ---- helpers ------------------------------------------------------------


def _open_leaf_pr(root: int, leaf_id: str, number: int, *, base: str | None = None,
                  head: str | None = None) -> GhPullRequest:
    return GhPullRequest(
        number=number,
        title=f"Leaf {leaf_id}",
        state="OPEN",
        merged=False,
        merged_at=None,
        head=head if head is not None else branch_model.impl_branch(root, leaf_id),
        base=base if base is not None else branch_model.feature_trunk(root),
        url=f"https://github.com/{REPO}/pull/{number}",
        raw={},
    )


def _toolbelt(*, gh: FakeGh) -> Toolbelt:
    return Toolbelt(
        git=RealGitClient(),
        files=FakeFileClient({}),
        gh=gh,  # type: ignore[arg-type]
        fs=None,
        twig=None,
    )


def _engine(log_dir, *, leaf_ids, gh, dry_run=True, gate_handler=None):
    inputs = LeafPrInputs(
        root_item_id=ROOT, repo=REPO, leaf_ids=tuple(leaf_ids), dry_run=dry_run,
    )
    return lp.build_engine(
        log_dir, inputs=inputs, toolbelt=_toolbelt(gh=gh), gate_handler=gate_handler,
    )


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs"
    d.mkdir()
    return d


def _result(engine, run_id, final_node):
    return lp.leaf_pr_result(completed_from_log(engine.log_path(run_id)), final_node)


# ---- dry-run ------------------------------------------------------------


async def test_dry_run_previews_and_opens_nothing(log_dir):
    gh = FakeGh()
    engine = _engine(log_dir, leaf_ids=["1", "2"], gh=gh)  # dry_run default True
    result = await engine.run("dry")
    assert isinstance(result, Completed)
    assert result.final_node == "end_success"
    res = _result(engine, "dry", result.final_node)
    assert res.verdict == "previewed"
    assert res.trunk_branch == f"feature/{ROOT}"
    assert res.leaves_total == 2
    assert res.opened == 0
    # nothing created; every leaf reported as a "would open" with no number.
    assert gh.created == []
    assert [lf.pr_number for lf in res.leaves] == [None, None]


# ---- real open ----------------------------------------------------------


async def test_real_open_creates_leaf_prs(log_dir):
    gh = FakeGh(next_pr_number=8000)
    engine = _engine(log_dir, leaf_ids=["1", "2"], gh=gh, dry_run=False)
    result = await engine.run("open")
    assert result.final_node == "end_success"
    res = _result(engine, "open", result.final_node)
    assert res.verdict == "opened"
    assert res.opened == 2
    assert res.reused == 0
    # head/base are the branch_model topology for each leaf.
    assert gh.created == [
        {"title": gh.created[0]["title"], "head": f"impl/{ROOT}-1",
         "base": f"feature/{ROOT}", "url": f"https://github.com/{REPO}/pull/8000"},
        {"title": gh.created[1]["title"], "head": f"impl/{ROOT}-2",
         "base": f"feature/{ROOT}", "url": f"https://github.com/{REPO}/pull/8001"},
    ]
    # the {leaf_id: pr_number} map feature_pr consumes.
    assert res.leaves == (LeafPr("1", 8000), LeafPr("2", 8001))


# ---- idempotency --------------------------------------------------------


async def test_idempotent_rerun_reuses_open_pr(log_dir):
    gh = FakeGh(next_pr_number=8000)
    engine1 = _engine(log_dir, leaf_ids=["1", "2"], gh=gh, dry_run=False)
    await engine1.run("first")
    assert len(gh.created) == 2
    # second run over the same gh state reuses the open PRs, creates nothing.
    engine2 = _engine(log_dir, leaf_ids=["1", "2"], gh=gh, dry_run=False)
    result = await engine2.run("second")
    assert result.final_node == "end_success"
    res = _result(engine2, "second", result.final_node)
    assert res.opened == 0
    assert res.reused == 2
    assert len(gh.created) == 2  # unchanged
    assert res.leaves == (LeafPr("1", 8000), LeafPr("2", 8001))


async def test_mixed_reuse_and_open(log_dir):
    gh = FakeGh(next_pr_number=8500)
    gh.open_prs.append(_open_leaf_pr(ROOT, "1", 4444))  # leaf 1 already open
    engine = _engine(log_dir, leaf_ids=["1", "2"], gh=gh, dry_run=False)
    result = await engine.run("mixed")
    assert result.final_node == "end_success"
    res = _result(engine, "mixed", result.final_node)
    assert res.reused == 1
    assert res.opened == 1
    assert res.leaves == (LeafPr("1", 4444), LeafPr("2", 8500))


# ---- fail-closed --------------------------------------------------------


async def test_no_leaves_fails(log_dir):
    gh = FakeGh()
    engine = _engine(log_dir, leaf_ids=[], gh=gh, dry_run=False)
    result = await engine.run("empty")
    assert result.final_node == "end_failed"
    assert gh.created == []


async def test_existing_pr_wrong_base_escalates(log_dir):
    gh = FakeGh()
    # leaf 1's open PR targets main, not the trunk — fail closed.
    gh.open_prs.append(_open_leaf_pr(ROOT, "1", 4444, base="main"))
    engine = _engine(log_dir, leaf_ids=["1", "2"], gh=gh, dry_run=False)
    result = await engine.run("wrongbase")
    assert result.final_node == "end_human"
    # nothing opened — we never half-apply over a known-bad leaf.
    assert gh.created == []


async def test_ambiguous_open_prs_escalate(log_dir):
    gh = FakeGh()
    head = branch_model.impl_branch(ROOT, "1")
    gh.open_prs.append(_open_leaf_pr(ROOT, "1", 4444, head=head))
    gh.open_prs.append(_open_leaf_pr(ROOT, "1", 4445, head=head))
    engine = _engine(log_dir, leaf_ids=["1"], gh=gh, dry_run=False)
    result = await engine.run("ambiguous")
    assert result.final_node == "end_human"
    assert gh.created == []


async def test_search_failure_is_permanent(log_dir):
    gh = FakeGh(raise_on_search=GhUnknownError(
        "boom", exit_code=1, stderr="boom", argv=(),
    ))
    engine = _engine(log_dir, leaf_ids=["1"], gh=gh, dry_run=False)
    result = await engine.run("searchfail")
    assert result.final_node == "end_failed"


async def test_create_failure_is_permanent(log_dir):
    gh = FakeGh(raise_on_create=GhUnknownError(
        "no commits between branches", exit_code=1, stderr="no commits", argv=(),
    ))
    engine = _engine(log_dir, leaf_ids=["1"], gh=gh, dry_run=False)
    result = await engine.run("createfail")
    assert result.final_node == "end_failed"


# ---- branch_model wiring ------------------------------------------------


def test_inputs_use_branch_model_topology():
    inputs = LeafPrInputs(root_item_id=ROOT, repo=REPO, leaf_ids=("7",))
    assert inputs.trunk_branch == branch_model.feature_trunk(ROOT)
    assert inputs.impl_branch_for("7") == branch_model.impl_branch(ROOT, "7")


# ---- demo engine smoke --------------------------------------------------


async def test_demo_engine_runs_dry(log_dir):
    engine = lp.build_engine(log_dir)
    result = await engine.run("demo")
    assert result.final_node == "end_success"
    res = lp.leaf_pr_result(completed_from_log(engine.log_path("demo")), result.final_node)
    assert res.verdict == "previewed"
    assert res.leaves_total == 2
