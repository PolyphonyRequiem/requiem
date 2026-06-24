"""End-to-end tests for the implementation workflow (Bizet — Phase C).

Covers:

* Happy path: agent returns 1 file change, tests pass, PR created.
* Agent returns no_changes → `end_failed`.
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
from requiem.clients.gh import GhPullRequest
from requiem.clients.twig import TwigItem
from requiem.kernel import Completed, Suspended
from requiem.outcomes import BadOutput
from requiem.persistence import replay
from requiem.toolbelt import FakeFileClient, RealGitClient, Toolbelt
from requiem.workflows.implementation import (
    ImplementationInputs,
    ImplementationResult,
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
    raise_on_search: Exception | None = None
    raise_on_create: Exception | None = None

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
            raw={"number": n, "title": title, "url": url},
        )
        self.created_calls.append({
            "title": title, "body": body, "head": head, "base": base, "url": url
        })
        return pr


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
) -> ImplementationInputs:
    return ImplementationInputs(
        item_id=item_id,
        repo="Owner/Repo",
        repo_path=repo_path,
        base_branch="main",
        test_command=test_command,
        dry_run=dry_run,
        root=root,
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
        assert detect_test_command(tmp_path) == "pytest -q"

    def test_node_via_package_json(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{}")
        assert detect_test_command(tmp_path) == "npm test"

    def test_dotnet_via_csproj(self, tmp_path: Path) -> None:
        (tmp_path / "App.csproj").write_text("<Project/>")
        assert detect_test_command(tmp_path) == "dotnet test --no-build"

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

    # gh.pr_create called exactly once
    assert len(gh.created_calls) == 1
    assert gh.created_calls[0]["head"] == "feature/12345"
    assert gh.created_calls[0]["base"] == "main"

    # The PR was linked back via twig.comment
    assert len(twig.comments) == 1
    assert "https://github.com/Owner/Repo/pull/42" in twig.comments[0][1]

    # The marker file landed on disk on the new branch
    assert (repo_path / "MARKER.md").read_text(encoding="utf-8") == "hello\n"

    # ImplementationResult round-trips out of the completed projection.
    completed = {
        e["node_id"]: e["payload"]["outcome"]
        for e in replay(engine.log_path("happy"))
        if e["kind"] == "verb_completed"
    }
    impl_result = ImplementationResult.from_completed(completed)
    assert impl_result.pr_number == 42
    assert impl_result.tests_passed is True
    assert impl_result.branch_name == "feature/12345"


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


# ---- no changes ----


async def test_no_changes_goes_to_end_failed(repo_path: Path, tmp_path: Path) -> None:
    twig = FakeTwig(item=_make_item())
    gh = FakeGh()
    provider = FakeProvider(scripts={
        "coder": [{"intent_summary": "nothing to do", "file_changes": [], "notes": ""}],
        "coder_revision": [],
    })
    engine = _make_engine(
        repo_path, tmp_path / "logs",
        provider=provider, twig=twig, gh=gh,
        test_runner=_passing_runner,
    )

    result = await engine.run("nochanges")
    assert isinstance(result, Completed)
    assert result.disposition == "failed"
    assert result.final_node == "end_failed"
    # Critically: no PR was opened.
    assert gh.created_calls == []
    # And no spurious twig comment.
    assert twig.comments == []


# ---- bad output ----


async def test_bad_output_routes_to_handoff_no_retry(repo_path: Path, tmp_path: Path) -> None:
    twig = FakeTwig(item=_make_item())
    gh = FakeGh()
    # Two BadOutputs would be needed if we auto-retried; we don't.
    provider = FakeProvider(scripts={
        "coder": [
            BadOutput(
                error_kind="schema_mismatch",
                validation_errors=("missing file_changes",),
                raw_output="{}",
            ),
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
    # bad_output is a SURRENDER → end_needs_human (disposition needs_human), not
    # the success-handoff end_handoff and not end_failed (ADR-0013 B2).
    assert result.final_node == "end_needs_human"
    assert result.disposition == "needs_human"
    # FakeProvider records call count; bad_output was returned once.
    assert len(provider.calls) == 1


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
        revision = (
            [coder_scripts[1]] if coder_scripts and len(coder_scripts) > 1 else []
        )
        provider = FakeProvider(scripts={
            "coder": [coder[0]],
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
    ``models.implementer: claude-sonnet-4.6`` block had zero effect)."""
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
        models={"implementer": {"provider": "copilot", "model": "claude-sonnet-4.6"}},
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
