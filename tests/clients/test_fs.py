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


# ---- error hierarchy sanity -----------------------------------------


def test_all_errors_inherit_from_fsclienterror():
    for cls in (
        FsNotFoundError,
        FsCrossVolumeError,
        FsGitError,
        FsNotAGitRepoError,
    ):
        assert issubclass(cls, FsClientError)
