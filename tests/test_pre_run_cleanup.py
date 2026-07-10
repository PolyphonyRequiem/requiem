from __future__ import annotations

import json
from pathlib import Path

import pytest

from requiem.clients.azuredevops import AdoUnknownError, FakeAdoClient
from requiem.clients.repo import RepoPullRequest
from requiem.pre_run_cleanup import (
    CleanupDriftError,
    CleanupSafetyError,
    run_pre_run_cleanup,
)


REPO = "microsoft/CloudVault/cloudvault-service-api"
REMOTE_URL = (
    "https://dev.azure.com/microsoft/CloudVault/"
    "_git/cloudvault-service-api"
)


def _pr(number: int, head: str, base: str = "main") -> RepoPullRequest:
    return RepoPullRequest(
        number=number,
        title=f"PR {number}",
        state="open",
        merged_at=None,
        head=head,
        base=base,
        url=f"https://example.test/pr/{number}",
        raw={"_repo": REPO},
    )


class RecordingAdo(FakeAdoClient):
    def __init__(self, *args, events: list[str], **kwargs):
        super().__init__(*args, **kwargs)
        self.events = events

    async def abandon_pr(
        self, repo: str, number: int, *, expected_head: str
    ) -> RepoPullRequest:
        self.events.append(f"abandon:{number}")
        return await super().abandon_pr(
            repo,
            number,
            expected_head=expected_head,
        )

    async def delete_branch_ref(
        self, repo: str, branch: str, *, expected_sha: str
    ) -> None:
        self.events.append(f"remote:{branch}")
        await super().delete_branch_ref(
            repo,
            branch,
            expected_sha=expected_sha,
        )


class FakeGit:
    def __init__(
        self,
        *,
        branches: dict[str, str] | None = None,
        worktrees: list[dict[str, str]] | None = None,
        remote_url: str = REMOTE_URL,
        events: list[str] | None = None,
    ) -> None:
        self.branches = dict(branches or {})
        self.worktrees = list(worktrees or [])
        self.remote_url = remote_url
        self.events = events if events is not None else []

    async def git_remote_url(self, remote: str = "origin") -> str:
        assert remote == "origin"
        return self.remote_url

    async def git_local_branches(self) -> dict[str, str]:
        return dict(self.branches)

    async def git_worktree_list(self) -> list[dict[str, str]]:
        return list(self.worktrees)

    async def git_delete_branch_ref(
        self, name: str, *, expected_sha: str
    ) -> None:
        if self.branches.get(name) != expected_sha:
            raise AdoUnknownError(f"local ref drift for {name}")
        self.events.append(f"local:{name}")
        del self.branches[name]


async def test_plan_owns_only_canonical_feature_and_impl_refs(
    tmp_path: Path,
) -> None:
    ado = FakeAdoClient(
        refs={
            (REPO, "feature/42"): "feature-sha",
            (REPO, "feature/420"): "other-root",
            (REPO, "impl/42-7"): "impl-sha",
            (REPO, "plan/42"): "plan-sha",
            (REPO, "evidence/42-7"): "evidence-sha",
        },
        open_prs=[
            _pr(1, "impl/42-7", "feature/42"),
            _pr(2, "feature/42"),
            _pr(3, "plan/42"),
            _pr(4, "impl/420-7"),
        ],
    )
    git = FakeGit(branches={
        "feature/42": "feature-sha",
        "impl/42-7": "impl-sha",
        "plan/42": "plan-sha",
        "evidence/42-7": "evidence-sha",
        "feature/420": "other-root",
    })
    manifest = tmp_path / "cleanup.json"

    result = await run_pre_run_cleanup(
        repo=REPO,
        repo_path=tmp_path,
        root_item=42,
        log_dir=tmp_path / "runs",
        repo_client=ado,
        git=git,
        manifest_path=manifest,
    )

    assert result.status == "planned"
    assert [pr.number for pr in result.before.active_prs] == [1, 2]
    assert [ref.name for ref in result.before.remote_refs] == [
        "feature/42",
        "impl/42-7",
    ]
    assert [ref.name for ref in result.before.local_refs] == [
        "feature/42",
        "impl/42-7",
    ]
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["status"] == "planned"
    assert payload["actions"] == []
    assert ado.abandoned_prs == []
    assert ado.deleted_refs == []


async def test_apply_orders_mutations_and_verifies_zero_state(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    ado = RecordingAdo(
        events=events,
        refs={
            (REPO, "feature/42"): "feature-sha",
            (REPO, "impl/42-8"): "impl-8",
            (REPO, "impl/42-7"): "impl-7",
        },
        open_prs=[
            _pr(2, "feature/42"),
            _pr(1, "impl/42-7", "feature/42"),
        ],
    )
    git = FakeGit(
        branches={
            "feature/42": "feature-sha",
            "impl/42-7": "impl-7",
        },
        events=events,
    )
    lease_checks: list[str] = []

    def check_lease() -> None:
        lease_checks.append("check")

    def clean_state(item: int, log_dir: Path) -> None:
        assert item == 42
        assert log_dir == (tmp_path / "runs").resolve()
        events.append("state")

    manifest = tmp_path / "cleanup.json"
    result = await run_pre_run_cleanup(
        repo=REPO,
        repo_path=tmp_path,
        root_item=42,
        log_dir=tmp_path / "runs",
        repo_client=ado,
        git=git,
        apply=True,
        manifest_path=manifest,
        lease_check=check_lease,
        lease_identity={"token": 9, "holder": "test"},
        local_state_cleaner=clean_state,
    )

    assert result.status == "completed"
    assert events == [
        "abandon:1",
        "abandon:2",
        "remote:impl/42-7",
        "remote:impl/42-8",
        "remote:feature/42",
        "local:impl/42-7",
        "local:feature/42",
        "state",
    ]
    assert lease_checks
    assert result.after is not None
    assert result.after.active_prs == ()
    assert result.after.remote_refs == ()
    assert result.after.local_refs == ()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    assert [action["kind"] for action in payload["actions"]] == [
        "abandon_pr",
        "abandon_pr",
        "delete_remote_ref",
        "delete_remote_ref",
        "delete_remote_ref",
        "delete_local_ref",
        "delete_local_ref",
        "clean_local_state",
    ]


async def test_apply_blocks_checked_out_candidate_before_remote_mutation(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    ado = RecordingAdo(
        events=events,
        refs={(REPO, "feature/42"): "feature-sha"},
        open_prs=[_pr(1, "feature/42")],
    )
    git = FakeGit(
        branches={"feature/42": "feature-sha"},
        worktrees=[{
            "worktree": str(tmp_path / "linked"),
            "branch": "refs/heads/feature/42",
        }],
        events=events,
    )
    manifest = tmp_path / "cleanup.json"

    with pytest.raises(CleanupSafetyError, match="checked out"):
        await run_pre_run_cleanup(
            repo=REPO,
            repo_path=tmp_path,
            root_item=42,
            log_dir=tmp_path / "runs",
            repo_client=ado,
            git=git,
            apply=True,
            manifest_path=manifest,
            lease_check=lambda: None,
            lease_identity={"token": 1, "holder": "test"},
            local_state_cleaner=lambda *_: None,
        )

    assert events == []
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert "checked out" in payload["error"]


async def test_apply_fails_on_state_drift_before_mutation(
    tmp_path: Path,
) -> None:
    class DriftingGit(FakeGit):
        calls = 0

        async def git_local_branches(self) -> dict[str, str]:
            self.calls += 1
            if self.calls == 1:
                return {"feature/42": "old-sha"}
            return {"feature/42": "new-sha"}

    events: list[str] = []
    ado = RecordingAdo(
        events=events,
        refs={(REPO, "feature/42"): "feature-sha"},
        open_prs=[_pr(1, "feature/42")],
    )
    git = DriftingGit(events=events)

    with pytest.raises(CleanupDriftError):
        await run_pre_run_cleanup(
            repo=REPO,
            repo_path=tmp_path,
            root_item=42,
            log_dir=tmp_path / "runs",
            repo_client=ado,
            git=git,
            apply=True,
            manifest_path=tmp_path / "cleanup.json",
            lease_check=lambda: None,
            lease_identity={"token": 1, "holder": "test"},
            local_state_cleaner=lambda *_: None,
        )

    assert events == []


async def test_remote_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(CleanupSafetyError, match="does not match"):
        await run_pre_run_cleanup(
            repo=REPO,
            repo_path=tmp_path,
            root_item=42,
            log_dir=tmp_path / "runs",
            repo_client=FakeAdoClient(),
            git=FakeGit(remote_url="https://github.com/example/wrong.git"),
        )
