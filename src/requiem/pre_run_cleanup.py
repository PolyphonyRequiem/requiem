"""Fail-closed cleanup of stale Requiem run state before Scenario launch."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import unquote, urlparse

from requiem import branch_model
from requiem.clients.azuredevops import AdoBranchRef
from requiem.clients.repo import RepoPullRequest


class PreRunCleanupError(RuntimeError):
    """Base error for a cleanup that cannot safely continue."""


class CleanupSafetyError(PreRunCleanupError):
    """A safety precondition was not satisfied."""


class CleanupDriftError(PreRunCleanupError):
    """Observed state changed after the manifest was written."""


@dataclass(frozen=True, slots=True)
class CheckedOutBranch:
    name: str
    worktree: str
    prunable: bool


@dataclass(frozen=True, slots=True)
class CleanupPullRequest:
    number: int
    head: str
    base: str
    url: str


@dataclass(frozen=True, slots=True)
class CleanupSnapshot:
    remote_url: str
    active_prs: tuple[CleanupPullRequest, ...]
    remote_refs: tuple[AdoBranchRef, ...]
    local_refs: tuple[AdoBranchRef, ...]
    checked_out: tuple[CheckedOutBranch, ...]


@dataclass(frozen=True, slots=True)
class CleanupResult:
    status: str
    manifest_path: Path
    before: CleanupSnapshot
    after: CleanupSnapshot | None


class CleanupRepoClient(Protocol):
    async def list_branch_refs(
        self, repo: str, *, prefix: str, limit: int = 1000
    ) -> list[AdoBranchRef]: ...

    async def list_active_prs(
        self, repo: str, *, limit: int = 1000
    ) -> list[RepoPullRequest]: ...

    async def abandon_pr(
        self, repo: str, number: int, *, expected_head: str
    ) -> RepoPullRequest: ...

    async def pr_view(self, repo: str, number: int) -> RepoPullRequest: ...

    async def delete_branch_ref(
        self, repo: str, branch: str, *, expected_sha: str
    ) -> None: ...


class CleanupGitClient(Protocol):
    async def git_remote_url(self, remote: str = "origin") -> str: ...

    async def git_local_branches(self) -> dict[str, str]: ...

    async def git_worktree_list(self) -> list[dict[str, str]]: ...

    async def git_delete_branch_ref(
        self, name: str, *, expected_sha: str
    ) -> None: ...


LeaseCheck = Callable[[], None]
LocalStateCleaner = Callable[[int, Path], None]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def default_manifest_path(log_dir: Path, root_item: int) -> Path:
    timestamp = _now().strftime("%Y%m%dT%H%M%S%fZ")
    return Path(log_dir) / f"pre-run-cleanup-{root_item}-{timestamp}.json"


def _owned_branch(name: str, root: str) -> bool:
    parsed = branch_model.parse_branch(name)
    return bool(
        parsed is not None
        and parsed.root == root
        and parsed.ref_class in {branch_model.FEATURE, branch_model.IMPL}
    )


def _bare_worktree_branch(value: str) -> str:
    prefix = "refs/heads/"
    return value[len(prefix):] if value.startswith(prefix) else value


def _normalise_ado_identity(repo: str) -> tuple[str, str, str]:
    parts = tuple(unquote(part).casefold() for part in repo.split("/"))
    if len(parts) != 3 or not all(parts):
        raise CleanupSafetyError(
            f"ADO repo must be 'org/project/repository', got {repo!r}"
        )
    return parts


def _ado_identity_from_remote(remote_url: str) -> tuple[str, str, str] | None:
    raw = remote_url.strip().rstrip("/")
    if raw.startswith("git@ssh.dev.azure.com:v3/"):
        parts = raw.removeprefix("git@ssh.dev.azure.com:v3/").split("/")
        if len(parts) == 3:
            return tuple(unquote(part).casefold() for part in parts)
        return None

    parsed = urlparse(raw)
    host = (parsed.hostname or "").casefold()
    path = [unquote(part) for part in parsed.path.split("/") if part]
    if host == "ssh.dev.azure.com":
        if len(path) == 4 and path[0].casefold() == "v3":
            return tuple(part.casefold() for part in path[1:])
        return None
    if host == "dev.azure.com":
        if len(path) >= 4 and path[2].casefold() == "_git":
            return tuple(part.casefold() for part in (path[0], path[1], path[3]))
        return None
    if host.endswith(".visualstudio.com"):
        org = host.removesuffix(".visualstudio.com")
        if len(path) >= 3 and path[1].casefold() == "_git":
            return tuple(part.casefold() for part in (org, path[0], path[2]))
    return None


def _verify_remote_identity(remote_url: str, repo: str) -> None:
    expected = _normalise_ado_identity(repo)
    actual = _ado_identity_from_remote(remote_url)
    if actual != expected:
        raise CleanupSafetyError(
            f"local git remote {remote_url!r} does not match ADO repo {repo!r}"
        )


def _snapshot_json(snapshot: CleanupSnapshot) -> dict[str, object]:
    return {
        "remote_url": snapshot.remote_url,
        "active_prs": [
            asdict(pr) for pr in snapshot.active_prs
        ],
        "remote_refs": [asdict(ref) for ref in snapshot.remote_refs],
        "local_refs": [asdict(ref) for ref in snapshot.local_refs],
        "checked_out": [asdict(entry) for entry in snapshot.checked_out],
    }


def _write_manifest(path: Path, manifest: dict[str, object]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


async def _discover(
    *,
    repo: str,
    root: str,
    remote: str,
    limit: int,
    repo_client: CleanupRepoClient,
    git: CleanupGitClient,
) -> CleanupSnapshot:
    feature = branch_model.feature_trunk(root)
    impl_prefix = f"{branch_model.IMPL}/{root}-"
    (
        feature_refs,
        impl_refs,
        prs,
        local_branches,
        worktrees,
        remote_url,
    ) = await asyncio.gather(
        repo_client.list_branch_refs(repo, prefix=feature, limit=limit),
        repo_client.list_branch_refs(repo, prefix=impl_prefix, limit=limit),
        repo_client.list_active_prs(repo, limit=limit),
        git.git_local_branches(),
        git.git_worktree_list(),
        git.git_remote_url(remote),
    )
    _verify_remote_identity(remote_url, repo)

    remote_by_name: dict[str, str] = {}
    for ref in [*feature_refs, *impl_refs]:
        if not _owned_branch(ref.name, root):
            continue
        previous = remote_by_name.setdefault(ref.name, ref.sha)
        if previous != ref.sha:
            raise CleanupSafetyError(
                f"ADO returned conflicting SHAs for branch {ref.name!r}"
            )

    active_prs = tuple(sorted((
        CleanupPullRequest(
            number=pr.number,
            head=pr.head,
            base=pr.base,
            url=pr.url,
        )
        for pr in prs
        if _owned_branch(pr.head, root)
    ), key=lambda pr: pr.number))
    remote_refs = tuple(
        AdoBranchRef(name=name, sha=sha)
        for name, sha in sorted(remote_by_name.items())
    )
    local_refs = tuple(
        AdoBranchRef(name=name, sha=sha)
        for name, sha in sorted(local_branches.items())
        if _owned_branch(name, root)
    )

    checked_out: list[CheckedOutBranch] = []
    for worktree in worktrees:
        name = _bare_worktree_branch(worktree.get("branch", ""))
        if _owned_branch(name, root):
            checked_out.append(CheckedOutBranch(
                name=name,
                worktree=worktree.get("worktree", ""),
                prunable=bool(worktree.get("prunable")),
            ))

    return CleanupSnapshot(
        remote_url=remote_url,
        active_prs=active_prs,
        remote_refs=remote_refs,
        local_refs=local_refs,
        checked_out=tuple(sorted(
            checked_out,
            key=lambda entry: (entry.name, entry.worktree),
        )),
    )


def _default_local_state_cleaner(root_item: int, log_dir: Path) -> None:
    from requiem.cli.main import _clean_patterns, cmd_clean

    args = argparse.Namespace(
        item=root_item,
        log_dir=str(log_dir),
        dry_run=False,
        keep_artifacts=False,
        ado_delete=False,
        force=True,
        include_manifest=True,
    )
    if cmd_clean(args) != 0:
        raise PreRunCleanupError(
            f"existing local-state cleanup failed for item {root_item}"
        )
    remaining: list[Path] = []
    if log_dir.exists():
        for pattern in _clean_patterns(root_item, include_manifest=True):
            remaining.extend(log_dir.glob(pattern))
    if remaining:
        names = ", ".join(sorted(path.name for path in remaining))
        raise PreRunCleanupError(
            f"local-state cleanup left matching artifacts: {names}"
        )


def _assert_zero(snapshot: CleanupSnapshot) -> None:
    if snapshot.active_prs or snapshot.remote_refs or snapshot.local_refs:
        raise PreRunCleanupError(
            "post-cleanup verification found residual PRs or refs"
        )
    if snapshot.checked_out:
        raise CleanupSafetyError(
            "post-cleanup verification found a candidate branch checked out"
        )


async def run_pre_run_cleanup(
    *,
    repo: str,
    repo_path: Path,
    root_item: int,
    log_dir: Path,
    repo_client: CleanupRepoClient,
    git: CleanupGitClient,
    apply: bool = False,
    manifest_path: Path | None = None,
    remote: str = "origin",
    limit: int = 1000,
    lease_check: LeaseCheck | None = None,
    lease_identity: dict[str, object] | None = None,
    local_state_cleaner: LocalStateCleaner = _default_local_state_cleaner,
) -> CleanupResult:
    """Plan or apply cleanup for one canonical ``(repo, root-item)`` scope."""
    root = branch_model.feature_trunk(root_item).partition("/")[2]
    repo_path = Path(repo_path).resolve()
    log_dir = Path(log_dir).resolve()
    path = Path(manifest_path or default_manifest_path(log_dir, root_item)).resolve()

    before = await _discover(
        repo=repo,
        root=root,
        remote=remote,
        limit=limit,
        repo_client=repo_client,
        git=git,
    )
    manifest: dict[str, object] = {
        "schema_version": 1,
        "status": "planned",
        "created_at": _now().isoformat(),
        "repo": repo,
        "repo_path": str(repo_path),
        "root_item": root_item,
        "remote": remote,
        "apply": apply,
        "lease": lease_identity,
        "before": _snapshot_json(before),
        "actions": [],
        "after": None,
        "error": None,
    }
    _write_manifest(path, manifest)
    if not apply:
        return CleanupResult(
            status="planned",
            manifest_path=path,
            before=before,
            after=None,
        )
    if lease_check is None or lease_identity is None:
        raise CleanupSafetyError(
            "--apply requires a current fenced lease identity"
        )

    actions = manifest["actions"]
    assert isinstance(actions, list)
    try:
        lease_check()
        if before.checked_out:
            locations = ", ".join(
                f"{entry.name} at {entry.worktree}" for entry in before.checked_out
            )
            raise CleanupSafetyError(
                f"candidate branches are checked out in worktrees: {locations}"
            )

        revalidated = await _discover(
            repo=repo,
            root=root,
            remote=remote,
            limit=limit,
            repo_client=repo_client,
            git=git,
        )
        lease_check()
        if revalidated != before:
            raise CleanupDriftError(
                "observed state changed after the cleanup manifest was written"
            )
        manifest["status"] = "applying"
        _write_manifest(path, manifest)

        for pr in before.active_prs:
            lease_check()
            abandoned = await repo_client.abandon_pr(
                repo,
                pr.number,
                expected_head=pr.head,
            )
            verified = await repo_client.pr_view(repo, pr.number)
            if (
                abandoned.state != "closed"
                or verified.state != "closed"
                or verified.head != pr.head
            ):
                raise PreRunCleanupError(
                    f"PR {pr.number} was not authoritatively abandoned"
                )
            actions.append({
                "kind": "abandon_pr",
                "number": pr.number,
                "head": pr.head,
            })
            _write_manifest(path, manifest)

        impl_refs = [
            ref for ref in before.remote_refs
            if branch_model.parse_branch(ref.name).ref_class == branch_model.IMPL
        ]
        feature_refs = [
            ref for ref in before.remote_refs
            if branch_model.parse_branch(ref.name).ref_class == branch_model.FEATURE
        ]
        for ref in [*impl_refs, *feature_refs]:
            lease_check()
            await repo_client.delete_branch_ref(
                repo,
                ref.name,
                expected_sha=ref.sha,
            )
            actions.append({
                "kind": "delete_remote_ref",
                "name": ref.name,
                "expected_sha": ref.sha,
            })
            _write_manifest(path, manifest)

        after_remote = await _discover(
            repo=repo,
            root=root,
            remote=remote,
            limit=limit,
            repo_client=repo_client,
            git=git,
        )
        lease_check()
        if after_remote.active_prs or after_remote.remote_refs:
            raise PreRunCleanupError(
                "remote cleanup verification found residual active PRs or refs"
            )
        if after_remote.checked_out:
            raise CleanupSafetyError(
                "a candidate branch became checked out before local deletion"
            )
        if after_remote.local_refs != before.local_refs:
            raise CleanupDriftError(
                "local candidate refs changed before local deletion"
            )

        local_impl = [
            ref for ref in before.local_refs
            if branch_model.parse_branch(ref.name).ref_class == branch_model.IMPL
        ]
        local_feature = [
            ref for ref in before.local_refs
            if branch_model.parse_branch(ref.name).ref_class == branch_model.FEATURE
        ]
        for ref in [*local_impl, *local_feature]:
            lease_check()
            await git.git_delete_branch_ref(
                ref.name,
                expected_sha=ref.sha,
            )
            actions.append({
                "kind": "delete_local_ref",
                "name": ref.name,
                "expected_sha": ref.sha,
            })
            _write_manifest(path, manifest)

        lease_check()
        local_state_cleaner(root_item, log_dir)
        actions.append({
            "kind": "clean_local_state",
            "root_item": root_item,
            "log_dir": str(log_dir),
        })
        _write_manifest(path, manifest)

        after = await _discover(
            repo=repo,
            root=root,
            remote=remote,
            limit=limit,
            repo_client=repo_client,
            git=git,
        )
        lease_check()
        _assert_zero(after)
        manifest["status"] = "completed"
        manifest["completed_at"] = _now().isoformat()
        manifest["after"] = _snapshot_json(after)
        _write_manifest(path, manifest)
        return CleanupResult(
            status="completed",
            manifest_path=path,
            before=before,
            after=after,
        )
    except Exception as error:
        manifest["status"] = "failed"
        manifest["error"] = f"{type(error).__name__}: {error}"
        try:
            observed = await _discover(
                repo=repo,
                root=root,
                remote=remote,
                limit=limit,
                repo_client=repo_client,
                git=git,
            )
            manifest["after"] = _snapshot_json(observed)
        except Exception as observe_error:
            manifest["after_error"] = (
                f"{type(observe_error).__name__}: {observe_error}"
            )
        _write_manifest(path, manifest)
        raise
