"""Tests for ADR-0030 §1 commit_context_pack verb (idempotent, dry-run).

These run against a real throwaway git repo via subprocess so the
verb's git_add + git_commit hit a real branch. Hermetic — no network,
no live ADO.

Pinned behaviour:
  * First call with a fresh hash commits AGENTS.md + rationale.md +
    acceptance.md + .plan_hash sentinel in one commit and returns
    ``committed=True``.
  * Second call with the same plan_hash is a no-op (the sentinel
    matches) and returns ``committed=False, reason='already_current'``.
  * A different plan_hash forces a fresh commit.
  * ``dry_run=True`` returns the pack info, commits nothing, writes
    nothing.
  * Doctrine truncation is surfaced via ``doctrine_truncated`` on the
    return value so the caller can emit the observability event.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from requiem.clients.fs import FilesystemClient
from requiem.context_pack import (
    ContextPack,
    commit_context_pack,
)
from requiem.outcomes import Success


def _make_repo(root: Path) -> Path:
    """Initialise a throwaway git repo with one commit. Returns the path."""
    root.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(cmd, cwd=root, check=True)
    (root / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return root


def _pack(plan_hash: str = "h0", **overrides) -> ContextPack:
    base = dict(
        leaf_id="42",
        agents_md="# Context for leaf 42\n\n(test fixture)\n",
        rationale_md="# Rationale\n\n(test fixture)\n",
        acceptance_md="# Acceptance\n\n(test fixture)\n",
        plan_hash=plan_hash,
        doctrine_truncated=False,
    )
    base.update(overrides)
    return ContextPack(**base)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "wt")


@pytest.fixture
def fs(repo: Path) -> FilesystemClient:
    return FilesystemClient(repo)


pytestmark = pytest.mark.asyncio


# ---- fresh commit path -------------------------------------------------


async def test_first_call_commits_all_four_files(repo: Path, fs: FilesystemClient) -> None:
    pack = _pack(plan_hash="abc123")
    outcome = await commit_context_pack(fs=fs, repo_path=repo, leaf_branch="impl/9000-42", pack=pack)
    assert isinstance(outcome, Success)
    value = outcome.value
    assert value["committed"] is True
    assert value["plan_hash"] == "abc123"
    # All four pack files landed in .requiem/
    assert (repo / ".requiem" / "AGENTS.md").exists()
    assert (repo / ".requiem" / "rationale.md").exists()
    assert (repo / ".requiem" / "acceptance.md").exists()
    assert (repo / ".requiem" / ".plan_hash").exists()
    # The sentinel content matches the pack hash.
    assert (repo / ".requiem" / ".plan_hash").read_text(encoding="utf-8").strip() == "abc123"
    # files_changed payload echoes the four relative paths.
    files_changed = value.get("files_changed", [])
    assert len(files_changed) == 4
    assert any("AGENTS.md" in p for p in files_changed)


async def test_first_call_makes_one_commit(repo: Path, fs: FilesystemClient) -> None:
    pack = _pack(plan_hash="abc123")
    before = subprocess.check_output(["git", "rev-list", "--count", "HEAD"], cwd=repo).decode().strip()
    await commit_context_pack(fs=fs, repo_path=repo, leaf_branch="impl/9000-42", pack=pack)
    after = subprocess.check_output(["git", "rev-list", "--count", "HEAD"], cwd=repo).decode().strip()
    assert int(after) == int(before) + 1
    # Commit message references the leaf id and plan_hash prefix.
    msg = subprocess.check_output(["git", "log", "-1", "--format=%s"], cwd=repo).decode().strip()
    assert "42" in msg
    assert "abc123"[:12] in msg


# ---- idempotency -------------------------------------------------------


async def test_second_call_same_hash_is_no_op(repo: Path, fs: FilesystemClient) -> None:
    pack = _pack(plan_hash="abc123")
    o1 = await commit_context_pack(fs=fs, repo_path=repo, leaf_branch="impl/9000-42", pack=pack)
    assert isinstance(o1, Success) and o1.value["committed"] is True

    before = subprocess.check_output(["git", "rev-list", "--count", "HEAD"], cwd=repo).decode().strip()
    o2 = await commit_context_pack(fs=fs, repo_path=repo, leaf_branch="impl/9000-42", pack=pack)
    after = subprocess.check_output(["git", "rev-list", "--count", "HEAD"], cwd=repo).decode().strip()

    assert isinstance(o2, Success)
    assert o2.value["committed"] is False
    assert o2.value["reason"] == "already_current"
    # No new commit was created.
    assert int(after) == int(before)


async def test_different_plan_hash_forces_fresh_commit(repo: Path, fs: FilesystemClient) -> None:
    o1 = await commit_context_pack(
        fs=fs, repo_path=repo, leaf_branch="impl/9000-42", pack=_pack(plan_hash="v1"),
    )
    assert isinstance(o1, Success) and o1.value["committed"] is True

    before = subprocess.check_output(["git", "rev-list", "--count", "HEAD"], cwd=repo).decode().strip()
    o2 = await commit_context_pack(
        fs=fs, repo_path=repo, leaf_branch="impl/9000-42",
        pack=_pack(plan_hash="v2", agents_md="# Context v2\n"),
    )
    after = subprocess.check_output(["git", "rev-list", "--count", "HEAD"], cwd=repo).decode().strip()
    assert isinstance(o2, Success) and o2.value["committed"] is True
    assert int(after) == int(before) + 1
    # Sentinel updated.
    assert (repo / ".requiem" / ".plan_hash").read_text(encoding="utf-8").strip() == "v2"


# ---- dry-run ------------------------------------------------------------


async def test_dry_run_writes_files_but_commits_nothing(repo: Path, fs: FilesystemClient) -> None:
    """ADR-0030 §1 (revised): dry_run writes the pack files to the
    worktree so coder_prompt's read_agents_md can splice them in, but
    skips the git commit. The on-disk pack is the read-side payload;
    the git commit is only the durable record (and gets skipped for
    non-``--live`` dogfood runs)."""
    pack = _pack(plan_hash="abc123")
    before = subprocess.check_output(["git", "rev-list", "--count", "HEAD"], cwd=repo).decode().strip()
    outcome = await commit_context_pack(
        fs=fs, repo_path=repo, leaf_branch="impl/9000-42", pack=pack, dry_run=True,
    )
    after = subprocess.check_output(["git", "rev-list", "--count", "HEAD"], cwd=repo).decode().strip()
    assert isinstance(outcome, Success)
    assert outcome.value["committed"] is False
    assert outcome.value["reason"] == "dry_run"
    # No new commit landed.
    assert int(after) == int(before)
    # BUT the files DID land on disk — that's the contract the
    # coder_prompt depends on.
    assert (repo / ".requiem" / "AGENTS.md").exists()
    assert (repo / ".requiem" / "rationale.md").exists()
    assert (repo / ".requiem" / "acceptance.md").exists()
    # files_changed reflects what was written (4 files including sentinel).
    assert len(outcome.value["files_changed"]) == 4


# ---- doctrine_truncated surfacing --------------------------------------


async def test_doctrine_truncated_flag_propagates_to_receipt(repo: Path, fs: FilesystemClient) -> None:
    pack = _pack(plan_hash="abc123", doctrine_truncated=True)
    outcome = await commit_context_pack(
        fs=fs, repo_path=repo, leaf_branch="impl/9000-42", pack=pack,
    )
    assert isinstance(outcome, Success)
    assert outcome.value["doctrine_truncated"] is True


async def test_doctrine_truncated_flag_propagates_in_dry_run(repo: Path, fs: FilesystemClient) -> None:
    """Even in dry_run the flag must reach the caller so the orchestrator
    can decide whether to emit context_pack_truncated."""
    pack = _pack(plan_hash="abc123", doctrine_truncated=True)
    outcome = await commit_context_pack(
        fs=fs, repo_path=repo, leaf_branch="impl/9000-42", pack=pack, dry_run=True,
    )
    assert isinstance(outcome, Success)
    assert outcome.value["doctrine_truncated"] is True
