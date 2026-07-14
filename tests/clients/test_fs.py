"""Tests for `requiem.clients.fs.FilesystemClient`.

We initialise a real (but local, network-free) git repo in `tmp_path`
for each test that touches git ops — same shape as the existing
toolbelt tests; ~1s overhead for the whole module, no fixtures hidden
behind monkeypatches.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from requiem.clients.fs import (
    FilesystemClient,
    FsClientError,
    FsCrossVolumeError,
    FsGitError,
    FsNotAGitRepoError,
    FsNotFoundError,
)


# ---- helpers ---------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    """Run git synchronously for test setup."""
    r = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        check=True,
    )
    return r.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A freshly-initialised git repo with one committed file on `main`."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "schumann@requiem.test")
    _git(tmp_path, "config", "user.name", "Schumann Test")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    seed = tmp_path / "seed.txt"
    seed.write_text("seed\n", encoding="utf-8")
    _git(tmp_path, "add", "seed.txt")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    return tmp_path


@pytest.fixture
def fs(repo: Path) -> FilesystemClient:
    return FilesystemClient(repo)


# ---- construction ----------------------------------------------------


def test_constructor_rejects_missing_root(tmp_path: Path):
    with pytest.raises(FsNotFoundError):
        FilesystemClient(tmp_path / "does-not-exist")


def test_constructor_accepts_non_git_directory(tmp_path: Path):
    c = FilesystemClient(tmp_path)
    assert c.repo_root == tmp_path.resolve()


# ---- atomic writes ---------------------------------------------------


def test_write_text_writes_utf8(tmp_path: Path):
    fs = FilesystemClient(tmp_path)
    dst = tmp_path / "hello.txt"
    fs.write_text(dst, "hello — world")
    assert dst.read_text(encoding="utf-8") == "hello — world"


def test_write_bytes_round_trip(tmp_path: Path):
    fs = FilesystemClient(tmp_path)
    dst = tmp_path / "bin.dat"
    payload = b"\x00\x01\x02 binary"
    fs.write_bytes(dst, payload)
    assert dst.read_bytes() == payload


def test_write_text_creates_parents(tmp_path: Path):
    fs = FilesystemClient(tmp_path)
    dst = tmp_path / "deep" / "nested" / "path" / "f.txt"
    fs.write_text(dst, "ok")
    assert dst.read_text(encoding="utf-8") == "ok"


def test_write_text_leaves_no_tempfile_on_success(tmp_path: Path):
    fs = FilesystemClient(tmp_path)
    dst = tmp_path / "x.txt"
    fs.write_text(dst, "ok")
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".x.txt.")]
    assert leftovers == []


def test_write_text_overwrites_existing(tmp_path: Path):
    fs = FilesystemClient(tmp_path)
    dst = tmp_path / "x.txt"
    dst.write_text("old", encoding="utf-8")
    fs.write_text(dst, "new")
    assert dst.read_text(encoding="utf-8") == "new"


# ---- reads / exists --------------------------------------------------


def test_read_text(tmp_path: Path):
    fs = FilesystemClient(tmp_path)
    (tmp_path / "r.txt").write_text("contents", encoding="utf-8")
    assert fs.read_text(tmp_path / "r.txt") == "contents"


def test_read_text_missing_raises(tmp_path: Path):
    fs = FilesystemClient(tmp_path)
    with pytest.raises(FsNotFoundError):
        fs.read_text(tmp_path / "nope.txt")


def test_exists(tmp_path: Path):
    fs = FilesystemClient(tmp_path)
    p = tmp_path / "a"
    assert fs.exists(p) is False
    p.write_text("x", encoding="utf-8")
    assert fs.exists(p) is True


# ---- cross-volume detection ------------------------------------------


def test_same_volume_check_passes_for_sibling_temp(tmp_path: Path):
    # No raise.
    FilesystemClient._assert_same_volume(tmp_path / "a.tmp", tmp_path / "a")


@pytest.mark.skipif(sys.platform != "win32", reason="drive letters are Windows-only")
def test_cross_volume_check_raises_on_different_drives():
    # We never actually touch these paths; the check is pure string math.
    with pytest.raises(FsCrossVolumeError):
        FilesystemClient._assert_same_volume(
            Path(r"C:\temp\src.tmp"), Path(r"Z:\dst\file.txt")
        )


# ---- git ops ---------------------------------------------------------


async def test_git_current_branch(fs: FilesystemClient):
    assert await fs.git_current_branch() == "main"


async def test_git_is_clean_on_fresh_repo(fs: FilesystemClient):
    assert await fs.git_is_clean() is True


async def test_git_is_clean_false_after_change(fs: FilesystemClient, repo: Path):
    (repo / "seed.txt").write_text("dirty\n", encoding="utf-8")
    assert await fs.git_is_clean() is False


async def test_git_status_porcelain_returns_lines(
    fs: FilesystemClient, repo: Path
):
    (repo / "new.txt").write_text("hi", encoding="utf-8")
    lines = await fs.git_status_porcelain()
    assert any("new.txt" in line for line in lines)


async def test_git_mv_moves_and_stages(fs: FilesystemClient, repo: Path):
    src = repo / "seed.txt"
    dst = repo / "archive" / "seed.txt"
    await fs.git_mv(src, dst)
    assert not src.exists()
    assert dst.exists()
    # `git mv` stages both sides of the rename.
    status = await fs.git_status_porcelain()
    assert any("seed.txt" in line for line in status)


async def test_git_mv_fallback_when_not_a_git_tree(tmp_path: Path, caplog):
    fs = FilesystemClient(tmp_path)
    src = tmp_path / "a.txt"
    src.write_text("hi", encoding="utf-8")
    dst = tmp_path / "sub" / "b.txt"
    with caplog.at_level("WARNING"):
        await fs.git_mv(src, dst)
    assert dst.read_text(encoding="utf-8") == "hi"
    assert not src.exists()
    assert any("not a git repo" in rec.getMessage() for rec in caplog.records)


async def test_git_mv_missing_source_raises(fs: FilesystemClient, repo: Path):
    with pytest.raises(FsNotFoundError):
        await fs.git_mv(repo / "nope.txt", repo / "dst.txt")


async def test_git_commit_returns_sha(fs: FilesystemClient, repo: Path):
    new = repo / "added.txt"
    new.write_text("payload", encoding="utf-8")
    sha = await fs.git_commit("add new file", paths=[new])
    assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)
    # And the tree is clean afterwards.
    assert await fs.git_is_clean() is True


async def test_git_commit_without_paths_uses_staged(
    fs: FilesystemClient, repo: Path
):
    (repo / "staged.txt").write_text("x", encoding="utf-8")
    _git(repo, "add", "staged.txt")
    sha = await fs.git_commit("commit staged")
    assert sha
    assert await fs.git_is_clean() is True


async def test_git_commit_nothing_to_commit_raises(fs: FilesystemClient):
    with pytest.raises(FsGitError):
        await fs.git_commit("nothing here")


async def test_git_ops_run_from_repo_root_not_pwd(
    fs: FilesystemClient, repo: Path
):
    # Spawn the client bound to `repo`, then chdir somewhere unrelated.
    # git_current_branch must still report `main`.
    elsewhere = repo.parent
    cwd = os.getcwd()
    try:
        os.chdir(elsewhere)
        assert await fs.git_current_branch() == "main"
    finally:
        os.chdir(cwd)


async def test_git_op_on_non_git_tree_raises(tmp_path: Path):
    fs = FilesystemClient(tmp_path)
    with pytest.raises(FsNotAGitRepoError):
        await fs.git_current_branch()


async def test_git_remote_url_reads_named_remote(
    fs: FilesystemClient, repo: Path
) -> None:
    _git(
        repo,
        "remote",
        "add",
        "origin",
        "https://dev.azure.com/contoso/project/_git/repo",
    )
    assert await fs.git_remote_url() == (
        "https://dev.azure.com/contoso/project/_git/repo"
    )


async def test_git_local_branches_and_compare_delete(
    fs: FilesystemClient, repo: Path
) -> None:
    _git(repo, "branch", "impl/42-7")
    branches = await fs.git_local_branches()
    expected_sha = branches["impl/42-7"]
    assert branches["main"] == expected_sha

    await fs.git_delete_branch_ref(
        "impl/42-7",
        expected_sha=expected_sha,
    )
    assert "impl/42-7" not in await fs.git_local_branches()


async def test_git_delete_branch_ref_rejects_sha_drift(
    fs: FilesystemClient, repo: Path
) -> None:
    _git(repo, "branch", "impl/42-7")
    with pytest.raises(FsGitError):
        await fs.git_delete_branch_ref(
            "impl/42-7",
            expected_sha="1" * 40,
        )
    assert "impl/42-7" in await fs.git_local_branches()


async def test_git_detach_head_preserves_worktree_contents(
    fs: FilesystemClient, repo: Path
) -> None:
    branch = "impl/42-7"
    _git(repo, "checkout", "-q", "-b", branch)
    expected_sha = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "seed.txt").write_text("modified\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("preserve\n", encoding="utf-8")

    await fs.git_detach_head(
        expected_branch=branch,
        expected_sha=expected_sha,
    )

    assert await fs.git_current_branch() == "HEAD"
    assert _git(repo, "rev-parse", "HEAD").strip() == expected_sha
    assert (repo / "seed.txt").read_text(encoding="utf-8") == "modified\n"
    assert (repo / "untracked.txt").read_text(encoding="utf-8") == "preserve\n"
    assert (await fs.git_local_branches())[branch] == expected_sha


async def test_git_detach_head_rejects_branch_drift(
    fs: FilesystemClient, repo: Path
) -> None:
    expected_sha = _git(repo, "rev-parse", "HEAD").strip()

    with pytest.raises(FsGitError, match="refused to detach changed HEAD"):
        await fs.git_detach_head(
            expected_branch="impl/42-7",
            expected_sha=expected_sha,
        )

    assert await fs.git_current_branch() == "main"


async def test_git_rebaseline_head_replaces_stale_context_with_remote_tree(
    fs: FilesystemClient, repo: Path
) -> None:
    remote = repo.parent / f"{repo.name}-remote.git"
    _git(repo.parent, "clone", "-q", "--bare", str(repo), str(remote))
    _git(repo, "remote", "add", "origin", str(remote))
    base_sha = _git(repo, "rev-parse", "main").strip()

    branch = "impl/42-7"
    _git(repo, "checkout", "-q", "-b", branch)
    pack = repo / ".requiem"
    pack.mkdir()
    (pack / "AGENTS.md").write_text("stale leaf context\n", encoding="utf-8")
    _git(repo, "add", ".requiem/AGENTS.md")
    _git(repo, "commit", "-q", "-m", "stale leaf")
    stale_sha = _git(repo, "rev-parse", "HEAD").strip()

    await fs.git_rebaseline_head(
        remote="origin",
        branch="main",
        expected_current_branch=branch,
        expected_current_sha=stale_sha,
        expected_target_sha=base_sha,
    )

    assert await fs.git_current_branch() == "HEAD"
    assert await fs.git_head_sha() == base_sha
    assert not (repo / ".requiem" / "AGENTS.md").exists()
    assert (await fs.git_local_branches())[branch] == stale_sha
    assert await fs.git_is_clean()


async def test_git_rebaseline_head_rejects_dirty_worktree_without_discard(
    fs: FilesystemClient, repo: Path
) -> None:
    remote = repo.parent / f"{repo.name}-remote.git"
    _git(repo.parent, "clone", "-q", "--bare", str(repo), str(remote))
    _git(repo, "remote", "add", "origin", str(remote))
    base_sha = _git(repo, "rev-parse", "main").strip()
    (repo / "seed.txt").write_text("operator change\n", encoding="utf-8")

    with pytest.raises(FsGitError, match="dirty worktree"):
        await fs.git_rebaseline_head(
            remote="origin",
            branch="main",
            expected_current_branch="main",
            expected_current_sha=base_sha,
            expected_target_sha=base_sha,
        )

    assert await fs.git_current_branch() == "main"
    assert await fs.git_head_sha() == base_sha
    assert (repo / "seed.txt").read_text(encoding="utf-8") == "operator change\n"


async def test_git_rebaseline_head_rejects_source_head_drift(
    fs: FilesystemClient, repo: Path
) -> None:
    base_sha = _git(repo, "rev-parse", "main").strip()

    with pytest.raises(FsGitError, match="changed HEAD"):
        await fs.git_rebaseline_head(
            remote="origin",
            branch="main",
            expected_current_branch="main",
            expected_current_sha="0" * 40,
            expected_target_sha=base_sha,
        )

    assert await fs.git_current_branch() == "main"
    assert await fs.git_head_sha() == base_sha
    assert await fs.git_is_clean()


async def test_git_rebaseline_head_rejects_fetched_target_mismatch(
    fs: FilesystemClient, repo: Path
) -> None:
    remote = repo.parent / f"{repo.name}-remote.git"
    _git(repo.parent, "clone", "-q", "--bare", str(repo), str(remote))
    _git(repo, "remote", "add", "origin", str(remote))
    base_sha = _git(repo, "rev-parse", "main").strip()

    with pytest.raises(FsGitError, match="authoritative branch SHA"):
        await fs.git_rebaseline_head(
            remote="origin",
            branch="main",
            expected_current_branch="main",
            expected_current_sha=base_sha,
            expected_target_sha="f" * 40,
        )

    assert await fs.git_current_branch() == "main"
    assert await fs.git_head_sha() == base_sha
    assert await fs.git_is_clean()


# ---- worktree cleanup primitives (run-#30 follow-up) ----------------
#
# The implementation workflow needs to scrub a poisoned worktree on
# bad_output / failed coder runs (run-#30 leaf 9 surfaced this). These
# primitives are thin wrappers around `git reset --hard HEAD` and
# `git clean -fd` so the cleanup verb can be tested in isolation.


async def test_git_reset_hard_drops_tracked_modifications(
    fs: FilesystemClient, repo: Path
):
    """`reset_hard` discards modifications to TRACKED files and
    returns to HEAD's tree state — the unambiguous half of the
    cleanup contract.
    """
    (repo / "seed.txt").write_text("polluted\n", encoding="utf-8")
    assert not await fs.git_is_clean()
    await fs.git_reset_hard()
    assert (repo / "seed.txt").read_text(encoding="utf-8") == "seed\n"


async def test_git_reset_hard_leaves_untracked_files(
    fs: FilesystemClient, repo: Path
):
    """`reset_hard` deliberately does NOT touch untracked files — it
    only reverts the index + tracked-file working copy. Untracked
    junk is removed separately by `git_clean_with_excludes`.
    """
    (repo / "untracked.txt").write_text("hi\n", encoding="utf-8")
    await fs.git_reset_hard()
    assert (repo / "untracked.txt").exists()


async def test_git_clean_with_excludes_removes_untracked(
    fs: FilesystemClient, repo: Path
):
    """The default clean call (no excludes) removes ALL untracked
    files and dirs — the runtime shape used by the cleanup verb
    when no `.requiem/` exists yet.
    """
    (repo / "orphan.cs").write_text("// junk\n", encoding="utf-8")
    (repo / "specs").mkdir()
    (repo / "specs" / "junk.md").write_text("# junk\n", encoding="utf-8")
    await fs.git_clean_with_excludes(excludes=[])
    assert not (repo / "orphan.cs").exists()
    assert not (repo / "specs").exists()


async def test_git_clean_with_excludes_preserves_excluded_paths(
    fs: FilesystemClient, repo: Path
):
    """The cleanup verb must NOT delete `.requiem/` bookkeeping
    (context pack, plan.tree.json) — those are framework-owned
    and surviving cleanup is the whole point of the excludes list.
    """
    (repo / ".requiem").mkdir()
    (repo / ".requiem" / "AGENTS.md").write_text(
        "context\n", encoding="utf-8"
    )
    (repo / "stray.cs").write_text("// rm me\n", encoding="utf-8")
    await fs.git_clean_with_excludes(excludes=[".requiem"])
    assert (repo / ".requiem" / "AGENTS.md").exists()
    assert not (repo / "stray.cs").exists()


async def test_git_reset_hard_followed_by_clean_yields_clean_workspace(
    fs: FilesystemClient, repo: Path
):
    """The composite contract `assert_clean_workspace` expects:
    after `reset_hard + clean(excludes=['.requiem'])`,
    `git_status_porcelain()` returns no lines outside `.requiem/`.
    Modelled on the implementation workflow's filter (`.requiem/`
    is treated as requiem-internal, not implementation content).
    """
    # Mix tracked-modification + untracked file + preserved .requiem/.
    (repo / "seed.txt").write_text("polluted\n", encoding="utf-8")
    (repo / "leaf.cs").write_text("// untracked\n", encoding="utf-8")
    (repo / ".requiem").mkdir()
    (repo / ".requiem" / "AGENTS.md").write_text("k\n", encoding="utf-8")
    await fs.git_reset_hard()
    await fs.git_clean_with_excludes(excludes=[".requiem"])
    # Nothing left to report except the preserved (untracked) .requiem/
    # bookkeeping — which the implementation workflow already filters.
    lines = await fs.git_status_porcelain()
    non_requiem = [
        line for line in lines
        if not line[3:].strip().strip('"').startswith(".requiem")
    ]
    assert non_requiem == [], (
        f"expected only .requiem/ untracked after cleanup, got: {lines!r}"
    )


# ---- error hierarchy sanity -----------------------------------------


def test_all_errors_inherit_from_fsclienterror():
    for cls in (
        FsNotFoundError,
        FsCrossVolumeError,
        FsGitError,
        FsNotAGitRepoError,
    ):
        assert issubclass(cls, FsClientError)


# ---- worktree primitive (ADR-0022, parity #5) -----------------------


async def test_git_worktree_add_creates_isolated_branch(fs: FilesystemClient, repo: Path):
    wt = repo.parent / "wt-leaf"
    await fs.git_worktree_add(wt, branch="impl/9300-1", from_ref="main")
    assert wt.exists()
    # A linked worktree's `.git` is a FILE (gitdir pointer), not a directory.
    assert (wt / ".git").is_file()
    # The worktree is on its own branch; the main checkout is untouched.
    leaf_fs = FilesystemClient(wt)
    assert await leaf_fs.git_current_branch() == "impl/9300-1"
    assert await fs.git_current_branch() == "main"


async def test_worktree_bound_client_runs_git_ops(fs: FilesystemClient, repo: Path):
    """A FilesystemClient bound to a worktree (`.git` file) runs git ops — the
    `_is_git_tree` fix. Commits in the worktree don't touch the main tree."""
    wt = repo.parent / "wt-ops"
    await fs.git_worktree_add(wt, branch="impl/9300-2", from_ref="main")
    leaf_fs = FilesystemClient(wt)
    leaf_fs.write_text(wt / "LEAF.md", "leaf\n")
    await leaf_fs.git_commit("leaf commit", [Path("LEAF.md")])
    # The file + commit live in the worktree only.
    assert (wt / "LEAF.md").exists()
    assert not (repo / "LEAF.md").exists()
    assert await leaf_fs.git_is_clean()


async def test_git_worktree_remove(fs: FilesystemClient, repo: Path):
    wt = repo.parent / "wt-rm"
    await fs.git_worktree_add(wt, branch="impl/9300-3", from_ref="main")
    assert wt.exists()
    await fs.git_worktree_remove(wt, force=True)
    assert not wt.exists()


async def test_two_worktrees_are_independent(fs: FilesystemClient, repo: Path):
    """Two worktrees added concurrently have independent branches + files —
    the isolation parity #5 relies on for parallel dispatch."""
    import asyncio
    wt1 = repo.parent / "wt-a"
    wt2 = repo.parent / "wt-b"
    await asyncio.gather(
        fs.git_worktree_add(wt1, branch="impl/9300-a", from_ref="main"),
        fs.git_worktree_add(wt2, branch="impl/9300-b", from_ref="main"),
    )
    f1, f2 = FilesystemClient(wt1), FilesystemClient(wt2)
    f1.write_text(wt1 / "A.md", "a\n")
    f2.write_text(wt2 / "B.md", "b\n")
    await asyncio.gather(
        f1.git_commit("a", [Path("A.md")]),
        f2.git_commit("b", [Path("B.md")]),
    )
    assert (wt1 / "A.md").exists() and not (wt1 / "B.md").exists()
    assert (wt2 / "B.md").exists() and not (wt2 / "A.md").exists()
    assert await f1.git_current_branch() == "impl/9300-a"
    assert await f2.git_current_branch() == "impl/9300-b"


async def test_git_worktree_list_enumerates(fs: FilesystemClient, repo: Path):
    wt = repo.parent / "wt-list"
    await fs.git_worktree_add(wt, branch="impl/9300-9", from_ref="main")
    entries = await fs.git_worktree_list()
    # The main checkout + the new linked worktree.
    branches = {e.get("branch") for e in entries}
    assert "refs/heads/main" in branches
    assert "refs/heads/impl/9300-9" in branches


async def test_git_worktree_prune_clears_stale_after_crash(
    fs: FilesystemClient, repo: Path
):
    """A worktree dir deleted without `git worktree remove` (a crash) leaves a
    prunable admin entry; `git_worktree_prune` clears it so a re-add on the same
    path doesn't collide (ADR-0022 GC)."""
    import shutil
    wt = repo.parent / "wt-crash"
    await fs.git_worktree_add(wt, branch="impl/9300-c", from_ref="main")
    shutil.rmtree(wt)  # simulate a crash: dir gone, admin entry stale
    stale = await fs.git_worktree_list()
    assert any("prunable" in e for e in stale)
    await fs.git_worktree_prune()
    assert not any("prunable" in e for e in await fs.git_worktree_list())
    # Re-add on the same path now succeeds (no "already registered" collision).
    await fs.git_worktree_add(wt, branch="impl/9300-c2", from_ref="main")
    assert wt.exists()
