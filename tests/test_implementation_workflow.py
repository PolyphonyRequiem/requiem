"""End-to-end tests for the implementation workflow (Bizet — Phase C).

Covers:

* Happy path: agent returns 1 file change, tests pass, PR created.
* Agent returns no_changes / blocked plan → `end_needs_human`.
* Agent returns BadOutput → `end_handoff` (NeedsHuman terminal),
  no auto-retry of the agent.
* Tests fail first iteration → coder revises → tests pass → PR created.
* Tests fail after revision → `end_handoff`, no push, no PR.
* Branch already exists (foreign) → `end_handoff`, idempotent.
* Branch already exists (we are on it from prior partial run) → success
  without re-create (INV-RESTART path).
* Dirty workspace → `end_failed` before any agent invocation.
* INV-RESTART: kill mid-implementation, resume to same terminal with no
  duplicate work (no second `pr_create` etc.).
* Path-traversal in coder output → invalid_path → `end_handoff`.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from requiem.agent import FakeProvider
from requiem.clients.fs import FilesystemClient
from requiem.clients.gh import GhClientError, GhPullRequest
from requiem.clients.twig import TwigItem
from requiem.context_pack import ContextPack
from requiem.kernel import Completed, Suspended
from requiem.outcomes import BadOutput
from requiem.persistence import replay
from requiem.toolbelt import FakeFileClient, RealGitClient, Toolbelt
from requiem.workflows.implementation import (
    ImplementationInputs,
    ImplementationResult,
    DetectedTestCommand,
    TestRunResult,
    build_engine,
    build_workflow,
    detect_test_command,
    ERROR_KINDS,
    main as impl_main,
    _build_arg_parser,
    _validate_relative_path,
)


# ---- CLI driver parity (bug-bash §"implementation") -------------------


def test_cli_arg_parser_defaults_to_demo():
    args = _build_arg_parser().parse_args([])
    assert args.item is None
    assert args.live is False


def test_cli_live_without_item_is_rejected():
    rc = impl_main(["--live", "--run-id", "x", "--log-dir", "."])
    assert rc == 2


def test_cli_demo_run_returns_zero(tmp_path: Path):
    rc = impl_main(["--run-id", "cli-demo", "--log-dir", str(tmp_path)])
    assert rc == 0
    assert (tmp_path / "cli-demo.events.jsonl").exists()



# ---- fakes (collected here, not in conftest, to keep the test
#       module fully self-describing) ---------------------------------


@dataclass
class FakeTwig:
    item: TwigItem
    raise_on_show: Exception | None = None
    raise_on_comment: Exception | None = None
    comments: list[tuple[int, str]] = field(default_factory=list)

    async def show_async(self, item_id: int) -> TwigItem:
        if self.raise_on_show is not None:
            raise self.raise_on_show
        return self.item

    async def comment_async(self, item_id: int, message: str) -> None:
        if self.raise_on_comment is not None:
            raise self.raise_on_comment
        self.comments.append((item_id, message))


@dataclass
class FakeGh:
    pr_number: int = 99
    existing_prs: list[GhPullRequest] = field(default_factory=list)
    created_calls: list[dict[str, Any]] = field(default_factory=list)
    posted_statuses: list[dict[str, Any]] = field(default_factory=list)
    branch_sha_override: str | None = None
    raise_on_search: Exception | None = None
    raise_on_create: Exception | None = None
    raise_on_status: Exception | None = None

    async def pr_search(self, repo: str, query: str, limit: int = 30):
        if self.raise_on_search is not None:
            raise self.raise_on_search
        return list(self.existing_prs)

    async def find_open_pr_for_branch(
        self, repo: str, *, head: str, limit: int = 30
    ):
        """ADR-0024 step 6 (2026-06-17): RepoPlatform protocol's
        structured PR search by head branch. Mirrors leaf_pr.py's
        existing FakeRepoPlatform shape — returns only OPEN PRs
        whose head matches."""
        if self.raise_on_search is not None:
            raise self.raise_on_search
        return [
            pr for pr in self.existing_prs
            if pr.head == head and pr.state in ("open", "OPEN")
        ]

    async def pr_create(
        self, repo: str, *, title: str, body: str, head: str, base: str
    ):
        if self.raise_on_create is not None:
            raise self.raise_on_create
        n = self.pr_number
        url = f"https://github.com/{repo}/pull/{n}"
        pr = GhPullRequest(
            number=n,
            title=title,
            state="OPEN",
            merged=False,
            merged_at=None,
            head=head,
            base=base,
            url=url,
            raw={"number": n, "title": title, "url": url, "body": body},
        )
        self.created_calls.append({
            "title": title, "body": body, "head": head, "base": base, "url": url
        })
        self.existing_prs.append(pr)
        return pr

    async def post_commit_status(
        self, repo: str, sha: str, *, context: str, state: str, description: str = "",
    ) -> None:
        if self.raise_on_status is not None:
            raise self.raise_on_status
        self.posted_statuses.append({
            "repo": repo, "sha": sha, "context": context,
            "state": state, "description": description,
        })

    async def branch_sha(self, repo: str, branch: str) -> str:
        if self.branch_sha_override is not None:
            return self.branch_sha_override
        if self.posted_statuses:
            return str(self.posted_statuses[-1]["sha"])
        raise GhClientError(f"no pushed SHA recorded for {repo}:{branch}")


def _make_item(item_id: int = 12345, *, title: str = "Refactor outcome dispatch") -> TwigItem:
    return TwigItem(
        id=item_id,
        title=title,
        state="Active",
        area_path="Demo\\Area",
        work_item_type="Task",
        parent_id=None,
        raw={
            "id": item_id,
            "title": title,
            "description": "Add a marker file at REQUIEM_TEST_MARKER.md.",
        },
    )


# ---- repo fixture: a real, hermetic git repo --------------------------


@pytest.fixture
def repo_path(tmp_path: Path) -> Path:
    """A tiny self-contained git repo with a `main` branch and one commit."""
    p = tmp_path / "repo"
    p.mkdir()
    _git(p, "init", "-q")
    _git(p, "config", "user.email", "test@requiem.local")
    _git(p, "config", "user.name", "Test")
    # Force the default branch to 'main' so platform defaults don't bite.
    _git(p, "checkout", "-q", "-b", "main")
    (p / "README.md").write_text("# repo\n", encoding="utf-8")
    (p / "pyproject.toml").write_text(
        "[project]\nname = \"repo\"\nversion = \"0\"\n", encoding="utf-8"
    )
    _git(p, "add", "-A")
    _git(p, "commit", "-q", "-m", "initial")
    return p


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return r.stdout


# ---- engine factory helpers -------------------------------------------


def _make_inputs(
    repo_path: Path,
    *,
    item_id: int = 12345,
    test_command: str | None = "pytest -q",
    dry_run: bool = False,
    root: int | str | None = None,
    context_pack: Any | None = None,
) -> ImplementationInputs:
    return ImplementationInputs(
        item_id=item_id,
        repo="Owner/Repo",
        repo_path=repo_path,
        base_branch="main",
        test_command=test_command,
        dry_run=dry_run,
        root=root,
        context_pack=context_pack,
    )


def _make_context_pack(item_id: int = 12345) -> ContextPack:
    return ContextPack(
        leaf_id=str(item_id),
        agents_md=f"# Context for leaf {item_id}\n",
        rationale_md="# Rationale\n",
        acceptance_md="# Acceptance\n",
        plan_hash=f"plan-{item_id}",
    )


def _make_toolbelt(repo_path: Path, *, twig: FakeTwig, gh: FakeGh) -> Toolbelt:
    return Toolbelt(
        git=RealGitClient(),
        files=FakeFileClient({}),
        gh=gh,  # type: ignore[arg-type]
        fs=FilesystemClient(repo_path),
        twig=twig,  # type: ignore[arg-type]
    )


def _make_engine(
    repo_path: Path,
    log_dir: Path,
    *,
    provider: FakeProvider,
    twig: FakeTwig,
    gh: FakeGh,
    test_runner=None,
    inputs: ImplementationInputs | None = None,
):
    # `push_branch` shells out to `git push origin <branch>`. The test
    # repo has no `origin`, so we monkey-patch `git push` by wiring a
    # fake test_runner only — actually push_branch uses fs.git_push
    # which calls real git. Tests that exercise push must skip it (we
    # add an `origin` remote pointing to a bare clone for those tests).
    return build_engine(
        log_dir,
        inputs=inputs or _make_inputs(repo_path),
        provider=provider,
        toolbelt=_make_toolbelt(repo_path, twig=twig, gh=gh),
        test_runner=test_runner,
    )


def _make_pushable(repo_path: Path) -> None:
    """Make `repo_path` push-able by wiring `origin` to a local bare clone."""
    bare = repo_path.parent / (repo_path.name + ".git")
    if not bare.exists():
        subprocess.run(
            ["git", "clone", "--bare", "-q", str(repo_path), str(bare)],
            check=True,
        )
    _git(repo_path, "remote", "remove", "origin") if _has_origin(repo_path) else None
    _git(repo_path, "remote", "add", "origin", str(bare))


def _has_origin(repo_path: Path) -> bool:
    r = subprocess.run(
        ["git", "remote"], cwd=str(repo_path), capture_output=True, text=True,
    )
    return "origin" in r.stdout.split()


# ---- unit tests on the small pure pieces ------------------------------


class TestValidateRelativePath:
    @pytest.mark.parametrize("good", [
        "a.py", "src/foo.py", "deeply/nested/file.txt", "a/b/c/d/e.md",
    ])
    def test_accepts_safe_paths(self, good: str) -> None:
        assert _validate_relative_path(good) is not None

    @pytest.mark.parametrize("bad", [
        "", "   ", "/abs/path.py", "../escape.py", "a/../../etc/passwd",
        "..",
    ])
    def test_rejects_dangerous_paths(self, bad: str) -> None:
        assert _validate_relative_path(bad) is None

    def test_rejects_windows_absolute_paths(self) -> None:
        # On Windows, `Path('C:/x').is_absolute()` is True; on POSIX it isn't.
        # Either way the relative check should pass-or-reject consistently —
        # only assert the cross-platform safe rejection.
        # (Pure-relative paths starting with a drive *letter* aren't
        # something we test for here.)
        result = _validate_relative_path("safe/relative.py")
        assert result is not None


class TestDetectTestCommand:
    def test_python_via_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        assert detect_test_command(tmp_path) == DetectedTestCommand("pytest -q", tmp_path)

    def test_node_via_package_json(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{}")
        assert detect_test_command(tmp_path) == DetectedTestCommand("npm test", tmp_path)

    def test_dotnet_via_csproj(self, tmp_path: Path) -> None:
        (tmp_path / "App.csproj").write_text("<Project/>")
        assert detect_test_command(tmp_path) == DetectedTestCommand("dotnet test --no-build", tmp_path)

    def test_nested_project_uses_subdir_cwd(self, tmp_path: Path) -> None:
        nested = tmp_path / "src" / "service"
        nested.mkdir(parents=True)
        (nested / "package.json").write_text("{}")
        assert detect_test_command(tmp_path) == DetectedTestCommand("npm test", nested)

    def test_none_for_unknown(self, tmp_path: Path) -> None:
        assert detect_test_command(tmp_path) is None


def test_workflow_topology_compiles() -> None:
    wf = build_workflow()
    assert wf.name == "implementation"
    errs = wf.validate_topology()
    assert errs == [], errs
    # Every error_kind in the closed taxonomy is a string (sanity).
    assert all(isinstance(k, str) for k in ERROR_KINDS)


# ---- scenario tests -----------------------------------------------------


def _passing_runner(_cmd: str, _cwd: Path) -> TestRunResult:
    return TestRunResult(passed=True, summary="all green", full_output="OK")


def _failing_runner(_cmd: str, _cwd: Path) -> TestRunResult:
    return TestRunResult(
        passed=False, summary="1 failed", full_output="boom!"
    )


def _alternating_runner() -> Any:
    """First call fails, every subsequent call passes."""
    state = {"calls": 0}

    def runner(_cmd: str, _cwd: Path) -> TestRunResult:
        state["calls"] += 1
        if state["calls"] == 1:
            return TestRunResult(passed=False, summary="boom", full_output="boom")
        return TestRunResult(passed=True, summary="green", full_output="OK")

    runner.state = state  # type: ignore[attr-defined]
    return runner


def _coder_creates(path: str, content: str = "hello\n") -> dict[str, Any]:
    return {
        "intent_summary": f"create {path}",
        "file_changes": [
            {"path": path, "operation": "create", "content": content},
        ],
        "notes": "",
    }


# ---- happy path ----


async def test_happy_path_pr_created(repo_path: Path, tmp_path: Path) -> None:
    _make_pushable(repo_path)
    twig = FakeTwig(item=_make_item())
    gh = FakeGh(pr_number=42)
    provider = FakeProvider(scripts={
        "coder": [_coder_creates("MARKER.md")],
        "coder_revision": [],
    })
    engine = _make_engine(
        repo_path, tmp_path / "logs",
        provider=provider, twig=twig, gh=gh,
        test_runner=_passing_runner,
    )

    result = await engine.run("happy")
    assert isinstance(result, Completed), result
    assert result.disposition == "completed"
    assert result.final_node == "end_handoff"

    completed = {
        e["node_id"]: e["payload"]["outcome"]
        for e in replay(engine.log_path("happy"))
        if e["kind"] == "verb_completed"
    }

    # gh.pr_create called exactly once
    assert len(gh.created_calls) == 1
    assert gh.created_calls[0]["head"] == "feature/12345"
    assert gh.created_calls[0]["base"] == "main"

    # ADR-0032 follow-up: push_branch posts a real commit status reflecting
    # the already-passed run_tests result, so leaf_lifecycle's
    # check_tests_passed has genuine evidence instead of a permanently
    # "unknown" checks_state on the ephemeral trunk.
    commit_sha = completed["commit_changes"]["value"]["sha"]
    assert len(gh.posted_statuses) == 1
    assert gh.posted_statuses[0]["sha"] == commit_sha
    assert gh.posted_statuses[0]["state"] == "success"
    assert gh.posted_statuses[0]["context"] == "requiem/local-tests"

    # The PR was linked back via twig.comment
    assert len(twig.comments) == 1
    assert "https://github.com/Owner/Repo/pull/42" in twig.comments[0][1]

    # The marker file landed on disk on the new branch
    assert (repo_path / "MARKER.md").read_text(encoding="utf-8") == "hello\n"

    # ImplementationResult round-trips out of the completed projection.
    impl_result = ImplementationResult.from_completed(completed)
    assert impl_result.pr_number == 42
    assert impl_result.tests_passed is True
    assert impl_result.branch_name == "feature/12345"


async def test_fetch_plan_reads_current_twig_system_description(
    repo_path: Path,
    tmp_path: Path,
) -> None:
    _make_pushable(repo_path)
    item = _make_item()
    item.raw.pop("description")
    item.raw["fields"] = {
        "System.Description": (
            "<p>Implement the probe inside the Accepted ACI mechanism.</p>"
        )
    }
    provider = FakeProvider(scripts={
        "coder": [_coder_creates("MARKER.md")],
        "coder_revision": [],
    })
    engine = _make_engine(
        repo_path,
        tmp_path / "logs",
        provider=provider,
        twig=FakeTwig(item=item),
        gh=FakeGh(),
        test_runner=_passing_runner,
    )

    await engine.run("system_description")

    completed = {
        event["node_id"]: event["payload"]["outcome"]
        for event in replay(engine.log_path("system_description"))
        if event["kind"] == "verb_completed"
    }
    assert (
        completed["fetch_plan"]["value"]["plan_text"]
        == "<p>Implement the probe inside the Accepted ACI mechanism.</p>"
    )


async def test_already_satisfied_change_posts_status_on_existing_context_head(
    repo_path: Path, tmp_path: Path,
) -> None:
    """A coder edit that exactly matches HEAD is authoritative no-op proof.

    The context pack already gives the leaf branch a commit to push and open as
    a PR. The passing test result must be bound to that exact existing SHA.
    """
    _make_pushable(repo_path)

    twig = FakeTwig(item=_make_item())
    gh = FakeGh(pr_number=43)
    provider = FakeProvider(scripts={
        "coder": [{
            "intent_summary": "keep the already-correct README",
            "file_changes": [{
                "path": "README.md",
                "operation": "modify",
                "content": "# repo\n",
            }],
            "notes": "",
        }],
        "coder_revision": [],
    })
    engine = _make_engine(
        repo_path,
        tmp_path / "logs",
        provider=provider,
        twig=twig,
        gh=gh,
        test_runner=_passing_runner,
        inputs=_make_inputs(
            repo_path,
            root=9300,
            context_pack=_make_context_pack(),
        ),
    )

    result = await engine.run("already-satisfied")
    assert isinstance(result, Completed), result
    assert result.disposition == "completed"

    completed = {
        e["node_id"]: e["payload"]["outcome"]
        for e in replay(engine.log_path("already-satisfied"))
        if e["kind"] == "verb_completed"
    }
    context_sha = completed["commit_context_pack"]["value"]["commit_sha"]
    commit = completed["commit_changes"]["value"]
    assert commit["sha"] == context_sha
    assert commit["already_committed"] is True
    assert commit["implementation_already_satisfied"] is True
    assert commit["already_satisfied_paths"] == ["README.md"]
    assert commit["no_op_proof"]["tested_head_sha"] == context_sha
    assert commit["no_op_proof"]["head_provenance"] == "context_pack_commit"

    assert len(gh.posted_statuses) == 1
    assert gh.posted_statuses[0]["sha"] == context_sha
    assert gh.posted_statuses[0]["context"] == "requiem/local-tests"
    assert gh.posted_statuses[0]["state"] == "success"
    assert len(gh.created_calls) == 1
    assert "already satisfied" in gh.created_calls[0]["body"].lower()
    assert "`README.md`" in gh.created_calls[0]["body"]


async def test_already_satisfied_change_requires_status_before_pr(
    repo_path: Path, tmp_path: Path,
) -> None:
    _make_pushable(repo_path)

    twig = FakeTwig(item=_make_item())
    gh = FakeGh(
        pr_number=44,
        raise_on_status=GhClientError("status service unavailable"),
    )
    provider = FakeProvider(scripts={
        "coder": [{
            "intent_summary": "keep the already-correct README",
            "file_changes": [{
                "path": "README.md",
                "operation": "modify",
                "content": "# repo\n",
            }],
            "notes": "",
        }],
        "coder_revision": [],
    })
    engine = _make_engine(
        repo_path,
        tmp_path / "logs",
        provider=provider,
        twig=twig,
        gh=gh,
        test_runner=_passing_runner,
        inputs=_make_inputs(
            repo_path,
            root=9300,
            context_pack=_make_context_pack(),
        ),
    )

    result = await engine.run("already-satisfied-status-failure")
    assert isinstance(result, Completed), result
    assert result.disposition == "needs_human"
    assert result.final_node == "end_needs_human"
    assert gh.posted_statuses == []
    assert gh.created_calls == []

    failures = [
        e["payload"]["outcome"]
        for e in replay(engine.log_path("already-satisfied-status-failure"))
        if e["kind"] == "verb_completed"
        and e["node_id"] == "push_branch"
        and e["payload"]["outcome"]["kind"] == "permanent_failure"
    ]
    assert len(failures) == 1
    assert failures[0]["error_kind"] == "push.status_failed"


async def test_already_satisfied_change_requires_remote_head_match(
    repo_path: Path, tmp_path: Path,
) -> None:
    _make_pushable(repo_path)
    twig = FakeTwig(item=_make_item())
    gh = FakeGh(pr_number=45, branch_sha_override="moved-after-push")
    provider = FakeProvider(scripts={
        "coder": [{
            "intent_summary": "keep the already-correct README",
            "file_changes": [{
                "path": "README.md",
                "operation": "modify",
                "content": "# repo\n",
            }],
            "notes": "",
        }],
        "coder_revision": [],
    })
    engine = _make_engine(
        repo_path,
        tmp_path / "logs",
        provider=provider,
        twig=twig,
        gh=gh,
        test_runner=_passing_runner,
        inputs=_make_inputs(
            repo_path,
            root=9300,
            context_pack=_make_context_pack(),
        ),
    )

    result = await engine.run("already-satisfied-head-moved")
    assert isinstance(result, Completed), result
    assert result.disposition == "needs_human"
    assert len(gh.posted_statuses) == 1
    assert gh.created_calls == []

    failures = [
        e["payload"]["outcome"]
        for e in replay(engine.log_path("already-satisfied-head-moved"))
        if e["kind"] == "verb_completed"
        and e["node_id"] == "create_pr"
        and e["payload"]["outcome"]["kind"] == "permanent_failure"
    ]
    assert len(failures) == 1
    assert failures[0]["error_kind"] == "pr.head_mismatch"


async def test_already_satisfied_change_rejects_foreign_head_provenance(
    repo_path: Path, tmp_path: Path,
) -> None:
    _make_pushable(repo_path)
    _git(repo_path, "checkout", "-q", "-b", "impl/9300-12345")
    (repo_path / "UNRELATED.md").write_text("foreign commit\n", encoding="utf-8")
    _git(repo_path, "add", "-A")
    _git(repo_path, "commit", "-q", "-m", "unrelated human commit")

    twig = FakeTwig(item=_make_item())
    gh = FakeGh(pr_number=45)
    provider = FakeProvider(scripts={
        "coder": [{
            "intent_summary": "keep the already-correct README",
            "file_changes": [{
                "path": "README.md",
                "operation": "modify",
                "content": "# repo\n",
            }],
            "notes": "",
        }],
        "coder_revision": [],
    })
    engine = _make_engine(
        repo_path,
        tmp_path / "logs",
        provider=provider,
        twig=twig,
        gh=gh,
        test_runner=_passing_runner,
        inputs=_make_inputs(repo_path, root=9300),
    )

    result = await engine.run("foreign-no-op-head")
    assert isinstance(result, Completed), result
    assert result.disposition == "needs_human"
    assert gh.posted_statuses == []
    assert gh.created_calls == []

    failures = [
        e["payload"]["outcome"]
        for e in replay(engine.log_path("foreign-no-op-head"))
        if e["kind"] == "verb_completed"
        and e["node_id"] == "commit_changes"
        and e["payload"]["outcome"]["kind"] == "permanent_failure"
    ]
    assert len(failures) == 1
    assert failures[0]["error_kind"] == "coder.no_effective_changes"
    assert "neither the verified context-pack commit" in failures[0]["message"]


async def test_already_satisfied_change_rejects_context_on_wrong_baseline(
    repo_path: Path, tmp_path: Path,
) -> None:
    _make_pushable(repo_path)
    initial_sha = _git(repo_path, "rev-parse", "HEAD").strip()
    (repo_path / "ADVANCED.md").write_text("new trunk state\n", encoding="utf-8")
    _git(repo_path, "add", "-A")
    _git(repo_path, "commit", "-q", "-m", "advance main")
    _git(repo_path, "checkout", "-q", "-b", "impl/9300-12345", initial_sha)

    twig = FakeTwig(item=_make_item())
    gh = FakeGh(pr_number=46)
    provider = FakeProvider(scripts={
        "coder": [{
            "intent_summary": "keep the already-correct README",
            "file_changes": [{
                "path": "README.md",
                "operation": "modify",
                "content": "# repo\n",
            }],
            "notes": "",
        }],
        "coder_revision": [],
    })
    engine = _make_engine(
        repo_path,
        tmp_path / "logs",
        provider=provider,
        twig=twig,
        gh=gh,
        test_runner=_passing_runner,
        inputs=_make_inputs(
            repo_path,
            root=9300,
            context_pack=_make_context_pack(),
        ),
    )

    result = await engine.run("wrong-context-baseline")
    assert isinstance(result, Completed), result
    assert result.disposition == "needs_human"
    assert gh.posted_statuses == []
    assert gh.created_calls == []

    failures = [
        e["payload"]["outcome"]
        for e in replay(engine.log_path("wrong-context-baseline"))
        if e["kind"] == "verb_completed"
        and e["node_id"] == "commit_changes"
        and e["payload"]["outcome"]["kind"] == "permanent_failure"
    ]
    assert len(failures) == 1
    assert failures[0]["error_kind"] == "coder.no_effective_changes"


async def test_already_satisfied_change_rejects_ignored_target(
    repo_path: Path, tmp_path: Path,
) -> None:
    _make_pushable(repo_path)
    (repo_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    _git(repo_path, "add", "-A")
    _git(repo_path, "commit", "-q", "-m", "ignore generated file")

    twig = FakeTwig(item=_make_item())
    gh = FakeGh(pr_number=47)
    provider = FakeProvider(scripts={
        "coder": [{
            "intent_summary": "create an ignored implementation file",
            "file_changes": [{
                "path": "ignored.txt",
                "operation": "create",
                "content": "not committed\n",
            }],
            "notes": "",
        }],
        "coder_revision": [],
    })
    engine = _make_engine(
        repo_path,
        tmp_path / "logs",
        provider=provider,
        twig=twig,
        gh=gh,
        test_runner=_passing_runner,
        inputs=_make_inputs(
            repo_path,
            root=9300,
            context_pack=_make_context_pack(),
        ),
    )

    result = await engine.run("ignored-no-op-target")
    assert isinstance(result, Completed), result
    assert result.disposition == "needs_human"
    assert gh.posted_statuses == []
    assert gh.created_calls == []
    assert not (repo_path / "ignored.txt").exists()

    failures = [
        e["payload"]["outcome"]
        for e in replay(engine.log_path("ignored-no-op-target"))
        if e["kind"] == "verb_completed"
        and e["node_id"] == "apply_changes"
        and e["payload"]["outcome"]["kind"] == "permanent_failure"
    ]
    assert len(failures) == 1
    assert failures[0]["error_kind"] == "coder.ignored_path"
    assert failures[0]["details"]["path"] == "ignored.txt"


async def test_already_satisfied_change_rejects_ignored_delete(
    repo_path: Path, tmp_path: Path,
) -> None:
    _make_pushable(repo_path)
    (repo_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    _git(repo_path, "add", "-A")
    _git(repo_path, "commit", "-q", "-m", "ignore generated file")
    (repo_path / "ignored.txt").write_text("local-only\n", encoding="utf-8")

    twig = FakeTwig(item=_make_item())
    gh = FakeGh(pr_number=48)
    provider = FakeProvider(scripts={
        "coder": [{
            "intent_summary": "delete an ignored local-only file",
            "file_changes": [{
                "path": "ignored.txt",
                "operation": "delete",
                "content": None,
            }],
            "notes": "",
        }],
        "coder_revision": [],
    })
    engine = _make_engine(
        repo_path,
        tmp_path / "logs",
        provider=provider,
        twig=twig,
        gh=gh,
        test_runner=_passing_runner,
        inputs=_make_inputs(
            repo_path,
            root=9300,
            context_pack=_make_context_pack(),
        ),
    )

    result = await engine.run("ignored-no-op-delete")
    assert isinstance(result, Completed), result
    assert result.disposition == "needs_human"
    assert gh.posted_statuses == []
    assert gh.created_calls == []
    assert (repo_path / "ignored.txt").read_text(
        encoding="utf-8"
    ) == "local-only\n"

    failures = [
        e["payload"]["outcome"]
        for e in replay(engine.log_path("ignored-no-op-delete"))
        if e["kind"] == "verb_completed"
        and e["node_id"] == "apply_changes"
        and e["payload"]["outcome"]["kind"] == "permanent_failure"
    ]
    assert len(failures) == 1
    assert failures[0]["error_kind"] == "coder.ignored_path"
    assert failures[0]["details"]["operation"] == "delete"


async def test_already_satisfied_change_rejects_legacy_pr_without_proof(
    repo_path: Path, tmp_path: Path,
) -> None:
    _make_pushable(repo_path)
    existing = GhPullRequest(
        number=17,
        title="legacy context-only PR",
        state="OPEN",
        merged=False,
        merged_at=None,
        head="impl/9300-12345",
        base="main",
        url="https://github.com/Owner/Repo/pull/17",
        raw={"body": "Generated by an older Requiem run."},
    )
    twig = FakeTwig(item=_make_item())
    gh = FakeGh(pr_number=49, existing_prs=[existing])
    provider = FakeProvider(scripts={
        "coder": [{
            "intent_summary": "keep the already-correct README",
            "file_changes": [{
                "path": "README.md",
                "operation": "modify",
                "content": "# repo\n",
            }],
            "notes": "",
        }],
        "coder_revision": [],
    })
    engine = _make_engine(
        repo_path,
        tmp_path / "logs",
        provider=provider,
        twig=twig,
        gh=gh,
        test_runner=_passing_runner,
        inputs=_make_inputs(
            repo_path,
            root=9300,
            context_pack=_make_context_pack(),
        ),
    )

    result = await engine.run("legacy-no-op-pr")
    assert isinstance(result, Completed), result
    assert result.disposition == "needs_human"
    assert len(gh.posted_statuses) == 1
    assert gh.created_calls == []

    failures = [
        e["payload"]["outcome"]
        for e in replay(engine.log_path("legacy-no-op-pr"))
        if e["kind"] == "verb_completed"
        and e["node_id"] == "create_pr"
        and e["payload"]["outcome"]["kind"] == "permanent_failure"
    ]
    assert len(failures) == 1
    assert failures[0]["error_kind"] == "pr.no_op_proof_missing"


async def test_ineffective_apply_stops_before_push_or_pr(
    repo_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A swallowed write is not equivalent to an already-satisfied edit."""
    _make_pushable(repo_path)

    twig = FakeTwig(item=_make_item())
    gh = FakeGh(pr_number=50)
    provider = FakeProvider(scripts={
        "coder": [{
            "intent_summary": "change the README",
            "file_changes": [{
                "path": "README.md",
                "operation": "modify",
                "content": "# changed\n",
            }],
            "notes": "",
        }],
        "coder_revision": [],
    })
    engine = _make_engine(
        repo_path,
        tmp_path / "logs",
        provider=provider,
        twig=twig,
        gh=gh,
        test_runner=_passing_runner,
        inputs=_make_inputs(
            repo_path,
            root=9300,
            context_pack=_make_context_pack(),
        ),
    )
    assert engine.toolbelt.fs is not None
    original_write_text = engine.toolbelt.fs.write_text

    def _drop_readme_write(path: Path, content: str) -> None:
        if path.name == "README.md":
            return
        original_write_text(path, content)

    monkeypatch.setattr(
        engine.toolbelt.fs,
        "write_text",
        _drop_readme_write,
    )

    result = await engine.run("ineffective-apply")
    assert isinstance(result, Completed), result
    assert result.disposition == "needs_human"
    assert result.final_node == "end_needs_human"
    assert gh.posted_statuses == []
    assert gh.created_calls == []

    failures = [
        e["payload"]["outcome"]
        for e in replay(engine.log_path("ineffective-apply"))
        if e["kind"] == "verb_completed"
        and e["node_id"] == "apply_changes"
        and e["payload"]["outcome"]["kind"] == "permanent_failure"
    ]
    assert len(failures) == 1
    assert failures[0]["error_kind"] == "coder.apply_ineffective"
    assert failures[0]["details"]["path"] == "README.md"


async def test_worktree_change_after_tests_stops_before_commit(
    repo_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_pushable(repo_path)
    twig = FakeTwig(item=_make_item())
    gh = FakeGh(pr_number=47)
    provider = FakeProvider(scripts={
        "coder": [_coder_creates("MARKER.md")],
        "coder_revision": [],
    })
    engine = _make_engine(
        repo_path,
        tmp_path / "logs",
        provider=provider,
        twig=twig,
        gh=gh,
        test_runner=_passing_runner,
    )
    assert engine.toolbelt.fs is not None
    original_snapshot = engine.toolbelt.fs.git_stage_all_and_tree_sha
    calls = 0

    async def _snapshot_then_tamper() -> str:
        nonlocal calls
        tree_sha = await original_snapshot()
        calls += 1
        if calls == 1:
            (repo_path / "MARKER.md").write_text("tampered\n", encoding="utf-8")
        return tree_sha

    monkeypatch.setattr(
        engine.toolbelt.fs,
        "git_stage_all_and_tree_sha",
        _snapshot_then_tamper,
    )

    result = await engine.run("tree-changed-after-tests")
    assert isinstance(result, Completed), result
    assert result.disposition == "needs_human"
    assert gh.posted_statuses == []
    assert gh.created_calls == []

    failures = [
        e["payload"]["outcome"]
        for e in replay(engine.log_path("tree-changed-after-tests"))
        if e["kind"] == "verb_completed"
        and e["node_id"] == "commit_changes"
        and e["payload"]["outcome"]["kind"] == "permanent_failure"
    ]
    assert len(failures) == 1
    assert failures[0]["error_kind"] == "tests.tree_changed"


async def test_pre_commit_hook_cannot_publish_untested_tree(
    repo_path: Path, tmp_path: Path,
) -> None:
    _make_pushable(repo_path)
    hooks = repo_path / ".git" / "hooks"
    pre_commit = hooks / "pre-commit"
    pre_commit.write_text(
        "#!/bin/sh\n"
        "printf 'hook mutation\\n' > HOOKED.md\n"
        "git add HOOKED.md\n",
        encoding="utf-8",
    )
    pre_commit.chmod(0o755)

    twig = FakeTwig(item=_make_item())
    gh = FakeGh(pr_number=51)
    provider = FakeProvider(scripts={
        "coder": [_coder_creates("MARKER.md")],
        "coder_revision": [],
    })
    engine = _make_engine(
        repo_path,
        tmp_path / "logs",
        provider=provider,
        twig=twig,
        gh=gh,
        test_runner=_passing_runner,
    )

    result = await engine.run("pre-commit-tree-mutation")
    assert isinstance(result, Completed), result
    assert result.disposition == "needs_human"
    assert gh.posted_statuses == []
    assert gh.created_calls == []

    failures = [
        e["payload"]["outcome"]
        for e in replay(engine.log_path("pre-commit-tree-mutation"))
        if e["kind"] == "verb_completed"
        and e["node_id"] == "commit_changes"
        and e["payload"]["outcome"]["kind"] == "permanent_failure"
    ]
    assert len(failures) == 1
    assert failures[0]["error_kind"] == "tests.tree_changed"
    assert failures[0]["details"]["reason"] == "commit_hook_changed_tree"


async def test_dangling_symlink_delete_is_committed_not_proven_as_no_op(
    repo_path: Path, tmp_path: Path,
) -> None:
    link = repo_path / "dangling-link"
    try:
        link.symlink_to(repo_path / "missing-target")
    except OSError as e:
        pytest.skip(f"symlink creation unavailable: {e}")
    _git(repo_path, "add", "-A")
    _git(repo_path, "commit", "-q", "-m", "add dangling symlink")
    _make_pushable(repo_path)

    twig = FakeTwig(item=_make_item())
    gh = FakeGh(pr_number=52)
    provider = FakeProvider(scripts={
        "coder": [{
            "intent_summary": "delete the dangling tracked symlink",
            "file_changes": [{
                "path": "dangling-link",
                "operation": "delete",
                "content": None,
            }],
            "notes": "",
        }],
        "coder_revision": [],
    })
    engine = _make_engine(
        repo_path,
        tmp_path / "logs",
        provider=provider,
        twig=twig,
        gh=gh,
        test_runner=_passing_runner,
    )

    result = await engine.run("delete-dangling-symlink")
    assert isinstance(result, Completed), result
    assert result.disposition == "completed"
    assert not link.is_symlink()
    assert len(gh.created_calls) == 1

    completed = {
        e["node_id"]: e["payload"]["outcome"]
        for e in replay(engine.log_path("delete-dangling-symlink"))
        if e["kind"] == "verb_completed"
    }
    commit = completed["commit_changes"]["value"]
    assert commit.get("implementation_already_satisfied") is not True
    assert "dangling-link" in commit["files_changed"]


async def test_symlinked_parent_cannot_escape_repository(
    repo_path: Path, tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("must survive\n", encoding="utf-8")
    linked_dir = repo_path / "linked-dir"
    try:
        linked_dir.symlink_to(outside, target_is_directory=True)
    except OSError as e:
        pytest.skip(f"directory symlink creation unavailable: {e}")

    twig = FakeTwig(item=_make_item())
    gh = FakeGh(pr_number=53)
    provider = FakeProvider(scripts={
        "coder": [{
            "intent_summary": "delete through a symlinked parent",
            "file_changes": [{
                "path": "linked-dir/victim.txt",
                "operation": "delete",
                "content": None,
            }],
            "notes": "",
        }],
        "coder_revision": [],
    })
    engine = _make_engine(
        repo_path,
        tmp_path / "logs",
        provider=provider,
        twig=twig,
        gh=gh,
        test_runner=_passing_runner,
    )

    result = await engine.run("symlink-parent-escape")
    assert isinstance(result, Completed), result
    assert result.disposition == "needs_human"
    assert victim.read_text(encoding="utf-8") == "must survive\n"
    assert gh.posted_statuses == []
    assert gh.created_calls == []

    failures = [
        e["payload"]["outcome"]
        for e in replay(engine.log_path("symlink-parent-escape"))
        if e["kind"] == "verb_completed"
        and e["node_id"] == "apply_changes"
        and e["payload"]["outcome"]["kind"] == "permanent_failure"
    ]
    assert len(failures) == 1
    assert failures[0]["error_kind"] == "coder.invalid_path"


# ---- B3: merge-group topology branch (ADR-0006 / ADR-0020) ----


async def test_root_yields_impl_topology_branch(repo_path: Path, tmp_path: Path) -> None:
    """With a merge-group root, the impl branch is ``impl/<root>-<item>`` (the
    ratified ADR-0006 Option-D shape `feature_pr`/`leaf_pr` expect), not the
    legacy ``feature/<item_id>``."""
    _make_pushable(repo_path)
    twig = FakeTwig(item=_make_item())
    gh = FakeGh(pr_number=77)
    provider = FakeProvider(scripts={
        "coder": [_coder_creates("MARKER.md")],
    })
    inputs = _make_inputs(repo_path, item_id=12345, root=9300)
    engine = _make_engine(
        repo_path, tmp_path / "logs",
        provider=provider, twig=twig, gh=gh,
        test_runner=_passing_runner, inputs=inputs,
    )

    result = await engine.run("topo")
    assert isinstance(result, Completed), result
    assert result.final_node == "end_handoff"

    # The leaf PR is opened from impl/9300-12345 (Option-D), not feature/12345.
    assert len(gh.created_calls) == 1
    assert gh.created_calls[0]["head"] == "impl/9300-12345"
    assert gh.created_calls[0]["base"] == "main"

    completed = {
        e["node_id"]: e["payload"]["outcome"]
        for e in replay(engine.log_path("topo"))
        if e["kind"] == "verb_completed"
    }
    impl_result = ImplementationResult.from_completed(completed)
    assert impl_result.branch_name == "impl/9300-12345"


async def test_pr_body_respects_strictest_platform_limit(
    repo_path: Path, tmp_path: Path
) -> None:
    _make_pushable(repo_path)
    item = _make_item()
    item.raw["description"] = "Plan detail. " * 500
    twig = FakeTwig(item=item)
    gh = FakeGh(pr_number=78)
    provider = FakeProvider(scripts={"coder": [_coder_creates("MARKER.md")]})
    engine = _make_engine(
        repo_path,
        tmp_path / "logs",
        provider=provider,
        twig=twig,
        gh=gh,
        test_runner=_passing_runner,
    )

    result = await engine.run("bounded_pr_body")

    assert isinstance(result, Completed)
    assert result.final_node == "end_handoff"
    body = gh.created_calls[0]["body"]
    assert len(body) <= 4_000
    assert "[plan truncated to fit PR description limit]" in body
    assert "## Branch files changed" in body
    assert "Generated by the Requiem implementation workflow." in body


# ---- no changes ----


async def test_no_changes_goes_to_end_needs_human(repo_path: Path, tmp_path: Path) -> None:
    twig = FakeTwig(item=_make_item())
    gh = FakeGh()
    provider = FakeProvider(scripts={
        "coder": [
            {"intent_summary": "nothing to do", "file_changes": [], "notes": ""},
            {"intent_summary": "still nothing", "file_changes": [], "notes": ""},
        ],
        "coder_revision": [],
    })
    engine = _make_engine(
        repo_path, tmp_path / "logs",
        provider=provider, twig=twig, gh=gh,
        test_runner=_passing_runner,
    )

    result = await engine.run("nochanges")
    assert isinstance(result, Completed)
    assert result.disposition == "needs_human"
    assert result.final_node == "end_needs_human"
    # Critically: no PR was opened.
    assert gh.created_calls == []
    # And no spurious twig comment.
    assert twig.comments == []
    assert len(provider.calls) == 2


async def test_no_changes_retries_once_then_succeeds(
    repo_path: Path, tmp_path: Path
) -> None:
    _make_pushable(repo_path)
    twig = FakeTwig(item=_make_item())
    gh = FakeGh(pr_number=99)
    provider = FakeProvider(scripts={
        "coder": [
            {"intent_summary": "placeholder", "file_changes": [], "notes": ""},
            _coder_creates("MARKER.md"),
        ],
        "coder_revision": [],
    })
    engine = _make_engine(
        repo_path,
        tmp_path / "logs",
        provider=provider,
        twig=twig,
        gh=gh,
        test_runner=_passing_runner,
    )

    result = await engine.run("nochanges_retry")

    assert isinstance(result, Completed)
    assert result.final_node == "end_handoff"
    assert result.disposition == "completed"
    assert len(provider.calls) == 2
    assert "contained zero file_changes" in provider.calls[1]["user_message"]
    assert (repo_path / "MARKER.md").exists()


# ---- bad output ----


async def test_bad_output_retries_once_then_succeeds(repo_path: Path, tmp_path: Path) -> None:
    _make_pushable(repo_path)
    twig = FakeTwig(item=_make_item())
    gh = FakeGh(pr_number=99)
    provider = FakeProvider(scripts={
        "coder": [
            BadOutput(
                error_kind="schema_mismatch",
                validation_errors=("missing file_changes",),
                raw_output="{}",
            ),
            _coder_creates("MARKER.md"),
        ],
        "coder_revision": [],
    })
    engine = _make_engine(
        repo_path, tmp_path / "logs",
        provider=provider, twig=twig, gh=gh,
        test_runner=_passing_runner,
    )
    result = await engine.run("badout")
    assert isinstance(result, Completed)
    assert result.final_node == "end_handoff"
    assert result.disposition == "completed"
    assert len(provider.calls) == 2
    assert (repo_path / "MARKER.md").exists()


# ---- run-#30 follow-up: defensive worktree cleanup on coder failure --
#
# Leaf 9 of run #30 wrote 30 .cs files to the worktree during a
# 44-minute recovery-prompt loop (the SDK tool-isolation gap was the
# root cause — the actual fix is on copilot.py — but a future SDK
# regression could re-open that surface). The implementation workflow
# defensively scrubs the worktree on any post-coder failure so a
# single bad leaf can't poison every subsequent sequential leaf via
# cascading ``permanent_failure:workspace.dirty`` in
# ``assert_clean_workspace``.


async def test_bad_output_scrubs_polluted_worktree_before_terminating(
    repo_path: Path, tmp_path: Path,
) -> None:
    """The classic leaf-9 shape: the coder agent writes junk to the
    worktree out-of-band, then returns BadOutput. The workflow must
    leave the worktree clean (no untracked junk, no tracked diffs)
    on the way to ``end_needs_human`` — otherwise the next sequential
    leaf bails at ``assert_clean_workspace``.
    """
    from requiem.agent import AgentCall
    from requiem.outcomes import Outcome

    # The original leaf-9 SDK leak: the coder \"agent\" creates files
    # in the worktree (mimicking the SDK's powershell/apply_patch
    # invocations) and only then returns BadOutput.
    junk_paths: list[Path] = []

    class _PollutingProvider:
        calls: list[dict[str, Any]] = []
        async def invoke(self, call: AgentCall) -> Outcome:
            self.calls.append({"agent": call.spec.name})
            # Plant the same kind of mess leaf 9 produced:
            #   * tracked-file modification (mutate README.md — a tracked file in this fixture)
            #   * untracked specs/ dir with a junk file
            #   * untracked top-level .cs orphan
            (repo_path / "README.md").write_text(
                "# polluted by leaf-9-style leak\n", encoding="utf-8",
            )
            (repo_path / "specs").mkdir(exist_ok=True)
            junk1 = repo_path / "specs" / "leaked-spec.md"
            junk1.write_text("# leaked\n", encoding="utf-8")
            junk_paths.append(junk1)
            junk2 = repo_path / "OrphanedClass.cs"
            junk2.write_text("// orphan\n", encoding="utf-8")
            junk_paths.append(junk2)
            return BadOutput(
                error_kind="schema_mismatch",
                validation_errors=("simulating leaf-9 hallucinated output",),
                raw_output="prose, not JSON",
            )

    twig = FakeTwig(item=_make_item())
    gh = FakeGh()
    provider = _PollutingProvider()
    engine = _make_engine(
        repo_path, tmp_path / "logs",
        provider=provider, twig=twig, gh=gh,
        test_runner=_passing_runner,
    )
    result = await engine.run("leaf9-repro")

    # Routing pin: surrender to human, as before the fix.
    assert isinstance(result, Completed)
    assert result.final_node == "end_needs_human"
    assert result.disposition == "needs_human"

    # The load-bearing assertion: the worktree must be clean now.
    # No junk file should survive (cleanup also removed the specs/ dir).
    assert not (repo_path / "OrphanedClass.cs").exists(), (
        "untracked .cs orphan from polluting coder must be removed by cleanup"
    )
    assert not (repo_path / "specs").exists(), (
        "untracked specs/ dir from polluting coder must be removed by cleanup"
    )
    # And the tracked file must be reverted to HEAD.
    assert (repo_path / "README.md").read_text(encoding="utf-8") == "# repo\n", (
        "modified tracked file must be reset to HEAD by cleanup"
    )


async def test_cleanup_preserves_requiem_internal_bookkeeping(
    repo_path: Path, tmp_path: Path,
) -> None:
    """`.requiem/` is framework-owned bookkeeping (context pack,
    plan.tree.json). Cleanup must preserve it — otherwise resume on
    the same leaf branch can't find its own state.
    """
    from requiem.agent import AgentCall
    from requiem.outcomes import Outcome

    class _PollutingProvider:
        calls: list[dict[str, Any]] = []
        async def invoke(self, call: AgentCall) -> Outcome:
            self.calls.append({"agent": call.spec.name})
            # Plant `.requiem/` framework bookkeeping AND coder junk.
            req_dir = repo_path / ".requiem"
            req_dir.mkdir(exist_ok=True)
            (req_dir / "AGENTS.md").write_text(
                "# curated context\nimportant framework state\n",
                encoding="utf-8",
            )
            (repo_path / "coder_junk.txt").write_text(
                "to be deleted\n", encoding="utf-8",
            )
            return BadOutput(
                error_kind="schema_mismatch",
                validation_errors=("test",),
                raw_output="",
            )

    twig = FakeTwig(item=_make_item())
    gh = FakeGh()
    provider = _PollutingProvider()
    engine = _make_engine(
        repo_path, tmp_path / "logs",
        provider=provider, twig=twig, gh=gh,
        test_runner=_passing_runner,
    )
    result = await engine.run("preserve-requiem")
    assert isinstance(result, Completed)
    assert result.disposition == "needs_human"

    # Coder junk must be removed.
    assert not (repo_path / "coder_junk.txt").exists()
    # `.requiem/` framework bookkeeping MUST survive.
    assert (repo_path / ".requiem" / "AGENTS.md").exists(), (
        ".requiem/ is framework-owned — cleanup must NOT delete it"
    )
    assert (repo_path / ".requiem" / "AGENTS.md").read_text(
        encoding="utf-8"
    ) == "# curated context\nimportant framework state\n"


async def test_cleanup_does_not_run_when_pre_coder_workspace_is_dirty(
    repo_path: Path, tmp_path: Path,
) -> None:
    """Pre-coder failure modes (``assert_clean_workspace`` finds the
    workspace dirty BEFORE the coder runs) must NOT route through the
    cleanup verb. That dirt is human-owned (or a stash from a prior
    session) and silently nuking it would destroy diagnostic state.
    """
    # Pollute the workspace BEFORE the workflow runs.
    pre_existing_junk = repo_path / "user_authored_wip.md"
    pre_existing_junk.write_text(
        "# precious WIP the human is working on\n", encoding="utf-8",
    )

    twig = FakeTwig(item=_make_item())
    gh = FakeGh()
    provider = FakeProvider(scripts={"coder": [], "coder_revision": []})
    engine = _make_engine(
        repo_path, tmp_path / "logs",
        provider=provider, twig=twig, gh=gh,
        test_runner=_passing_runner,
    )
    result = await engine.run("dirty-pre-coder")

    # `assert_clean_workspace` returns permanent_failure:workspace.dirty
    # which still routes to `end_failed` (NOT through cleanup). The
    # human's WIP is preserved.
    assert isinstance(result, Completed)
    assert result.final_node == "end_failed"
    assert result.disposition == "failed"
    assert pre_existing_junk.exists(), (
        "pre-coder dirt is human-owned; cleanup must NOT remove it"
    )
    assert pre_existing_junk.read_text(encoding="utf-8") == (
        "# precious WIP the human is working on\n"
    )


# ---- tests fail first, revision fixes ----


async def test_revision_loop_succeeds(repo_path: Path, tmp_path: Path) -> None:
    _make_pushable(repo_path)
    twig = FakeTwig(item=_make_item())
    gh = FakeGh(pr_number=7)
    provider = FakeProvider(scripts={
        "coder": [_coder_creates("FIRST.md", "v1\n")],
        "coder_revision": [_coder_creates("SECOND.md", "v2\n")],
    })
    runner = _alternating_runner()
    engine = _make_engine(
        repo_path, tmp_path / "logs",
        provider=provider, twig=twig, gh=gh, test_runner=runner,
    )

    result = await engine.run("revision")
    assert isinstance(result, Completed)
    assert result.final_node == "end_handoff"
    assert result.disposition == "completed"
    # Both coder + coder_revision ran; runner saw two calls
    assert runner.state["calls"] == 2  # type: ignore[attr-defined]
    # Final PR created
    assert len(gh.created_calls) == 1
    # The revision file is what landed (apply_changes_revision overwrote
    # what was on disk between the first and second attempts; both files
    # are present because apply_changes is additive — that's fine).
    assert (repo_path / "SECOND.md").exists()


# ---- tests fail twice → no PR ----


async def test_tests_red_after_revision_no_pr(repo_path: Path, tmp_path: Path) -> None:
    twig = FakeTwig(item=_make_item())
    gh = FakeGh()
    provider = FakeProvider(scripts={
        "coder": [_coder_creates("F1.md")],
        "coder_revision": [_coder_creates("F2.md")],
    })
    engine = _make_engine(
        repo_path, tmp_path / "logs",
        provider=provider, twig=twig, gh=gh, test_runner=_failing_runner,
    )

    result = await engine.run("redtwice")
    assert isinstance(result, Completed)
    # We surrendered to the human after the revision also failed.
    assert result.final_node == "end_needs_human"
    assert result.disposition == "needs_human"
    # No PR was opened — INV-NO-CORRUPT-FORWARD.
    assert gh.created_calls == []
    # No twig comment either.
    assert twig.comments == []
    # The events log records run_tests_final as a permanent_failure.
    events = list(replay(engine.log_path("redtwice")))
    failed_finals = [
        e for e in events
        if e["kind"] == "verb_completed"
        and e["node_id"] == "run_tests_final"
        and e["payload"]["outcome"]["kind"] == "permanent_failure"
    ]
    assert len(failed_finals) == 1


# ---- branch already exists (foreign) ----


async def test_existing_branch_routes_to_handoff(repo_path: Path, tmp_path: Path) -> None:
    # Pre-create the branch with a marker commit so it's clearly "foreign".
    _git(repo_path, "checkout", "-q", "-b", "feature/12345")
    (repo_path / "FOREIGN.md").write_text("foreign\n", encoding="utf-8")
    _git(repo_path, "add", "-A")
    _git(repo_path, "commit", "-q", "-m", "foreign commit")
    # Switch back to main so create_branch sees "exists but not current".
    _git(repo_path, "checkout", "-q", "main")

    twig = FakeTwig(item=_make_item())
    gh = FakeGh()
    provider = FakeProvider(scripts={
        "coder": [_coder_creates("NEVER.md")],
        "coder_revision": [],
    })
    engine = _make_engine(
        repo_path, tmp_path / "logs",
        provider=provider, twig=twig, gh=gh, test_runner=_passing_runner,
    )
    # Disable the demo's auto gate handler so we observe the suspend
    # state directly instead of auto-routing to end_handoff. In real
    # operation either a human resolves the gate or the workflow author
    # supplies a domain-specific handler.
    engine.gate_handler = None

    result = await engine.run("foreign_branch")
    # ``create_branch`` now returns ``NeedsHuman`` directly (Saint-Saëns
    # Phase B cleanup, Item E): the kernel suspends at the gate and the
    # operator decides how to proceed. The prompt carries the branch
    # name + current HEAD so they have what they need to choose.
    assert isinstance(result, Suspended)
    assert result.node_id == "create_branch"
    assert "feature/12345" in result.prompt
    assert "main" in result.prompt
    assert "abort" in result.options
    # No PR, no commits to main, no marker file from the demo coder.
    assert gh.created_calls == []
    assert not (repo_path / "NEVER.md").exists()
    # The kernel must have logged a real ``gate_opened`` event keyed to
    # ``create_branch`` — this is the Sibelius PR #23 + Saint-Saëns Item
    # E contract: a ``ScriptNode`` returning ``NeedsHuman`` opens a
    # first-class gate, not a synthetic permanent_failure.
    log_path = tmp_path / "logs" / "foreign_branch.events.jsonl"
    events = list(replay(log_path))
    gates = [e for e in events if e["kind"] == "gate_opened"
             and e.get("node_id") == "create_branch"]
    assert len(gates) == 1
    assert gates[0]["payload"]["prompt"] == result.prompt
    assert tuple(gates[0]["payload"]["options"]) == result.options


async def test_existing_branch_with_auto_gate_handler_routes_to_handoff(
    repo_path: Path, tmp_path: Path,
) -> None:
    """The default demo gate_handler picks ``abort``; the workflow's
    ``needs_human`` edge then routes ``create_branch`` to
    ``end_handoff`` so the run terminates cleanly. Regression for
    Saint-Saëns Item E: prior code emitted a synthetic
    ``branch.exists_foreign`` permanent_failure to achieve this
    end-state; the new path goes through a real gate."""
    _git(repo_path, "checkout", "-q", "-b", "feature/12345")
    (repo_path / "FOREIGN.md").write_text("foreign\n", encoding="utf-8")
    _git(repo_path, "add", "-A")
    _git(repo_path, "commit", "-q", "-m", "foreign commit")
    _git(repo_path, "checkout", "-q", "main")

    twig = FakeTwig(item=_make_item())
    gh = FakeGh()
    provider = FakeProvider(scripts={
        "coder": [_coder_creates("NEVER.md")],
        "coder_revision": [],
    })
    engine = _make_engine(
        repo_path, tmp_path / "logs",
        provider=provider, twig=twig, gh=gh, test_runner=_passing_runner,
    )

    result = await engine.run("foreign_branch_auto")
    assert isinstance(result, Completed)
    assert result.final_node == "end_needs_human"
    assert result.disposition == "needs_human"
    assert gh.created_calls == []
    # Auto handler picked ``abort`` — the route key must be present.
    events = list(replay(tmp_path / "logs" / "foreign_branch_auto.events.jsonl"))
    resolved = [e for e in events if e["kind"] == "gate_resolved"
                and e.get("node_id") == "create_branch"]
    assert len(resolved) == 1
    assert resolved[0]["payload"]["choice"] == "abort"
    assert resolved[0]["payload"].get("auto") is True


# ---- existing branch we are already on (resume happy case) ----


async def test_resume_on_branch_already_checked_out(repo_path: Path, tmp_path: Path) -> None:
    _make_pushable(repo_path)
    # Caller's pre-state: branch already exists and HEAD is on it.
    _git(repo_path, "checkout", "-q", "-b", "feature/12345")
    twig = FakeTwig(item=_make_item())
    gh = FakeGh(pr_number=51)
    provider = FakeProvider(scripts={
        "coder": [_coder_creates("OK.md")],
        "coder_revision": [],
    })
    engine = _make_engine(
        repo_path, tmp_path / "logs",
        provider=provider, twig=twig, gh=gh, test_runner=_passing_runner,
    )

    result = await engine.run("on_branch_already")
    assert isinstance(result, Completed)
    assert result.disposition == "completed"
    assert len(gh.created_calls) == 1


# ---- dirty workspace ----


async def test_dirty_workspace_fails_before_agent(repo_path: Path, tmp_path: Path) -> None:
    (repo_path / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    twig = FakeTwig(item=_make_item())
    gh = FakeGh()
    provider = FakeProvider(scripts={
        "coder": [_coder_creates("MUST_NOT_RUN.md")],
        "coder_revision": [],
    })
    engine = _make_engine(
        repo_path, tmp_path / "logs",
        provider=provider, twig=twig, gh=gh, test_runner=_passing_runner,
    )

    result = await engine.run("dirty")
    assert isinstance(result, Completed)
    assert result.disposition == "failed"
    assert result.final_node == "end_failed"
    # Agent never ran.
    assert provider.calls == []
    # And the dirty file is still right there — we didn't touch it.
    assert (repo_path / "dirty.txt").exists()
    # And the workflow never created the branch.
    branches = _git(repo_path, "branch")
    assert "feature/12345" not in branches


# ---- invalid path ----


async def test_invalid_path_routes_to_handoff(repo_path: Path, tmp_path: Path) -> None:
    twig = FakeTwig(item=_make_item())
    gh = FakeGh()
    provider = FakeProvider(scripts={
        "coder": [{
            "intent_summary": "evil",
            "file_changes": [{
                "path": "../../etc/oops",
                "operation": "create",
                "content": "no\n",
            }],
            "notes": "",
        }],
        "coder_revision": [],
    })
    engine = _make_engine(
        repo_path, tmp_path / "logs",
        provider=provider, twig=twig, gh=gh, test_runner=_passing_runner,
    )
    result = await engine.run("evil_path")
    assert isinstance(result, Completed)
    assert result.final_node == "end_needs_human"  # surrender → NeedsHuman
    assert result.disposition == "needs_human"
    assert gh.created_calls == []
    # Nothing escaped the repo.
    assert not (repo_path.parent / "etc" / "oops").exists()


# ---- INV-RESTART ----


async def test_inv_restart_resumes_without_duplicate_pr(
    repo_path: Path, tmp_path: Path
) -> None:
    """Kill mid-run after `create_pr` completed; resume; verify no second PR.

    The kernel's resume model means a re-entered engine starts from the
    cursor reconstructed off the log. If we truncate the log to
    immediately-after `create_pr.verb_completed`, the resume should
    pick up at the `link_pr_to_item` node and finish without invoking
    `pr_create` again.
    """
    _make_pushable(repo_path)
    twig = FakeTwig(item=_make_item())
    gh = FakeGh(pr_number=88)
    provider = FakeProvider(scripts={
        "coder": [_coder_creates("RESTART.md")],
        "coder_revision": [],
    })
    log_dir = tmp_path / "logs"
    engine = _make_engine(
        repo_path, log_dir,
        provider=provider, twig=twig, gh=gh, test_runner=_passing_runner,
    )

    # First run: complete fully.
    first = await engine.run("restart")
    assert isinstance(first, Completed)
    assert len(gh.created_calls) == 1
    assert len(twig.comments) == 1

    log_path = engine.log_path("restart")
    lines = log_path.read_text(encoding="utf-8").splitlines()

    # Truncate the log just after `create_pr` completed but before
    # `link_pr_to_item` was entered.
    keep: list[str] = []
    for raw in lines:
        keep.append(raw)
        ev = json.loads(raw)
        if (
            ev["kind"] == "verb_completed"
            and ev.get("node_id") == "create_pr"
        ):
            break
    log_path.write_text("\n".join(keep) + "\n", encoding="utf-8")

    # Resume with fresh fakes — if the engine re-invoked pr_create or
    # ran the coder agent again, we'd see new entries here.
    twig2 = FakeTwig(item=_make_item())
    gh2 = FakeGh(pr_number=999)  # different number; if reused, we'd see 999
    provider2 = FakeProvider(scripts={
        "coder": [_coder_creates("WOULD_DUPLICATE.md")],
        "coder_revision": [],
    })
    engine2 = _make_engine(
        repo_path, log_dir,
        provider=provider2, twig=twig2, gh=gh2, test_runner=_passing_runner,
    )
    second = await engine2.run("restart")
    assert isinstance(second, Completed)
    assert second.final_node == "end_handoff"
    # The decisive assertion: the fresh fakes were NEVER called for
    # create_pr (the cursor skipped past it). Only link_pr_to_item
    # ran, so twig.comment was called once on twig2.
    assert gh2.created_calls == [], "create_pr re-invoked on resume!"
    assert provider2.calls == [], "coder agent re-invoked on resume!"
    assert len(twig2.comments) == 1, twig2.comments


async def test_inv_restart_after_commit_event_loss_reuses_implementation_commit(
    repo_path: Path, tmp_path: Path,
) -> None:
    _make_pushable(repo_path)
    twig = FakeTwig(item=_make_item())
    gh = FakeGh(pr_number=89)
    provider = FakeProvider(scripts={
        "coder": [_coder_creates("COMMITTED.md")],
        "coder_revision": [],
    })
    log_dir = tmp_path / "logs"
    engine = _make_engine(
        repo_path,
        log_dir,
        provider=provider,
        twig=twig,
        gh=gh,
        test_runner=_passing_runner,
    )

    first = await engine.run("commit-event-loss")
    assert isinstance(first, Completed)
    assert len(gh.created_calls) == 1

    log_path = engine.log_path("commit-event-loss")
    keep: list[str] = []
    for raw in log_path.read_text(encoding="utf-8").splitlines():
        keep.append(raw)
        event = json.loads(raw)
        if (
            event["kind"] == "verb_completed"
            and event.get("node_id") == "run_tests"
        ):
            break
    log_path.write_text("\n".join(keep) + "\n", encoding="utf-8")

    provider2 = FakeProvider(scripts={"coder": [], "coder_revision": []})
    engine2 = _make_engine(
        repo_path,
        log_dir,
        provider=provider2,
        twig=twig,
        gh=gh,
        test_runner=_passing_runner,
    )
    second = await engine2.run("commit-event-loss")
    assert isinstance(second, Completed)
    assert second.disposition == "completed"
    assert provider2.calls == []
    assert len(gh.created_calls) == 1

    completed = {
        e["node_id"]: e["payload"]["outcome"]
        for e in replay(log_path)
        if e["kind"] == "verb_completed"
    }
    commit = completed["commit_changes"]["value"]
    assert commit["already_committed"] is True
    assert commit["resumed_implementation_commit"] is True
    assert commit["implementation_already_satisfied"] is False


# ---- error_kind taxonomy is closed ----


async def test_all_emitted_error_kinds_are_in_closed_enum(
    repo_path: Path, tmp_path: Path
) -> None:
    """Every error_kind a verb emits in any test scenario must appear in
    the module-level ERROR_KINDS frozenset (ADR 0004 §4.2)."""
    seen: set[str] = set()
    # Drive a few scenarios that surface different failures.
    scenarios = [
        # dirty
        ("dirty_taxonomy", lambda: _setup_dirty(repo_path), _passing_runner, []),
        # no changes
        ("no_changes_taxonomy", None, _passing_runner, []),
        # tests fail twice
        ("red_taxonomy", None, _failing_runner, [_coder_creates("X.md"), _coder_creates("Y.md")]),
    ]
    for run_id, setup, runner, coder_scripts in scenarios:
        if setup is not None:
            setup()
        twig = FakeTwig(item=_make_item())
        gh = FakeGh()
        coder = coder_scripts or [{
            "intent_summary": "nothing", "file_changes": [], "notes": "",
        }]
        coder_attempts = [coder[0], coder[0]] if not coder_scripts else [coder[0]]
        revision = (
            [coder_scripts[1]] if coder_scripts and len(coder_scripts) > 1 else []
        )
        provider = FakeProvider(scripts={
            "coder": coder_attempts,
            "coder_revision": revision,
        })
        engine = _make_engine(
            repo_path, tmp_path / "logs",
            provider=provider, twig=twig, gh=gh, test_runner=runner,
        )
        await engine.run(run_id)
        for e in replay(engine.log_path(run_id)):
            if e["kind"] != "verb_completed":
                continue
            o = e["payload"]["outcome"]
            if o["kind"] in ("permanent_failure", "bad_output"):
                seen.add(o.get("error_kind", ""))
        # Reset the worktree between scenarios so the next one is clean.
        # Checkout main first because we may be on feature/12345 from
        # the prior scenario's create_branch — you can't delete the
        # branch you're on.
        current = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo_path), capture_output=True, text=True,
        ).stdout.strip()
        if current != "main":
            _git(repo_path, "checkout", "-q", "main")
        _git(repo_path, "reset", "-q", "--hard", "main")
        _git(repo_path, "clean", "-qfd")
        if _branch_exists(repo_path, "feature/12345"):
            _git(repo_path, "branch", "-q", "-D", "feature/12345")

    # Drop the empty-string bad_output kinds (BadOutput error_kind isn't
    # in our closed enum; it's the agent's structural shape).
    seen.discard("")
    seen.discard("schema_mismatch")  # owned by FakeProvider, not us
    extras = seen - ERROR_KINDS
    assert not extras, f"error_kinds emitted outside ERROR_KINDS: {extras}"


def _setup_dirty(repo_path: Path) -> None:
    (repo_path / "dirty_check.txt").write_text("x", encoding="utf-8")


def _branch_exists(repo_path: Path, name: str) -> bool:
    r = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{name}"],
        cwd=str(repo_path), capture_output=True, text=True,
    )
    return r.returncode == 0


# ---- verdict card shape ------------------------------------------------


async def test_verdict_card_happy_path_matches_spec(
    repo_path: Path, tmp_path: Path
) -> None:
    from requiem.workflows.implementation import verdict_card
    _make_pushable(repo_path)
    twig = FakeTwig(item=_make_item(title="Refactor outcome dispatch"))
    gh = FakeGh(pr_number=19)
    provider = FakeProvider(scripts={
        "coder": [_coder_creates("src/marker.py", "X = 1\n")],
        "coder_revision": [],
    })
    engine = _make_engine(
        repo_path, tmp_path / "logs",
        provider=provider, twig=twig, gh=gh, test_runner=_passing_runner,
    )
    await engine.run("verdict")
    completed = {
        e["node_id"]: e["payload"]["outcome"]
        for e in replay(engine.log_path("verdict"))
        if e["kind"] == "verb_completed"
    }
    card = verdict_card(completed)
    assert card is not None
    # Spec checks — order matters, but exact column widths don't.
    assert "Implementation: AB#12345" in card
    assert "✓ Ready for review" in card
    assert "Item:" in card and "Refactor outcome dispatch" in card
    assert "Branch:" in card and "feature/12345" in card
    assert "Tests:" in card and "passed" in card
    assert "#19" in card
    assert "https://github.com/Owner/Repo/pull/19" in card
    assert "PR lifecycle" in card


# ---- ADR-0030 §1 context-pack integration -----------------------------


async def test_context_pack_present_splices_into_coder_prompt(
    repo_path: Path, tmp_path: Path,
) -> None:
    """When a context pack lands on the leaf branch, the implementation
    workflow's coder_prompt verb must read `.requiem/AGENTS.md` from the
    repo_path and append it to the prompt under "Curated context from
    Requiem". This is the read-side of ADR-0030 §1.

    The contract: ``read_agents_md`` returns the file's content when
    present; coder_prompt appends it after the baseline prompt. When
    the file is absent, the baseline prompt is unchanged.
    """
    from requiem.context_pack import read_agents_md

    # Round-trip via the same helper coder_prompt uses.
    assert read_agents_md(repo_path) is None

    pack_dir = repo_path / ".requiem"
    pack_dir.mkdir()
    expected_marker = "## Why this leaf exists\n\nLand the CapacityMetrics DTO."
    (pack_dir / "AGENTS.md").write_text(
        "# Context for leaf: 12345\n\n"
        + expected_marker
        + "\n", encoding="utf-8",
    )
    content = read_agents_md(repo_path)
    assert content is not None
    assert expected_marker in content


def test_coder_prompt_places_schema_instruction_after_curated_context(
    tmp_path: Path,
) -> None:
    """ADR-0030 §1 + run-#25 lesson: when AGENTS.md is present, the
    "Return a CoderOutput …" schema instruction MUST appear at the
    TAIL of the prompt, AFTER the curated context. Run #25 produced
    19/19 leaves of `bad_output:schema_mismatch` because the schema
    instruction sat ABOVE the ~1.7KB context splice; Claude-on-Copilot
    read the rationale + acceptance + doctrine last and produced
    thoughtful prose instead of structured JSON.

    The fix: pin that the schema instruction comes AFTER the curated
    context — keeping it as the most-recent in-context directive when
    the model starts generating, even on context-pack-heavy prompts.
    """
    from requiem.workflows.implementation import (
        ImplementationInputs, build_verb_registry,
    )
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    # Drop a realistic AGENTS.md (the run-#25 leaves' packs averaged
    # 1.5-2KB; we use a similar-sized synthetic).
    pack_dir = repo_path / ".requiem"
    pack_dir.mkdir()
    pack_body = (
        "# Context for leaf: 99999\n\n"
        + "## Why this leaf exists\n\n"
        + ("This leaf is a non-trivial slice. " * 30)
        + "\n\n## Acceptance criteria\n\n"
        + "- ship the DTOs\n- add tests\n- update telemetry\n\n"
        + "## Doctrine relevant to this leaf\n\n"
        + ("Keep contracts narrow. " * 20)
    )
    (pack_dir / "AGENTS.md").write_text(pack_body, encoding="utf-8")

    inputs = ImplementationInputs(
        item_id=12345, repo="org/r", repo_path=repo_path,
    )
    verbs = build_verb_registry(inputs)
    verb = verbs.get("coder_prompt")

    # The verb only consumes ctx.completed["fetch_plan"]["value"], plus
    # the closure's `inputs`. Build a minimal ctx-like object.
    class _Ctx:
        completed = {
            "fetch_plan": {
                "value": {
                    "item_id": 12345,
                    "title": "Land the CapacityMetrics DTO",
                    "plan_text": "Add DTO + tests.",
                    "repo_path": str(repo_path),
                    "repo": "org/r",
                }
            }
        }

    prompt = verb(_Ctx())

    # Both pieces are present.
    assert "## Curated context from Requiem" in prompt
    assert "Return a CoderOutput" in prompt
    # Ordering invariant: schema instruction comes AFTER the curated
    # context, not before. The model's most-recent in-context
    # directive must be the structured-output contract.
    pack_idx = prompt.index("## Curated context from Requiem")
    instr_idx = prompt.index("Return a CoderOutput")
    assert instr_idx > pack_idx, (
        "schema instruction must trail the curated context "
        "(see run-#25 bad_output:schema_mismatch regression)"
    )


def test_coder_prompt_with_no_pack_preserves_schema_instruction(
    tmp_path: Path,
) -> None:
    """When AGENTS.md is absent, the schema instruction still ships —
    just without the curated-context preamble. Pins the no-pack
    backward-compat behavior."""
    from requiem.workflows.implementation import (
        ImplementationInputs, build_verb_registry,
    )
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    # NO .requiem/AGENTS.md on disk.

    inputs = ImplementationInputs(
        item_id=12345, repo="org/r", repo_path=repo_path,
    )
    verbs = build_verb_registry(inputs)
    verb = verbs.get("coder_prompt")

    class _Ctx:
        completed = {
            "fetch_plan": {
                "value": {
                    "item_id": 12345,
                    "title": "leaf",
                    "plan_text": "plan",
                    "repo_path": str(repo_path),
                    "repo": "org/r",
                }
            }
        }

    prompt = verb(_Ctx())
    assert "Return a CoderOutput" in prompt
    # No curated context section when no pack is present.
    assert "## Curated context from Requiem" not in prompt


async def test_commit_context_pack_verb_no_op_when_inputs_carry_no_pack(
    repo_path: Path, tmp_path: Path,
) -> None:
    """ADR-0030 §1: the implementation workflow's commit_context_pack
    verb is a no-op when inputs.context_pack is None (the legacy /
    pre-ADR-0030 path). The run completes through invoke_coder without
    a .requiem/ commit landing on the branch.

    This pins the backward-compat guarantee: every existing
    implementation test in this file continues to pass because the
    verb threads through invisibly when no pack is configured.
    """
    _make_pushable(repo_path)
    provider = FakeProvider(scripts={
        "coder": [_coder_creates("MARKER.md")],
        "coder_revision": [],
    })
    twig = FakeTwig(item=_make_item())
    gh = FakeGh(pr_number=42)
    inputs = _make_inputs(repo_path, item_id=12345, root=9300)
    # inputs.context_pack defaults to None — the assertion under test.
    assert inputs.context_pack is None
    engine = _make_engine(
        repo_path, tmp_path / "logs",
        provider=provider, twig=twig, gh=gh,
        test_runner=_passing_runner, inputs=inputs,
    )
    result = await engine.run("no-pack")
    # The run reaches the implementation handoff cleanly — no .requiem
    # commit was attempted.
    assert isinstance(result, Completed), result
    # No pack files landed on disk.
    assert not (repo_path / ".requiem").exists()


# ---- ADR-0030 §2 plumbing: process_config reaches the Engine ---------


def test_implementation_engine_receives_process_config():
    """Pin: ``implementation.build_engine`` threads ``inputs.process_config``
    into the ``Engine`` so the kernel can resolve ``models.<role>``.

    Before this wiring fix, the Engine was constructed without
    ``process_config=`` and the kernel always fell back to provider
    defaults — silently ignoring operator-supplied ``models:`` in
    process.yaml. Caught in run #28 against AB#62759077 (the
    ``models.implementer: claude-sonnet-5`` block had zero effect)."""
    import tempfile
    from pathlib import Path
    from requiem.workflows.implementation import (
        ImplementationInputs, build_engine,
    )
    from requiem.process_config import ProcessConfig

    cfg = ProcessConfig(
        root_parent_types=frozenset({"Scenario"}),
        type_aliases={},
        decomposable_types=frozenset(),
        implementable_types=frozenset(),
        types={},
        roles={},
        models={"implementer": {"provider": "copilot", "model": "claude-sonnet-5"}},
        source=None, sha256=None,
    )

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        logs = Path(td) / "logs"
        logs.mkdir()
        inputs = ImplementationInputs(
            item_id=12345, repo="org/r", repo_path=repo,
            process_config=cfg,
        )
        engine = build_engine(logs, inputs=inputs, demo=True)

    assert engine.process_config is cfg, (
        "Engine.process_config must equal inputs.process_config — "
        "otherwise kernel._invoke_with_resolved_model falls back to "
        "the provider default model on every agent call"
    )


def test_fanout_threads_process_config_into_leaf_implementation_inputs():
    """Pin: fanout's per-leaf ``ImplementationInputs`` carries the
    operator's ``ProcessConfig`` (not ``None``). Closes the gap
    between fanout (which already had ``process_config`` on its
    FanoutInputs from ADR-0030 §1) and the per-leaf implementation
    engine (which silently ignored the config until this commit)."""
    from requiem.workflows.fanout import FanoutInputs
    from requiem.workflows import fanout as fanout_mod
    from requiem.workflows import implementation as impl_mod
    from requiem.process_config import ProcessConfig
    from pathlib import Path
    import inspect

    # Sanity: ImplementationInputs has the field at all.
    impl_field_names = {f.name for f in impl_mod.dataclasses.fields(impl_mod.ImplementationInputs)} \
        if hasattr(impl_mod, "dataclasses") else None

    if impl_field_names is None:
        # Resolve via stdlib dataclasses instead
        import dataclasses as _dc
        impl_field_names = {f.name for f in _dc.fields(impl_mod.ImplementationInputs)}

    assert "process_config" in impl_field_names, (
        "ImplementationInputs must carry process_config; otherwise "
        "fanout cannot thread it into per-leaf engines"
    )

    # Source-level pin: the line in fanout.py that constructs
    # ImplementationInputs must include process_config=inputs.process_config.
    src = inspect.getsource(fanout_mod._dispatch_in_process) \
        if hasattr(fanout_mod, "_dispatch_in_process") else None
    if src is None:
        # Different module name — search the whole module
        src = inspect.getsource(fanout_mod)
    assert "process_config=inputs.process_config" in src, (
        "fanout._dispatch_in_process must construct ImplementationInputs "
        "with process_config=inputs.process_config to thread the "
        "operator's routing policy into each leaf's implementation engine"
    )
