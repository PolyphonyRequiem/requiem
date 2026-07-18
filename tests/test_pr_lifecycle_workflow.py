"""End-to-end tests for the PR-lifecycle workflow (Gluck, Phase C).

Covers the path-coverage matrix from the workflow brief plus INV-RESTART
and INV-CANCEL. Mocks the gh boundary via ``FakePrToolkit`` (the public
``PrToolkit`` Protocol) — never touches the real ``gh`` binary.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from requiem.agent import FakeProvider
from requiem.clients.gh import (
    GhAuthError,
    GhPullRequest,
    GhRateLimitedError,
)
from requiem.kernel import Completed, Failed
from requiem.outcomes import BadOutput  # noqa: F401  (used implicitly by FakeProvider)
from requiem.persistence import replay
from requiem.workflows.pr_lifecycle import (
    FakePrToolkit,
    MergeResult,
    MergeabilityReport,
    ReviewComment,
    ReviewSummary,
    build_engine,
    build_result,
)


# ---- shared fixtures + helpers --------------------------------------


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs"
    d.mkdir()
    return d


def _pr(
    *,
    number: int = 347,
    state: str = "OPEN",
    merged: bool = False,
    title: str = "feat: real LLM providers",
) -> GhPullRequest:
    return GhPullRequest(
        number=number,
        title=title,
        state=state,
        merged=merged,
        merged_at=None,
        head="feature/llm",
        base="main",
        url=f"https://github.com/PolyphonyRequiem/requiem/pull/{number}",
    )


def _mergeable_clean() -> MergeabilityReport:
    return MergeabilityReport(
        mergeable=True, mergeable_state="clean",
        checks_state="success", conflicts=False,
    )


def _toolkit(
    *,
    pr: GhPullRequest | None = None,
    reviews: list[list[ReviewSummary]] | None = None,
    comments: list[list[ReviewComment]] | None = None,
    mergeable: list[MergeabilityReport] | None = None,
    merge_result: MergeResult | None = None,
    push_shas: list[str] | None = None,
) -> FakePrToolkit:
    return FakePrToolkit(
        pr=pr or _pr(),
        review_snapshots=reviews or [[ReviewSummary(id=1, state="APPROVED", user="alice")]],
        comment_snapshots=comments or [[]],
        mergeability_snapshots=mergeable or [_mergeable_clean()],
        merge_result=merge_result or MergeResult(sha="deadbeefcafef00d", merged=True, strategy="squash"),
        push_shas=push_shas or ["push1aaaaaaaaaaaa"],
    )


def _engine(log_dir: Path, *, toolkit: FakePrToolkit, provider: FakeProvider | None = None, **kw):
    return build_engine(
        log_dir,
        repo="PolyphonyRequiem/requiem",
        pr_number=347,
        toolkit=toolkit,
        provider=provider or FakeProvider(scripts={}),
        poll_interval_s=0.0,
        poll_timeout_s=0.0,
        **kw,
    )


def _completed_map(log_path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for ev in replay(log_path):
        if ev["kind"] == "verb_completed":
            out[ev["node_id"]] = ev["payload"]["outcome"]
    return out


# ---- case 1: PR already merged --------------------------------------


async def test_already_merged_short_circuits(log_dir: Path):
    tk = _toolkit(pr=_pr(state="MERGED", merged=True))
    engine = _engine(log_dir, toolkit=tk)
    result = await engine.run("already_merged")
    assert isinstance(result, Completed), result
    assert result.final_node == "end_already_merged"
    completed = _completed_map(log_dir / "already_merged.events.jsonl")
    res = build_result(completed)
    assert res.final_state == "ALREADY_MERGED"
    assert tk.merge_count == 0
    assert tk.push_count == 0


# ---- case 2: open PR, approvals, mergeable → straight to merge ------


async def test_approvals_clean_straight_to_merge(log_dir: Path):
    tk = _toolkit()  # default = approvals + clean + merge ok
    engine = _engine(log_dir, toolkit=tk)
    result = await engine.run("happy")
    assert isinstance(result, Completed), result
    assert result.final_node == "end_merged"
    completed = _completed_map(log_dir / "happy.events.jsonl")
    res = build_result(completed)
    assert res.final_state == "MERGED"
    assert res.merged is True
    assert res.merge_sha == "deadbeefcafef00d"
    assert tk.merge_count == 1


# ---- case 3: open PR, comments → synth → address → push → merge -----


async def test_comments_loop_synthesizes_addresses_pushes_then_merges(log_dir: Path):
    comments = [
        ReviewComment(id=1, path="a.py", line=10, body="rename", user="alice"),
        ReviewComment(id=2, path="a.py", line=20, body="fix bug", user="alice"),
        ReviewComment(id=3, path="b.py", line=5,  body="test gap", user="bob"),
    ]
    tk = _toolkit(
        reviews=[[], [ReviewSummary(id=9, state="APPROVED", user="alice")]],
        comments=[comments, []],
        push_shas=["newsha111111111"],
    )
    provider = FakeProvider(scripts={
        "comment_synthesizer": [
            {"actionable_items": [
                {"file": "a.py", "line_range": [10, 25],
                 "change_summary": "rename + fix",
                 "original_comment_ids": [1, 2]},
                {"file": "b.py", "line_range": [5, 7],
                 "change_summary": "add test",
                 "original_comment_ids": [3]},
            ], "non_actionable": []},
        ],
        "comment_addresser": [
            {"file_changes": [
                {"path": "a.py", "operation": "modify", "content": "# fixed\n"},
            ], "summary": "applied 2 items",
             "items_addressed": [1, 2, 3]},
        ],
    })
    engine = _engine(log_dir, toolkit=tk, provider=provider, max_iterations=1)
    result = await engine.run("loop")
    assert isinstance(result, Completed), result
    assert result.final_node == "end_merged"
    completed = _completed_map(log_dir / "loop.events.jsonl")
    res = build_result(completed)
    assert res.final_state == "MERGED"
    assert res.merged is True
    assert res.iterations == 1
    assert res.comments_addressed == 2
    assert tk.push_count == 1
    assert tk.merge_count == 1


async def test_empty_addressal_retries_once(log_dir: Path):
    comments = [
        ReviewComment(id=1, path="a.py", line=10, body="rename", user="alice"),
    ]
    tk = _toolkit(
        reviews=[[], [ReviewSummary(id=9, state="APPROVED", user="alice")]],
        comments=[comments, []],
        push_shas=["newsha111111111"],
    )
    provider = FakeProvider(scripts={
        "comment_synthesizer": [{
            "actionable_items": [{
                "file": "a.py",
                "line_range": [10, 10],
                "change_summary": "rename",
                "original_comment_ids": [1],
            }],
            "non_actionable": [],
        }],
        "comment_addresser": [
            {"file_changes": [], "summary": "placeholder", "items_addressed": []},
            {
                "file_changes": [{
                    "path": "a.py",
                    "operation": "modify",
                    "content": "# fixed\n",
                }],
                "summary": "renamed",
                "items_addressed": [1],
            },
        ],
    })
    engine = _engine(log_dir, toolkit=tk, provider=provider, max_iterations=1)

    result = await engine.run("empty_addressal_recovers")

    assert isinstance(result, Completed), result
    assert result.final_node == "end_merged"
    assert [
        call["agent"] for call in provider.calls
    ].count("comment_addresser") == 2
    retry_prompt = [
        call["user_message"]
        for call in provider.calls
        if call["agent"] == "comment_addresser"
    ][1]
    assert "zero file_changes" in retry_prompt


async def test_repeated_empty_addressal_fails_closed(log_dir: Path):
    comments = [
        ReviewComment(id=1, path="a.py", line=10, body="rename", user="alice"),
    ]
    tk = _toolkit(reviews=[[]], comments=[comments])
    empty = {"file_changes": [], "summary": "placeholder", "items_addressed": []}
    provider = FakeProvider(scripts={
        "comment_synthesizer": [{
            "actionable_items": [{
                "file": "a.py",
                "line_range": [10, 10],
                "change_summary": "rename",
                "original_comment_ids": [1],
            }],
            "non_actionable": [],
        }],
        "comment_addresser": [empty, empty],
    })
    engine = _engine(log_dir, toolkit=tk, provider=provider, max_iterations=1)

    result = await engine.run("empty_addressal_repeated")

    assert isinstance(result, Completed), result
    assert result.final_node == "needs_human_end"
    completed = _completed_map(log_dir / "empty_addressal_repeated.events.jsonl")
    assert completed["validate_addressal_retry"]["error_kind"] == (
        "addressal.no_changes"
    )
    assert tk.push_count == 0


async def test_stale_addressal_replace_retries_once(log_dir: Path):
    comments = [
        ReviewComment(id=1, path="a.py", line=10, body="rename", user="alice"),
    ]
    tk = _toolkit(
        reviews=[[], [ReviewSummary(id=9, state="APPROVED", user="alice")]],
        comments=[comments, []],
        push_shas=["newsha111111111"],
    )
    provider = FakeProvider(scripts={
        "comment_synthesizer": [{
            "actionable_items": [{
                "file": "a.py",
                "line_range": [10, 10],
                "change_summary": "rename",
                "original_comment_ids": [1],
            }],
            "non_actionable": [],
        }],
        "comment_addresser": [
            {
                "file_changes": [{
                    "path": "missing.py",
                    "operation": "replace",
                    "old_content": "stale",
                    "content": "fixed",
                }],
                "summary": "renamed",
                "items_addressed": [1],
            },
            {
                "file_changes": [{
                    "path": "a.py",
                    "operation": "modify",
                    "content": "# fixed\n",
                }],
                "summary": "renamed",
                "items_addressed": [1],
            },
        ],
    })
    engine = _engine(log_dir, toolkit=tk, provider=provider, max_iterations=1)

    result = await engine.run("stale_addressal_recovers")

    assert isinstance(result, Completed), result
    assert result.final_node == "end_merged"
    assert [
        call["agent"] for call in provider.calls
    ].count("comment_addresser") == 2
    retry_prompt = [
        call["user_message"]
        for call in provider.calls
        if call["agent"] == "comment_addresser"
    ][1]
    assert "writing missing.py" in retry_prompt or "old_content match" in retry_prompt


# ---- case 4: agent returns BadOutput → NeedsHuman -------------------


async def test_synthesizer_bad_output_routes_to_needs_human(log_dir: Path):
    tk = _toolkit(
        reviews=[[]],
        comments=[[ReviewComment(id=1, path="x.py", line=1, body="?", user="a")]],
    )
    # Schema mismatch: ``actionable_items`` is required.
    provider = FakeProvider(scripts={
        "comment_synthesizer": [{"NOT_a_real_field": True}],
    })
    engine = _engine(log_dir, toolkit=tk, provider=provider)
    result = await engine.run("badout")
    assert isinstance(result, Completed), result
    assert result.final_node == "needs_human_end"; assert result.disposition == "failed"
    completed = _completed_map(log_dir / "badout.events.jsonl")
    assert completed["synthesize_comments"]["kind"] == "bad_output"
    res = build_result(completed)
    assert res.final_state == "OPEN_NEEDS_HUMAN"
    assert tk.merge_count == 0


# ---- case 5: address-loop hits max_iterations -----------------------


async def test_address_loop_max_iterations_yields_needs_human(log_dir: Path):
    comments = [ReviewComment(id=1, path="a.py", line=1, body="fix", user="alice")]
    # Always serve comments → never approvals → loop runs forever (but capped).
    tk = _toolkit(
        reviews=[[]],            # one snapshot, repeated
        comments=[comments],
        push_shas=["sha-aaa", "sha-bbb", "sha-ccc", "sha-ddd"],
    )
    syn = {"actionable_items": [
        {"file": "a.py", "line_range": [1, 2], "change_summary": "fix",
         "original_comment_ids": [1]},
    ], "non_actionable": []}
    addr = lambda sha: {"file_changes": [
        {"path": "a.py", "operation": "modify", "content": f"# {sha}\n"},
    ], "summary": "", "items_addressed": [1]}
    provider = FakeProvider(scripts={
        "comment_synthesizer": [syn] * 4,
        "comment_addresser":  [addr("sha-aaa"), addr("sha-bbb"),
                               addr("sha-ccc"), addr("sha-ddd")],
    })
    engine = _engine(log_dir, toolkit=tk, provider=provider, max_iterations=2)
    result = await engine.run("max_iter")
    assert isinstance(result, Completed), result
    assert result.final_node == "needs_human_end"; assert result.disposition == "failed"
    completed = _completed_map(log_dir / "max_iter.events.jsonl")
    cp = completed["dispatch_poll"]
    assert cp["kind"] == "permanent_failure"
    assert cp["error_kind"] == "needs_human.max_iterations"
    assert tk.push_count == 2
    assert tk.merge_count == 0


# ---- case 6: no-progress detection (same SHA across iterations) ----


async def test_no_progress_detection_yields_needs_human(log_dir: Path):
    comments = [ReviewComment(id=1, path="a.py", line=1, body="fix", user="a")]
    tk = _toolkit(
        reviews=[[]],
        comments=[comments],
        push_shas=["stuck-sha", "stuck-sha"],  # second push produces SAME sha
    )
    syn = {"actionable_items": [
        {"file": "a.py", "line_range": [1, 2], "change_summary": "fix",
         "original_comment_ids": [1]},
    ], "non_actionable": []}
    addr = lambda sha: {"file_changes": [
        {"path": "a.py", "operation": "modify", "content": f"# {sha}\n"},
    ], "summary": "", "items_addressed": [1]}
    provider = FakeProvider(scripts={
        "comment_synthesizer": [syn, syn],
        "comment_addresser":  [addr("stuck-sha"), addr("stuck-sha")],
    })
    engine = _engine(log_dir, toolkit=tk, provider=provider, max_iterations=5)
    result = await engine.run("noprog")
    assert isinstance(result, Completed), result
    completed = _completed_map(log_dir / "noprog.events.jsonl")
    cp = completed["check_progress"]
    assert cp["kind"] == "permanent_failure"
    assert cp["error_kind"] == "needs_human.no_progress"


# ---- case 7: conflicts on merge → NeedsHuman ------------------------


async def test_conflicts_routes_to_needs_human(log_dir: Path):
    tk = _toolkit(
        mergeable=[MergeabilityReport(
            mergeable=False, mergeable_state="dirty",
            checks_state=None, conflicts=True,
        )],
    )
    engine = _engine(log_dir, toolkit=tk)
    result = await engine.run("conflicts")
    assert isinstance(result, Completed), result
    completed = _completed_map(log_dir / "conflicts.events.jsonl")
    ccm = completed["check_can_merge"]
    assert ccm["kind"] == "permanent_failure"
    assert ccm["error_kind"] == "needs_human.conflicts"
    assert tk.merge_count == 0


# ---- case 8: failing checks → NeedsHuman ----------------------------


async def test_failing_checks_routes_to_needs_human(log_dir: Path):
    tk = _toolkit(
        mergeable=[MergeabilityReport(
            mergeable=True, mergeable_state="blocked",
            checks_state="failure", conflicts=False,
        )],
    )
    engine = _engine(log_dir, toolkit=tk)
    result = await engine.run("checks")
    assert isinstance(result, Completed), result
    completed = _completed_map(log_dir / "checks.events.jsonl")
    ccm = completed["check_can_merge"]
    assert ccm["kind"] == "permanent_failure"
    assert ccm["error_kind"] == "needs_human.checks_failing"
    assert tk.merge_count == 0


# ---- case 9: closed-not-merged PR → NeedsHuman ---------------------


async def test_closed_not_merged_pr_routes_to_needs_human(log_dir: Path):
    tk = _toolkit(pr=_pr(state="CLOSED", merged=False))
    engine = _engine(log_dir, toolkit=tk)
    result = await engine.run("closed")
    assert isinstance(result, Completed), result
    completed = _completed_map(log_dir / "closed.events.jsonl")
    ci = completed["check_initial_state"]
    assert ci["kind"] == "permanent_failure"
    assert ci["error_kind"] == "needs_human.closed_not_merged"
    res = build_result(completed)
    assert res.final_state == "OPEN_NEEDS_HUMAN"
    assert tk.merge_count == 0


# ---- case 10: INV-RESTART — kill mid-polling, resume completes -----


async def test_inv_restart_resume_mid_polling_completes(log_dir: Path):
    """Per INV-RESTART: kill the run mid-poll, then resume with the same
    run_id. The re-run reads the PR fresh and proceeds to completion.

    We exercise restart by truncating the log just after ``poll_review``
    completes in round 1 (comments seen) — the resume then re-runs the
    agents from script index 0 in a FRESH engine, which is exactly the
    behaviour an operator would see after killing & restarting the CLI.
    """
    comments = [ReviewComment(id=1, path="a.py", line=1, body="fix", user="a")]

    # Run 1: drive through one full loop iteration + a merge in round 2.
    tk1 = _toolkit(
        reviews=[[], [ReviewSummary(id=9, state="APPROVED", user="alice")]],
        comments=[comments, []],
        push_shas=["push-1"],
    )
    syn = {"actionable_items": [
        {"file": "a.py", "line_range": [1, 2], "change_summary": "fix",
         "original_comment_ids": [1]},
    ], "non_actionable": []}
    addr = {"file_changes": [
        {"path": "a.py", "operation": "modify", "content": "# push-1\n"},
    ], "summary": "", "items_addressed": [1]}
    provider1 = FakeProvider(scripts={
        "comment_synthesizer": [syn],
        "comment_addresser":  [addr],
    })
    engine1 = _engine(log_dir, toolkit=tk1, provider=provider1)
    run_id = "restart"
    result1 = await engine1.run(run_id)
    assert isinstance(result1, Completed), result1
    assert result1.final_node == "end_merged"

    # Truncate the log to just after `push_addressal` completes.
    log_path = log_dir / f"{run_id}.events.jsonl"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    keep: list[str] = []
    for raw in lines:
        keep.append(raw)
        ev = json.loads(raw)
        if ev["kind"] == "verb_completed" and ev.get("node_id") == "push_addressal":
            break
    log_path.write_text("\n".join(keep) + "\n", encoding="utf-8")

    # Resume with a fresh engine: same toolkit shape, BUT the toolkit
    # state has reset (review_idx=0) — so on resume the kernel re-enters
    # check_progress (next node) and then poll_review (next snapshot:
    # approvals — we point review_snapshots[0] to approvals so resume sees
    # the right state immediately because the FakePrToolkit cursor is
    # fresh).
    #
    # This is the operator-visible behaviour: the gh boundary returns
    # whatever GitHub currently says, not what it said before the crash.
    tk2 = _toolkit(
        reviews=[[ReviewSummary(id=9, state="APPROVED", user="alice")]],
        comments=[[]],
        push_shas=["push-1"],
    )
    # New provider — the agents already ran in run 1, the resume should
    # NOT call them again. If it does, the empty scripts will surface
    # `fake.unscripted` and the assertion below will fire.
    provider2 = FakeProvider(scripts={})
    engine2 = _engine(log_dir, toolkit=tk2, provider=provider2)
    result2 = await engine2.run(run_id)
    assert isinstance(result2, Completed), result2
    assert result2.final_node == "end_merged"

    # The post-resume run reached the merge — proving no replay drift.
    completed = _completed_map(log_path)
    res = build_result(completed)
    assert res.final_state == "MERGED"
    assert res.merged is True

    # Agents must NOT have been re-invoked.
    agents_called = [c["agent"] for c in provider2.calls]
    assert agents_called == [], agents_called


# ---- case 11: INV-CANCEL — cancel mid-loop, no further mutations ---


async def test_inv_cancel_short_circuits_mid_loop(log_dir: Path):
    """Per INV-CANCEL-SHORT-CIRCUITS-RETRY: writing a ``cancel_requested``
    event into the log while a run is paused causes the next resume to
    terminate with ``cancelled``, without further state-mutating calls.
    """
    comments = [ReviewComment(id=1, path="a.py", line=1, body="fix", user="a")]
    tk = _toolkit(
        reviews=[[]],
        comments=[comments],
        push_shas=["sha-aaa"],
    )
    syn = {"actionable_items": [
        {"file": "a.py", "line_range": [1, 2], "change_summary": "fix",
         "original_comment_ids": [1]},
    ], "non_actionable": []}
    addr = {"file_changes": [
        {"path": "a.py", "operation": "modify", "content": "# sha-aaa\n"},
    ], "summary": "", "items_addressed": [1]}
    provider = FakeProvider(scripts={
        "comment_synthesizer": [syn] * 2,
        "comment_addresser":  [addr, addr],
    })
    # Run with a very low max_iterations so we don't accidentally let the
    # loop finish before we cancel.
    engine = _engine(log_dir, toolkit=tk, provider=provider, max_iterations=20)
    run_id = "cancel"

    # Drive ONE iteration partially: we run, let it complete one loop,
    # then inject cancel_requested before any further mutation.
    # Simplest: run once (loop completes because tk returns approvals)
    # — but here tk only ever returns comments, so the loop would run.
    # Easier approach: run with a deadline-style toolkit that has only
    # ONE push scripted; when the loop comes back the second time, the
    # push will exhaust... but we want to cancel BEFORE that. Cancel
    # mechanism via in-log event: write the event, then resume.
    #
    # Pattern: do one iteration of the loop (provider/toolkit support
    # exactly that), kill, write cancel_requested, then resume.
    #
    # We simulate "kill" by truncating after push_addressal in iteration
    # 1, then we INJECT cancel_requested and resume.

    # Step 1: do a full run (which will exhaust toolkit/provider as the
    # loop tries to do iteration 2). We accept whatever terminal it
    # reaches — what we care about is having a long-enough log to
    # truncate. Use a single-push toolkit + provider so the run gets at
    # least past push_addressal, then fails on the second iteration.
    result_first = await engine.run(run_id)
    # Either Completed (no_progress detected) or Failed — both fine.

    log_path = log_dir / f"{run_id}.events.jsonl"
    lines = log_path.read_text(encoding="utf-8").splitlines()

    # Truncate to just AFTER first push_addressal verb_completed.
    keep: list[str] = []
    for raw in lines:
        keep.append(raw)
        ev = json.loads(raw)
        if ev["kind"] == "verb_completed" and ev.get("node_id") == "push_addressal":
            break

    # Inject a cancel_requested event into the log.
    last_id = max(json.loads(l)["event_id"] for l in keep)
    cancel_ev = {
        "event_id": last_id + 1,
        "run_id": run_id,
        "ts": "2026-05-31T00:00:00+00:00",
        "kind": "cancel_requested",
        "schema_version": 1,
        "node_id": None,
        "team_id": None,
        "agent_id": None,
        "payload": {"reason": "operator", "requested_by": "test"},
    }
    keep.append(json.dumps(cancel_ev))
    log_path.write_text("\n".join(keep) + "\n", encoding="utf-8")

    # Step 2: resume. The kernel honours INV-CANCEL: terminate immediately.
    tk2 = _toolkit(
        reviews=[[]], comments=[comments], push_shas=["should-not-be-used"],
    )
    provider2 = FakeProvider(scripts={
        "comment_synthesizer": [syn], "comment_addresser": [addr],
    })
    engine2 = _engine(log_dir, toolkit=tk2, provider=provider2, max_iterations=20)
    result = await engine2.run(run_id)
    assert isinstance(result, Failed), result
    assert result.error_kind == "cancelled"

    # No further toolkit mutations after cancel.
    assert tk2.push_count == 0
    assert tk2.merge_count == 0
    assert tk2.review_request_count == 0
    # No agent re-invocations either.
    assert provider2.calls == []


# ---- case 12: gh auth error → NeedsHuman ---------------------------


async def test_gh_auth_error_routes_to_needs_human(log_dir: Path):
    """Ravel L-1: GhAuthError → needs_human, never silent retry."""
    tk = _toolkit()
    tk.raise_on_pr_view = GhAuthError(
        "gh: authentication failed", exit_code=1, stderr="HTTP 401", argv=()
    )
    engine = _engine(log_dir, toolkit=tk)
    result = await engine.run("auth")
    assert isinstance(result, Completed), result
    assert result.final_node == "needs_human_end"; assert result.disposition == "failed"
    completed = _completed_map(log_dir / "auth.events.jsonl")
    fp = completed["fetch_pr"]
    assert fp["kind"] == "permanent_failure"
    assert fp["error_kind"] == "needs_human.auth"


# ---- case 13: gh rate-limited → RetryableFailure (within budget) ---


async def test_gh_rate_limited_is_retryable(log_dir: Path):
    """Rate-limit maps to RetryableFailure — the only retry-eligible
    gh-error per the client's exit-code table."""
    from datetime import timedelta
    tk = _toolkit()

    # Raise on first call, succeed on retry: monkey-patch the bound method.
    call_n = {"i": 0}
    real_pr_view = tk.pr_view

    async def flaky_pr_view(repo, number):
        call_n["i"] += 1
        if call_n["i"] == 1:
            raise GhRateLimitedError(
                "rate limited", retry_after=timedelta(seconds=0),
                exit_code=1, stderr="rate limit", argv=(),
            )
        return await real_pr_view(repo, number)

    tk.pr_view = flaky_pr_view  # type: ignore[method-assign]

    engine = _engine(log_dir, toolkit=tk)
    result = await engine.run("ratelimit")
    assert isinstance(result, Completed), result
    assert result.final_node == "end_merged"
    completed = _completed_map(log_dir / "ratelimit.events.jsonl")
    assert completed["fetch_pr"]["kind"] == "success"
    assert call_n["i"] == 2  # one rate-limit + one success


# ---- case 14: dry_run mode never mutates ---------------------------


async def test_dry_run_skips_mutations(log_dir: Path):
    comments = [ReviewComment(id=1, path="a.py", line=1, body="fix", user="a")]
    tk = _toolkit(
        reviews=[[], [ReviewSummary(id=9, state="APPROVED", user="a")]],
        comments=[comments, []],
        push_shas=["should-not-push"],
    )
    syn = {"actionable_items": [
        {"file": "a.py", "line_range": [1, 2], "change_summary": "fix",
         "original_comment_ids": [1]},
    ], "non_actionable": []}
    addr = {"file_changes": [
        {"path": "a.py", "operation": "modify", "content": "# fakesha\n"},
    ], "summary": "",
            "items_addressed": [1]}
    provider = FakeProvider(scripts={
        "comment_synthesizer": [syn],
        "comment_addresser":  [addr],
    })
    engine = _engine(log_dir, toolkit=tk, provider=provider, dry_run=True)
    result = await engine.run("dry")
    assert isinstance(result, Completed), result
    assert result.final_node == "end_merged"
    assert tk.push_count == 0       # dry_run skipped the push
    assert tk.merge_count == 0       # dry_run skipped the merge
    assert tk.review_request_count == 0
    completed = _completed_map(log_dir / "dry.events.jsonl")
    res = build_result(completed)
    assert res.dry_run is True
    assert res.merged is False
