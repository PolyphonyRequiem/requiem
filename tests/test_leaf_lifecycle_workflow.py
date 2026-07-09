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
from requiem.persistence import replay
from requiem.toolbelt import Toolbelt
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
               conflicts=False, policies=True) -> RepoMergeabilityReport:
    return RepoMergeabilityReport(
        mergeable=mergeable,
        mergeable_state=state,
        checks_state=checks,
        conflicts=conflicts,
        policies_satisfied=policies,
    )


def _toolkit(
    *,
    prs: list[RepoPullRequest] | None = None,
    mergeability: list[RepoMergeabilityReport] | None = None,
    push_shas: list[str] | None = None,
) -> FakeLeafLifecycleToolkit:
    return FakeLeafLifecycleToolkit(
        pr_snapshots=prs or [_pr()],
        branch_sha_snapshots=["head-sha-1", "head-sha-2", "head-sha-3"],
        mergeability_snapshots=mergeability or [_mergeable()],
        complete_result=RepoCompleteResult(
            number=41,
            merged=True,
            merge_sha="merge-sha-41",
            strategy="squash",
        ),
        push_shas=push_shas or ["push-sha-1", "push-sha-2", "push-sha-3"],
    )


def _provider(*, reviewer=None, synth=None, addresser=None) -> FakeProvider:
    return FakeProvider(scripts={
        "leaf_reviewer": reviewer or [
            {"verdict": "approve", "comments": [], "summary": "ok"}
        ],
        "comment_synthesizer": synth or [],
        "comment_addresser": addresser or [],
    })


def _engine(log_dir: Path, *, toolkit: FakeLeafLifecycleToolkit,
            provider: FakeProvider | None = None,
            repo_path: Path | None = None,
            dry_run=False, max_iterations=3, default_branch="main"):
    inputs = LeafLifecycleInputs(
        repo=REPO,
        repo_path=repo_path or log_dir,
        leaf_id="1",
        root_item_id=ROOT,
        pr_number=41,
        default_branch=default_branch,
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
    assert completed["prune_context_pack"]["value"]["pruned"] is False
    assert completed["prune_context_pack"]["value"]["commit_sha"] is None


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
    tk = _toolkit(mergeability=[_mergeable(checks="pending")])
    engine = _engine(log_dir, toolkit=tk)
    result = await engine.run("tests_unknown")
    assert result.final_node == "needs_human_end"
    completed = _completed_map(log_dir / "tests_unknown.events.jsonl")
    assert completed["check_tests_passed"]["error_kind"] == "needs_human.tests_status_unknown"
    assert "review_leaf" not in completed


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
        (_mergeable(mergeable=True, state="blocked", checks="pending"), "needs_human.tests_status_unknown"),
        (_mergeable(mergeable=None, state="unknown", checks="success", policies=False), "needs_human.mergeability_unknown"),
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
