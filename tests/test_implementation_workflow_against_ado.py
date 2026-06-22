"""ADR-0024 step 6 / ADR-0025 Gap B load-bearing proof.

These tests prove the load-bearing property of step 6: the per-leaf
``implementation`` workflow runs unchanged against ``FakeAdoClient`` when
wired via ``toolbelt.repo``, with ``toolbelt.gh`` set to None. Step 6
is exactly this — ``implementation`` depends on the ``RepoPlatform``
Protocol, not on ``GhClient`` as a concrete type.

This is the equivalent of ``tests/test_trunk_topology_against_ado.py``
for the per-leaf workflow that dispatches inside fanout (ADR-0021) or
the kanban executor (ADR-0014). If these tests pass, a ``--commit``
run against an ADO repo can now reach the implementation phase without
crashing on a missing ``toolbelt.gh``.

The tests use the in-memory ``FakeAdoClient``. Live ADO is a deploy-time
validation step (`az login` + a reachable ADO repo) and is the final
gate after ADR-0025 Gap C ships a worker backend.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from requiem.agent import FakeProvider
from requiem.clients.azuredevops import FakeAdoClient
from requiem.clients.fs import FilesystemClient
from requiem.clients.twig import TwigItem
from requiem.kernel import Completed
from requiem.toolbelt import FakeFileClient, RealGitClient, Toolbelt
from requiem.workflows.implementation import (
    ImplementationInputs,
    ImplementationResult,
    TestRunResult,
    build_engine,
)


ADO_REPO = "Contoso/Polyphony/widgets"  # <org>/<project>/<repo>


# ---- helpers (copy of test_implementation_workflow.py's helpers,
#       narrowed to what the ADO-path proof needs) -----------------------


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return r.stdout


def _has_origin(repo_path: Path) -> bool:
    r = subprocess.run(
        ["git", "remote"], cwd=str(repo_path), capture_output=True, text=True,
    )
    return "origin" in r.stdout.split()


@pytest.fixture
def repo_path(tmp_path: Path) -> Path:
    """A tiny self-contained git repo with a `main` branch and one commit."""
    p = tmp_path / "repo"
    p.mkdir()
    _git(p, "init", "-q")
    _git(p, "config", "user.email", "test@requiem.local")
    _git(p, "config", "user.name", "Test")
    _git(p, "checkout", "-q", "-b", "main")
    (p / "README.md").write_text("# repo\n", encoding="utf-8")
    (p / "pyproject.toml").write_text(
        "[project]\nname = \"repo\"\nversion = \"0\"\n", encoding="utf-8"
    )
    _git(p, "add", "-A")
    _git(p, "commit", "-q", "-m", "initial")
    return p


def _make_pushable(repo_path: Path) -> None:
    """Make `repo_path` push-able by wiring `origin` to a local bare clone."""
    bare = repo_path.parent / (repo_path.name + ".git")
    if not bare.exists():
        subprocess.run(
            ["git", "clone", "--bare", "-q", str(repo_path), str(bare)],
            check=True,
        )
    if _has_origin(repo_path):
        _git(repo_path, "remote", "remove", "origin")
    _git(repo_path, "remote", "add", "origin", str(bare))


class _FakeTwig:
    """Minimal TwigClient stand-in (the protocol is duck-typed by the
    workflow). Constructed once per test with the item the workflow will
    fetch, plus a comment log for the link-back step."""

    def __init__(self, item: TwigItem) -> None:
        self._item = item
        self.comments: list[tuple[int, str]] = []

    async def show_async(self, item_id: int) -> TwigItem:
        if int(item_id) != int(self._item.id):
            from requiem.clients.twig import TwigItemNotFoundError
            raise TwigItemNotFoundError(f"no such item {item_id}")
        return self._item

    async def comment_async(self, item_id: int, body: str) -> None:
        self.comments.append((int(item_id), body))


def _make_item(item_id: int = 62762021, *, title: str = "Implement SKU probe") -> TwigItem:
    """Mirror the shape of a real ADO leaf the dogfood would produce."""
    return TwigItem(
        id=item_id,
        title=title,
        state="Active",
        area_path="CloudVault\\CVAPI",
        work_item_type="Task",
        parent_id=None,
        raw={
            "id": item_id,
            "title": title,
            "description": "Add a marker file at REQUIEM_TEST_MARKER.md.",
        },
    )


def _coder_creates(path: str, content: str = "hello\n") -> dict[str, object]:
    return {
        "intent_summary": f"create {path}",
        "file_changes": [
            {"path": path, "operation": "create", "content": content},
        ],
        "notes": "",
    }


def _passing_runner(_cmd: str, _cwd: Path) -> TestRunResult:
    return TestRunResult(passed=True, summary="all green", full_output="OK")


def _ado_toolbelt(
    repo_path: Path,
    *,
    ado: FakeAdoClient,
    twig: _FakeTwig,
) -> Toolbelt:
    """Wire ``FakeAdoClient`` via the new platform-neutral ``toolbelt.repo``
    field. Crucially, ``toolbelt.gh`` is None — the implementation
    workflow must NOT silently fall back to a GitHub client we haven't
    supplied. This is the proof that the workflow truly depends on the
    Protocol, not on the concrete ``GhClient`` type."""
    return Toolbelt(
        git=RealGitClient(),
        files=FakeFileClient({}),
        gh=None,
        repo=ado,
        fs=FilesystemClient(repo_path),
        twig=twig,  # type: ignore[arg-type]
    )


def _make_inputs(repo_path: Path, *, item_id: int = 62762021,
                 root: int | str | None = None) -> ImplementationInputs:
    return ImplementationInputs(
        item_id=item_id,
        repo=ADO_REPO,
        repo_path=repo_path,
        base_branch="main",
        test_command="pytest -q",
        dry_run=False,
        root=root,
    )


# ---- the load-bearing tests -------------------------------------------


async def test_implementation_opens_pr_against_ado_via_toolbelt_repo(
    repo_path: Path, tmp_path: Path,
) -> None:
    """Implementation runs end-to-end against FakeAdoClient with
    toolbelt.gh=None. This is the workflow refit ADR-0025 Gap B
    delivers: a leaf can be implemented against an ADO repo using the
    same single workflow that powers the GitHub path."""
    _make_pushable(repo_path)
    twig = _FakeTwig(item=_make_item())
    ado = FakeAdoClient(default_branches={ADO_REPO: "main"})
    provider = FakeProvider(scripts={
        "coder": [_coder_creates("MARKER.md")],
        "coder_revision": [],
    })

    engine = build_engine(
        tmp_path / "logs",
        inputs=_make_inputs(repo_path, item_id=62762021),
        provider=provider,
        toolbelt=_ado_toolbelt(repo_path, ado=ado, twig=twig),
        test_runner=_passing_runner,
    )

    result = await engine.run("ado-happy")
    assert isinstance(result, Completed), result
    assert result.disposition == "completed"
    assert result.final_node == "end_handoff"

    # The PR opened against ADO via FakeAdoClient.pr_create — one call,
    # correct head + base.
    assert len(ado.created_prs) == 1
    assert ado.created_prs[0]["head"] == "feature/62762021"
    assert ado.created_prs[0]["base"] == "main"

    # The PR was linked back via twig.comment_async. The URL shape comes
    # from FakeAdoClient.pr_create's synthesised URL — different from
    # the GitHub format but the link-back logic doesn't care.
    assert len(twig.comments) == 1
    linked_body = twig.comments[0][1]
    assert "PR opened by Requiem implementation workflow:" in linked_body

    # The marker file landed on disk.
    assert (repo_path / "MARKER.md").read_text(encoding="utf-8") == "hello\n"


async def test_implementation_uses_impl_branch_against_ado(
    repo_path: Path, tmp_path: Path,
) -> None:
    """With a merge-group root, the impl branch is ``impl/<root>-<item>``
    per ADR-0006 Option D — same naming convention regardless of
    platform. The leaf PR is opened from that branch against
    ``main``."""
    _make_pushable(repo_path)
    twig = _FakeTwig(item=_make_item(item_id=62762022))
    ado = FakeAdoClient(default_branches={ADO_REPO: "main"})
    provider = FakeProvider(scripts={
        "coder": [_coder_creates("MARKER2.md")],
    })

    engine = build_engine(
        tmp_path / "logs",
        inputs=_make_inputs(repo_path, item_id=62762022, root=62759077),
        provider=provider,
        toolbelt=_ado_toolbelt(repo_path, ado=ado, twig=twig),
        test_runner=_passing_runner,
    )

    result = await engine.run("ado-topo")
    assert isinstance(result, Completed), result
    assert result.final_node == "end_handoff"

    # Branch naming is ADR-0006 Option D regardless of platform.
    assert len(ado.created_prs) == 1
    assert ado.created_prs[0]["head"] == "impl/62759077-62762022"
    assert ado.created_prs[0]["base"] == "main"


async def test_implementation_pr_creation_failure_against_ado(
    repo_path: Path, tmp_path: Path,
) -> None:
    """When FakeAdoClient.pr_create raises (AdoClientError), the
    implementation workflow must catch it through the
    _REPO_CLIENT_ERRORS tuple and surface as a structured failure —
    NOT crash with an uncaught GhClientError-only handler."""
    from requiem.clients.azuredevops import AdoUnknownError

    _make_pushable(repo_path)
    twig = _FakeTwig(item=_make_item(item_id=62762023))
    ado = FakeAdoClient(
        default_branches={ADO_REPO: "main"},
        raise_on_create=AdoUnknownError("simulated ADO outage", status=503),
    )
    provider = FakeProvider(scripts={
        "coder": [_coder_creates("MARKER3.md")],
    })

    engine = build_engine(
        tmp_path / "logs",
        inputs=_make_inputs(repo_path, item_id=62762023),
        provider=provider,
        toolbelt=_ado_toolbelt(repo_path, ado=ado, twig=twig),
        test_runner=_passing_runner,
    )

    result = await engine.run("ado-fail")
    # The workflow surfaces an unhandled PR-create as a needs_human
    # terminal (the create_pr branch routes its except-clause through
    # PermanentFailure -> end_needs_human). The load-bearing assertion
    # is that we got HERE — not that the exception propagated.
    assert isinstance(result, Completed)
    assert result.final_node == "end_needs_human"

    # No PR was created (the fake raised before mutating state).
    assert ado.created_prs == []


async def test_implementation_pr_search_failure_against_ado(
    repo_path: Path, tmp_path: Path,
) -> None:
    """find_open_pr_for_branch failure on ADO must be caught by the
    workflow through _REPO_CLIENT_ERRORS — not crash on
    GhClientError-only handling."""
    from requiem.clients.azuredevops import AdoUnknownError

    _make_pushable(repo_path)
    twig = _FakeTwig(item=_make_item(item_id=62762024))
    ado = FakeAdoClient(
        default_branches={ADO_REPO: "main"},
        raise_on_search=AdoUnknownError("simulated search outage", status=503),
    )
    provider = FakeProvider(scripts={
        "coder": [_coder_creates("MARKER4.md")],
    })

    engine = build_engine(
        tmp_path / "logs",
        inputs=_make_inputs(repo_path, item_id=62762024),
        provider=provider,
        toolbelt=_ado_toolbelt(repo_path, ado=ado, twig=twig),
        test_runner=_passing_runner,
    )

    result = await engine.run("ado-search-fail")
    assert isinstance(result, Completed)
    assert result.final_node == "end_needs_human"
    # No PR was opened because the idempotency probe failed first.
    assert ado.created_prs == []


async def test_implementation_fail_closed_with_no_repo_client(
    repo_path: Path, tmp_path: Path,
) -> None:
    """When neither toolbelt.repo nor toolbelt.gh is set, the workflow
    must fail closed at create_pr — never silently skip the PR step or
    fall back to a fake/stub. INV-NO-CORRUPT-FORWARD applied at
    construction."""
    _make_pushable(repo_path)
    twig = _FakeTwig(item=_make_item(item_id=62762025))
    no_repo_toolbelt = Toolbelt(
        git=RealGitClient(),
        files=FakeFileClient({}),
        gh=None,
        repo=None,
        fs=FilesystemClient(repo_path),
        twig=twig,  # type: ignore[arg-type]
    )
    provider = FakeProvider(scripts={
        "coder": [_coder_creates("MARKER5.md")],
    })

    engine = build_engine(
        tmp_path / "logs",
        inputs=_make_inputs(repo_path, item_id=62762025),
        provider=provider,
        toolbelt=no_repo_toolbelt,
        test_runner=_passing_runner,
    )

    result = await engine.run("no-repo")
    # Missing-repo-client routes through PermanentFailure → end_needs_human
    # (the create_pr verb's _require_repo_platform check returns
    # PermanentFailure; the workflow routes permanent_failure to the
    # NeedsHuman terminal).
    assert isinstance(result, Completed)
    assert result.final_node == "end_needs_human"


# ---- back-compat: gh-only path still works ----------------------------


async def test_back_compat_gh_only_still_works_on_implementation(
    repo_path: Path, tmp_path: Path,
) -> None:
    """The existing GitHub path — toolbelt.gh set, toolbelt.repo=None —
    must continue to work after the refit. This is the
    no-regression pin for every test in
    ``test_implementation_workflow.py`` that wires via toolbelt.gh."""
    from requiem.clients.gh import GhPullRequest

    _make_pushable(repo_path)
    twig = _FakeTwig(item=_make_item(item_id=12345))

    # Minimal in-place FakeGh matching the existing test fixture's
    # shape — enough to satisfy find_open_pr_for_branch + pr_create.
    class _FakeGh:
        def __init__(self) -> None:
            self.existing_prs: list[GhPullRequest] = []
            self.created_calls: list[dict[str, object]] = []
            self.pr_number = 42

        async def find_open_pr_for_branch(
            self, repo: str, *, head: str, limit: int = 30,
        ) -> list[GhPullRequest]:
            return [pr for pr in self.existing_prs
                    if pr.head == head and pr.state == "open"]

        async def pr_create(self, repo: str, *, title: str, body: str,
                            head: str, base: str) -> GhPullRequest:
            n = self.pr_number
            url = f"https://github.com/{repo}/pull/{n}"
            pr = GhPullRequest(
                number=n, title=title, state="open",
                merged_at=None, head=head, base=base, url=url,
                raw={"number": n, "title": title, "url": url},
            )
            self.created_calls.append(
                {"title": title, "body": body, "head": head,
                 "base": base, "url": url}
            )
            return pr

    gh = _FakeGh()
    provider = FakeProvider(scripts={
        "coder": [_coder_creates("MARKER_GH.md")],
        "coder_revision": [],
    })

    # repo=None forces the workflow to fall through to gh — the
    # back-compat path documented in _require_repo_platform's docstring.
    gh_only_toolbelt = Toolbelt(
        git=RealGitClient(),
        files=FakeFileClient({}),
        gh=gh,  # type: ignore[arg-type]
        repo=None,
        fs=FilesystemClient(repo_path),
        twig=twig,  # type: ignore[arg-type]
    )

    engine = build_engine(
        tmp_path / "logs",
        inputs=ImplementationInputs(
            item_id=12345, repo="Owner/Repo", repo_path=repo_path,
            base_branch="main", test_command="pytest -q", dry_run=False,
        ),
        provider=provider,
        toolbelt=gh_only_toolbelt,
        test_runner=_passing_runner,
    )

    result = await engine.run("gh-back-compat")
    assert isinstance(result, Completed)
    assert result.disposition == "completed"
    assert result.final_node == "end_handoff"
    # PR opened on the GitHub fake (proving back-compat path works).
    assert len(gh.created_calls) == 1
    assert gh.created_calls[0]["head"] == "feature/12345"
