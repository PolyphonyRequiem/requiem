"""plan_pr workflow tests — open the approved plan as a reviewable plan/<root> PR.

Real hermetic git repo + a bare origin (so `git push` works) + a duck-typed
FakeGh, mirroring test_implementation_workflow.py. Covers:

* dry-run default previews (no branch / commit / push / PR)
* real open: plan/<root> branch, plan doc committed + pushed, PR opened
* commit stages ONLY the plan doc (never sweeps unrelated working-tree edits)
* idempotent re-run reuses the PR, no duplicate branch/commit/PR
* leaf .plan.md approval gate (approved opens; needs-human fails closed)
* artefact guards: missing / unsupported schema / not-approved / root-mismatch
* doc-path containment (`..` escape → end_failed)
* foreign branch → end_human; pr_search failure → end_failed
* wrong-base existing PR → end_human
* base_branch override flows into the PR base (ADR-0006 Option-D forward-compat)
* best-effort twig backlink on the opened PR
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from requiem.clients.fs import FilesystemClient
from requiem.clients.gh import GhPullRequest, GhUnknownError
from requiem.kernel import Completed
from requiem.toolbelt import FakeFileClient, RealGitClient, Toolbelt
from requiem.workflows import plan_pr as pp
from requiem.workflows.planning import completed_from_log

pytestmark = pytest.mark.asyncio

ROOT = 4242
REPO = "Owner/Repo"


# ---- fakes --------------------------------------------------------------


@dataclass
class FakeGh:
    next_pr_number: int = 77
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
        self.open_prs.append(pr)  # so a re-run's pr_search finds it
        return pr


@dataclass
class FakeTwig:
    raise_on_comment: Exception | None = None
    comments: list[tuple[int, str]] = field(default_factory=list)

    async def comment_async(self, item_id: int, message: str) -> None:
        if self.raise_on_comment is not None:
            raise self.raise_on_comment
        self.comments.append((item_id, message))


# ---- repo fixture (real git repo with a pushable bare origin) -----------


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)
    return r.stdout


@pytest.fixture
def repo_path(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-q")

    p = tmp_path / "repo"
    p.mkdir()
    _git(p, "init", "-q")
    _git(p, "config", "user.email", "test@requiem.local")
    _git(p, "config", "user.name", "Test")
    _git(p, "checkout", "-q", "-b", "main")
    (p / "README.md").write_text("# repo\n", encoding="utf-8")
    _git(p, "add", "-A")
    _git(p, "commit", "-q", "-m", "initial")
    _git(p, "remote", "add", "origin", str(origin))
    _git(p, "push", "-q", "-u", "origin", "main")
    return p


def _write_tree(dir_: Path, *, verdict: str = "approved", item_id: int = ROOT, name: str = "p") -> Path:
    import json

    tree = pp._demo_tree(item_id)
    tree["verdict"] = verdict
    path = dir_ / f"{name}.plan.tree.json"
    path.write_text(json.dumps(tree), encoding="utf-8")
    return path


def _toolbelt(repo_path: Path, *, gh: FakeGh, twig: FakeTwig | None = None) -> Toolbelt:
    return Toolbelt(
        git=RealGitClient(),
        files=FakeFileClient({}),
        gh=gh,  # type: ignore[arg-type]
        fs=FilesystemClient(repo_path),
        twig=twig,  # type: ignore[arg-type]
    )


def _engine(log_dir, repo_path, artifact, *, gh, twig=None, dry_run=None, base="main", doc_path=None, gate_handler=None):
    return pp.build_engine(
        log_dir,
        plan_artifact_path=artifact,
        root_item_id=ROOT,
        repo=REPO,
        repo_path=repo_path,
        base_branch=base,
        plan_doc_path=doc_path,
        dry_run=dry_run,
        toolbelt=_toolbelt(repo_path, gh=gh, twig=twig),
        gate_handler=gate_handler,
    )


def _val(engine, run_id, node):
    completed = completed_from_log(engine.log_path(run_id))
    return (completed.get(node) or {}).get("value") or {}


def _abort(node_id, prompt, options):
    return "abort" if "abort" in options else options[-1]


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs"
    d.mkdir()
    return d


# ---- dry-run ------------------------------------------------------------


async def test_dry_run_default_previews_no_mutations(log_dir, repo_path, tmp_path):
    artifact = _write_tree(tmp_path)
    gh = FakeGh()
    engine = _engine(log_dir, repo_path, artifact, gh=gh)  # dry_run defaults True
    result = await engine.run("dry")
    assert isinstance(result, Completed)
    assert result.final_node == "end_success"
    res = pp.plan_pr_result(completed_from_log(engine.log_path("dry")), result.final_node)
    assert res.verdict == "previewed"
    # No branch, no commit, no PR, no doc written.
    assert gh.created == []
    assert _git(repo_path, "branch", "--list", f"plan/{ROOT}").strip() == ""
    assert not (repo_path / ".requiem" / "plans" / f"{ROOT}.plan.md").exists()
    # ...but load_plan still validated + rendered.
    assert _val(engine, "dry", "load_plan")["rendered_md"].startswith("# Plan: AB#4242")


# ---- real open ----------------------------------------------------------


async def test_real_open_creates_branch_commit_push_pr(log_dir, repo_path, tmp_path):
    artifact = _write_tree(tmp_path)
    gh = FakeGh()
    twig = FakeTwig()
    engine = _engine(log_dir, repo_path, artifact, gh=gh, twig=twig, dry_run=False)
    result = await engine.run("open")
    assert isinstance(result, Completed)
    assert result.final_node == "end_success"

    res = pp.plan_pr_result(completed_from_log(engine.log_path("open")), result.final_node)
    assert res.verdict == "opened"
    assert res.branch_name == f"plan/{ROOT}"
    assert res.pr_number == 77
    assert res.reused_existing is False

    # Branch exists and HEAD is on it; doc committed; PR base/head correct.
    assert _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD").strip() == f"plan/{ROOT}"
    doc = repo_path / ".requiem" / "plans" / f"{ROOT}.plan.md"
    assert doc.exists()
    tracked = _git(repo_path, "ls-files", str(doc.relative_to(repo_path).as_posix())).strip()
    assert tracked != ""
    assert gh.created == [{
        "title": gh.created[0]["title"], "head": f"plan/{ROOT}", "base": "main",
        "url": "https://github.com/Owner/Repo/pull/77",
    }]
    # Pushed to origin.
    remote_branches = _git(repo_path, "branch", "-r")
    assert f"origin/plan/{ROOT}" in remote_branches
    # Best-effort backlink fired.
    assert twig.comments and twig.comments[0][0] == ROOT


async def test_commit_stages_only_the_plan_doc(log_dir, repo_path, tmp_path):
    artifact = _write_tree(tmp_path)
    # An unrelated, uncommitted working-tree edit must NOT be swept into the commit.
    (repo_path / "STRAY.txt").write_text("do not commit me\n", encoding="utf-8")
    gh = FakeGh()
    engine = _engine(log_dir, repo_path, artifact, gh=gh, dry_run=False)
    result = await engine.run("open")
    assert result.final_node == "end_success"
    committed = _git(repo_path, "show", "--name-only", "--pretty=format:", "HEAD").split()
    assert any(".requiem/plans" in f for f in committed)
    assert "STRAY.txt" not in committed
    # STRAY is still untracked.
    assert "STRAY.txt" in _git(repo_path, "status", "--porcelain")


async def test_idempotent_rerun_reuses_pr(log_dir, repo_path, tmp_path):
    artifact = _write_tree(tmp_path)
    gh = FakeGh()
    e1 = _engine(log_dir, repo_path, artifact, gh=gh, dry_run=False)
    await e1.run("open1")
    assert len(gh.created) == 1

    e2 = _engine(log_dir, repo_path, artifact, gh=gh, dry_run=False)
    result = await e2.run("open2")
    assert result.final_node == "end_success"
    res = pp.plan_pr_result(completed_from_log(e2.log_path("open2")), result.final_node)
    assert res.reused_existing is True
    assert res.pr_number == 77
    assert len(gh.created) == 1  # no duplicate PR


# ---- leaf .plan.md approval gate ---------------------------------------


async def test_leaf_md_approved_opens_pr(log_dir, repo_path, tmp_path):
    md = tmp_path / "leaf.plan.md"
    md.write_text(
        "# Plan: leaf-plan\n\n- **Verdict:** approved\n\n## Summary\n\nDo the thing.\n",
        encoding="utf-8",
    )
    gh = FakeGh()
    engine = _engine(log_dir, repo_path, md, gh=gh, dry_run=False)
    result = await engine.run("leaf")
    assert result.final_node == "end_success"
    assert len(gh.created) == 1
    # The committed doc is the leaf markdown verbatim.
    doc = (repo_path / ".requiem" / "plans" / f"{ROOT}.plan.md").read_text(encoding="utf-8")
    assert "Do the thing." in doc


async def test_leaf_md_needs_human_fails_closed(log_dir, repo_path, tmp_path):
    md = tmp_path / "leaf.plan.md"
    md.write_text("# Plan: leaf\n\n- **Verdict:** needs human\n", encoding="utf-8")
    gh = FakeGh()
    engine = _engine(log_dir, repo_path, md, gh=gh, dry_run=False)
    result = await engine.run("leafnh")
    assert result.final_node == "end_failed"
    assert gh.created == []


# ---- artefact guards ----------------------------------------------------


async def test_missing_artifact_fails(log_dir, repo_path, tmp_path):
    engine = _engine(log_dir, repo_path, tmp_path / "nope.plan.tree.json", gh=FakeGh(), dry_run=False)
    result = await engine.run("miss")
    assert result.final_node == "end_failed"


async def test_unsupported_schema_fails(log_dir, repo_path, tmp_path):
    import json

    tree = pp._demo_tree(ROOT)
    tree["schema_version"] = 1
    path = tmp_path / "old.plan.tree.json"
    path.write_text(json.dumps(tree), encoding="utf-8")
    engine = _engine(log_dir, repo_path, path, gh=FakeGh(), dry_run=False)
    result = await engine.run("old")
    assert result.final_node == "end_failed"


async def test_not_approved_tree_fails(log_dir, repo_path, tmp_path):
    artifact = _write_tree(tmp_path, verdict="needs_human")
    engine = _engine(log_dir, repo_path, artifact, gh=FakeGh(), dry_run=False)
    result = await engine.run("nh")
    assert result.final_node == "end_failed"


async def test_root_mismatch_fails(log_dir, repo_path, tmp_path):
    artifact = _write_tree(tmp_path, item_id=9999)  # tree item_id != ROOT
    engine = _engine(log_dir, repo_path, artifact, gh=FakeGh(), dry_run=False)
    result = await engine.run("rootmm")
    assert result.final_node == "end_failed"


async def test_doc_path_escape_fails(log_dir, repo_path, tmp_path):
    artifact = _write_tree(tmp_path)
    engine = _engine(log_dir, repo_path, artifact, gh=FakeGh(), dry_run=False, doc_path="../escape.md")
    result = await engine.run("escape")
    assert result.final_node == "end_failed"
    assert _val(engine, "escape", "load_plan") == {}  # load_plan returned PermanentFailure


# ---- runtime gates ------------------------------------------------------


async def test_foreign_branch_routes_to_human(log_dir, repo_path, tmp_path):
    artifact = _write_tree(tmp_path)
    # A prior run left plan/<root> behind; HEAD is on main.
    _git(repo_path, "branch", f"plan/{ROOT}")
    engine = _engine(log_dir, repo_path, artifact, gh=FakeGh(), dry_run=False, gate_handler=_abort)
    result = await engine.run("foreign")
    assert isinstance(result, Completed)
    assert result.final_node == "end_human"


async def test_pr_search_failure_fails(log_dir, repo_path, tmp_path):
    artifact = _write_tree(tmp_path)
    gh = FakeGh(raise_on_search=GhUnknownError("boom", exit_code=1, stderr="x"))
    engine = _engine(log_dir, repo_path, artifact, gh=gh, dry_run=False)
    result = await engine.run("searchfail")
    assert result.final_node == "end_failed"


async def test_existing_pr_wrong_base_routes_to_human(log_dir, repo_path, tmp_path):
    artifact = _write_tree(tmp_path)
    stale = GhPullRequest(
        number=5, title="stale", state="OPEN", merged=False, merged_at=None,
        head=f"plan/{ROOT}", base="develop", url="u", raw={},
    )
    gh = FakeGh(open_prs=[stale])
    engine = _engine(log_dir, repo_path, artifact, gh=gh, dry_run=False, gate_handler=_abort)
    result = await engine.run("wrongbase")
    assert result.final_node == "end_human"
    assert gh.created == []  # did not create a second PR


# ---- base override (Option-D forward-compat) ----------------------------


async def test_base_branch_override_flows_to_pr(log_dir, repo_path, tmp_path):
    artifact = _write_tree(tmp_path)
    # The Option-D orchestrator passes feature/<root> as the trunk.
    _git(repo_path, "branch", f"feature/{ROOT}", "main")
    gh = FakeGh()
    engine = _engine(log_dir, repo_path, artifact, gh=gh, dry_run=False, base=f"feature/{ROOT}")
    result = await engine.run("override")
    assert result.final_node == "end_success"
    assert gh.created[0]["base"] == f"feature/{ROOT}"
    assert gh.created[0]["head"] == f"plan/{ROOT}"
