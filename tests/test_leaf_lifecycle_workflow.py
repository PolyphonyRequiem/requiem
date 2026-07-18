"""Workflow tests for `requiem.workflows.leaf_lifecycle`.

These cover the guarded self-merge loop for implementation-leaf PRs only:
`impl/<root>-<item>` → `feature/<root>`. The final `feature/<root>` PR remains
out of scope and human-gated elsewhere.
"""
from __future__ import annotations

import subprocess
from dataclasses import replace as _dc_replace
from pathlib import Path

import pytest

from requiem.agent import FakeProvider
from requiem.clients.fs import FilesystemClient
from requiem.clients.repo import (
    RepoCompleteResult,
    RepoMergeabilityReport,
    RepoPullRequest,
)
from requiem.kernel import Completed, Suspended
from requiem.outcomes import RetryableFailure
from requiem.persistence import replay
from requiem.toolbelt import Toolbelt
from requiem.workflows.implementation import TestRunResult
from requiem.workflows import leaf_lifecycle as leaf_lifecycle_module
from requiem.workflows.leaf_lifecycle import (
    FakeLeafLifecycleToolkit,
    LeafLifecycleInputs,
    build_engine,
    build_result,
)

ROOT = 700
REPO = "Owner/Repo"


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs"
    d.mkdir()
    return d


@pytest.fixture
def repo_path(tmp_path: Path) -> Path:
    """A tiny, hermetic git repo — separate from ``log_dir`` — that
    ``apply_addressal``/``push_addressal`` can safely commit into via a
    real ``FilesystemClient``.
    """
    p = tmp_path / "repo"
    p.mkdir()
    _git(p, "init", "-q")
    _git(p, "config", "user.email", "test@requiem.local")
    _git(p, "config", "user.name", "Test")
    (p / "README.md").write_text("# repo\n", encoding="utf-8")
    _git(p, "add", "-A")
    _git(p, "commit", "-q", "-m", "initial")
    return p


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
    )
    return r.stdout



def _pr(*, state: str = "open", merged: bool = False,
        head: str = f"impl/{ROOT}-1", base: str = f"feature/{ROOT}") -> RepoPullRequest:
    return RepoPullRequest(
        number=41,
        title="leaf 1",
        state=state,
        merged=merged,
        merged_at=None,
        head=head,
        base=base,
        url="https://example.test/pr/41",
        raw={},
    )


def _mergeable(*, mergeable=True, state="clean", checks="success",
               conflicts=False, policies=True,
               head_sha=None) -> RepoMergeabilityReport:
    return RepoMergeabilityReport(
        mergeable=mergeable,
        mergeable_state=state,
        checks_state=checks,
        conflicts=conflicts,
        policies_satisfied=policies,
        head_sha=head_sha,
    )


def _toolkit(
    *,
    prs: list[RepoPullRequest] | None = None,
    mergeability: list[RepoMergeabilityReport] | None = None,
    branch_shas: list[str] | None = None,
    push_shas: list[str] | None = None,
) -> FakeLeafLifecycleToolkit:
    return FakeLeafLifecycleToolkit(
        pr_snapshots=prs or [_pr()],
        branch_sha_snapshots=branch_shas or ["head-sha-1"],
        mergeability_snapshots=mergeability or [_mergeable()],
        complete_result=RepoCompleteResult(
            number=41,
            merged=True,
            merge_sha="merge-sha-41",
            strategy="squash",
        ),
        push_shas=push_shas or ["push-sha-1", "push-sha-2", "push-sha-3"],
    )


def _provider(
    *,
    reviewer=None,
    compacted_reviewer=None,
    synth=None,
    addresser=None,
) -> FakeProvider:
    scripts = {
        "leaf_reviewer": reviewer or [
            {"verdict": "approve", "comments": [], "summary": "ok"}
        ],
        "comment_synthesizer": synth or [],
        "comment_addresser": addresser or [],
    }
    if compacted_reviewer is not None:
        scripts["compacted_leaf_reviewer"] = compacted_reviewer
    return FakeProvider(scripts=scripts)


def _passing_test_runner(command: str, cwd: Path) -> TestRunResult:
    return TestRunResult(
        passed=True,
        summary=f"passed via {command} in {cwd}",
        full_output="",
    )


def _engine(log_dir: Path, *, toolkit: FakeLeafLifecycleToolkit,
            provider: FakeProvider | None = None,
            repo_path: Path | None = None,
            dry_run=False, max_iterations=3, default_branch="main",
            test_runner=_passing_test_runner):
    inputs = LeafLifecycleInputs(
        repo=REPO,
        repo_path=repo_path or log_dir,
        leaf_id="1",
        root_item_id=ROOT,
        pr_number=41,
        default_branch=default_branch,
        test_command="pytest -q",
        dry_run=dry_run,
        max_iterations=max_iterations,
    )
    toolbelt = (
        _dc_replace(Toolbelt.real(), fs=FilesystemClient(repo_path))
        if repo_path is not None
        else None
    )
    return build_engine(
        log_dir,
        inputs=inputs,
        toolkit=toolkit,
        provider=provider or _provider(),
        toolbelt=toolbelt,
        test_runner=test_runner,
    )


def _completed_map(log_path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for ev in replay(log_path):
        if ev["kind"] == "verb_completed":
            out[ev["node_id"]] = ev["payload"]["outcome"]
    return out


async def test_happy_path_approves_and_merges(log_dir: Path, repo_path: Path):
    tk = _toolkit()
    engine = _engine(log_dir, toolkit=tk, repo_path=repo_path)
    result = await engine.run("happy")
    assert isinstance(result, Completed)
    assert result.final_node == "end_merged"
    res = build_result(_completed_map(log_dir / "happy.events.jsonl"))
    assert res.final_state == "merged"
    assert res.merge_sha == "merge-sha-41"
    assert tk.complete_calls[0]["strategy"] == "squash"
    assert tk.complete_calls[0]["expected_head_sha"] == "push-sha-1"


async def test_review_prompt_binds_verdict_to_leaf_plan_contract(
    log_dir: Path,
    repo_path: Path,
):
    tk = _toolkit()
    tk.review_diff_text = (
        "diff --git a/.requiem/rationale.md b/.requiem/rationale.md\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/.requiem/rationale.md\n"
        "@@ -0,0 +1,3 @@\n"
        "+# Rationale dump\n"
        "+\n"
        "+Implement the probe via the Accepted ACI mechanism.\n"
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    provider = _provider()
    engine = _engine(
        log_dir,
        toolkit=tk,
        provider=provider,
        repo_path=repo_path,
    )

    await engine.run("plan_contract_review")

    prompt = provider.calls[0]["user_message"]
    assert "Implement the probe via the Accepted ACI mechanism." in prompt
    assert "Reject or escalate changes that omit required scope" in prompt
    assert "Merge-bound diff (complete=True)" in prompt
    assert "definitively missing from the PR" in prompt
    assert "return `request_changes`" in prompt
    assert ".requiem/rationale.md" not in prompt.split("Merge-bound diff:", 1)[1]


async def test_moderate_review_diff_is_supplied_completely(
    log_dir: Path,
    repo_path: Path,
):
    tk = _toolkit()
    final_marker = "END-OF-MODERATE-DIFF"
    tk.review_diff_text = (
        "diff --git a/probe.cs b/probe.cs\n"
        "--- a/probe.cs\n"
        "+++ b/probe.cs\n"
        "@@ -0,0 +1 @@\n"
        f"+{'x' * 40_000}{final_marker}\n"
    )
    provider = _provider()
    engine = _engine(
        log_dir,
        toolkit=tk,
        provider=provider,
        repo_path=repo_path,
    )

    await engine.run("moderate_complete_review")

    completed = _completed_map(log_dir / "moderate_complete_review.events.jsonl")
    review = completed["prepare_review"]["value"]
    assert review["review_diff_chars"] > 30_000
    assert review["diff_complete"] is True
    assert review["diff"].endswith(final_marker)
    prompt = provider.calls[0]["user_message"]
    assert "Merge-bound diff (complete=True)" in prompt
    assert final_marker in prompt


async def test_token_exhaustion_gets_one_tool_free_compacted_review(
    log_dir: Path, repo_path: Path
):
    tk = _toolkit()
    tk.review_diff_text = (
        "diff --git a/.requiem/AGENTS.md b/.requiem/AGENTS.md\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/.requiem/AGENTS.md\n"
        "@@ -0,0 +1 @@\n"
        "+internal context\n"
        "diff --git a/.requiem/rationale.md b/.requiem/rationale.md\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/.requiem/rationale.md\n"
        "@@ -0,0 +1 @@\n"
        "+Preserve the required deployment contract.\n"
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+safe\n"
    )
    timeout = RetryableFailure(
        retry_key="review#1",
        error_kind="network_timeout",
        message=(
            "session input tokens (91647) exceeded "
            "max_cumulative_input_tokens=80000"
        ),
        receipts=({
            "kind": "llm_call",
            "model": "claude-sonnet-5",
            "input_tokens": 91647,
            "output_tokens": 59,
            "latency_ms": 240061,
            "request_id": "live-run-40",
            "error": (
                "session input tokens (91647) exceeded "
                "max_cumulative_input_tokens=80000"
            ),
        },),
    )
    provider = _provider(
        reviewer=[timeout],
        compacted_reviewer=[{
            "verdict": "approve",
            "comments": [],
            "summary": "merge-bound diff is safe",
        }],
    )
    engine = _engine(
        log_dir,
        toolkit=tk,
        provider=provider,
        repo_path=repo_path,
    )

    result = await engine.run("review_token_recovery")

    assert result.final_node == "end_merged"
    completed = _completed_map(log_dir / "review_token_recovery.events.jsonl")
    assert completed["recover_review_token_exhaustion"]["kind"] == "success"
    assert (
        completed["recover_review_token_exhaustion"]["value"]["trigger"][
            "input_tokens"
        ]
        == 91647
    )
    assert completed["verify_compacted_review"]["kind"] == "success"
    calls = {call["agent"]: call for call in provider.calls}
    assert ".requiem/AGENTS.md" not in calls["leaf_reviewer"]["user_message"]
    compacted_prompt = calls["compacted_leaf_reviewer"]["user_message"]
    assert calls["compacted_leaf_reviewer"]["model_options"] == {
        "disable_repo_tools": True
    }
    assert "Do not call tools" in compacted_prompt
    assert "Preserve the required deployment contract." in compacted_prompt
    assert "fully satisfies the leaf plan contract" in compacted_prompt
    assert "supplied merge-bound diff is complete" in compacted_prompt
    assert "return `request_changes`" in compacted_prompt
    assert "diff --git a/app.py b/app.py" in compacted_prompt
    assert ".requiem/AGENTS.md" not in compacted_prompt


async def test_non_token_reviewer_failure_is_not_disguised_as_human_gate(
    log_dir: Path
):
    provider = _provider(reviewer=[RetryableFailure(
        retry_key="review#1",
        error_kind="provider_unavailable",
        message="temporary upstream outage",
    )])
    engine = _engine(log_dir, toolkit=_toolkit(), provider=provider)

    result = await engine.run("review_runtime_failure")

    assert result.final_node == "needs_human_end"
    completed = _completed_map(log_dir / "review_runtime_failure.events.jsonl")
    assert (
        completed["recover_review_token_exhaustion"]["error_kind"]
        == "review.runtime_failure"
    )
    assert build_result(completed).final_state == "failed"
    assert all(
        call["agent"] != "compacted_leaf_reviewer" for call in provider.calls
    )


async def test_oversized_diff_refuses_unsafe_compacted_review(log_dir: Path):
    tk = _toolkit()
    tk.review_diff_text = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        f"+{'x' * (leaf_lifecycle_module.MAX_COMPACTED_REVIEW_DIFF_CHARS + 1)}\n"
    )
    provider = _provider(reviewer=[RetryableFailure(
        retry_key="review#1",
        error_kind="network_timeout",
        message=(
            "session input tokens (90000) exceeded "
            "max_cumulative_input_tokens=80000"
        ),
    )])
    engine = _engine(log_dir, toolkit=tk, provider=provider)

    result = await engine.run("review_compaction_unsafe")

    assert result.final_node == "needs_human_end"
    completed = _completed_map(log_dir / "review_compaction_unsafe.events.jsonl")
    assert (
        completed["recover_review_token_exhaustion"]["error_kind"]
        == "review.compaction_unsafe"
    )
    assert build_result(completed).final_state == "failed"


async def test_async_merge_completion_is_confirmed_by_authoritative_requery(
    log_dir: Path, repo_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(leaf_lifecycle_module, "MERGE_CONFIRMATION_RETRY_DELAY_S", 0.0)
    merged_pr = _dc_replace(
        _pr(state="merged", merged=True),
        raw={
            "mergeStatus": "succeeded",
            "lastMergeCommit": {"commitId": "merge-sha-41"},
        },
    )
    tk = _toolkit(prs=[_pr(), _pr(), _pr(), _pr(), merged_pr])
    tk.complete_result = RepoCompleteResult(
        number=41,
        merged=False,
        merge_sha=None,
        strategy="squash",
    )
    engine = _engine(log_dir, toolkit=tk, repo_path=repo_path)

    result = await engine.run("async_merge_confirmation")

    assert result.final_node == "end_merged"
    completed = _completed_map(log_dir / "async_merge_confirmation.events.jsonl")
    assert completed["merge_pr"]["error_kind"] == "merge.not_confirmed"
    assert completed["verify_merge_confirmation"]["value"] == {
        "merged": True,
        "merge_sha": "merge-sha-41",
        "strategy": "squash",
        "confirmation_method": "authoritative_pr_requery",
        "confirmation_attempt": 2,
    }
    assert len(tk.complete_calls) == 1
    lifecycle_result = build_result(completed)
    assert lifecycle_result.final_state == "merged"
    assert lifecycle_result.merge_sha == "merge-sha-41"


async def test_unresolved_merge_confirmation_suspends_with_escalation_brief(
    log_dir: Path, repo_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(leaf_lifecycle_module, "MERGE_CONFIRMATION_RETRY_DELAY_S", 0.0)
    tk = _toolkit()
    tk.complete_result = RepoCompleteResult(
        number=41,
        merged=False,
        merge_sha=None,
        strategy="squash",
    )
    engine = _engine(log_dir, toolkit=tk, repo_path=repo_path)

    result = await engine.run("unresolved_merge_confirmation")

    assert isinstance(result, Suspended)
    assert result.node_id == "verify_merge_confirmation"
    assert result.options == ("retry_verification", "abort")
    completed = _completed_map(log_dir / "unresolved_merge_confirmation.events.jsonl")
    brief = completed["verify_merge_confirmation"]["context"]
    assert brief["trigger"]["problem_kind"] == "merge.not_confirmed"
    assert brief["recovery_attempts"] == [
        {
            "kind": "authoritative_pr_requery",
            "attempts": 3,
            "result": "pr_open_not_merged",
        }
    ]
    assert brief["recommended_option"] == "retry_verification"
    assert len(tk.complete_calls) == 1
    assert build_result(completed).final_state == "needs_human"


async def test_approve_prunes_context_pack_before_merge(log_dir: Path, repo_path: Path):
    """Run #39 postmortem: every leaf writes its own `.requiem/AGENTS.md`
    (+ siblings) at the SAME fixed path. Once any leaf's copy lands on
    trunk, every other leaf's differing copy at that identical path is a
    guaranteed, spurious merge conflict — not a real code conflict. The
    fix prunes the pack from the leaf branch before it ever reaches
    `check_can_merge`/`merge_pr`, so trunk never sees it at all.
    """
    from requiem.context_pack import CONTEXT_PACK_DIR

    pack_dir = repo_path / CONTEXT_PACK_DIR
    pack_dir.mkdir()
    (pack_dir / "AGENTS.md").write_text("# Context for leaf: 1\n", encoding="utf-8")
    (pack_dir / ".plan_hash").write_text("deadbeef\n", encoding="utf-8")
    _git(repo_path, "add", "-A")
    _git(repo_path, "commit", "-q", "-m", "chore(context): requiem context pack for leaf 1")

    tk = _toolkit()
    engine = _engine(log_dir, toolkit=tk, repo_path=repo_path)
    result = await engine.run("prune")
    assert result.final_node == "end_merged"

    completed = _completed_map(log_dir / "prune.events.jsonl")
    prune_outcome = completed["prune_context_pack"]
    assert prune_outcome["value"]["pruned"] is True
    assert prune_outcome["value"]["commit_sha"] is not None
    assert prune_outcome["value"]["status_posted"] is True
    assert tk.posted_statuses == [{
        "repo": REPO,
        "sha": "push-sha-1",
        "context": "requiem/local-tests",
        "state": "success",
        "description": (
            "requiem: local tests passed before framework-only "
            "context-pack cleanup"
        ),
    }]
    call_names = [name for name, _args in tk.calls]
    push_index = call_names.index("git_push")
    status_index = call_names.index("post_commit_status")
    mergeability_index = call_names.index("pr_mergeability", status_index)
    assert push_index < status_index < mergeability_index

    assert not pack_dir.exists()
    log_msg = _git(repo_path, "log", "-1", "--format=%s")
    assert "drop requiem context-pack scaffold" in log_msg


async def test_prune_context_pack_is_idempotent_when_absent(log_dir: Path, repo_path: Path):
    """No context pack ever landed on this leaf branch (e.g. a config
    without ADR-0030's context-pack step) — pruning is a safe no-op that
    still reaches the merge instead of erroring.
    """
    tk = _toolkit()
    engine = _engine(log_dir, toolkit=tk, repo_path=repo_path)
    result = await engine.run("prune_absent")
    assert result.final_node == "end_merged"
    completed = _completed_map(log_dir / "prune_absent.events.jsonl")
    prune_value = completed["prune_context_pack"]["value"]
    assert prune_value["pruned"] is False
    assert prune_value["commit_sha"] is None
    assert prune_value["status_posted"] is True
    assert tk.posted_statuses[0]["sha"] == "push-sha-1"


async def test_fresh_cleanup_status_recovers_before_merge(
    log_dir: Path,
    repo_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(leaf_lifecycle_module, "TEST_STATUS_RETRY_DELAY_S", 0.0)
    tk = _toolkit(
        mergeability=[
            _mergeable(),
            _mergeable(checks="unknown"),
            _mergeable(),
            _mergeable(),
        ],
        branch_shas=["initial-sha", "cleanup-sha", "cleanup-sha"],
        push_shas=["cleanup-sha"],
    )
    engine = _engine(log_dir, toolkit=tk, repo_path=repo_path)

    result = await engine.run("cleanup_status_recovers")

    assert result.final_node == "end_merged"
    completed = _completed_map(log_dir / "cleanup_status_recovers.events.jsonl")
    assert completed["verify_tests_status_before_merge"]["value"][
        "verification_attempt"
    ] == 1
    assert tk.posted_statuses[0]["sha"] == "cleanup-sha"


async def test_request_changes_loops_once_then_merges(log_dir: Path, repo_path: Path):
    tk = _toolkit(push_shas=["push-sha-1"])
    provider = _provider(
        reviewer=[
            {
                "verdict": "request_changes",
                "summary": "fix the bug",
                "comments": [
                    {"file": "app.py", "line": 10, "body": "fix bug", "severity": "major"}
                ],
            },
            {"verdict": "approve", "comments": [], "summary": "now good"},
        ],
        synth=[
            {
                "actionable_items": [
                    {
                        "file": "app.py",
                        "line_range": [10, 10],
                        "change_summary": "fix bug",
                        "original_comment_ids": [1],
                    }
                ],
                "non_actionable": [],
            }
        ],
        addresser=[
            {"file_changes": [
                {"path": "app.py", "operation": "modify", "content": "# fixed\n"},
            ], "summary": "fixed", "items_addressed": [1]}
        ],
    )
    engine = _engine(log_dir, toolkit=tk, provider=provider, repo_path=repo_path)
    result = await engine.run("loop")
    assert result.final_node == "end_merged"
    res = build_result(_completed_map(log_dir / "loop.events.jsonl"))
    assert res.final_state == "merged"
    assert res.iterations == 1
    assert res.comments_addressed == 1
    assert len(tk.complete_calls) == 1


async def test_approve_with_non_blocking_comments_reconciles_and_merges(
    log_dir: Path, repo_path: Path
):
    tk = _toolkit()
    provider = _provider(reviewer=[{
        "verdict": "approve",
        "summary": "safe with optional cleanup",
        "comments": [
            {
                "file": "app.py",
                "line": 10,
                "body": "consider renaming this later",
                "severity": "minor",
            },
            {
                "file": "README.md",
                "line": None,
                "body": "small wording nit",
                "severity": "nit",
            },
        ],
    }])
    engine = _engine(log_dir, toolkit=tk, provider=provider, repo_path=repo_path)

    result = await engine.run("approve_non_blocking")

    assert result.final_node == "end_merged"
    completed = _completed_map(log_dir / "approve_non_blocking.events.jsonl")
    assert completed["dispatch_review"]["error_kind"] == "review.inconsistent"
    assert (
        completed["reconcile_review_inconsistency"]["value"]["method"]
        == "accepted_non_blocking_comments"
    )
    assert completed["verify_review_reconciliation"]["error_kind"] == "review.approved"
    assert "deliberate_review_inconsistency" not in completed


async def test_approve_with_actionable_comments_enters_rework_loop(
    log_dir: Path, repo_path: Path
):
    tk = _toolkit(push_shas=["push-sha-1"])
    provider = _provider(
        reviewer=[
            {
                "verdict": "approve",
                "summary": "safe after one concrete fix",
                "comments": [{
                    "file": "app.py",
                    "line": 10,
                    "body": "fix the unsafe branch",
                    "severity": "major",
                }],
            },
            {"verdict": "approve", "comments": [], "summary": "now good"},
        ],
        synth=[{
            "actionable_items": [{
                "file": "app.py",
                "line_range": [10, 10],
                "change_summary": "fix the unsafe branch",
                "original_comment_ids": [1],
            }],
            "non_actionable": [],
        }],
        addresser=[{
            "file_changes": [{
                "path": "app.py",
                "operation": "modify",
                "content": "# fixed\n",
            }],
            "summary": "fixed",
            "items_addressed": [1],
        }],
    )
    engine = _engine(log_dir, toolkit=tk, provider=provider, repo_path=repo_path)

    result = await engine.run("approve_actionable")

    assert result.final_node == "end_merged"
    completed = _completed_map(log_dir / "approve_actionable.events.jsonl")
    assert (
        completed["reconcile_review_inconsistency"]["value"]["method"]
        == "promoted_actionable_comments"
    )
    assert completed["verify_review_reconciliation"]["kind"] == "success"
    assert build_result(completed).comments_addressed == 1


async def test_ambiguous_review_gets_one_bounded_reviewer_deliberation(
    log_dir: Path, repo_path: Path
):
    tk = _toolkit()
    provider = _provider(reviewer=[
        {
            "verdict": "request_changes",
            "comments": [],
            "summary": "something should change",
        },
        {"verdict": "approve", "comments": [], "summary": "safe as-is"},
    ])
    engine = _engine(log_dir, toolkit=tk, provider=provider, repo_path=repo_path)

    result = await engine.run("review_deliberation")

    assert result.final_node == "end_merged"
    completed = _completed_map(log_dir / "review_deliberation.events.jsonl")
    assert (
        completed["reconcile_review_inconsistency"]["error_kind"]
        == "review.deliberation_required"
    )
    assert completed["deliberate_review_inconsistency"]["kind"] == "success"
    attempts = completed["verify_review_reconciliation"]["details"]["recovery_attempts"]
    assert [attempt["kind"] for attempt in attempts] == [
        "deterministic_reconciliation",
        "reviewer_deliberation",
    ]


async def test_unresolved_review_inconsistency_suspends_with_escalation_brief(
    log_dir: Path
):
    tk = _toolkit()
    provider = _provider(reviewer=[
        {
            "verdict": "request_changes",
            "comments": [],
            "summary": "something should change",
        },
        {
            "verdict": "needs_human",
            "comments": [],
            "summary": "requirements are ambiguous",
        },
    ])
    engine = _engine(log_dir, toolkit=tk, provider=provider)

    result = await engine.run("review_escalation")

    assert isinstance(result, Suspended)
    assert result.node_id == "verify_review_reconciliation"
    assert result.options == ("retry_review", "abort")
    completed = _completed_map(log_dir / "review_escalation.events.jsonl")
    brief = completed["verify_review_reconciliation"]["context"]
    assert brief["trigger"]["problem_kind"] == "review.inconsistent"
    assert brief["recommended_option"] == "retry_review"
    assert len(brief["recovery_attempts"]) == 2


async def test_tests_not_passed_precondition_blocks_before_review(log_dir: Path):
    tk = _toolkit(mergeability=[_mergeable(checks="failure")])
    engine = _engine(log_dir, toolkit=tk)
    result = await engine.run("tests_gate")
    assert result.final_node == "needs_human_end"
    completed = _completed_map(log_dir / "tests_gate.events.jsonl")
    assert completed["check_tests_passed"]["error_kind"] == "needs_human.tests_not_passed"
    assert "review_leaf" not in completed
    assert tk.complete_calls == []


async def test_tests_status_unknown_precondition_blocks_before_review(log_dir: Path):
    tk = _toolkit(mergeability=[_mergeable(checks="unknown")])
    engine = _engine(log_dir, toolkit=tk)
    result = await engine.run("tests_unknown")
    assert result.final_node == "needs_human_end"
    completed = _completed_map(log_dir / "tests_unknown.events.jsonl")
    assert completed["check_tests_passed"]["error_kind"] == "needs_human.tests_status_unknown"
    assert "review_leaf" not in completed


async def test_pending_test_status_recovers_after_authoritative_reread(
    log_dir: Path,
    repo_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(leaf_lifecycle_module, "TEST_STATUS_RETRY_DELAY_S", 0.0)
    tk = _toolkit(
        mergeability=[
            _mergeable(checks="pending"),
            _mergeable(),
            _mergeable(),
        ],
    )
    engine = _engine(log_dir, toolkit=tk, repo_path=repo_path)

    result = await engine.run("tests_pending_recovers")

    assert result.final_node == "end_merged"
    completed = _completed_map(log_dir / "tests_pending_recovers.events.jsonl")
    assert completed["check_tests_passed"]["error_kind"] == (
        "tests_status.recheck_required"
    )
    assert completed["verify_tests_status_before_review"]["value"][
        "verification_attempt"
    ] == 1


async def test_test_status_verification_rejects_concurrent_head_change(
    log_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(leaf_lifecycle_module, "TEST_STATUS_RETRY_DELAY_S", 0.0)
    tk = _toolkit(
        mergeability=[
            _mergeable(checks="pending", head_sha="expected-sha"),
            _mergeable(checks="pending", head_sha="expected-sha"),
        ],
        branch_shas=["initial-sha", "changed-sha"],
    )
    engine = _engine(log_dir, toolkit=tk)

    result = await engine.run("tests_head_changed")

    assert result.final_node == "needs_human_end"
    completed = _completed_map(log_dir / "tests_head_changed.events.jsonl")
    outcome = completed["verify_tests_status_before_review"]
    assert outcome["error_kind"] == "needs_human.tests_status_unknown"
    assert outcome["details"]["expected_head_sha"] == "expected-sha"
    assert outcome["details"]["current_head_sha"] == "changed-sha"


async def test_review_fixes_rerun_tests_publish_status_and_recover_unknown(
    log_dir: Path,
    repo_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(leaf_lifecycle_module, "TEST_STATUS_RETRY_DELAY_S", 0.0)
    review = {
        "verdict": "request_changes",
        "summary": "fix it",
        "comments": [{"file": "a.py", "line": 1, "body": "fix", "severity": "major"}],
    }
    synth = {
        "actionable_items": [{
            "file": "a.py",
            "line_range": [1, 1],
            "change_summary": "fix",
            "original_comment_ids": [1],
        }],
        "non_actionable": [],
    }
    addr = {
        "file_changes": [{"path": "a.py", "operation": "create", "content": "fixed\n"}],
        "summary": "fixed",
        "items_addressed": [1],
    }
    test_calls: list[tuple[str, Path]] = []

    def runner(command: str, cwd: Path) -> TestRunResult:
        test_calls.append((command, cwd))
        return TestRunResult(passed=True, summary="green", full_output="")

    tk = _toolkit(
        mergeability=[
            _mergeable(),
            _mergeable(checks="unknown"),
            _mergeable(),
            _mergeable(),
        ],
        branch_shas=[
            "initial-sha",
            "review-fix-sha",
            "review-fix-sha",
        ],
        push_shas=["review-fix-sha"],
    )
    engine = _engine(
        log_dir,
        toolkit=tk,
        provider=_provider(
            reviewer=[review, {"verdict": "approve", "comments": [], "summary": "ok"}],
            synth=[synth],
            addresser=[addr],
        ),
        repo_path=repo_path,
        test_runner=runner,
    )

    result = await engine.run("addressal_status_recovers")

    assert result.final_node == "end_merged"
    assert test_calls == [("pytest -q", repo_path)]
    completed = _completed_map(log_dir / "addressal_status_recovers.events.jsonl")
    assert completed["run_addressal_tests"]["value"]["passed"] is True
    assert completed["check_tests_passed"]["error_kind"] == (
        "tests_status.recheck_required"
    )
    assert completed["verify_tests_status_before_review"]["value"][
        "verification_attempt"
    ] == 1
    assert tk.posted_statuses[0] == {
        "repo": REPO,
        "sha": "review-fix-sha",
        "context": "requiem/local-tests",
        "state": "success",
        "description": "requiem: local tests passed after review fixes",
    }


async def test_transient_addressal_timeout_retries_once(
    log_dir: Path,
    repo_path: Path,
):
    review = {
        "verdict": "request_changes",
        "summary": "add missing rollout wiring",
        "comments": [{
            "file": "rollout.json",
            "line": 1,
            "body": "add the rollout step",
            "severity": "blocker",
        }],
    }
    synth = {
        "actionable_items": [{
            "file": "rollout.json",
            "line_range": [1, 1],
            "change_summary": "add the rollout step",
            "original_comment_ids": [1],
        }],
        "non_actionable": [],
    }
    timeout = RetryableFailure(
        retry_key="addressal#1",
        error_kind="network_timeout",
        message="comment addresser became idle",
        after=0.0,
    )
    addressal = {
        "file_changes": [{
            "path": "rollout.json",
            "operation": "create",
            "content": '{"step": "probe"}\n',
        }],
        "summary": "added rollout step",
        "items_addressed": [1],
    }
    provider = _provider(
        reviewer=[review, {"verdict": "approve", "comments": [], "summary": "ok"}],
        synth=[synth],
        addresser=[timeout, addressal],
    )
    engine = _engine(
        log_dir,
        toolkit=_toolkit(),
        provider=provider,
        repo_path=repo_path,
    )

    result = await engine.run("addressal_timeout_recovers")

    assert result.final_node == "end_merged"
    assert [
        call["agent"] for call in provider.calls
    ].count("comment_addresser") == 2
    completed = _completed_map(log_dir / "addressal_timeout_recovers.events.jsonl")
    assert completed["address_comments"]["kind"] == "success"
    assert (repo_path / "rollout.json").read_text(encoding="utf-8") == (
        '{"step": "probe"}\n'
    )


async def test_review_fix_test_failure_blocks_before_push(
    log_dir: Path,
    repo_path: Path,
):
    review = {
        "verdict": "request_changes",
        "summary": "fix it",
        "comments": [{"file": "a.py", "line": 1, "body": "fix", "severity": "major"}],
    }
    synth = {
        "actionable_items": [{
            "file": "a.py",
            "line_range": [1, 1],
            "change_summary": "fix",
            "original_comment_ids": [1],
        }],
        "non_actionable": [],
    }
    addr = {
        "file_changes": [{"path": "a.py", "operation": "create", "content": "broken\n"}],
        "summary": "fixed",
        "items_addressed": [1],
    }

    def runner(command: str, cwd: Path) -> TestRunResult:
        return TestRunResult(passed=False, summary="failed", full_output="failed")

    tk = _toolkit()
    engine = _engine(
        log_dir,
        toolkit=tk,
        provider=_provider(reviewer=[review], synth=[synth], addresser=[addr]),
        repo_path=repo_path,
        test_runner=runner,
    )

    result = await engine.run("addressal_tests_fail")

    assert result.final_node == "needs_human_end"
    completed = _completed_map(log_dir / "addressal_tests_fail.events.jsonl")
    assert completed["run_addressal_tests"]["error_kind"] == (
        "needs_human.tests_not_passed"
    )
    assert not any(name == "git_push" for name, _args in tk.calls)
    assert tk.posted_statuses == []


async def test_review_fix_worktree_drift_after_tests_blocks_before_push(
    log_dir: Path,
    repo_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    review = {
        "verdict": "request_changes",
        "summary": "fix it",
        "comments": [{"file": "a.py", "line": 1, "body": "fix", "severity": "major"}],
    }
    synth = {
        "actionable_items": [{
            "file": "a.py",
            "line_range": [1, 1],
            "change_summary": "fix",
            "original_comment_ids": [1],
        }],
        "non_actionable": [],
    }
    addr = {
        "file_changes": [{"path": "a.py", "operation": "create", "content": "fixed\n"}],
        "summary": "fixed",
        "items_addressed": [1],
    }
    original = FilesystemClient.git_stage_all_and_tree_sha
    stage_calls = 0

    async def stage_with_drift(fs: FilesystemClient) -> str:
        nonlocal stage_calls
        stage_calls += 1
        if stage_calls == 2:
            (repo_path / "drift.py").write_text("untested\n", encoding="utf-8")
        return await original(fs)

    monkeypatch.setattr(
        FilesystemClient,
        "git_stage_all_and_tree_sha",
        stage_with_drift,
    )
    tk = _toolkit()
    engine = _engine(
        log_dir,
        toolkit=tk,
        provider=_provider(reviewer=[review], synth=[synth], addresser=[addr]),
        repo_path=repo_path,
    )

    result = await engine.run("addressal_tree_drift")

    assert result.final_node == "needs_human_end"
    completed = _completed_map(log_dir / "addressal_tree_drift.events.jsonl")
    outcome = completed["push_addressal"]
    assert outcome["error_kind"] == "needs_human.tests_status_unknown"
    assert outcome["details"]["reason"] == "tested_tree_changed"
    assert not any(name == "git_push" for name, _args in tk.calls)
    assert tk.posted_statuses == []


async def test_unresolved_fresh_test_status_suspends_with_escalation_brief(
    log_dir: Path,
    repo_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(leaf_lifecycle_module, "TEST_STATUS_RETRY_DELAY_S", 0.0)
    review = {
        "verdict": "request_changes",
        "summary": "fix it",
        "comments": [{"file": "a.py", "line": 1, "body": "fix", "severity": "major"}],
    }
    synth = {
        "actionable_items": [{
            "file": "a.py",
            "line_range": [1, 1],
            "change_summary": "fix",
            "original_comment_ids": [1],
        }],
        "non_actionable": [],
    }
    addr = {
        "file_changes": [{"path": "a.py", "operation": "create", "content": "fixed\n"}],
        "summary": "fixed",
        "items_addressed": [1],
    }
    unknown = _mergeable(checks="unknown")
    tk = _toolkit(
        mergeability=[_mergeable(), unknown, unknown, unknown, unknown],
        branch_shas=[
            "initial-sha",
            "review-fix-sha",
            "review-fix-sha",
            "review-fix-sha",
            "review-fix-sha",
        ],
        push_shas=["review-fix-sha"],
    )
    engine = _engine(
        log_dir,
        toolkit=tk,
        provider=_provider(reviewer=[review], synth=[synth], addresser=[addr]),
        repo_path=repo_path,
    )

    result = await engine.run("addressal_status_stays_unknown")

    assert isinstance(result, Suspended)
    assert result.node_id == "verify_tests_status_before_review"
    assert result.options == ("retry_verification", "abort")
    completed = _completed_map(
        log_dir / "addressal_status_stays_unknown.events.jsonl"
    )
    brief = completed["verify_tests_status_before_review"]["context"]
    assert brief["trigger"]["problem_kind"] == "tests_status.recheck_required"
    assert brief["recovery_attempts"] == [{
        "kind": "authoritative_test_status_reread",
        "attempts": 3,
        "result": "checks_unknown",
    }]
    assert brief["recommended_option"] == "retry_verification"
    assert tk.complete_calls == []


async def test_reviewer_needs_human_routes_to_needs_human(log_dir: Path):
    tk = _toolkit()
    engine = _engine(log_dir, toolkit=tk, provider=_provider(reviewer=[
        {"verdict": "needs_human", "comments": [], "summary": "ambiguous"}
    ]))
    result = await engine.run("reviewer_human")
    assert result.final_node == "needs_human_end"
    res = build_result(_completed_map(log_dir / "reviewer_human.events.jsonl"))
    assert res.final_state == "needs_human"


async def test_max_iterations_escalates(log_dir: Path, repo_path: Path):
    tk = _toolkit(push_shas=["sha-1", "sha-2"])
    review1 = {
        "verdict": "request_changes",
        "summary": "same fix",
        "comments": [{"file": "a.py", "line": 1, "body": "fix", "severity": "major"}],
    }
    review2 = {
        "verdict": "request_changes",
        "summary": "second fix",
        "comments": [{"file": "a.py", "line": 2, "body": "add test", "severity": "major"}],
    }
    synth1 = {
        "actionable_items": [{
            "file": "a.py", "line_range": [1, 1], "change_summary": "fix", "original_comment_ids": [1]
        }],
        "non_actionable": [],
    }
    synth2 = {
        "actionable_items": [{
            "file": "a.py", "line_range": [2, 2], "change_summary": "add test", "original_comment_ids": [1]
        }],
        "non_actionable": [],
    }
    addr = {"file_changes": [
        {"path": "a.py", "operation": "modify", "content": "# sha-x\n"},
    ], "summary": "fixed", "items_addressed": [1]}
    engine = _engine(
        log_dir,
        toolkit=tk,
        provider=_provider(
            reviewer=[review1, review2],
            synth=[synth1, synth2],
            addresser=[addr, addr],
        ),
        max_iterations=1,
        repo_path=repo_path,
    )
    result = await engine.run("max_iter")
    assert result.final_node == "needs_human_end"
    completed = _completed_map(log_dir / "max_iter.events.jsonl")
    assert completed["check_progress"]["error_kind"] == "needs_human.max_iterations"


async def test_same_sha_twice_trips_no_progress(log_dir: Path, repo_path: Path):
    tk = _toolkit(push_shas=["same-sha", "same-sha"])
    review = {
        "verdict": "request_changes",
        "summary": "same fix",
        "comments": [{"file": "a.py", "line": 1, "body": "fix", "severity": "major"}],
    }
    synth = {
        "actionable_items": [{
            "file": "a.py", "line_range": [1, 1], "change_summary": "fix", "original_comment_ids": [1]
        }],
        "non_actionable": [],
    }
    addr = {"file_changes": [
        {"path": "a.py", "operation": "modify", "content": "# same-sha\n"},
    ], "summary": "fixed", "items_addressed": [1]}
    engine = _engine(
        log_dir,
        toolkit=tk,
        provider=_provider(reviewer=[review, review], synth=[synth, synth], addresser=[addr, addr]),
        repo_path=repo_path,
    )
    result = await engine.run("no_progress")
    assert result.final_node == "needs_human_end"
    completed = _completed_map(log_dir / "no_progress.events.jsonl")
    assert completed["check_progress"]["error_kind"] == "needs_human.no_progress"


async def test_same_findings_twice_on_new_sha_escalates(log_dir: Path, repo_path: Path):
    tk = _toolkit(push_shas=["sha-1", "sha-2"])
    review = {
        "verdict": "request_changes",
        "summary": "same fix",
        "comments": [{"file": "a.py", "line": 1, "body": "fix", "severity": "major"}],
    }
    synth = {
        "actionable_items": [{
            "file": "a.py", "line_range": [1, 1], "change_summary": "fix", "original_comment_ids": [1]
        }],
        "non_actionable": [],
    }
    addr = {"file_changes": [
        {"path": "a.py", "operation": "modify", "content": "# sha-x\n"},
    ], "summary": "fixed", "items_addressed": [1]}
    engine = _engine(
        log_dir,
        toolkit=tk,
        provider=_provider(reviewer=[review, review], synth=[synth, synth], addresser=[addr, addr]),
        repo_path=repo_path,
    )
    result = await engine.run("same_findings")
    assert result.final_node == "needs_human_end"
    completed = _completed_map(log_dir / "same_findings.events.jsonl")
    assert completed["check_progress"]["error_kind"] == "needs_human.same_findings"


@pytest.mark.parametrize(
    ("report", "error_kind"),
    [
        (_mergeable(mergeable=False, state="dirty", checks="failure", conflicts=True), "needs_human.conflicts"),
        (_mergeable(mergeable=True, state="blocked", checks="failure"), "needs_human.tests_not_passed"),
        (
            _mergeable(
                mergeable=True,
                state="blocked",
                checks="unknown",
                head_sha="unpublished-sha",
            ),
            "needs_human.tests_status_unknown",
        ),
        (
            _mergeable(
                mergeable=False,
                state="rejectedByPolicy",
                checks="success",
                policies=False,
            ),
            "needs_human.policies_unsatisfied",
        ),
        (
            _mergeable(mergeable=False, state="failure", checks="success"),
            "needs_human.not_mergeable",
        ),
    ],
)
async def test_mergeability_fail_closed_paths(log_dir: Path, repo_path: Path, report, error_kind):
    precheck = _mergeable()
    tk = _toolkit(mergeability=[precheck, report])
    engine = _engine(log_dir, toolkit=tk, repo_path=repo_path)
    result = await engine.run(f"mergeability_{error_kind.split('.')[-1]}")
    assert result.final_node == "needs_human_end"
    completed = _completed_map(log_dir / f"mergeability_{error_kind.split('.')[-1]}.events.jsonl")
    assert completed["check_can_merge"]["error_kind"] == error_kind


async def test_mergeability_unknown_recovers_after_authoritative_reread(
    log_dir: Path, repo_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(leaf_lifecycle_module, "MERGEABILITY_RETRY_DELAY_S", 0.0)
    tk = _toolkit(mergeability=[
        _mergeable(),
        _mergeable(mergeable=None, state="unknown"),
        _mergeable(),
    ])
    engine = _engine(log_dir, toolkit=tk, repo_path=repo_path)

    result = await engine.run("mergeability_converges")

    assert result.final_node == "end_merged"
    completed = _completed_map(log_dir / "mergeability_converges.events.jsonl")
    assert completed["check_can_merge"]["error_kind"] == "mergeability.recheck_required"
    assert completed["verify_mergeability"]["value"]["verification_attempt"] == 1
    assert [call[0] for call in tk.calls].count("pr_mergeability") == 3
    assert len(tk.complete_calls) == 1


async def test_test_status_flap_during_mergeability_verification_recovers(
    log_dir: Path,
    repo_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(leaf_lifecycle_module, "TEST_STATUS_RETRY_DELAY_S", 0.0)
    tk = _toolkit(
        mergeability=[
            _mergeable(),
            _mergeable(mergeable=None, state="queued"),
            _mergeable(mergeable=None, state="queued", checks="unknown"),
            _mergeable(),
            _mergeable(),
        ],
        branch_shas=["initial-sha", "cleanup-sha", "cleanup-sha"],
        push_shas=["cleanup-sha"],
    )
    engine = _engine(log_dir, toolkit=tk, repo_path=repo_path)

    result = await engine.run("mergeability_checks_flap")

    assert result.final_node == "end_merged"
    completed = _completed_map(log_dir / "mergeability_checks_flap.events.jsonl")
    assert completed["verify_mergeability"]["error_kind"] == (
        "tests_status.recheck_required"
    )
    assert completed["verify_tests_status_before_merge"]["value"][
        "verification_attempt"
    ] == 1
    assert len(tk.complete_calls) == 1


async def test_unresolved_mergeability_suspends_with_escalation_brief(
    log_dir: Path, repo_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(leaf_lifecycle_module, "MERGEABILITY_RETRY_DELAY_S", 0.0)
    unknown = _mergeable(mergeable=None, state="queued")
    tk = _toolkit(mergeability=[_mergeable(), unknown, unknown, unknown, unknown])
    engine = _engine(log_dir, toolkit=tk, repo_path=repo_path)

    result = await engine.run("mergeability_stays_unknown")

    assert isinstance(result, Suspended)
    assert result.node_id == "verify_mergeability"
    assert result.options == ("retry_verification", "abort")
    completed = _completed_map(log_dir / "mergeability_stays_unknown.events.jsonl")
    brief = completed["verify_mergeability"]["context"]
    assert brief["trigger"]["problem_kind"] == "mergeability.recheck_required"
    assert brief["recovery_attempts"] == [
        {
            "kind": "authoritative_mergeability_reread",
            "attempts": 3,
            "result": "mergeability_queued",
        }
    ]
    assert brief["recommended_option"] == "retry_verification"
    assert [call[0] for call in tk.calls].count("pr_mergeability") == 5
    assert tk.complete_calls == []
    assert build_result(completed).final_state == "needs_human"


@pytest.mark.parametrize(
    ("resolved_report", "error_kind"),
    [
        (
            _mergeable(
                mergeable=False,
                state="conflicts",
                conflicts=True,
            ),
            "needs_human.conflicts",
        ),
        (
            _mergeable(
                mergeable=False,
                state="rejectedByPolicy",
                policies=False,
            ),
            "needs_human.policies_unsatisfied",
        ),
    ],
)
async def test_mergeability_reread_preserves_concrete_escalations(
    log_dir: Path,
    repo_path: Path,
    resolved_report: RepoMergeabilityReport,
    error_kind: str,
):
    tk = _toolkit(mergeability=[
        _mergeable(),
        _mergeable(mergeable=None, state="queued"),
        resolved_report,
    ])
    engine = _engine(log_dir, toolkit=tk, repo_path=repo_path)

    result = await engine.run(f"mergeability_resolves_{error_kind.split('.')[-1]}")

    assert result.final_node == "needs_human_end"
    completed = _completed_map(
        log_dir / f"mergeability_resolves_{error_kind.split('.')[-1]}.events.jsonl"
    )
    assert completed["verify_mergeability"]["error_kind"] == error_kind
    assert tk.complete_calls == []


async def test_scope_violation_rejected_on_initial_fetch(log_dir: Path):
    tk = _toolkit(prs=[_pr(base="main")])
    engine = _engine(log_dir, toolkit=tk)
    result = await engine.run("scope_initial")
    assert result.final_node == "needs_human_end"
    completed = _completed_map(log_dir / "scope_initial.events.jsonl")
    assert completed["assert_leaf_scope"]["error_kind"] == "needs_human.scope_violation"


async def test_scope_violation_rejected_on_runtime_recheck(log_dir: Path):
    tk = _toolkit(prs=[_pr(), _pr(base="main")])
    engine = _engine(log_dir, toolkit=tk)
    result = await engine.run("scope_runtime")
    assert result.final_node == "needs_human_end"
    completed = _completed_map(log_dir / "scope_runtime.events.jsonl")
    assert completed["prepare_review"]["error_kind"] == "needs_human.scope_violation"


async def test_already_merged_short_circuits(log_dir: Path):
    tk = _toolkit(prs=[_pr(state="merged", merged=True)])
    engine = _engine(log_dir, toolkit=tk)
    result = await engine.run("already")
    assert result.final_node == "end_already_merged"
    res = build_result(_completed_map(log_dir / "already.events.jsonl"))
    assert res.final_state == "already_merged"
    assert tk.complete_calls == []


async def test_dry_run_does_not_call_pr_complete(log_dir: Path):
    tk = _toolkit()
    engine = _engine(log_dir, toolkit=tk, dry_run=True)
    result = await engine.run("dry_merge")
    assert result.final_node == "end_merged"
    assert tk.complete_calls == []
    res = build_result(_completed_map(log_dir / "dry_merge.events.jsonl"))
    assert res.final_state == "merged"


def test_inputs_reject_default_branch_equal_to_expected_base():
    with pytest.raises(ValueError):
        LeafLifecycleInputs(
            repo=REPO,
            repo_path=Path("."),
            leaf_id="1",
            root_item_id=ROOT,
            pr_number=41,
            default_branch=f"feature/{ROOT}",
        )
