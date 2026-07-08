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
      → dispatch_review
          ├─ approve         → prune_context_pack → check_can_merge → merge_pr → end_merged
          ├─ request_changes → synthesize_comments → address_comments
          │                    → apply_addressal → push_addressal
          │                    → check_progress → prepare_review
          └─ needs_human     → needs_human_end

Fail-closed rules:

* The leaf PR's own verified CI/build status must be explicitly green before
  review. Self-reported handoff metadata is not used as a pass/fail signal.
* The live PR head/base must stay exactly ``impl/<root>-<item>`` →
  ``feature/<root>``; any drift or protected-base target escalates.
* Conflicts, failing/pending checks, unsatisfied policies, indeterminate
  mergeability, repeated identical findings, and no-progress loops surrender to a
  human — never an optimistic merge.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from requiem import branch_model
from requiem.agent import LEAF_REVIEWER, FakeProvider
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
    RepoCompleteResult,
    RepoMergeStrategy,
    RepoMergeabilityReport,
    RepoPullRequest,
)
from requiem.coder_output import apply_file_changes
from requiem.dsl import AgentRegistry, VerbRegistry, Workflow, WorkflowBuilder
from requiem.kernel import Engine
from requiem.outcomes import Outcome, PermanentFailure, RetryableFailure, Success
from requiem.review_schemas import LeafReviewReport
from requiem.toolbelt import Toolbelt
from requiem.workflows.pr_lifecycle import (
    COMMENT_ADDRESSER,
    COMMENT_SYNTHESIZER,
    CommentSynthesis,
)

MODULE = "requiem.workflows.leaf_lifecycle"
LeafLifecycleState = Literal["merged", "already_merged", "needs_human", "failed"]


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
    ) -> RepoCompleteResult:
        return await self._repo.pr_complete(
            repo,
            number,
            strategy=strategy,
            expected_head=expected_head,
            expected_base=expected_base,
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
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    complete_calls: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._pr_idx = 0
        self._sha_idx = 0
        self._merge_idx = 0
        self._push_idx = 0

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
        return sha

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
        return report

    async def pr_complete(
        self,
        repo: str,
        number: int,
        *,
        strategy: RepoMergeStrategy,
        expected_head: str | None = None,
        expected_base: str | None = None,
    ) -> RepoCompleteResult:
        self.calls.append(
            ("pr_complete", (repo, number, strategy, expected_head, expected_base))
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
    inputs: LeafLifecycleInputs, toolkit: LeafLifecycleToolkit
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
        if report.checks_state == "success":
            return Success(
                value={
                    "checks_state": report.checks_state,
                    "mergeable": report.mergeable,
                    "mergeable_state": report.mergeable_state,
                    "conflicts": report.conflicts,
                    "policies_satisfied": report.policies_satisfied,
                }
            )
        if report.checks_state == "failure":
            return PermanentFailure(
                error_kind="needs_human.tests_not_passed",
                message="leaf PR checks/build validation are failing",
                details={
                    "checks_state": report.checks_state,
                    "mergeable_state": report.mergeable_state,
                },
            )
        return PermanentFailure(
            error_kind="needs_human.tests_status_unknown",
            message="leaf PR checks/build validation are pending or unavailable",
            details={
                "checks_state": report.checks_state,
                "mergeable_state": report.mergeable_state,
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
            diff = toolkit.review_diff(inputs.repo_path, base=pr.base, head=pr.head)
        except Exception as e:  # noqa: BLE001
            return _map_platform_error(
                e,
                run_id=ctx.run_id,
                node_id=ctx.node_id,
                attempt=ctx.attempt,
                operation="prepare_review",
            )
        return Success(
            value={
                "head": pr.head,
                "base": pr.base,
                "head_sha": head_sha,
                "diff": diff[:30000],
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
            "feature trunk as-is. Return `request_changes` for concrete code fixes "
            "Requiem can implement automatically. Return `needs_human` for any "
            "ambiguous or unsafe case.\n\n"
            f"Diff:\n{prep['diff']}"
        )

    @verbs.register("dispatch_review")
    def _dispatch_review(ctx):
        parsed = ctx.completed["review_leaf"]["value"]["parsed"]
        verdict = parsed.get("verdict")
        comments = list(parsed.get("comments") or [])
        summary = str(parsed.get("summary", ""))
        if verdict == "approve":
            if comments:
                return PermanentFailure(
                    error_kind="needs_human.review_inconsistent",
                    message="reviewer approved but still emitted comments",
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
                error_kind="needs_human.review_inconsistent",
                message=f"unexpected review verdict {verdict!r}",
            )
        if not comments:
            return PermanentFailure(
                error_kind="needs_human.review_inconsistent",
                message="reviewer requested changes but emitted no comments",
            )
        return Success(
            value={
                "summary": summary,
                "comments": comments,
                "fingerprint": _fingerprint_comments(comments),
            }
        )

    @verbs.register("synth_prompt")
    def _synth_prompt(ctx):
        review = ctx.completed["dispatch_review"]["value"]
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
            "with file_changes (full content per changed file) — you "
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
                    "reason": "dry_run",
                    "change_count": applied.get("change_count", 0),
                }
            )
        fs = _require_fs(ctx)
        if isinstance(fs, PermanentFailure):
            return fs
        message = f"address review comments: {parsed.get('summary', '')[:200]}".strip()
        try:
            if await fs.git_is_clean():
                # Idempotent resume: a prior commit already landed.
                commit_sha = None
            else:
                await fs._git("add", "-A")  # noqa: SLF001 — staging shortcut
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
        return Success(
            value={"sha": sha, "pushed": True, "commit_sha": commit_sha},
            inspected_artifacts=(f"commit:{sha}",),
        )

    @verbs.register("check_progress")
    def _check_progress(ctx):
        cur_sha = ctx.completed["push_addressal"]["value"]["sha"]
        review = ctx.completed["dispatch_review"]["value"]
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
        return Success(
            value={"pruned": commit_sha is not None, "commit_sha": commit_sha, "sha": sha},
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
            return PermanentFailure(
                error_kind="needs_human.conflicts",
                message=f"leaf PR has merge conflicts ({report.mergeable_state})",
            )
        if report.checks_state == "failure":
            return PermanentFailure(
                error_kind="needs_human.tests_not_passed",
                message=f"leaf PR checks/build validation are failing ({report.mergeable_state})",
            )
        if report.checks_state != "success":
            return PermanentFailure(
                error_kind="needs_human.tests_status_unknown",
                message=f"leaf PR checks/build validation are pending or unavailable ({report.checks_state})",
            )
        if report.mergeable is None:
            return PermanentFailure(
                error_kind="needs_human.mergeability_unknown",
                message="mergeability is indeterminate",
            )
        if not report.policies_satisfied:
            return PermanentFailure(
                error_kind="needs_human.policies_unsatisfied",
                message="required merge policies are not satisfied",
            )
        if not report.mergeable:
            return PermanentFailure(
                error_kind="needs_human.not_mergeable",
                message=f"leaf PR is not mergeable ({report.mergeable_state})",
            )
        return Success(
            value={
                "mergeable": report.mergeable,
                "mergeable_state": report.mergeable_state,
                "checks_state": report.checks_state,
                "conflicts": report.conflicts,
                "policies_satisfied": report.policies_satisfied,
            }
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
        try:
            result = await toolkit.pr_complete(
                inputs.repo,
                inputs.pr_number,
                strategy=inputs.merge_strategy,
                expected_head=inputs.expected_head,
                expected_base=inputs.expected_base,
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
                error_kind="needs_human.merge_not_confirmed",
                message="merge completion did not confirm a merged PR",
            )
        return Success(
            value={
                "merged": result.merged,
                "merge_sha": result.merge_sha,
                "strategy": result.strategy,
            }
        )

    return verbs


def build_agent_registry() -> AgentRegistry:
    reg = AgentRegistry()
    reg.register(LEAF_REVIEWER)
    reg.register(COMMENT_SYNTHESIZER)
    reg.register(COMMENT_ADDRESSER)
    return reg


def build_workflow() -> Workflow:
    return (
        WorkflowBuilder("leaf-lifecycle", module=MODULE, version="0.1")
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
            .edge("check_tests_passed", on="permanent_failure", to="needs_human_end")
        .script("prepare_review", verb="prepare_review", retry_max=2)
            .edge("prepare_review", on="success", to="review_leaf")
            .edge("prepare_review", on="retry_exhausted", to="needs_human_end")
            .edge("prepare_review", on="permanent_failure", to="needs_human_end")
        .agent("review_leaf", agent="leaf_reviewer", prompt_verb="review_prompt")
            .edge("review_leaf", on="success", to="dispatch_review")
            .edge("review_leaf", on="bad_output", to="needs_human_end")
            .edge("review_leaf", on="permanent_failure", to="needs_human_end")
        .script("dispatch_review", verb="dispatch_review")
            .edge("dispatch_review", on="success", to="synthesize_comments")
            .edge("dispatch_review", on="permanent_failure:review.approved", to="prune_context_pack")
            .edge("dispatch_review", on="permanent_failure", to="needs_human_end")
        .agent("synthesize_comments", agent="comment_synthesizer", prompt_verb="synth_prompt")
            .edge("synthesize_comments", on="success", to="address_comments")
            .edge("synthesize_comments", on="bad_output", to="needs_human_end")
            .edge("synthesize_comments", on="permanent_failure", to="needs_human_end")
        .agent("address_comments", agent="comment_addresser", prompt_verb="address_prompt")
            .edge("address_comments", on="success", to="apply_addressal")
            .edge("address_comments", on="bad_output", to="needs_human_end")
            .edge("address_comments", on="permanent_failure", to="needs_human_end")
        .script("apply_addressal", verb="apply_addressal")
            .edge("apply_addressal", on="success", to="push_addressal")
            .edge("apply_addressal", on="permanent_failure", to="needs_human_end")
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
            .edge("check_can_merge", on="retry_exhausted", to="needs_human_end")
            .edge("check_can_merge", on="permanent_failure", to="needs_human_end")
        .script("merge_pr", verb="merge_pr", retry_max=2)
            .edge("merge_pr", on="success", to="end_merged")
            .edge("merge_pr", on="retry_exhausted", to="needs_human_end")
            .edge("merge_pr", on="permanent_failure", to="needs_human_end")
        .terminate("end_merged", disposition="completed")
        .terminate("end_already_merged", disposition="completed")
        .terminate("needs_human_end", disposition="failed")
        .humanize({
            "start": "Starting leaf PR lifecycle",
            "fetch_pr": "Fetched leaf PR",
            "check_initial_state": "Checked leaf PR state",
            "assert_leaf_scope": "Verified leaf PR scope",
            "check_tests_passed": "Verified leaf test precondition",
            "prepare_review": "Prepared local leaf review context",
            "review_leaf": "Reviewed leaf PR",
            "dispatch_review": "Dispatched review verdict",
            "synthesize_comments": "Synthesised reviewer findings",
            "address_comments": "Addressed reviewer findings",
            "apply_addressal": "Applied addressal file changes",
            "push_addressal": "Pushed addressal commits",
            "check_progress": "Checked review-loop progress",
            "prune_context_pack": "Pruned requiem context-pack scaffold before merge",
            "check_can_merge": "Checked leaf mergeability",
            "merge_pr": "Merged leaf PR",
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
        verbs=build_verb_registry(inputs, toolkit),
        agents=build_agent_registry(),
        provider=provider,
        toolbelt=tb,
        log_dir=log_dir,
        gate_handler=gate_handler,
    )


def build_result(completed: dict[str, dict[str, Any]]) -> LeafLifecycleResult:
    fetch = (completed.get("fetch_pr") or {}).get("value") or {}
    merge = (completed.get("merge_pr") or {}).get("value") or {}
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
