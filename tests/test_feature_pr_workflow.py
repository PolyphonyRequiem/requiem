"""feature_pr workflow tests — the trunk→main integration gate (ADR-0018).

feature_pr touches only ``gh`` (pr_view / pr_search / pr_create) and ``twig``
(best-effort backlink) — no git/fs — so these tests need no real repo, just a
scriptable Fake gh client that reports per-PR merged state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import requiem.workflows.feature_pr as fp
from requiem.clients.gh import GhPullRequest, GhUnknownError
from requiem.kernel import Completed
from requiem.toolbelt import FakeFileClient, RealGitClient, Toolbelt
from requiem.workflows.feature_pr import FeaturePrInputs, LeafPr
from requiem.workflows.planning import completed_from_log

ROOT = 500
REPO = "Owner/Repo"


# ---- fakes --------------------------------------------------------------


@dataclass
class FakeGh:
    next_pr_number: int = 9000
    by_number: dict[int, GhPullRequest] = field(default_factory=dict)
    open_prs: list[GhPullRequest] = field(default_factory=list)
    created: list[dict[str, Any]] = field(default_factory=list)
    raise_on_view: Exception | None = None
    raise_on_search: Exception | None = None
    raise_on_create: Exception | None = None

    async def pr_view(self, repo: str, number: int) -> GhPullRequest:
        if self.raise_on_view is not None:
            raise self.raise_on_view
        try:
            return self.by_number[number]
        except KeyError as e:
            raise GhUnknownError(
                f"no such PR #{number}", exit_code=1, stderr="not found", argv=(),
            ) from e

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


@dataclass
class FakeTwig:
    raise_on_comment: Exception | None = None
    comments: list[tuple[int, str]] = field(default_factory=list)

    async def comment_async(self, item_id: int, message: str) -> None:
        if self.raise_on_comment is not None:
            raise self.raise_on_comment
        self.comments.append((item_id, message))


# ---- helpers ------------------------------------------------------------


def _leaf_pr(root: int, leaf_id: str, number: int, *, base: str | None = None,
             head: str | None = None, merged: bool = True) -> GhPullRequest:
    """A leaf PR as gh would report it."""
    from requiem import branch_model

    return GhPullRequest(
        number=number,
        title=f"Leaf {leaf_id}",
        state="MERGED" if merged else "OPEN",
        merged=merged,
        merged_at=None,
        head=head if head is not None else branch_model.impl_branch(root, leaf_id),
        base=base if base is not None else branch_model.feature_trunk(root),
        url=f"https://github.com/{REPO}/pull/{number}",
        raw={},
    )


def _toolbelt(*, gh: FakeGh, twig: FakeTwig | None = None) -> Toolbelt:
    return Toolbelt(
        git=RealGitClient(),
        files=FakeFileClient({}),
        gh=gh,  # type: ignore[arg-type]
        fs=None,
        twig=twig,  # type: ignore[arg-type]
    )


def _engine(log_dir, *, leaves, gh, twig=None, dry_run=True, base="main", gate_handler=None):
    inputs = FeaturePrInputs(
        root_item_id=ROOT, repo=REPO, leaves=tuple(leaves), base_branch=base, dry_run=dry_run,
    )
    return fp.build_engine(
        log_dir, inputs=inputs, toolbelt=_toolbelt(gh=gh, twig=twig), gate_handler=gate_handler,
    )


def _two_merged_leaves(gh: FakeGh) -> list[LeafPr]:
    gh.by_number[101] = _leaf_pr(ROOT, "1", 101)
    gh.by_number[102] = _leaf_pr(ROOT, "2", 102)
    return [LeafPr("1", 101), LeafPr("2", 102)]


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs"
    d.mkdir()
    return d


def _result(engine, run_id, final_node):
    return fp.feature_pr_result(completed_from_log(engine.log_path(run_id)), final_node)


# ---- dry-run ------------------------------------------------------------


async def test_dry_run_default_previews_no_pr(log_dir):
    gh = FakeGh()
    leaves = _two_merged_leaves(gh)
    engine = _engine(log_dir, leaves=leaves, gh=gh)  # dry_run default True
    result = await engine.run("dry")
    assert isinstance(result, Completed)
    assert result.final_node == "end_success"
    res = _result(engine, "dry", result.final_node)
    assert res.verdict == "previewed"
    assert res.trunk_branch == f"feature/{ROOT}"
    assert res.leaves_ready == 2
    # readiness fully verified, but NO integration PR opened.
    assert gh.created == []


# ---- real open ----------------------------------------------------------


async def test_real_open_creates_trunk_pr_and_backlink(log_dir):
    gh = FakeGh(next_pr_number=7000)
    twig = FakeTwig()
    leaves = _two_merged_leaves(gh)
    engine = _engine(log_dir, leaves=leaves, gh=gh, twig=twig, dry_run=False)
    result = await engine.run("open")
    assert result.final_node == "end_success"
    res = _result(engine, "open", result.final_node)
    assert res.verdict == "opened"
    assert res.pr_number == 7000
    assert res.reused_existing is False
    assert gh.created == [{
        "title": gh.created[0]["title"],
        "head": f"feature/{ROOT}", "base": "main",
        "url": f"https://github.com/{REPO}/pull/7000",
    }]
    # best-effort backlink fired against the root work item.
    assert twig.comments and twig.comments[0][0] == ROOT


async def test_idempotent_rerun_reuses_pr(log_dir):
    gh = FakeGh(next_pr_number=7000)
    leaves = _two_merged_leaves(gh)
    e1 = _engine(log_dir, leaves=leaves, gh=gh, dry_run=False)
    await e1.run("open1")
    e2 = _engine(log_dir, leaves=leaves, gh=gh, dry_run=False)
    result = await e2.run("open2")
    res = _result(e2, "open2", result.final_node)
    assert res.verdict == "opened"
    assert res.pr_number == 7000
    assert res.reused_existing is True
    # only one PR ever created.
    assert len(gh.created) == 1


# ---- fail closed: no leaves --------------------------------------------


async def test_no_leaves_fails_closed(log_dir):
    gh = FakeGh()
    engine = _engine(log_dir, leaves=[], gh=gh, dry_run=False)
    result = await engine.run("empty")
    assert result.final_node == "end_failed"
    assert _result(engine, "empty", result.final_node).verdict == "failed"
    assert gh.created == []


# ---- not-ready leaves all route to human -------------------------------


async def test_leaf_with_no_pr_is_not_ready(log_dir):
    gh = FakeGh()
    gh.by_number[101] = _leaf_pr(ROOT, "1", 101)
    leaves = [LeafPr("1", 101), LeafPr("2", None)]  # leaf 2 has no PR yet
    engine = _engine(log_dir, leaves=leaves, gh=gh, dry_run=False)
    result = await engine.run("nopr")
    assert result.final_node == "end_human"
    assert _result(engine, "nopr", result.final_node).verdict == "needs_human"
    assert gh.created == []


async def test_unmerged_leaf_is_not_ready(log_dir):
    gh = FakeGh()
    gh.by_number[101] = _leaf_pr(ROOT, "1", 101, merged=False)
    leaves = [LeafPr("1", 101)]
    engine = _engine(log_dir, leaves=leaves, gh=gh, dry_run=False)
    result = await engine.run("unmerged")
    assert result.final_node == "end_human"
    assert gh.created == []


async def test_wrong_base_leaf_is_not_ready(log_dir):
    # leaf PR'd to main instead of the trunk — topology violation.
    gh = FakeGh()
    gh.by_number[101] = _leaf_pr(ROOT, "1", 101, base="main")
    leaves = [LeafPr("1", 101)]
    engine = _engine(log_dir, leaves=leaves, gh=gh, dry_run=False)
    result = await engine.run("wrongbase")
    assert result.final_node == "end_human"
    assert gh.created == []


async def test_head_mismatch_leaf_is_not_ready(log_dir):
    gh = FakeGh()
    gh.by_number[101] = _leaf_pr(ROOT, "1", 101, head="impl/999-1")
    leaves = [LeafPr("1", 101)]
    engine = _engine(log_dir, leaves=leaves, gh=gh, dry_run=False)
    result = await engine.run("headmm")
    assert result.final_node == "end_human"
    assert gh.created == []


async def test_pr_view_failure_is_not_ready(log_dir):
    gh = FakeGh()  # by_number empty → pr_view raises GhUnknownError
    leaves = [LeafPr("1", 404)]
    engine = _engine(log_dir, leaves=leaves, gh=gh, dry_run=False)
    result = await engine.run("viewfail")
    assert result.final_node == "end_human"
    assert gh.created == []


# ---- existing trunk PR --------------------------------------------------


async def test_existing_open_trunk_pr_reused(log_dir):
    gh = FakeGh()
    leaves = _two_merged_leaves(gh)
    gh.open_prs.append(GhPullRequest(
        number=4242, title="existing", state="OPEN", merged=False, merged_at=None,
        head=f"feature/{ROOT}", base="main",
        url=f"https://github.com/{REPO}/pull/4242", raw={},
    ))
    engine = _engine(log_dir, leaves=leaves, gh=gh, dry_run=False)
    result = await engine.run("reuse")
    res = _result(engine, "reuse", result.final_node)
    assert res.verdict == "opened"
    assert res.pr_number == 4242
    assert res.reused_existing is True
    assert gh.created == []  # reused, never created


async def test_existing_trunk_pr_wrong_base_routes_to_human(log_dir):
    gh = FakeGh()
    leaves = _two_merged_leaves(gh)
    gh.open_prs.append(GhPullRequest(
        number=4242, title="wrong", state="OPEN", merged=False, merged_at=None,
        head=f"feature/{ROOT}", base="develop",  # not the expected base
        url=f"https://github.com/{REPO}/pull/4242", raw={},
    ))
    engine = _engine(log_dir, leaves=leaves, gh=gh, dry_run=False)
    result = await engine.run("wrongbasepr")
    assert result.final_node == "end_human"
    assert gh.created == []


# ---- client failures ----------------------------------------------------


async def test_pr_search_failure_fails(log_dir):
    from requiem.clients.gh import GhUnknownError as GhErr

    gh = FakeGh(raise_on_search=GhErr("boom", exit_code=2, stderr="boom", argv=()))
    leaves = _two_merged_leaves(gh)
    engine = _engine(log_dir, leaves=leaves, gh=gh, dry_run=False)
    result = await engine.run("searchfail")
    assert result.final_node == "end_failed"


async def test_pr_create_failure_fails(log_dir):
    from requiem.clients.gh import GhUnknownError as GhErr

    gh = FakeGh(raise_on_create=GhErr("boom", exit_code=2, stderr="boom", argv=()))
    leaves = _two_merged_leaves(gh)
    engine = _engine(log_dir, leaves=leaves, gh=gh, dry_run=False)
    result = await engine.run("createfail")
    assert result.final_node == "end_failed"


# ---- branch_model wiring ------------------------------------------------


def test_inputs_derive_trunk_and_impl_branches():
    inputs = FeaturePrInputs(root_item_id=ROOT, repo=REPO, leaves=(LeafPr("7", 1),))
    assert inputs.trunk_branch == f"feature/{ROOT}"
    assert inputs.impl_branch_for("7") == f"impl/{ROOT}-7"


# ---- requirement-disposition gate (ADR-0006 INV-DRIVER-GATES-FEATURE-MERGE) --


def _engine_disp(log_dir, *, leaves, gh, dispositions, twig=None, dry_run=False,
                 gate_handler=None):
    """Build a feature_pr engine with an explicit disposition set."""
    inputs = FeaturePrInputs(
        root_item_id=ROOT, repo=REPO, leaves=tuple(leaves), base_branch="main",
        dry_run=dry_run, dispositions=tuple(dispositions),
    )
    return fp.build_engine(
        log_dir, inputs=inputs, toolbelt=_toolbelt(gh=gh, twig=twig),
        gate_handler=gate_handler,
    )


async def test_empty_dispositions_pass_through(log_dir):
    """No disposition set supplied ⇒ the gate is a no-op (pre-gate behaviour)."""
    gh = FakeGh(next_pr_number=7100)
    twig = FakeTwig()
    leaves = _two_merged_leaves(gh)
    engine = _engine_disp(log_dir, leaves=leaves, gh=gh, twig=twig,
                          dispositions=())
    result = await engine.run("disp_empty")
    assert result.final_node == "end_success"
    res = _result(engine, "disp_empty", result.final_node)
    assert res.verdict == "opened"
    assert res.dispositions_total == 0
    assert res.dispositions_satisfied == 0
    # PR opened — readiness passed and there was nothing to gate on.
    assert len(gh.created) == 1


async def test_all_dispositions_satisfied_opens_pr(log_dir):
    gh = FakeGh(next_pr_number=7200)
    twig = FakeTwig()
    leaves = _two_merged_leaves(gh)
    dispositions = [
        fp.ItemDisposition("1", state="Done", satisfied=True),
        fp.ItemDisposition("2", state="Closed", satisfied=True),
    ]
    engine = _engine_disp(log_dir, leaves=leaves, gh=gh, twig=twig,
                          dispositions=dispositions)
    result = await engine.run("disp_ok")
    assert result.final_node == "end_success"
    res = _result(engine, "disp_ok", result.final_node)
    assert res.verdict == "opened"
    assert res.dispositions_total == 2
    assert res.dispositions_satisfied == 2
    assert len(gh.created) == 1


async def test_unsatisfied_disposition_gates_to_human_no_pr(log_dir):
    """A single unsatisfied in-scope item fails the merge closed — no PR opens."""
    gh = FakeGh(next_pr_number=7300)
    twig = FakeTwig()
    leaves = _two_merged_leaves(gh)  # leaves are all merged (readiness passes)
    dispositions = [
        fp.ItemDisposition("1", state="Done", satisfied=True),
        fp.ItemDisposition("2", state="Active", satisfied=False),  # laggard
    ]
    engine = _engine_disp(log_dir, leaves=leaves, gh=gh, twig=twig,
                          dispositions=dispositions)
    result = await engine.run("disp_gate")
    # Fail-closed: readiness passed, but a disposition is unsatisfied.
    assert result.final_node == "end_human"
    res = _result(engine, "disp_gate", result.final_node)
    assert res.verdict == "needs_human"
    # No integration PR opened over an unsatisfied requirement set.
    assert gh.created == []


async def test_disposition_gate_runs_after_readiness(log_dir):
    """If readiness fails first, the disposition gate never runs (ordering)."""
    gh = FakeGh(next_pr_number=7400)
    # One leaf NOT merged ⇒ verify_readiness gates before verify_dispositions.
    gh.by_number[201] = _leaf_pr(ROOT, "1", 201, merged=True)
    gh.by_number[202] = _leaf_pr(ROOT, "2", 202, merged=False)
    leaves = [LeafPr("1", 201), LeafPr("2", 202)]
    dispositions = [fp.ItemDisposition("1", state="Active", satisfied=False)]
    engine = _engine_disp(log_dir, leaves=leaves, gh=gh, dispositions=dispositions)
    result = await engine.run("disp_order")
    assert result.final_node == "end_human"
    # The readiness gate fired; the disposition node never recorded a value.
    completed = completed_from_log(engine.log_path("disp_order"))
    assert "verify_readiness" in completed
    assert "verify_dispositions" not in completed


async def test_disposition_gate_honoured_in_dry_run(log_dir):
    """The gate is enforced even in dry-run (it's read-only but still gates)."""
    gh = FakeGh()
    leaves = _two_merged_leaves(gh)
    dispositions = [fp.ItemDisposition("9", state="Active", satisfied=False)]
    engine = _engine_disp(log_dir, leaves=leaves, gh=gh, dispositions=dispositions,
                          dry_run=True)
    result = await engine.run("disp_dry")
    assert result.final_node == "end_human"
    assert gh.created == []


async def test_end_human_reports_needs_human_disposition(log_dir):
    """Regression: the human-handoff terminal reports needs_human, not failed.

    Mirrors close_out issue #29 — a needs-human gate must not masquerade as a
    failed run on the CLI topline.
    """
    gh = FakeGh()
    leaves = _two_merged_leaves(gh)
    dispositions = [fp.ItemDisposition("1", state="Active", satisfied=False)]
    engine = _engine_disp(log_dir, leaves=leaves, gh=gh, dispositions=dispositions)
    result = await engine.run("disp_needs_human")
    assert result.final_node == "end_human"
    # The kernel-level disposition (drives the CLI topline) is needs_human.
    assert result.disposition == "needs_human"
