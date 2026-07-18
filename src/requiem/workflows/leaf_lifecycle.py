"""leaf_lifecycle workflow — reviewer-and-rework self-merge for leaf PRs.

This workflow owns the *implementation-leaf* merge leg only:
``impl/<root>-<item>`` → ``feature/<root>``. It never touches the final
``feature/<root>`` → default-branch PR; ADR-0018's no-self-merge invariant stays
scoped to that final, human-gated feature PR.

Topology (mirrors ``pr_lifecycle``'s builder style, with one small pragmatic
addition: ``prepare_review`` materialises a stable local diff/checkout for the
reviewer and comment-addresser agents)::

    start
      → fetch_pr
      → check_initial_state
          ├─ already merged      → end_already_merged
          ├─ closed not merged   → needs_human_end
          └─ open                → assert_leaf_scope
      → check_tests_passed
      → prepare_review
      → review_leaf
          └─ token exhaustion    → compacted tool-free review → dispatch_review
      → dispatch_review
          ├─ approve             → prune_context_pack → check_can_merge
          │                                           ├─ ready → merge_pr
          │                                           │           ├─ confirmed → end_merged
          │                                           │           └─ unconfirmed → verify_merge_confirmation
          │                                           └─ indeterminate → verify_mergeability
          ├─ request_changes     → synthesize_comments → address_comments
          │                        → apply_addressal → run_addressal_tests
          │                        → push_addressal
          │                        → check_progress → prepare_review
          ├─ inconsistent review → reconcile_review_inconsistency
          │                        ├─ deterministic → verify_review_reconciliation
          │                        └─ ambiguous → deliberate_review_inconsistency
          │                                      → verify_review_reconciliation
          └─ needs_human         → needs_human_end

Fail-closed rules:

* The leaf PR's own verified CI/build status must be explicitly green before
  review. Self-reported handoff metadata is not used as a pass/fail signal.
* The live PR head/base must stay exactly ``impl/<root>-<item>`` →
  ``feature/<root>``; any drift or protected-base target escalates.
* Conflicts, failing checks, unsatisfied policies, repeated identical findings,
  and no-progress loops surrender to a human. Pending or freshly published test
  evidence and indeterminate mergeability receive bounded authoritative rereads;
  unresolved states still surrender rather than merge optimistically.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Protocol

from requiem import branch_model
from requiem.agent import AgentSpec, LEAF_REVIEWER, FakeProvider
from requiem.clients.azuredevops import (
    AdoAuthError,
    AdoClientError,
    AdoNotFoundError,
    AdoRateLimitedError,
    AdoServerError,
    AdoUnknownError,
)
from requiem.clients.fs import FsGitError
from requiem.context_pack import CONTEXT_PACK_DIR
from requiem.clients.gh import (
    GhAuthError,
    GhClientError,
    GhNotFoundError,
    GhRateLimitedError,
    GhServerError,
    GhUnknownError,
)
from requiem.clients.repo import (
    MergeCapableRepoPlatform,
    REQUIRED_TEST_STATUS_CONTEXT,
    RepoCompleteResult,
    RepoMergeStrategy,
    RepoMergeabilityReport,
    RepoPullRequest,
)
from requiem.coder_output import apply_file_changes
from requiem.dsl import AgentRegistry, VerbRegistry, Workflow, WorkflowBuilder
from requiem.kernel import Engine
from requiem.outcomes import (
    NeedsHuman,
    Outcome,
    PermanentFailure,
    RetryableFailure,
    Success,
)
from requiem.review_schemas import LeafReviewReport
from requiem.token_budget import (
    is_cumulative_input_exhaustion,
    token_failure_evidence,
)
from requiem.toolbelt import Toolbelt
from requiem.workflows.implementation import (
    DetectedTestCommand,
    _default_test_runner as default_test_runner,
    detect_test_command,
)
from requiem.workflows.pr_lifecycle import (
    COMMENT_ADDRESSER,
    COMMENT_SYNTHESIZER,
    CommentSynthesis,
)

MODULE = "requiem.workflows.leaf_lifecycle"
LeafLifecycleState = Literal["merged", "already_merged", "needs_human", "failed"]
MERGE_CONFIRMATION_RETRY_MAX = 30
MERGE_CONFIRMATION_RETRY_DELAY_S = 1.0
MERGEABILITY_RETRY_MAX = 2
MERGEABILITY_RETRY_DELAY_S = 1.0
TEST_STATUS_RETRY_MAX = 2
TEST_STATUS_RETRY_DELAY_S = 1.0
MAX_REVIEW_DIFF_CHARS = 200_000
MAX_COMPACTED_REVIEW_DIFF_CHARS = 20_000
MAX_REVIEW_CONTRACT_CHARS = 12_000

COMPACTED_LEAF_REVIEWER = AgentSpec(
    name="compacted_leaf_reviewer",
    charter=(
        "You are Requiem's bounded recovery reviewer. A prior reviewer session "
        "exhausted its input-token budget. Review only the complete merge-bound "
        "diff supplied in the prompt; repository inspection tools are disabled. "
        "Return `approve`, `request_changes`, or `needs_human` using the same "
        "safety standard as the normal leaf reviewer."
    ),
    response_model=LeafReviewReport,
    role="reviewer",
    model_options={"disable_repo_tools": True},
)


@dataclass(frozen=True, slots=True)
class LeafLifecycleInputs:
    repo: str
    repo_path: Path
    leaf_id: str
    root_item_id: int
    pr_number: int
    default_branch: str
    max_iterations: int = 3
    merge_strategy: RepoMergeStrategy = "squash"
    test_command: str | None = None
    dry_run: bool = False

    def __post_init__(self) -> None:
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be >= 1")
        if self.expected_base in {"main", "master"}:
            raise ValueError(
                "leaf_lifecycle refuses to operate on a protected default-base branch"
            )
        if self.expected_base == self.default_branch:
            raise ValueError(
                "leaf_lifecycle expected_base must not equal the repo default branch"
            )

    @property
    def expected_head(self) -> str:
        return branch_model.impl_branch(self.root_item_id, self.leaf_id)

    @property
    def expected_base(self) -> str:
        return branch_model.feature_trunk(self.root_item_id)


@dataclass(frozen=True, slots=True)
class LeafLifecycleResult:
    final_state: LeafLifecycleState
    pr_number: int
    merge_sha: str | None
    iterations: int
    comments_addressed: int


class LeafLifecycleToolkit(Protocol):
    async def pr_view(self, repo: str, number: int) -> RepoPullRequest: ...
    async def branch_sha(self, repo: str, branch: str) -> str: ...
    async def post_commit_status(
        self,
        repo: str,
        sha: str,
        *,
        context: str,
        state: Literal["success", "failure", "pending"],
        description: str = "",
    ) -> None: ...
    async def pr_mergeability(
        self, repo: str, number: int
    ) -> RepoMergeabilityReport: ...
    async def pr_complete(
        self,
        repo: str,
        number: int,
        *,
        strategy: RepoMergeStrategy,
        expected_head: str | None = None,
        expected_base: str | None = None,
        expected_head_sha: str | None = None,
    ) -> RepoCompleteResult: ...
    async def git_push(self, repo_path: Path, branch: str) -> str: ...
    def review_diff(self, repo_path: Path, *, base: str, head: str) -> str: ...


class RealLeafLifecycleToolkit:
    def __init__(self, repo_client: MergeCapableRepoPlatform) -> None:
        self._repo = repo_client

    async def pr_view(self, repo: str, number: int) -> RepoPullRequest:
        return await self._repo.pr_view(repo, number)

    async def branch_sha(self, repo: str, branch: str) -> str:
        return await self._repo.branch_sha(repo, branch)

    async def post_commit_status(
        self,
        repo: str,
        sha: str,
        *,
        context: str,
        state: Literal["success", "failure", "pending"],
        description: str = "",
    ) -> None:
        poster = getattr(self._repo, "post_commit_status", None)
        if poster is None:
            raise RuntimeError("repository platform cannot post commit statuses")
        await poster(
            repo,
            sha,
            context=context,
            state=state,
            description=description,
        )

    async def pr_mergeability(
        self, repo: str, number: int
    ) -> RepoMergeabilityReport:
        return await self._repo.pr_mergeability(repo, number)

    async def pr_complete(
        self,
        repo: str,
        number: int,
        *,
        strategy: RepoMergeStrategy,
        expected_head: str | None = None,
        expected_base: str | None = None,
        expected_head_sha: str | None = None,
    ) -> RepoCompleteResult:
        return await self._repo.pr_complete(
            repo,
            number,
            strategy=strategy,
            expected_head=expected_head,
            expected_base=expected_base,
            expected_head_sha=expected_head_sha,
        )

    async def git_push(self, repo_path: Path, branch: str) -> str:
        proc = subprocess.run(
            ["git", "push", "origin", branch],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"git push failed ({proc.returncode}): {proc.stderr[:512]}"
            )
        sha_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if sha_proc.returncode != 0:
            raise RuntimeError(
                f"git rev-parse failed ({sha_proc.returncode}): {sha_proc.stderr[:512]}"
            )
        return sha_proc.stdout.strip()

    def review_diff(self, repo_path: Path, *, base: str, head: str) -> str:
        self._run_git(repo_path, "fetch", "--prune", "origin", base, head)
        self._run_git(repo_path, "checkout", "-B", head, f"origin/{head}")
        diff = self._run_git(
            repo_path,
            "diff",
            "--unified=3",
            f"origin/{base}...HEAD",
        )
        return diff or "(no diff)"

    @staticmethod
    def _run_git(repo_path: Path, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr[:512]}"
            )
        return proc.stdout


@dataclass
class FakeLeafLifecycleToolkit:
    pr_snapshots: list[RepoPullRequest]
    branch_sha_snapshots: list[str]
    mergeability_snapshots: list[RepoMergeabilityReport]
    review_diff_text: str = "diff --git a/app.py b/app.py\n+ok\n"
    complete_result: RepoCompleteResult | None = None
    push_shas: list[str] = field(default_factory=list)
    raise_on_pr_view: Exception | None = None
    raise_on_mergeability: Exception | None = None
    raise_on_complete: Exception | None = None
    raise_on_push: Exception | None = None
    raise_on_post_status: Exception | None = None
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    complete_calls: list[dict[str, Any]] = field(default_factory=list)
    posted_statuses: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._pr_idx = 0
        self._sha_idx = 0
        self._merge_idx = 0
        self._push_idx = 0
        self._last_head_sha: str | None = None

    async def pr_view(self, repo: str, number: int) -> RepoPullRequest:
        self.calls.append(("pr_view", (repo, number)))
        if self.raise_on_pr_view is not None:
            raise self.raise_on_pr_view
        pr = self.pr_snapshots[min(self._pr_idx, len(self.pr_snapshots) - 1)]
        self._pr_idx += 1
        return pr

    async def branch_sha(self, repo: str, branch: str) -> str:
        self.calls.append(("branch_sha", (repo, branch)))
        sha = self.branch_sha_snapshots[min(self._sha_idx, len(self.branch_sha_snapshots) - 1)]
        self._sha_idx += 1
        self._last_head_sha = sha
        return sha

    async def post_commit_status(
        self,
        repo: str,
        sha: str,
        *,
        context: str,
        state: Literal["success", "failure", "pending"],
        description: str = "",
    ) -> None:
        self.calls.append(
            ("post_commit_status", (repo, sha, context, state, description))
        )
        if self.raise_on_post_status is not None:
            raise self.raise_on_post_status
        self.posted_statuses.append({
            "repo": repo,
            "sha": sha,
            "context": context,
            "state": state,
            "description": description,
        })

    async def pr_mergeability(
        self, repo: str, number: int
    ) -> RepoMergeabilityReport:
        self.calls.append(("pr_mergeability", (repo, number)))
        if self.raise_on_mergeability is not None:
            raise self.raise_on_mergeability
        report = self.mergeability_snapshots[
            min(self._merge_idx, len(self.mergeability_snapshots) - 1)
        ]
        self._merge_idx += 1
        if report.head_sha is None and self._last_head_sha is not None:
            return replace(report, head_sha=self._last_head_sha)
        return report

    async def pr_complete(
        self,
        repo: str,
        number: int,
        *,
        strategy: RepoMergeStrategy,
        expected_head: str | None = None,
        expected_base: str | None = None,
        expected_head_sha: str | None = None,
    ) -> RepoCompleteResult:
        self.calls.append(
            (
                "pr_complete",
                (
                    repo,
                    number,
                    strategy,
                    expected_head,
                    expected_base,
                    expected_head_sha,
                ),
            )
        )
        if self.raise_on_complete is not None:
            raise self.raise_on_complete
        live = await self.pr_view(repo, number)
        if expected_head is not None and live.head != expected_head:
            raise RuntimeError("head precondition failed")
        if expected_base is not None and live.base != expected_base:
            raise RuntimeError("base precondition failed")
        self.complete_calls.append({
            "repo": repo,
            "number": number,
            "strategy": strategy,
            "expected_head": expected_head,
            "expected_base": expected_base,
            "expected_head_sha": expected_head_sha,
        })
        if self.complete_result is None:
            raise RuntimeError("FakeLeafLifecycleToolkit: no complete_result scripted")
        return self.complete_result

    async def git_push(self, repo_path: Path, branch: str) -> str:
        self.calls.append(("git_push", (str(repo_path), branch)))
        if self.raise_on_push is not None:
            raise self.raise_on_push
        if not self.push_shas:
            raise RuntimeError("FakeLeafLifecycleToolkit: no push_shas scripted")
        sha = self.push_shas[min(self._push_idx, len(self.push_shas) - 1)]
        self._push_idx += 1
        self._last_head_sha = sha
        return sha

    def review_diff(self, repo_path: Path, *, base: str, head: str) -> str:
        self.calls.append(("review_diff", (str(repo_path), base, head)))
        return self.review_diff_text


def _fingerprint_comments(comments: list[dict[str, Any]]) -> str:
    canon = tuple(
        sorted(
            (
                str(c.get("file", "")),
                int(c["line"]) if c.get("line") is not None else None,
                str(c.get("body", "")),
            )
            for c in comments
        )
    )
    return hashlib.sha256(repr(canon).encode("utf-8")).hexdigest()


def _without_context_pack_diff(diff: str) -> tuple[str, list[str]]:
    """Remove Requiem's internal context pack from merge-bound review evidence."""
    sections = re.split(r"(?m)(?=^diff --git )", diff)
    if len(sections) == 1:
        return diff, []

    kept: list[str] = []
    omitted: list[str] = []
    context_prefix = f"diff --git a/{CONTEXT_PACK_DIR}/"
    for section in sections:
        if not section.startswith("diff --git "):
            kept.append(section)
            continue
        header = section.partition("\n")[0]
        if header.startswith(context_prefix):
            omitted.append(header.partition(" b/")[2] or header)
            continue
        kept.append(section)

    return "".join(kept).strip(), omitted


def _review_contract_diff(diff: str) -> str:
    """Retain the leaf plan contract while excluding other internal context."""
    contract_paths = {
        f"{CONTEXT_PACK_DIR}/acceptance.md",
        f"{CONTEXT_PACK_DIR}/rationale.md",
    }
    sections = re.split(r"(?m)(?=^diff --git )", diff)
    contract: list[str] = []
    for section in sections:
        if not section.startswith("diff --git "):
            continue
        header = section.partition("\n")[0]
        path = header.partition(" b/")[2]
        if path in contract_paths:
            contract.append(section)
    return "".join(contract).strip()


def _merge_sha_from_pr(pr: RepoPullRequest) -> str | None:
    for raw_field, id_field in (
        ("lastMergeCommit", "commitId"),
        ("mergeCommit", "oid"),
    ):
        commit = pr.raw.get(raw_field)
        if isinstance(commit, dict) and commit.get(id_field):
            return str(commit[id_field])
    return None


def _merge_confirmation_evidence(pr: RepoPullRequest) -> dict[str, Any]:
    return {
        "pr_number": pr.number,
        "pr_state": pr.state,
        "merged": pr.merged,
        "merged_at": pr.merged_at.isoformat() if pr.merged_at else None,
        "head": pr.head,
        "base": pr.base,
        "merge_status": pr.raw.get("mergeStatus"),
        "completion_queue_time": pr.raw.get("completionQueueTime"),
        "closed_date": pr.raw.get("closedDate"),
        "merge_failure_type": pr.raw.get("mergeFailureType"),
        "merge_failure_message": pr.raw.get("mergeFailureMessage"),
        "merge_sha": _merge_sha_from_pr(pr),
        "url": pr.url,
    }


def _mergeability_evidence(report: RepoMergeabilityReport) -> dict[str, Any]:
    return {
        "mergeable": report.mergeable,
        "mergeable_state": report.mergeable_state,
        "checks_state": report.checks_state,
        "conflicts": report.conflicts,
        "policies_satisfied": report.policies_satisfied,
        "head_sha": report.head_sha,
    }


def _published_test_status_for_head(
    completed: dict[str, dict[str, Any]],
    head_sha: str,
) -> dict[str, Any] | None:
    for node_id in ("prune_context_pack", "push_addressal"):
        value = (completed.get(node_id) or {}).get("value") or {}
        if value.get("status_posted") is True and value.get("sha") == head_sha:
            return {
                "kind": "fresh_status_publication",
                "source_node": node_id,
                "sha": head_sha,
            }
    return None


def _assess_concrete_mergeability(report: RepoMergeabilityReport) -> Outcome | None:
    evidence = _mergeability_evidence(report)
    if report.conflicts:
        return PermanentFailure(
            error_kind="needs_human.conflicts",
            message=f"leaf PR has merge conflicts ({report.mergeable_state})",
            details={"report": evidence},
        )
    if report.checks_state == "failure":
        return PermanentFailure(
            error_kind="needs_human.tests_not_passed",
            message=f"leaf PR checks/build validation are failing ({report.mergeable_state})",
            details={"report": evidence},
        )
    if report.checks_state != "success":
        return PermanentFailure(
            error_kind="needs_human.tests_status_unknown",
            message=(
                "leaf PR checks/build validation are pending or unavailable "
                f"({report.checks_state})"
            ),
            details={"report": evidence},
        )
    if report.mergeable is None:
        return None
    if not report.policies_satisfied:
        return PermanentFailure(
            error_kind="needs_human.policies_unsatisfied",
            message="required merge policies are not satisfied",
            details={"report": evidence},
        )
    if not report.mergeable:
        return PermanentFailure(
            error_kind="needs_human.not_mergeable",
            message=f"leaf PR is not mergeable ({report.mergeable_state})",
            details={"report": evidence},
        )
    return Success(value=evidence)


def _map_platform_error(
    err: Exception,
    *,
    run_id: str,
    node_id: str,
    attempt: int,
    operation: str,
) -> Outcome:
    if isinstance(err, (GhRateLimitedError, AdoRateLimitedError)):
        return RetryableFailure(
            retry_key=f"{run_id}:{node_id}:{operation}",
            error_kind="repo.rate_limited",
            message=f"{operation}: rate-limited",
            attempt=attempt,
            after=30.0,
        )
    if isinstance(err, GhServerError):
        return RetryableFailure(
            retry_key=f"{run_id}:{node_id}:{operation}",
            error_kind="repo.server_error",
            message=f"{operation}: HTTP {err.status}",
            attempt=attempt,
            after=30.0,
        )
    if isinstance(err, AdoServerError):
        return RetryableFailure(
            retry_key=f"{run_id}:{node_id}:{operation}",
            error_kind="repo.server_error",
            message=f"{operation}: HTTP {err.status}",
            attempt=attempt,
            after=30.0,
        )
    if isinstance(err, (GhNotFoundError, AdoNotFoundError)):
        return PermanentFailure(
            error_kind="pr.not_found",
            message=f"{operation}: {err}",
        )
    if isinstance(err, (GhAuthError, AdoAuthError)):
        return PermanentFailure(
            error_kind="needs_human.auth",
            message=f"{operation}: {err}",
        )
    if isinstance(err, (GhUnknownError, GhClientError, AdoUnknownError, AdoClientError)):
        return PermanentFailure(
            error_kind="needs_human.repo_unknown",
            message=f"{operation}: {err}",
        )
    return PermanentFailure(
        error_kind=f"needs_human.{operation}_crash",
        message=f"{operation}: {type(err).__name__}: {err}",
    )


def build_verb_registry(
    inputs: LeafLifecycleInputs,
    toolkit: LeafLifecycleToolkit,
    *,
    test_runner=default_test_runner,
) -> VerbRegistry:
    verbs = VerbRegistry()
    protected_bases = {inputs.default_branch, "main", "master"}

    def _require_fs(ctx):
        fs = ctx.toolbelt.fs
        if fs is None:
            return PermanentFailure(
                error_kind="toolbelt.missing_client",
                message="leaf_lifecycle workflow requires toolbelt.fs",
            )
        return fs

    def _actionable_review(ctx) -> dict[str, Any]:
        dispatch = ctx.completed["dispatch_review"]
        if dispatch.get("kind") == "success":
            return dispatch["value"]
        return ctx.completed["verify_review_reconciliation"]["value"]

    async def _assess_test_status(ctx, report: RepoMergeabilityReport) -> Outcome | None:
        evidence = _mergeability_evidence(report)
        if report.checks_state == "success":
            return None
        if report.checks_state == "failure":
            return PermanentFailure(
                error_kind="needs_human.tests_not_passed",
                message="leaf PR checks/build validation are failing",
                details={"report": evidence},
            )
        head_sha = report.head_sha
        if head_sha is None:
            try:
                head_sha = await toolkit.branch_sha(inputs.repo, inputs.expected_head)
            except Exception as e:  # noqa: BLE001
                return _map_platform_error(
                    e,
                    run_id=ctx.run_id,
                    node_id=ctx.node_id,
                    attempt=ctx.attempt,
                    operation="check_tests_head",
                )
        publication = _published_test_status_for_head(ctx.completed, head_sha)
        if report.checks_state == "pending":
            recovery_basis: dict[str, Any] | None = {
                "kind": "authoritative_pending_status",
                "sha": head_sha,
            }
        else:
            recovery_basis = publication
        if recovery_basis is not None:
            return PermanentFailure(
                error_kind="tests_status.recheck_required",
                message=(
                    "required validation is still propagating for the current "
                    f"leaf head ({report.checks_state})"
                ),
                details={
                    "report": evidence,
                    "head_sha": head_sha,
                    "recovery_basis": recovery_basis,
                },
            )
        return PermanentFailure(
            error_kind="needs_human.tests_status_unknown",
            message=(
                "no required validation status is available for the current "
                "leaf head"
            ),
            details={
                "report": evidence,
                "head_sha": head_sha,
                "recovery_basis": None,
            },
        )

    @verbs.register("start_run")
    def _start(ctx):
        return Success(
            value={
                "intent": "leaf_lifecycle",
                "repo": inputs.repo,
                "pr_number": inputs.pr_number,
                "leaf_id": inputs.leaf_id,
                "expected_head": inputs.expected_head,
                "expected_base": inputs.expected_base,
                "dry_run": inputs.dry_run,
            }
        )

    @verbs.register("fetch_pr")
    async def _fetch_pr(ctx):
        try:
            pr = await toolkit.pr_view(inputs.repo, inputs.pr_number)
        except Exception as e:  # noqa: BLE001
            return _map_platform_error(
                e,
                run_id=ctx.run_id,
                node_id=ctx.node_id,
                attempt=ctx.attempt,
                operation="fetch_pr",
            )
        return Success(
            value={
                "number": pr.number,
                "title": pr.title,
                "url": pr.url,
                "state": pr.state,
                "merged": pr.merged,
                "head": pr.head,
                "base": pr.base,
            }
        )

    @verbs.register("check_initial_state")
    def _check_initial_state(ctx):
        pr = ctx.completed["fetch_pr"]["value"]
        state = str(pr.get("state", "")).lower()
        if pr.get("merged") or state == "merged":
            return PermanentFailure(
                error_kind="already_merged",
                message=f"leaf PR #{inputs.pr_number} is already merged",
            )
        if state == "closed":
            return PermanentFailure(
                error_kind="needs_human.closed_not_merged",
                message="leaf PR was closed without merge",
            )
        return Success(value={"state": "open"})

    @verbs.register("assert_leaf_scope")
    async def _assert_leaf_scope(ctx):
        pr = ctx.completed["fetch_pr"]["value"]
        actual_head = str(pr.get("head", ""))
        actual_base = str(pr.get("base", ""))
        if actual_head != inputs.expected_head:
            return PermanentFailure(
                error_kind="needs_human.scope_violation",
                message=(
                    f"leaf PR head {actual_head!r} != expected "
                    f"{inputs.expected_head!r}"
                ),
            )
        if actual_base != inputs.expected_base:
            return PermanentFailure(
                error_kind="needs_human.scope_violation",
                message=(
                    f"leaf PR base {actual_base!r} != expected "
                    f"{inputs.expected_base!r}"
                ),
            )
        if actual_base in protected_bases:
            return PermanentFailure(
                error_kind="needs_human.protected_base",
                message=f"leaf PR targets protected base {actual_base!r}",
            )
        try:
            head_sha = await toolkit.branch_sha(inputs.repo, actual_head)
        except Exception as e:  # noqa: BLE001
            return _map_platform_error(
                e,
                run_id=ctx.run_id,
                node_id=ctx.node_id,
                attempt=ctx.attempt,
                operation="assert_leaf_scope",
            )
        return Success(
            value={
                "head": actual_head,
                "base": actual_base,
                "head_sha": head_sha,
            }
        )

    @verbs.register("check_tests_passed")
    async def _check_tests_passed(ctx):
        try:
            report = await toolkit.pr_mergeability(inputs.repo, inputs.pr_number)
        except Exception as e:  # noqa: BLE001
            return _map_platform_error(
                e,
                run_id=ctx.run_id,
                node_id=ctx.node_id,
                attempt=ctx.attempt,
                operation="check_tests_passed",
            )
        assessment = await _assess_test_status(ctx, report)
        if assessment is not None:
            return assessment
        return Success(
            value={
                "checks_state": report.checks_state,
                "mergeable": report.mergeable,
                "mergeable_state": report.mergeable_state,
                "conflicts": report.conflicts,
                "policies_satisfied": report.policies_satisfied,
            },
        )

    @verbs.register("verify_tests_status")
    async def _verify_tests_status(ctx):
        before_merge = ctx.node_id == "verify_tests_status_before_merge"
        source_nodes = (
            ("check_can_merge", "verify_mergeability")
            if before_merge
            else ("check_tests_passed",)
        )
        trigger_source = next(
            (
                node_id
                for node_id in source_nodes
                if (ctx.completed.get(node_id) or {}).get("error_kind")
                == "tests_status.recheck_required"
            ),
            None,
        )
        if trigger_source is None:
            return PermanentFailure(
                error_kind="needs_human.tests_status_unknown",
                message="test-status verification has no recovery trigger",
            )
        trigger = ctx.completed[trigger_source]
        trigger_details = trigger.get("details") or {}
        expected_head_sha = str(trigger_details.get("head_sha") or "")
        recovery_basis = trigger_details.get("recovery_basis") or {}
        try:
            report = await toolkit.pr_mergeability(inputs.repo, inputs.pr_number)
            current_head_sha = await toolkit.branch_sha(
                inputs.repo,
                inputs.expected_head,
            )
        except Exception as e:  # noqa: BLE001
            return _map_platform_error(
                e,
                run_id=ctx.run_id,
                node_id=ctx.node_id,
                attempt=ctx.attempt,
                operation="verify_tests_status",
            )

        evidence = {
            **_mergeability_evidence(report),
            "head_sha": current_head_sha,
        }
        if report.head_sha is not None and report.head_sha != current_head_sha:
            return PermanentFailure(
                error_kind="needs_human.tests_status_unknown",
                message="leaf head changed during required-validation verification",
                details={
                    "expected_head_sha": expected_head_sha,
                    "status_head_sha": report.head_sha,
                    "current_head_sha": current_head_sha,
                    "report": evidence,
                },
            )
        if current_head_sha != expected_head_sha:
            return PermanentFailure(
                error_kind="needs_human.tests_status_unknown",
                message="leaf head changed while required validation was being verified",
                details={
                    "expected_head_sha": expected_head_sha,
                    "current_head_sha": current_head_sha,
                    "report": evidence,
                },
            )
        if report.checks_state == "success":
            return Success(
                value={
                    **evidence,
                    "verification_method": "authoritative_test_status_reread",
                    "verification_attempt": ctx.attempt,
                }
            )
        if report.checks_state == "failure":
            return PermanentFailure(
                error_kind="needs_human.tests_not_passed",
                message="leaf PR checks/build validation are failing",
                details={"report": evidence},
            )

        can_wait = recovery_basis.get("kind") in {
            "authoritative_pending_status",
            "fresh_status_publication",
        }
        if can_wait and ctx.attempt <= TEST_STATUS_RETRY_MAX:
            return RetryableFailure(
                retry_key=f"{ctx.run_id}:{ctx.node_id}:tests-status",
                error_kind="tests_status.propagating",
                message=(
                    "required validation has not appeared on the authoritative "
                    f"commit-status feed yet ({report.checks_state})"
                ),
                attempt=ctx.attempt,
                after=TEST_STATUS_RETRY_DELAY_S,
            )

        return NeedsHuman(
            gate="tests_status_unknown",
            prompt=(
                "Authoritative ADO reads still cannot establish required "
                "validation for the exact leaf head. Retry the read-only "
                "verification or abort this leaf."
            ),
            options=("retry_verification", "abort"),
            context={
                "trigger": {
                    "source_node": trigger_source,
                    "problem_kind": trigger.get("error_kind"),
                    "message": trigger.get("message"),
                },
                "evidence": evidence,
                "recovery_attempts": [
                    {
                        "kind": "authoritative_test_status_reread",
                        "attempts": ctx.attempt,
                        "result": f"checks_{report.checks_state}",
                    }
                ],
                "remaining_uncertainty": {
                    "head_sha": current_head_sha,
                    "checks_state": report.checks_state,
                },
                "recommended_option": "retry_verification",
                "rationale": (
                    "The workflow has evidence that required validation was "
                    "pending or freshly published for this exact SHA, but ADO "
                    "has not produced an authoritative terminal status."
                ),
            },
        )

    @verbs.register("prepare_review")
    async def _prepare_review(ctx):
        try:
            pr = await toolkit.pr_view(inputs.repo, inputs.pr_number)
        except Exception as e:  # noqa: BLE001
            return _map_platform_error(
                e,
                run_id=ctx.run_id,
                node_id=ctx.node_id,
                attempt=ctx.attempt,
                operation="prepare_review",
            )
        if pr.head != inputs.expected_head or pr.base != inputs.expected_base:
            return PermanentFailure(
                error_kind="needs_human.scope_violation",
                message=(
                    f"live leaf PR drifted to {pr.head!r} → {pr.base!r}; expected "
                    f"{inputs.expected_head!r} → {inputs.expected_base!r}"
                ),
            )
        if pr.base in protected_bases:
            return PermanentFailure(
                error_kind="needs_human.protected_base",
                message=f"leaf PR targets protected base {pr.base!r}",
            )
        try:
            head_sha = await toolkit.branch_sha(inputs.repo, pr.head)
            raw_diff = toolkit.review_diff(
                inputs.repo_path, base=pr.base, head=pr.head
            )
        except Exception as e:  # noqa: BLE001
            return _map_platform_error(
                e,
                run_id=ctx.run_id,
                node_id=ctx.node_id,
                attempt=ctx.attempt,
                operation="prepare_review",
            )
        review_diff, omitted_context_files = _without_context_pack_diff(raw_diff)
        review_contract = _review_contract_diff(raw_diff)
        if not review_diff:
            review_diff = "(no merge-bound diff after excluding Requiem context pack)"
        return Success(
            value={
                "head": pr.head,
                "base": pr.base,
                "head_sha": head_sha,
                "diff": review_diff[:MAX_REVIEW_DIFF_CHARS],
                "diff_complete": len(review_diff) <= MAX_REVIEW_DIFF_CHARS,
                "review_diff_chars": len(review_diff),
                "raw_diff_chars": len(raw_diff),
                "omitted_context_pack_files": omitted_context_files,
                "review_contract": review_contract[:MAX_REVIEW_CONTRACT_CHARS],
                "review_contract_complete": (
                    len(review_contract) <= MAX_REVIEW_CONTRACT_CHARS
                ),
            }
        )

    @verbs.register("review_prompt")
    def _review_prompt(ctx):
        prep = ctx.completed["prepare_review"]["value"]
        return (
            f"Review leaf PR #{inputs.pr_number} for repo {inputs.repo}.\n"
            f"Leaf: {inputs.leaf_id}\n"
            f"Head: {prep['head']} @ {prep['head_sha']}\n"
            f"Base: {prep['base']}\n\n"
            "Return `approve` only when the diff is safe to squash-merge into the "
            "feature trunk as-is **and fully satisfies the leaf plan contract**. "
            "Reject or escalate changes that omit required scope, contradict the "
            "plan, or acknowledge that required work was left undone. Return "
            "`request_changes` for concrete code fixes Requiem can implement "
            "automatically. Return `needs_human` for any ambiguous or unsafe case, "
            "including a missing or incomplete plan contract. Do not use "
            "`needs_human` merely to ask whether an explicit implementation "
            "rationale, ambient permission, or deployment convention matches "
            "undocumented external practice. Name a concrete contradiction or "
            "safety defect and return `request_changes`; otherwise judge the "
            "implementation shown.\n\n"
            "Leaf plan contract "
            f"(complete={prep['review_contract_complete']}):\n"
            f"{prep['review_contract'] or '(missing)'}\n\n"
            "Requiem-internal context files omitted: "
            f"{len(prep['omitted_context_pack_files'])}\n"
            "Merge-bound diff "
            f"(complete={prep['diff_complete']}):\n"
            "When complete=True, every merge-bound file is shown below. "
            "Any plan-required file or surface absent from that complete diff is "
            "definitively missing from the PR; return `request_changes` with "
            "concrete findings rather than `needs_human` merely to ask whether "
            "other files exist. The named PR base is also authoritative for work "
            "already merged from dependency leaves. Inspect the base when the "
            "implementation claims a dependency has not landed. If required types "
            "or contracts exist there but this PR uses stand-ins or leaves them "
            "unwired, that is a concrete integration omission: return "
            "`request_changes`, not `needs_human` to ask about dependency status.\n\n"
            f"Merge-bound diff:\n{prep['diff']}"
        )

    @verbs.register("recover_review_token_exhaustion")
    def _recover_review_token_exhaustion(ctx):
        trigger = ctx.completed["review_leaf"]
        evidence = token_failure_evidence(trigger)
        if not is_cumulative_input_exhaustion(trigger):
            return PermanentFailure(
                error_kind="review.runtime_failure",
                message="reviewer failed before producing a review report",
                details={"trigger": evidence},
                receipts=tuple(trigger.get("receipts") or ()),
            )

        prep = ctx.completed["prepare_review"]["value"]
        diff = str(prep.get("diff") or "")
        if not prep.get("diff_complete") or len(diff) > MAX_COMPACTED_REVIEW_DIFF_CHARS:
            return PermanentFailure(
                error_kind="review.compaction_unsafe",
                message=(
                    "reviewer exhausted its token budget, but the complete "
                    "merge-bound diff is too large for the bounded compacted retry"
                ),
                details={
                    "trigger": evidence,
                    "review_diff_chars": prep.get("review_diff_chars"),
                    "max_compacted_review_diff_chars": (
                        MAX_COMPACTED_REVIEW_DIFF_CHARS
                    ),
                },
                receipts=tuple(trigger.get("receipts") or ()),
            )

        return Success(
            value={
                "diff": diff,
                "diff_complete": True,
                "omitted_context_pack_files": prep.get(
                    "omitted_context_pack_files", []
                ),
                "review_contract": prep.get("review_contract", ""),
                "review_contract_complete": prep.get(
                    "review_contract_complete", False
                ),
                "trigger": evidence,
            }
        )

    @verbs.register("compacted_review_prompt")
    def _compacted_review_prompt(ctx):
        recovery = ctx.completed["recover_review_token_exhaustion"]["value"]
        return (
            f"Retry the review for leaf PR #{inputs.pr_number} after the prior "
            "reviewer exhausted its input-token budget. This is the single "
            "bounded recovery attempt.\n\n"
            "The diff below is complete for merge-bound files. Requiem's "
            f"internal `{CONTEXT_PACK_DIR}/` context pack was omitted because "
            "the workflow removes it before merge. Do not call tools or seek "
            "additional repository context. If the supplied diff is genuinely "
            "insufficient to decide safely, return `needs_human`.\n\n"
            "Approve only if the merge-bound diff fully satisfies the leaf plan "
            "contract. Reject or escalate changes that omit required scope, "
            "contradict the plan, or leave required work undone. A missing or "
            "incomplete contract requires `needs_human`. Because the supplied "
            "merge-bound diff is complete, any plan-required file or surface "
            "absent from it is definitively missing; return `request_changes` "
            "with concrete findings rather than asking whether other files "
            "exist.\n\n"
            "Leaf plan contract "
            f"(complete={recovery['review_contract_complete']}):\n"
            f"{recovery['review_contract'] or '(missing)'}\n\n"
            f"Previous failure evidence:\n"
            f"{json.dumps(recovery['trigger'], indent=2, sort_keys=True)}\n\n"
            f"Merge-bound diff:\n{recovery['diff']}"
        )

    @verbs.register("verify_compacted_review")
    def _verify_compacted_review(ctx):
        recovery = ctx.completed["recover_review_token_exhaustion"]
        replacement = ctx.completed["review_leaf_compacted"]
        if recovery.get("kind") != "success" or replacement.get("kind") != "success":
            return PermanentFailure(
                error_kind="review.compacted_verification_failed",
                message="compacted review did not produce verifiable replacement evidence",
            )
        parsed = LeafReviewReport.model_validate(
            replacement["value"]["parsed"]
        ).model_dump()
        return Success(
            value={
                "parsed": parsed,
                "recovery": recovery["value"]["trigger"],
            }
        )

    @verbs.register("finalize_review_runtime_failure")
    def _finalize_review_runtime_failure(ctx):
        trigger = ctx.completed["review_leaf_compacted"]
        evidence = token_failure_evidence(trigger)
        return PermanentFailure(
            error_kind="review.compacted_retry_failed",
            message=(
                "bounded compacted reviewer retry failed before producing "
                "a review report"
            ),
            details={
                "trigger": evidence,
                "recovery": ctx.completed["recover_review_token_exhaustion"]["value"],
            },
            receipts=tuple(trigger.get("receipts") or ()),
        )

    @verbs.register("dispatch_review")
    def _dispatch_review(ctx):
        primary = ctx.completed["review_leaf"]
        if primary.get("kind") == "success":
            parsed = primary["value"]["parsed"]
        else:
            parsed = ctx.completed["verify_compacted_review"]["value"]["parsed"]
        verdict = parsed.get("verdict")
        comments = list(parsed.get("comments") or [])
        summary = str(parsed.get("summary", ""))
        if verdict == "approve":
            if comments:
                return PermanentFailure(
                    error_kind="review.inconsistent",
                    message="reviewer approved but still emitted comments",
                    details={"report": parsed},
                )
            return PermanentFailure(
                error_kind="review.approved",
                message="reviewer approved leaf merge",
            )
        if verdict == "needs_human":
            return PermanentFailure(
                error_kind="needs_human.reviewer",
                message=summary or "reviewer escalated to human",
                details={"report": parsed},
            )
        if verdict != "request_changes":
            return PermanentFailure(
                error_kind="review.inconsistent",
                message=f"unexpected review verdict {verdict!r}",
                details={"report": parsed},
            )
        if not comments:
            return PermanentFailure(
                error_kind="review.inconsistent",
                message="reviewer requested changes but emitted no comments",
                details={"report": parsed},
            )
        return Success(
            value={
                "summary": summary,
                "comments": comments,
                "fingerprint": _fingerprint_comments(comments),
            }
        )

    @verbs.register("reconcile_review_inconsistency")
    def _reconcile_review_inconsistency(ctx):
        trigger = ctx.completed["dispatch_review"]
        report = dict((trigger.get("details") or {}).get("report") or {})
        verdict = report.get("verdict")
        comments = list(report.get("comments") or [])
        summary = str(report.get("summary", ""))

        if verdict == "approve" and comments:
            actionable = [
                comment
                for comment in comments
                if comment.get("severity") in {"blocker", "major"}
            ]
            canonical_verdict = "request_changes" if actionable else "approve"
            method = (
                "promoted_actionable_comments"
                if actionable
                else "accepted_non_blocking_comments"
            )
            return Success(
                value={
                    "report": {
                        "verdict": canonical_verdict,
                        "comments": comments,
                        "summary": summary,
                    },
                    "method": method,
                    "trigger": {
                        "error_kind": trigger.get("error_kind"),
                        "message": trigger.get("message"),
                    },
                }
            )

        return PermanentFailure(
            error_kind="review.deliberation_required",
            message="review report cannot be reconciled mechanically",
            details={
                "report": report,
                "trigger": {
                    "error_kind": trigger.get("error_kind"),
                    "message": trigger.get("message"),
                },
            },
        )

    @verbs.register("review_inconsistency_prompt")
    def _review_inconsistency_prompt(ctx):
        trigger = ctx.completed["dispatch_review"]
        report = (trigger.get("details") or {}).get("report") or {}
        return (
            "Reconcile this internally inconsistent leaf-review report. "
            "Return a fresh canonical LeafReviewReport. Use `approve` only if "
            "the leaf is safe to merge as-is and every retained comment is "
            "non-blocking (`minor` or `nit`). Use `request_changes` only with "
            "one or more concrete `blocker` or `major` comments Requiem can "
            "address. Use `needs_human` only for genuinely ambiguous risk that "
            "cannot be resolved from the report and diff context already "
            "available to you.\n\n"
            f"Original report:\n{json.dumps(report, indent=2, sort_keys=True)}"
        )

    @verbs.register("verify_review_reconciliation")
    def _verify_review_reconciliation(ctx):
        recovery = ctx.completed["reconcile_review_inconsistency"]
        if recovery.get("kind") == "success":
            recovery_value = recovery["value"]
            report = dict(recovery_value["report"])
            attempts = [
                {
                    "kind": "deterministic_reconciliation",
                    "result": recovery_value["method"],
                }
            ]
        else:
            report = dict(
                ctx.completed["deliberate_review_inconsistency"]["value"]["parsed"]
            )
            attempts = [
                {
                    "kind": "deterministic_reconciliation",
                    "result": "ambiguous",
                },
                {
                    "kind": "reviewer_deliberation",
                    "result": report.get("verdict"),
                },
            ]

        verdict = report.get("verdict")
        comments = list(report.get("comments") or [])
        summary = str(report.get("summary", ""))
        actionable = [
            comment
            for comment in comments
            if comment.get("severity") in {"blocker", "major"}
        ]

        if verdict == "approve" and not actionable:
            return PermanentFailure(
                error_kind="review.approved",
                message="review inconsistency reconciled as safe to merge",
                details={"report": report, "recovery_attempts": attempts},
            )

        if verdict == "request_changes" and comments:
            return Success(
                value={
                    "summary": summary,
                    "comments": comments,
                    "fingerprint": _fingerprint_comments(comments),
                    "recovery_attempts": attempts,
                }
            )

        if verdict == "approve" and actionable:
            return Success(
                value={
                    "summary": summary,
                    "comments": comments,
                    "fingerprint": _fingerprint_comments(comments),
                    "recovery_attempts": attempts
                    + [
                        {
                            "kind": "authoritative_verifier",
                            "result": "promoted_actionable_comments",
                        }
                    ],
                }
            )

        trigger = ctx.completed["dispatch_review"]
        return NeedsHuman(
            gate="review_inconsistent",
            prompt=(
                "Review reconciliation still cannot establish a safe canonical "
                "disposition. Retry the reviewer with human guidance or abort "
                "this leaf."
            ),
            options=("retry_review", "abort"),
            context={
                "trigger": {
                    "source_node": "dispatch_review",
                    "problem_kind": trigger.get("error_kind"),
                    "message": trigger.get("message"),
                },
                "evidence": {"original_report": (trigger.get("details") or {}).get("report")},
                "recovery_attempts": attempts,
                "remaining_uncertainty": {
                    "verdict": verdict,
                    "comments": comments,
                    "summary": summary,
                },
                "recommended_option": "retry_review",
                "rationale": (
                    "A fresh review is safer than inferring approval or inventing "
                    "actionable changes from an incomplete report."
                ),
            },
        )

    @verbs.register("synth_prompt")
    def _synth_prompt(ctx):
        review = _actionable_review(ctx)
        rendered = "\n".join(
            f"#{i} {c['file']}:{c.get('line') or '?'} [{c['severity']}] {c['body']}"
            for i, c in enumerate(review.get("comments", []), start=1)
        )
        return (
            "Synthesise the following structured leaf-review findings into "
            "actionable items. Group findings that ask for the same change. "
            "Return CommentSynthesis.\n\n"
            f"{rendered}"
        )

    @verbs.register("address_prompt")
    def _address_prompt(ctx):
        synth = ctx.completed["synthesize_comments"]["value"]["parsed"]
        items = synth.get("actionable_items") or []
        rendered = "\n".join(
            f"- {item['file']} (lines {item.get('line_range')}): {item['change_summary']}"
            for item in items
        )
        return (
            f"Repo checkout: {inputs.repo_path}\n"
            f"Branch: {inputs.expected_head}\n"
            "Apply the following actionable items. Return AddressResult "
            "with file_changes. Prefer exact `replace` operations with "
            "`old_content` and replacement `content` for localized edits "
            "to large files; the old text must match exactly once. Use "
            "full-file `modify` only for bounded whole-file changes. You "
            "cannot commit yourself.\n\n"
            f"{rendered}"
        )

    @verbs.register("apply_addressal")
    def _apply_addressal(ctx):
        parsed = ctx.completed["address_comments"]["value"]["parsed"]
        raw_changes = parsed.get("file_changes") or []
        if inputs.dry_run:
            return Success(value={
                "applied_paths": [],
                "dry_run": True,
                "change_count": len(raw_changes),
            })
        fs = _require_fs(ctx)
        if isinstance(fs, PermanentFailure):
            return fs
        return apply_file_changes(inputs.repo_path, fs, raw_changes)

    @verbs.register("run_addressal_tests")
    async def _run_addressal_tests(ctx):
        if inputs.dry_run:
            return Success(
                value={
                    "passed": True,
                    "summary": "(dry-run: tests not executed)",
                    "command": inputs.test_command or "(auto-detect skipped)",
                    "tested_tree_sha": None,
                }
            )
        detected = (
            DetectedTestCommand(command=inputs.test_command, cwd=inputs.repo_path)
            if inputs.test_command is not None
            else detect_test_command(inputs.repo_path)
        )
        if detected is None:
            return PermanentFailure(
                error_kind="needs_human.tests_status_unknown",
                message=(
                    f"could not detect the required test command in {inputs.repo_path}"
                ),
                details={"reason": "test_command_undetected"},
            )
        try:
            result = test_runner(
                detected.command,
                detected.cwd or inputs.repo_path,
            )
        except Exception as e:  # noqa: BLE001
            return PermanentFailure(
                error_kind="needs_human.tests_status_unknown",
                message=f"test runner crashed: {type(e).__name__}: {e}",
                details={
                    "reason": "test_runner_error",
                    "command": detected.command,
                },
            )
        if not result.passed:
            return PermanentFailure(
                error_kind="needs_human.tests_not_passed",
                message=f"review-fix tests failed via {detected.command!r}",
                details={
                    "passed": False,
                    "summary": result.summary,
                    "command": detected.command,
                },
            )
        fs = _require_fs(ctx)
        if isinstance(fs, PermanentFailure):
            return fs
        try:
            tested_tree_sha = await fs.git_stage_all_and_tree_sha()
        except FsGitError as e:
            return PermanentFailure(
                error_kind="needs_human.tests_status_unknown",
                message=f"could not snapshot the tested worktree: {e.stderr.strip() or e}",
                details={
                    "reason": "tested_tree_unavailable",
                    "stderr": e.stderr,
                },
            )
        return Success(
            value={
                "passed": True,
                "summary": result.summary,
                "command": detected.command,
                "tested_tree_sha": tested_tree_sha,
            }
        )

    @verbs.register("push_addressal")
    async def _push_addressal(ctx):
        addr_outcome = ctx.completed["address_comments"]["value"]
        parsed = addr_outcome.get("parsed") or {}
        if inputs.dry_run:
            applied = ctx.completed["apply_addressal"]["value"]
            return Success(
                value={
                    "sha": None,
                    "pushed": False,
                    "status_posted": False,
                    "reason": "dry_run",
                    "change_count": applied.get("change_count", 0),
                }
            )
        fs = _require_fs(ctx)
        if isinstance(fs, PermanentFailure):
            return fs
        message = f"address review comments: {parsed.get('summary', '')[:200]}".strip()
        tested_tree_sha = str(
            ctx.completed["run_addressal_tests"]["value"].get("tested_tree_sha") or ""
        )
        if not tested_tree_sha:
            return PermanentFailure(
                error_kind="needs_human.tests_status_unknown",
                message="review-fix test evidence is missing its Git tree identity",
                details={"reason": "tested_tree_unavailable"},
            )
        try:
            current_tree_sha = await fs.git_stage_all_and_tree_sha()
            if current_tree_sha != tested_tree_sha:
                return PermanentFailure(
                    error_kind="needs_human.tests_status_unknown",
                    message="worktree changed after review-fix tests passed",
                    details={
                        "reason": "tested_tree_changed",
                        "tested_tree_sha": tested_tree_sha,
                        "current_tree_sha": current_tree_sha,
                    },
                )
            if await fs.git_is_clean():
                # Idempotent resume: a prior commit already landed.
                commit_sha = None
            else:
                commit_sha = await fs.git_commit(message or "address review comments")
        except FsGitError as e:
            return PermanentFailure(
                error_kind="needs_human.commit_failed",
                message=f"git commit failed: {e.stderr.strip() or e}",
                details={"stderr": e.stderr},
            )
        try:
            sha = await toolkit.git_push(inputs.repo_path, inputs.expected_head)
        except Exception as e:  # noqa: BLE001
            return _map_platform_error(
                e,
                run_id=ctx.run_id,
                node_id=ctx.node_id,
                attempt=ctx.attempt,
                operation="git_push",
            )
        try:
            await toolkit.post_commit_status(
                inputs.repo,
                sha,
                context=REQUIRED_TEST_STATUS_CONTEXT,
                state="success",
                description="requiem: local tests passed after review fixes",
            )
        except Exception as e:  # noqa: BLE001
            return _map_platform_error(
                e,
                run_id=ctx.run_id,
                node_id=ctx.node_id,
                attempt=ctx.attempt,
                operation="post_commit_status",
            )
        return Success(
            value={
                "sha": sha,
                "pushed": True,
                "commit_sha": commit_sha,
                "status_posted": True,
            },
            inspected_artifacts=(f"commit:{sha}",),
        )

    @verbs.register("check_progress")
    def _check_progress(ctx):
        cur_sha = ctx.completed["push_addressal"]["value"]["sha"]
        review = _actionable_review(ctx)
        prior = (ctx.completed.get("check_progress") or {}).get("value") or {}
        iteration = int(prior.get("iteration", 0)) + 1
        last_sha = prior.get("last_sha")
        last_fp = prior.get("last_fingerprint")
        cur_fp = review.get("fingerprint")
        synth = ctx.completed["synthesize_comments"]["value"]["parsed"]
        items_this_round = len(synth.get("actionable_items") or [])
        addressed = int(prior.get("comments_addressed", 0)) + items_this_round

        if last_sha is not None and last_sha == cur_sha:
            return PermanentFailure(
                error_kind="needs_human.no_progress",
                message=f"review loop produced the same SHA twice ({cur_sha[:8]})",
            )
        if (
            last_fp is not None
            and cur_fp is not None
            and last_fp == cur_fp
            and last_sha is not None
            and last_sha != cur_sha
        ):
            return PermanentFailure(
                error_kind="needs_human.same_findings",
                message="review loop reproduced the same findings on a new SHA",
            )
        if iteration > inputs.max_iterations:
            return PermanentFailure(
                error_kind="needs_human.max_iterations",
                message=(
                    f"review loop hit max_iterations={inputs.max_iterations} "
                    "without approval"
                ),
            )
        return Success(
            value={
                "iteration": iteration,
                "last_sha": cur_sha,
                "last_fingerprint": cur_fp,
                "comments_addressed": addressed,
            }
        )

    @verbs.register("prune_context_pack")
    async def _prune_context_pack(ctx):
        # Every leaf writes its OWN context pack to the SAME fixed path
        # (`.requiem/AGENTS.md` + siblings — see context_pack.py). That's
        # fine while the leaf branch is alone, but once ANY leaf's copy
        # lands on the shared trunk, every OTHER leaf's differing copy at
        # that identical path becomes an unavoidable, spurious merge
        # conflict — not a real code conflict, just two leaves independently
        # "adding" the same path with different content. Run #39 postmortem:
        # this was the actual cause behind 1/6 landed leaves'
        # `needs_human.conflicts` (and would eventually hit every leaf after
        # the first one to merge). The pack is requiem-internal scratch
        # state for the review/address loop (mirrors `cleanup_worktree`'s
        # `.requiem` exclude in implementation.py) — nothing downstream of
        # `check_can_merge` reads it, so it's safe (and correct) to prune it
        # from the branch entirely before trunk ever sees it.
        if inputs.dry_run:
            return Success(value={"pruned": False, "reason": "dry_run"})
        fs = _require_fs(ctx)
        if isinstance(fs, PermanentFailure):
            return fs
        pack_dir = inputs.repo_path / CONTEXT_PACK_DIR
        try:
            if pack_dir.exists():
                shutil.rmtree(pack_dir)
            if await fs.git_is_clean():
                # Idempotent resume: either there was never a pack to
                # prune, or a prior attempt committed the removal but
                # died before the push below — either way, nothing new
                # to commit here.
                commit_sha = None
            else:
                commit_sha = await fs.git_commit(
                    "chore(context): drop requiem context-pack scaffold before merge",
                    paths=[Path(CONTEXT_PACK_DIR)],
                )
        except FsGitError as e:
            return PermanentFailure(
                error_kind="needs_human.commit_failed",
                message=f"failed to prune context pack: {e.stderr.strip() or e}",
                details={"stderr": e.stderr},
            )
        except OSError as e:
            return PermanentFailure(
                error_kind="needs_human.commit_failed",
                message=f"failed to remove {CONTEXT_PACK_DIR}: {e}",
            )
        try:
            sha = await toolkit.git_push(inputs.repo_path, inputs.expected_head)
        except Exception as e:  # noqa: BLE001
            return _map_platform_error(
                e,
                run_id=ctx.run_id,
                node_id=ctx.node_id,
                attempt=ctx.attempt,
                operation="git_push",
            )
        try:
            await toolkit.post_commit_status(
                inputs.repo,
                sha,
                context=REQUIRED_TEST_STATUS_CONTEXT,
                state="success",
                description=(
                    "requiem: local tests passed before framework-only "
                    "context-pack cleanup"
                ),
            )
        except Exception as e:  # noqa: BLE001
            return _map_platform_error(
                e,
                run_id=ctx.run_id,
                node_id=ctx.node_id,
                attempt=ctx.attempt,
                operation="post_commit_status",
            )
        return Success(
            value={
                "pruned": commit_sha is not None,
                "commit_sha": commit_sha,
                "sha": sha,
                "status_posted": True,
            },
            inspected_artifacts=(f"commit:{sha}",),
        )

    @verbs.register("check_can_merge")
    async def _check_can_merge(ctx):
        try:
            report = await toolkit.pr_mergeability(inputs.repo, inputs.pr_number)
        except Exception as e:  # noqa: BLE001
            return _map_platform_error(
                e,
                run_id=ctx.run_id,
                node_id=ctx.node_id,
                attempt=ctx.attempt,
                operation="check_can_merge",
            )
        if report.conflicts:
            return _assess_concrete_mergeability(report)
        test_assessment = await _assess_test_status(ctx, report)
        if test_assessment is not None:
            return test_assessment
        assessment = _assess_concrete_mergeability(report)
        if assessment is not None:
            return assessment
        return PermanentFailure(
            error_kind="mergeability.recheck_required",
            message=(
                "ADO mergeability is still being computed "
                f"({report.mergeable_state})"
            ),
            details={"report": _mergeability_evidence(report)},
        )

    # ADO commonly reports queued/notSet immediately after the cleanup push,
    # then converges without another mutation. Re-read only the authoritative
    # server projection and never infer mergeability from the local checkout.
    @verbs.register("verify_mergeability")
    async def _verify_mergeability(ctx):
        try:
            report = await toolkit.pr_mergeability(inputs.repo, inputs.pr_number)
        except Exception as e:  # noqa: BLE001
            return _map_platform_error(
                e,
                run_id=ctx.run_id,
                node_id=ctx.node_id,
                attempt=ctx.attempt,
                operation="verify_mergeability",
            )

        if report.conflicts:
            return _assess_concrete_mergeability(report)
        test_assessment = await _assess_test_status(ctx, report)
        if test_assessment is not None:
            return test_assessment
        assessment = _assess_concrete_mergeability(report)
        if assessment is not None:
            if isinstance(assessment, Success):
                return Success(
                    value={
                        **assessment.value,
                        "verification_method": "authoritative_mergeability_reread",
                        "verification_attempt": ctx.attempt,
                    }
                )
            return assessment

        if ctx.attempt <= MERGEABILITY_RETRY_MAX:
            return RetryableFailure(
                retry_key=f"{ctx.run_id}:{ctx.node_id}:mergeability",
                error_kind="mergeability.propagating",
                message=(
                    "ADO mergeability is still being computed "
                    f"({report.mergeable_state})"
                ),
                attempt=ctx.attempt,
                after=MERGEABILITY_RETRY_DELAY_S,
            )

        trigger = ctx.completed["check_can_merge"]
        evidence = _mergeability_evidence(report)
        return NeedsHuman(
            gate="mergeability_unknown",
            prompt=(
                "Authoritative ADO reads still cannot establish whether the leaf "
                "PR is mergeable. Retry the read-only verification or abort this leaf."
            ),
            options=("retry_verification", "abort"),
            context={
                "trigger": {
                    "source_node": "check_can_merge",
                    "problem_kind": trigger.get("error_kind"),
                    "message": trigger.get("message"),
                },
                "evidence": evidence,
                "recovery_attempts": [
                    {
                        "kind": "authoritative_mergeability_reread",
                        "attempts": ctx.attempt,
                        "result": f"mergeability_{report.mergeable_state}",
                    }
                ],
                "remaining_uncertainty": evidence,
                "recommended_option": "retry_verification",
                "rationale": (
                    "The PR still has green checks and no reported conflict, so "
                    "another authoritative read may observe ADO's asynchronous "
                    "mergeability computation."
                ),
            },
        )

    @verbs.register("merge_pr")
    async def _merge_pr(ctx):
        if inputs.dry_run:
            return Success(
                value={
                    "merged": False,
                    "merge_sha": None,
                    "strategy": inputs.merge_strategy,
                    "dry_run": True,
                }
            )
        validated_head_sha = ""
        for source_node in ("check_can_merge", "verify_mergeability"):
            source = ctx.completed.get(source_node) or {}
            if source.get("kind") != "success":
                continue
            validated_head_sha = str(
                (source.get("value") or {}).get("head_sha") or ""
            )
            if validated_head_sha:
                break
        if not validated_head_sha:
            return PermanentFailure(
                error_kind="needs_human.tests_status_unknown",
                message="merge refused because the validated leaf head SHA is unavailable",
            )
        try:
            result = await toolkit.pr_complete(
                inputs.repo,
                inputs.pr_number,
                strategy=inputs.merge_strategy,
                expected_head=inputs.expected_head,
                expected_base=inputs.expected_base,
                expected_head_sha=validated_head_sha,
            )
        except Exception as e:  # noqa: BLE001
            return _map_platform_error(
                e,
                run_id=ctx.run_id,
                node_id=ctx.node_id,
                attempt=ctx.attempt,
                operation="merge_pr",
            )
        if not result.merged:
            return PermanentFailure(
                error_kind="merge.not_confirmed",
                message="merge completion did not confirm a merged PR",
                details={
                    "completion_result": {
                        "number": result.number,
                        "merged": result.merged,
                        "merge_sha": result.merge_sha,
                        "strategy": result.strategy,
                    }
                },
            )
        return Success(
            value={
                "merged": result.merged,
                "merge_sha": result.merge_sha,
                "strategy": result.strategy,
            }
        )

    # ADO may return active/queued from the completion PATCH and close the PR
    # shortly afterward. Re-query authoritatively without repeating the merge.
    @verbs.register("verify_merge_confirmation")
    async def _verify_merge_confirmation(ctx):
        try:
            pr = await toolkit.pr_view(inputs.repo, inputs.pr_number)
        except Exception as e:  # noqa: BLE001
            return _map_platform_error(
                e,
                run_id=ctx.run_id,
                node_id=ctx.node_id,
                attempt=ctx.attempt,
                operation="verify_merge_confirmation",
            )

        evidence = _merge_confirmation_evidence(pr)
        scope_matches = (
            pr.head == inputs.expected_head and pr.base == inputs.expected_base
        )
        if pr.merged and scope_matches:
            merge_sha = evidence["merge_sha"]
            artifacts = (pr.url,) + ((f"commit:{merge_sha}",) if merge_sha else ())
            return Success(
                value={
                    "merged": True,
                    "merge_sha": merge_sha,
                    "strategy": inputs.merge_strategy,
                    "confirmation_method": "authoritative_pr_requery",
                    "confirmation_attempt": ctx.attempt,
                },
                inspected_artifacts=artifacts,
            )

        if (
            scope_matches
            and pr.state == "open"
            and ctx.attempt <= MERGE_CONFIRMATION_RETRY_MAX
        ):
            return RetryableFailure(
                retry_key=f"{ctx.run_id}:{ctx.node_id}:merge_confirmation",
                error_kind="merge.confirmation_pending",
                message=(
                    "merge completion is not authoritative yet; "
                    f"PR state={pr.state!r}, merge status={evidence['merge_status']!r}"
                ),
                attempt=ctx.attempt,
                after=MERGE_CONFIRMATION_RETRY_DELAY_S,
            )

        trigger = ctx.completed["merge_pr"]
        retry_recommended = scope_matches and pr.state == "open"
        return NeedsHuman(
            gate="merge_not_confirmed",
            prompt=(
                "Authoritative PR state still does not prove the leaf merged. "
                "Retry the read-only verification or abort this leaf."
            ),
            options=("retry_verification", "abort"),
            context={
                "trigger": {
                    "source_node": "merge_pr",
                    "problem_kind": trigger.get("error_kind"),
                    "message": trigger.get("message"),
                },
                "evidence": evidence,
                "recovery_attempts": [
                    {
                        "kind": "authoritative_pr_requery",
                        "attempts": ctx.attempt,
                        "result": (
                            "scope_mismatch"
                            if not scope_matches
                            else f"pr_{pr.state}_not_merged"
                        ),
                    }
                ],
                "remaining_uncertainty": {
                    "expected_head": inputs.expected_head,
                    "expected_base": inputs.expected_base,
                    "observed_head": pr.head,
                    "observed_base": pr.base,
                    "pr_state": pr.state,
                    "merge_status": evidence["merge_status"],
                },
                "recommended_option": (
                    "retry_verification" if retry_recommended else "abort"
                ),
                "rationale": (
                    "The PR is still open, so another authoritative read may "
                    "observe an asynchronous completion."
                    if retry_recommended
                    else "The PR is not merged or no longer matches the expected leaf scope."
                ),
            },
        )

    return verbs


def build_agent_registry() -> AgentRegistry:
    reg = AgentRegistry()
    reg.register(LEAF_REVIEWER)
    reg.register(COMPACTED_LEAF_REVIEWER)
    reg.register(COMMENT_SYNTHESIZER)
    reg.register(COMMENT_ADDRESSER)
    return reg


def build_workflow() -> Workflow:
    return (
        WorkflowBuilder("leaf-lifecycle", module=MODULE, version="0.2")
        .entry("start")
        .script("start", verb="start_run")
            .edge("start", on="success", to="fetch_pr")
        .script("fetch_pr", verb="fetch_pr", retry_max=2)
            .edge("fetch_pr", on="success", to="check_initial_state")
            .edge("fetch_pr", on="retry_exhausted", to="needs_human_end")
            .edge("fetch_pr", on="permanent_failure", to="needs_human_end")
        .script("check_initial_state", verb="check_initial_state")
            .edge("check_initial_state", on="success", to="assert_leaf_scope")
            .edge("check_initial_state", on="permanent_failure:already_merged", to="end_already_merged")
            .edge("check_initial_state", on="permanent_failure", to="needs_human_end")
        .script("assert_leaf_scope", verb="assert_leaf_scope", retry_max=2)
            .edge("assert_leaf_scope", on="success", to="check_tests_passed")
            .edge("assert_leaf_scope", on="retry_exhausted", to="needs_human_end")
            .edge("assert_leaf_scope", on="permanent_failure", to="needs_human_end")
        .script("check_tests_passed", verb="check_tests_passed")
            .edge("check_tests_passed", on="success", to="prepare_review")
            .edge(
                "check_tests_passed",
                on="permanent_failure:tests_status.recheck_required",
                to="verify_tests_status_before_review",
            )
            .edge("check_tests_passed", on="permanent_failure", to="needs_human_end")
        .script(
            "verify_tests_status_before_review",
            verb="verify_tests_status",
            retry_max=TEST_STATUS_RETRY_MAX,
        )
            .edge(
                "verify_tests_status_before_review",
                on="success",
                to="prepare_review",
            )
            .edge(
                "verify_tests_status_before_review",
                on="needs_human:retry_verification",
                to="verify_tests_status_before_review",
            )
            .edge(
                "verify_tests_status_before_review",
                on="needs_human:abort",
                to="needs_human_end",
            )
            .edge(
                "verify_tests_status_before_review",
                on="retry_exhausted",
                to="needs_human_end",
            )
            .edge(
                "verify_tests_status_before_review",
                on="permanent_failure",
                to="needs_human_end",
            )
        .script("prepare_review", verb="prepare_review", retry_max=2)
            .edge("prepare_review", on="success", to="review_leaf")
            .edge("prepare_review", on="retry_exhausted", to="needs_human_end")
            .edge("prepare_review", on="permanent_failure", to="needs_human_end")
        .agent("review_leaf", agent="leaf_reviewer", prompt_verb="review_prompt")
            .edge("review_leaf", on="success", to="dispatch_review")
            .edge("review_leaf", on="retry_exhausted", to="recover_review_token_exhaustion")
            .edge("review_leaf", on="bad_output", to="needs_human_end")
            .edge("review_leaf", on="permanent_failure", to="needs_human_end")
        .script(
            "recover_review_token_exhaustion",
            verb="recover_review_token_exhaustion",
        )
            .edge(
                "recover_review_token_exhaustion",
                on="success",
                to="review_leaf_compacted",
            )
            .edge(
                "recover_review_token_exhaustion",
                on="permanent_failure",
                to="needs_human_end",
            )
        .agent(
            "review_leaf_compacted",
            agent="compacted_leaf_reviewer",
            prompt_verb="compacted_review_prompt",
        )
            .edge("review_leaf_compacted", on="success", to="verify_compacted_review")
            .edge(
                "review_leaf_compacted",
                on="retry_exhausted",
                to="finalize_review_runtime_failure",
            )
            .edge(
                "review_leaf_compacted",
                on="bad_output",
                to="finalize_review_runtime_failure",
            )
            .edge("review_leaf_compacted", on="permanent_failure", to="needs_human_end")
        .script("verify_compacted_review", verb="verify_compacted_review")
            .edge("verify_compacted_review", on="success", to="dispatch_review")
            .edge("verify_compacted_review", on="permanent_failure", to="needs_human_end")
        .script(
            "finalize_review_runtime_failure",
            verb="finalize_review_runtime_failure",
        )
            .edge(
                "finalize_review_runtime_failure",
                on="permanent_failure",
                to="needs_human_end",
            )
        .script("dispatch_review", verb="dispatch_review")
            .edge("dispatch_review", on="success", to="synthesize_comments")
            .edge("dispatch_review", on="permanent_failure:review.approved", to="prune_context_pack")
            .edge("dispatch_review", on="permanent_failure:review.inconsistent", to="reconcile_review_inconsistency")
            .edge("dispatch_review", on="permanent_failure", to="needs_human_end")
        .script("reconcile_review_inconsistency", verb="reconcile_review_inconsistency")
            .edge("reconcile_review_inconsistency", on="success", to="verify_review_reconciliation")
            .edge("reconcile_review_inconsistency", on="permanent_failure:review.deliberation_required", to="deliberate_review_inconsistency")
            .edge("reconcile_review_inconsistency", on="permanent_failure", to="needs_human_end")
        .agent("deliberate_review_inconsistency", agent="leaf_reviewer", prompt_verb="review_inconsistency_prompt")
            .edge("deliberate_review_inconsistency", on="success", to="verify_review_reconciliation")
            .edge("deliberate_review_inconsistency", on="bad_output", to="needs_human_end")
            .edge("deliberate_review_inconsistency", on="permanent_failure", to="needs_human_end")
        .script("verify_review_reconciliation", verb="verify_review_reconciliation")
            .edge("verify_review_reconciliation", on="success", to="synthesize_comments")
            .edge("verify_review_reconciliation", on="permanent_failure:review.approved", to="prune_context_pack")
            .edge("verify_review_reconciliation", on="needs_human:retry_review", to="review_leaf")
            .edge("verify_review_reconciliation", on="needs_human:abort", to="needs_human_end")
            .edge("verify_review_reconciliation", on="permanent_failure", to="needs_human_end")
        .agent("synthesize_comments", agent="comment_synthesizer", prompt_verb="synth_prompt")
            .edge("synthesize_comments", on="success", to="address_comments")
            .edge("synthesize_comments", on="bad_output", to="needs_human_end")
            .edge("synthesize_comments", on="permanent_failure", to="needs_human_end")
        .agent(
            "address_comments",
            agent="comment_addresser",
            prompt_verb="address_prompt",
            retry_max=1,
        )
            .edge("address_comments", on="success", to="apply_addressal")
            .edge("address_comments", on="bad_output", to="needs_human_end")
            .edge("address_comments", on="permanent_failure", to="needs_human_end")
        .script("apply_addressal", verb="apply_addressal")
            .edge("apply_addressal", on="success", to="run_addressal_tests")
            .edge("apply_addressal", on="permanent_failure", to="needs_human_end")
        .script("run_addressal_tests", verb="run_addressal_tests")
            .edge("run_addressal_tests", on="success", to="push_addressal")
            .edge(
                "run_addressal_tests",
                on="permanent_failure",
                to="needs_human_end",
            )
        .script("push_addressal", verb="push_addressal", retry_max=2)
            .edge("push_addressal", on="success", to="check_progress")
            .edge("push_addressal", on="retry_exhausted", to="needs_human_end")
            .edge("push_addressal", on="permanent_failure", to="needs_human_end")
        .script("check_progress", verb="check_progress")
            .edge("check_progress", on="success", to="check_tests_passed")
            .edge("check_progress", on="permanent_failure", to="needs_human_end")
        .script("prune_context_pack", verb="prune_context_pack", retry_max=2)
            .edge("prune_context_pack", on="success", to="check_can_merge")
            .edge("prune_context_pack", on="retry_exhausted", to="needs_human_end")
            .edge("prune_context_pack", on="permanent_failure", to="needs_human_end")
        .script("check_can_merge", verb="check_can_merge", retry_max=2)
            .edge("check_can_merge", on="success", to="merge_pr")
            .edge(
                "check_can_merge",
                on="permanent_failure:tests_status.recheck_required",
                to="verify_tests_status_before_merge",
            )
            .edge(
                "check_can_merge",
                on="permanent_failure:mergeability.recheck_required",
                to="verify_mergeability",
            )
            .edge("check_can_merge", on="retry_exhausted", to="needs_human_end")
            .edge("check_can_merge", on="permanent_failure", to="needs_human_end")
        .script(
            "verify_mergeability",
            verb="verify_mergeability",
            retry_max=MERGEABILITY_RETRY_MAX,
        )
            .edge("verify_mergeability", on="success", to="merge_pr")
            .edge(
                "verify_mergeability",
                on="permanent_failure:tests_status.recheck_required",
                to="verify_tests_status_before_merge",
            )
            .edge(
                "verify_mergeability",
                on="needs_human:retry_verification",
                to="verify_mergeability",
            )
            .edge("verify_mergeability", on="needs_human:abort", to="needs_human_end")
            .edge("verify_mergeability", on="retry_exhausted", to="needs_human_end")
            .edge("verify_mergeability", on="permanent_failure", to="needs_human_end")
        .script(
            "verify_tests_status_before_merge",
            verb="verify_tests_status",
            retry_max=TEST_STATUS_RETRY_MAX,
        )
            .edge(
                "verify_tests_status_before_merge",
                on="success",
                to="check_can_merge",
            )
            .edge(
                "verify_tests_status_before_merge",
                on="needs_human:retry_verification",
                to="verify_tests_status_before_merge",
            )
            .edge(
                "verify_tests_status_before_merge",
                on="needs_human:abort",
                to="needs_human_end",
            )
            .edge(
                "verify_tests_status_before_merge",
                on="retry_exhausted",
                to="needs_human_end",
            )
            .edge(
                "verify_tests_status_before_merge",
                on="permanent_failure",
                to="needs_human_end",
            )
        .script("merge_pr", verb="merge_pr", retry_max=2)
            .edge("merge_pr", on="success", to="end_merged")
            .edge(
                "merge_pr",
                on="permanent_failure:merge.not_confirmed",
                to="verify_merge_confirmation",
            )
            .edge("merge_pr", on="retry_exhausted", to="needs_human_end")
            .edge("merge_pr", on="permanent_failure", to="needs_human_end")
        .script(
            "verify_merge_confirmation",
            verb="verify_merge_confirmation",
            retry_max=MERGE_CONFIRMATION_RETRY_MAX,
        )
            .edge("verify_merge_confirmation", on="success", to="end_merged")
            .edge(
                "verify_merge_confirmation",
                on="needs_human:retry_verification",
                to="verify_merge_confirmation",
            )
            .edge("verify_merge_confirmation", on="needs_human:abort", to="needs_human_end")
            .edge("verify_merge_confirmation", on="retry_exhausted", to="needs_human_end")
            .edge("verify_merge_confirmation", on="permanent_failure", to="needs_human_end")
        .terminate("end_merged", disposition="completed")
        .terminate("end_already_merged", disposition="completed")
        .terminate("needs_human_end", disposition="failed")
        .humanize({
            "start": "Starting leaf PR lifecycle",
            "fetch_pr": "Fetched leaf PR",
            "check_initial_state": "Checked leaf PR state",
            "assert_leaf_scope": "Verified leaf PR scope",
            "check_tests_passed": "Verified leaf test precondition",
            "verify_tests_status_before_review": "Verified leaf test-status propagation",
            "prepare_review": "Prepared local leaf review context",
            "review_leaf": "Reviewed leaf PR",
            "recover_review_token_exhaustion": "Prepared bounded review recovery",
            "review_leaf_compacted": "Retried leaf review with compact context",
            "verify_compacted_review": "Verified compacted review evidence",
            "finalize_review_runtime_failure": "Recorded reviewer runtime failure",
            "dispatch_review": "Dispatched review verdict",
            "reconcile_review_inconsistency": "Reconciled inconsistent review evidence",
            "deliberate_review_inconsistency": "Deliberated on inconsistent review evidence",
            "verify_review_reconciliation": "Verified reconciled review disposition",
            "synthesize_comments": "Synthesised reviewer findings",
            "address_comments": "Addressed reviewer findings",
            "apply_addressal": "Applied addressal file changes",
            "run_addressal_tests": "Re-ran tests after review fixes",
            "push_addressal": "Pushed addressal commits",
            "check_progress": "Checked review-loop progress",
            "prune_context_pack": "Pruned requiem context-pack scaffold before merge",
            "check_can_merge": "Checked leaf mergeability",
            "verify_mergeability": "Verified leaf mergeability propagation",
            "verify_tests_status_before_merge": "Verified final test-status propagation",
            "merge_pr": "Merged leaf PR",
            "verify_merge_confirmation": "Verified leaf merge completion",
            "end_merged": "Leaf PR lifecycle",
            "end_already_merged": "Leaf PR lifecycle",
            "needs_human_end": "Leaf PR lifecycle",
        })
        .build()
    )


def build_engine(
    log_dir: Path,
    *,
    inputs: LeafLifecycleInputs | None = None,
    toolkit: LeafLifecycleToolkit | None = None,
    provider: Any = None,
    toolbelt: Toolbelt | None = None,
    gate_handler=None,
    test_runner=None,
) -> Engine:
    log_dir.mkdir(parents=True, exist_ok=True)
    demo_mode = inputs is None

    if demo_mode:
        inputs = LeafLifecycleInputs(
            repo="Owner/Repo",
            repo_path=log_dir,
            leaf_id="1",
            root_item_id=4242,
            pr_number=347,
            default_branch="main",
            dry_run=True,
        )
    if provider is None:
        provider = FakeProvider(
            scripts={
                "leaf_reviewer": [{"verdict": "approve", "comments": [], "summary": "looks good"}],
                "comment_synthesizer": [],
                "comment_addresser": [],
            }
        )

    tb = toolbelt or Toolbelt.real()
    if toolkit is None:
        if demo_mode:
            toolkit = FakeLeafLifecycleToolkit(
                pr_snapshots=[
                    RepoPullRequest(
                        number=inputs.pr_number,
                        title="Demo leaf",
                        state="open",
                        merged=False,
                        merged_at=None,
                        head=inputs.expected_head,
                        base=inputs.expected_base,
                        url=f"https://example.test/pr/{inputs.pr_number}",
                    )
                ],
                branch_sha_snapshots=["demo-sha-1"],
                mergeability_snapshots=[
                    RepoMergeabilityReport(
                        mergeable=True,
                        mergeable_state="clean",
                        checks_state="success",
                        conflicts=False,
                        policies_satisfied=True,
                    )
                ],
                complete_result=RepoCompleteResult(
                    number=inputs.pr_number,
                    merged=True,
                    merge_sha="demo-merge-sha",
                    strategy=inputs.merge_strategy,
                ),
            )
        else:
            repo_client = tb.repo or tb.gh
            if not isinstance(repo_client, MergeCapableRepoPlatform):
                raise ValueError(
                    "leaf_lifecycle requires a MergeCapableRepoPlatform at toolbelt.repo"
                )
            toolkit = RealLeafLifecycleToolkit(repo_client)

    if isinstance(provider, FakeProvider) and "leaf_reviewer" not in provider.scripts:
        provider.scripts["leaf_reviewer"] = [
            {"verdict": "approve", "comments": [], "summary": "looks good"}
        ]

    return Engine(
        workflow=build_workflow(),
        verbs=build_verb_registry(
            inputs,
            toolkit,
            test_runner=test_runner or default_test_runner,
        ),
        agents=build_agent_registry(),
        provider=provider,
        toolbelt=tb,
        log_dir=log_dir,
        gate_handler=gate_handler,
    )


def build_result(completed: dict[str, dict[str, Any]]) -> LeafLifecycleResult:
    fetch = (completed.get("fetch_pr") or {}).get("value") or {}
    merge = (completed.get("merge_pr") or {}).get("value") or {}
    verification = completed.get("verify_merge_confirmation") or {}
    if verification.get("kind") == "success":
        merge = verification.get("value") or merge
    progress = (completed.get("check_progress") or {}).get("value") or {}
    initial = completed.get("check_initial_state") or {}

    if (
        initial.get("kind") == "permanent_failure"
        and initial.get("error_kind") == "already_merged"
    ):
        final_state: LeafLifecycleState = "already_merged"
    elif merge.get("dry_run") or merge.get("merged"):
        final_state = "merged"
    elif any(
        o.get("kind") == "permanent_failure"
        and str(o.get("error_kind", "")).startswith("needs_human")
        for o in completed.values()
    ):
        final_state = "needs_human"
    elif any(o.get("kind") == "needs_human" for o in completed.values()):
        final_state = "needs_human"
    elif any(o.get("kind") == "permanent_failure" for o in completed.values()):
        final_state = "failed"
    else:
        final_state = "needs_human"

    return LeafLifecycleResult(
        final_state=final_state,
        pr_number=int(fetch.get("number") or 0),
        merge_sha=str(merge.get("merge_sha")) if merge.get("merge_sha") else None,
        iterations=int(progress.get("iteration") or 0),
        comments_addressed=int(progress.get("comments_addressed") or 0),
    )
