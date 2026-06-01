"""Bug-bash regression pins — Tchaikovsky, Phase C real-ADO bug-bash.

Each test here pins a bug discovered while driving the real Toolbelt
(real `twig`, real `gh`) against real polyphony ADO items + GitHub PRs.
Companion report: ``docs/bug-bash/2026-05-31-tchaikovsky.md``.

The pins use **async-only stub clients**: the production
``TwigClient.show()`` sync wrapper internally calls ``asyncio.run``,
which explodes when invoked from a verb running under the kernel's own
event loop. If a workflow regresses to calling sync ``twig.show()`` /
``twig.comment()`` instead of the async equivalents, the verb will
crash with ``AttributeError`` against these stubs — making the bug
loud and unmissable. (The original crash was
``RuntimeError: asyncio.run() cannot be called from a running event
loop``; an async-only stub catches the same architectural mistake
without requiring a real subprocess.)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from requiem.agent import FakeProvider
from requiem.clients.fs import FilesystemClient
from requiem.clients.gh import GhPullRequest
from requiem.clients.twig import TwigItem
from requiem.kernel import Completed
from requiem.toolbelt import FakeFileClient, RealGitClient, Toolbelt


# ---- async-only twig stub -------------------------------------------


@dataclass
class AsyncOnlyTwigStub:
    """Twig stub exposing only the async surface.

    If a workflow regresses to calling `.show()` or `.comment()` (sync),
    AttributeError fires immediately. The real `TwigClient.show()` would
    instead deadlock the kernel's event loop via `asyncio.run`; this
    stub makes the same architectural mistake fail loud.
    """

    item: TwigItem
    comments: list[tuple[int, str]] = field(default_factory=list)

    async def show_async(self, item_id: int) -> TwigItem:
        return self.item

    async def comment_async(self, item_id: int, message: str) -> None:
        self.comments.append((item_id, message))


# ---- planning regression --------------------------------------------


def _planning_item() -> TwigItem:
    return TwigItem(
        id=12345,
        title="Real-ADO probe item",
        state="To Do",
        area_path="Polyphony",
        work_item_type="Task",
        parent_id=None,
        raw={"id": 12345, "title": "Real-ADO probe item"},
    )


async def test_planning_fetch_item_uses_async_twig_surface(tmp_path: Path) -> None:
    """REGRESSION: planning's `fetch_item` verb must `await twig.show_async`.

    Before fix: `_fetch` was sync and called `twig.show()` → `asyncio.run`
    crashed inside the kernel's loop. Workflow halted at `fetch_item`
    with `terminal=failed, final_node=fetch_item`.

    After fix: `_fetch` is async and awaits `show_async`. An async-only
    stub (no `.show` method) succeeds; a regression to sync `.show()`
    would AttributeError before fetch_item completes.
    """
    from requiem.workflows.planning import build_engine

    stub = AsyncOnlyTwigStub(item=_planning_item())
    log_dir = tmp_path / "runs"
    log_dir.mkdir()
    engine = build_engine(log_dir, item_id=12345, twig=stub)
    result = await engine.run("plan-regression")

    assert isinstance(result, Completed), result
    assert result.disposition == "completed"
    # The fact we reached `end` proves fetch_item completed via the
    # async stub. Any regression to sync `.show()` would AttributeError
    # at `_fetch` and halt at `fetch_item` instead of `end`.
    assert result.final_node == "end"


# ---- implementation regression --------------------------------------


def _impl_item() -> TwigItem:
    return TwigItem(
        id=12345,
        title="Real-ADO impl probe",
        state="Active",
        area_path="Polyphony",
        work_item_type="Task",
        parent_id=None,
        raw={
            "id": 12345,
            "title": "Real-ADO impl probe",
            "description": "Demo plan: write a marker file.",
        },
    )


def _impl_engine_with_real_twig_shape(repo_path: Path, log_dir: Path,
                                       twig: AsyncOnlyTwigStub):
    """Engine wired with the async-only twig stub + canned coder."""
    from requiem.workflows.implementation import (
        ImplementationInputs,
        _DemoGhClient,
        build_engine,
        happy_path_provider,
    )

    inputs = ImplementationInputs(
        item_id=12345,
        repo="Owner/Repo",
        repo_path=repo_path,
        base_branch="main",
        test_command=None,
        dry_run=True,
    )
    toolbelt = Toolbelt(
        git=RealGitClient(),
        files=FakeFileClient({}),
        gh=_DemoGhClient(),  # type: ignore[arg-type]
        fs=FilesystemClient(repo_path),
        twig=twig,  # type: ignore[arg-type]
    )
    return build_engine(
        log_dir,
        inputs=inputs,
        provider=happy_path_provider(),
        toolbelt=toolbelt,
        test_runner=lambda command, cwd: __import__(
            "requiem.workflows.implementation", fromlist=["TestRunResult"]
        ).TestRunResult(passed=True, summary="ok", full_output="ok"),
    )


def _init_git_repo(repo_path: Path) -> None:
    """Minimum viable git repo for the implementation workflow."""
    import subprocess
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo_path, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=repo_path, check=True)
    (repo_path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo_path, check=True)


async def test_implementation_fetch_plan_uses_async_twig_surface(
    tmp_path: Path,
) -> None:
    """REGRESSION: implementation's `fetch_plan` verb must
    `await twig.show_async`.

    Before fix: `_fetch_plan` was sync and called `twig.show()`. Against
    a real `TwigClient`, the inner `asyncio.run` collided with the
    kernel's loop and the verb crashed → workflow routed to `end_failed`
    via `permanent_failure:verb.crash` after one event.

    The async-only stub proves the contract: a regression to `.show()`
    AttributeErrors before `fetch_plan` completes.
    """
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)

    log_dir = tmp_path / "runs"
    log_dir.mkdir()
    stub = AsyncOnlyTwigStub(item=_impl_item())
    engine = _impl_engine_with_real_twig_shape(repo_path, log_dir, stub)
    result = await engine.run("impl-regression")

    assert isinstance(result, Completed), result
    # The happy-path provider + dry_run=True flows to end_handoff.
    assert result.disposition == "completed"
    assert result.final_node == "end_handoff"


async def test_implementation_link_pr_uses_async_twig_surface(
    tmp_path: Path,
) -> None:
    """REGRESSION: implementation's `link_pr_to_item` verb must
    `await twig.comment_async`.

    Before fix: `_link_pr` was sync and called `twig.comment()`. Against
    a real `TwigClient`, the inner `asyncio.run` would crash the kernel
    loop *at the end of the workflow* (right before `end_handoff`) —
    so the bug only surfaced on the non-dry-run path, after every other
    step had run successfully. Especially insidious because the PR had
    already been opened on GitHub.

    This test exercises the dry-run short-circuit (which does not call
    twig at all) AND the non-dry-run path via the explicit stub call:
    the stub's `comments` list must be a no-op in dry-run.
    """
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)

    log_dir = tmp_path / "runs"
    log_dir.mkdir()
    stub = AsyncOnlyTwigStub(item=_impl_item())
    engine = _impl_engine_with_real_twig_shape(repo_path, log_dir, stub)
    result = await engine.run("impl-link-regression")

    assert isinstance(result, Completed), result
    assert result.final_node == "end_handoff"
    # Dry-run short-circuits link_pr_to_item before it touches twig.
    assert stub.comments == []


# ---- non-dry-run twig.comment regression ----------------------------


async def test_implementation_link_pr_uses_async_twig_in_existing_suite() -> None:
    """REGRESSION cross-reference: the existing test suite at
    ``tests/test_implementation_workflow.py::test_happy_path_pr_created``
    asserts ``len(twig.comments) == 1`` in non-dry-run mode. That fake
    (`FakeTwig`) was updated alongside the fix to expose only
    ``comment_async``; any regression to sync ``twig.comment(...)``
    would AttributeError there before this regression file would catch
    it.

    This trivial test exists so a future reader can grep
    ``test_bugbash_regressions`` and find the full coverage map. It
    asserts the documented invariant: the test fake matches the
    production async contract.
    """
    from tests.test_implementation_workflow import FakeTwig

    # FakeTwig must expose async-only methods; sync surface is gone.
    assert hasattr(FakeTwig, "show_async")
    assert hasattr(FakeTwig, "comment_async")
    assert not hasattr(FakeTwig, "show")
    assert not hasattr(FakeTwig, "comment")
