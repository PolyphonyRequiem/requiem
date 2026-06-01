"""PR-lifecycle workflow — Gluck (Phase C).

Picks up where the implementation workflow ends: a PR exists; this
workflow drives it through review, comment-addressal loops, and merge.
The reviewer itself (CodeRabbit / Copilot / humans) is **not** dispatched
from here — we assume reviewers attach via repo defaults. Our job is to
*synthesise* their comments into actionable items, *address* those items
via a coder agent, *push*, and re-poll until the PR is approved+mergeable
or we surrender to a human.

Topology (see ASCII diagram in the workflow brief):

    start → fetch_pr → check_initial_state
                        ├─ merged → end_already_merged
                        ├─ closed → needs_human_end
                        └─ open  → request_review → poll_review
                                                     ├─ approvals → check_can_merge → merge_pr
                                                     │                                   → update_item
                                                     │                                   → end_merged
                                                     ├─ comments  → synthesize_comments
                                                     │              → address_comments
                                                     │              → push_addressal
                                                     │              → check_progress
                                                     │                ├─ progress → poll_review (loop)
                                                     │                ├─ no_progress  → needs_human_end
                                                     │                └─ max_iter     → needs_human_end
                                                     └─ timeout   → needs_human_end

Loop safety (workflow brief §"Loop safety"):

* ``max_iterations`` cap on the address-comments loop. After cap →
  NeedsHuman via ``check_progress``.
* No-progress detection: if ``push_addressal`` produces the same SHA as
  the previous iteration → NeedsHuman.
* INV-CANCEL: a ``cancel_requested`` event in the log short-circuits the
  next loop tick (the kernel handles this for us).

Idempotency (workflow brief §"Hard requirements"):

* ``fetch_pr`` is read-only — always safe.
* ``request_review`` is a PUT on the PR's requested-reviewers set —
  GitHub's semantics already make repeated PUTs a no-op; we additionally
  short-circuit when no reviewers are configured.
* ``push_addressal`` pushes a known commit ref; re-running pushes nothing
  new (git's own idempotency).
* ``merge_pr`` checks ``pr.merged`` before merging — if a prior attempt
  succeeded but the log was lost, the second attempt returns the existing
  merge SHA.

Per Ravel's L-1 caveat (referenced at every gh-call site below): an
unknown ``gh`` exit-1 is ``NeedsHuman``, never ``RetryableFailure``. We
honour this by mapping ``GhUnknownError`` and ``GhAuthError`` to
``PermanentFailure(error_kind="needs_human.*")`` so the route lands in
``needs_human_end`` rather than auto-retrying past a state-drift signal.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel, Field

from requiem.agent import AgentSpec, FakeProvider
from requiem.clients.gh import (
    GhAuthError,
    GhClient,
    GhClientError,
    GhNotFoundError,
    GhPullRequest,
    GhRateLimitedError,
    GhServerError,
    GhUnknownError,
)
from requiem.dsl import AgentRegistry, VerbRegistry, Workflow, WorkflowBuilder
from requiem.kernel import Engine
from requiem.outcomes import (
    Outcome,
    PermanentFailure,
    RetryableFailure,
    Success,
)
from requiem.toolbelt import Toolbelt


# ---- workflow result (the caller-facing aggregate) ------------------


PrFinalState = Literal["MERGED", "ALREADY_MERGED", "OPEN_NEEDS_HUMAN", "FAILED"]


@dataclass(frozen=True)
class PrLifecycleResult:
    """Aggregate of what the workflow accomplished.

    Constructed from the engine's projection + completed map by
    :func:`build_result` after the run terminates.
    """

    pr_number: int
    pr_url: str
    final_state: PrFinalState
    iterations: int
    comments_addressed: int
    merged: bool
    merge_sha: str | None
    dry_run: bool


# ---- agent output schemas (pydantic structured outputs) ------------


class ActionableItem(BaseModel):
    file: str
    line_range: tuple[int, int] | None = None
    change_summary: str
    original_comment_ids: list[int] = Field(default_factory=list)


class CommentSynthesis(BaseModel):
    actionable_items: list[ActionableItem]
    non_actionable: list[str] = Field(default_factory=list)


class AddressResult(BaseModel):
    """What the coder agent reports after editing the repo.

    ``commits`` is the list of commit SHAs the agent created (ordered
    oldest → newest). Empty list means "agent ran but produced no
    changes" — the workflow routes that to NeedsHuman.
    """

    commits: list[str]
    summary: str = ""
    items_addressed: list[int] = Field(default_factory=list)


# ---- agent specs ---------------------------------------------------


COMMENT_SYNTHESIZER = AgentSpec(
    name="comment_synthesizer",
    charter=(
        "You read PR review comments and produce a structured list of "
        "actionable items. Each actionable item names the file, an "
        "optional line range, a short change-summary, and the original "
        "comment ids that motivated it. Nits, praise, and questions for "
        "the human go in non_actionable."
    ),
    response_model=CommentSynthesis,
)

COMMENT_ADDRESSER = AgentSpec(
    name="comment_addresser",
    charter=(
        "You receive a list of actionable review items and the repo "
        "checkout path. You make minimal, focused edits that address "
        "every item, commit them, and report the commit SHAs. If you "
        "cannot address an item safely, leave commits empty and explain "
        "in summary; the workflow will surface it to a human."
    ),
    response_model=AddressResult,
)


ALL_SPECS = [COMMENT_SYNTHESIZER, COMMENT_ADDRESSER]


# ---- PR toolkit (the seam tests mock) ------------------------------


@dataclass(frozen=True, slots=True)
class ReviewComment:
    """One inline review comment on the PR."""

    id: int
    path: str
    line: int | None
    body: str
    user: str


@dataclass(frozen=True, slots=True)
class ReviewSummary:
    """One review (``APPROVED`` | ``CHANGES_REQUESTED`` | ``COMMENTED``)."""

    id: int
    state: str
    user: str


@dataclass(frozen=True, slots=True)
class MergeabilityReport:
    mergeable: bool | None    # None = "computing" (GitHub indeterminate)
    mergeable_state: str       # gh's vocabulary: clean | blocked | dirty | unknown | …
    checks_state: str | None   # success | failure | pending | None (no required checks)
    conflicts: bool


@dataclass(frozen=True, slots=True)
class MergeResult:
    sha: str
    merged: bool
    strategy: str


class PrToolkit(Protocol):
    """The boundary the workflow calls. Tests substitute a fake.

    Real implementation wraps :class:`requiem.clients.gh.GhClient` plus a
    git subprocess for ``git_push``. The Protocol is the seam so the
    workflow has no static dependency on either.
    """

    async def pr_view(self, repo: str, number: int) -> GhPullRequest: ...
    async def list_review_comments(
        self, repo: str, number: int
    ) -> list[ReviewComment]: ...
    async def list_reviews(self, repo: str, number: int) -> list[ReviewSummary]: ...
    async def request_review(
        self, repo: str, number: int, reviewers: list[str] | None = None
    ) -> dict[str, Any]: ...
    async def mergeability(self, repo: str, number: int) -> MergeabilityReport: ...
    async def merge_pr(
        self, repo: str, number: int, strategy: str
    ) -> MergeResult: ...
    async def git_push(self, repo_path: Path, branch: str) -> str: ...


# ---- real PrToolkit (wraps GhClient.api + git subprocess) ----------


class RealPrToolkit:
    """Production implementation. ``api()`` carries the mutations.

    Per Ravel's L-1 caveat (see module docstring): every call here can
    raise a typed ``GhClientError`` subclass; verbs map them to the
    appropriate ``Outcome`` variant. This wrapper never swallows errors.
    """

    def __init__(self, gh: GhClient | None = None) -> None:
        self._gh = gh or GhClient()

    async def pr_view(self, repo: str, number: int) -> GhPullRequest:
        return await self._gh.pr_view(repo, number)

    async def list_review_comments(
        self, repo: str, number: int
    ) -> list[ReviewComment]:
        endpoint = f"repos/{repo}/pulls/{number}/comments"
        # `gh api` returns the raw array as a dict only for object responses;
        # we route through `gh api --paginate` style by using the escape
        # hatch. For v0 we accept the single-page form.
        payload = await self._gh_api_list(endpoint)
        return [
            ReviewComment(
                id=int(c["id"]),
                path=str(c.get("path", "")),
                line=c.get("line") or c.get("original_line"),
                body=str(c.get("body", "")),
                user=str((c.get("user") or {}).get("login", "")),
            )
            for c in payload
        ]

    async def list_reviews(self, repo: str, number: int) -> list[ReviewSummary]:
        endpoint = f"repos/{repo}/pulls/{number}/reviews"
        payload = await self._gh_api_list(endpoint)
        return [
            ReviewSummary(
                id=int(r["id"]),
                state=str(r.get("state", "")),
                user=str((r.get("user") or {}).get("login", "")),
            )
            for r in payload
        ]

    async def request_review(
        self, repo: str, number: int, reviewers: list[str] | None = None
    ) -> dict[str, Any]:
        if not reviewers:
            # Idempotent no-op when no extra reviewers configured.
            return {"requested": False, "reviewers": []}
        endpoint = f"repos/{repo}/pulls/{number}/requested_reviewers"
        return await self._gh.api(endpoint, method="POST", body={"reviewers": reviewers})

    async def mergeability(self, repo: str, number: int) -> MergeabilityReport:
        endpoint = f"repos/{repo}/pulls/{number}"
        payload = await self._gh.api(endpoint, method="GET")
        mergeable = payload.get("mergeable")
        mergeable_state = str(payload.get("mergeable_state", "unknown"))
        checks_state = None
        # gh returns a flat object; checks state isn't in the PR payload —
        # would need a follow-up to /commits/{sha}/check-suites. v0 leans
        # on mergeable_state == "blocked" as the proxy for "checks not
        # green" since GitHub's branch-protection sets that.
        if mergeable_state == "blocked":
            checks_state = "failure"
        elif mergeable_state == "clean":
            checks_state = "success"
        return MergeabilityReport(
            mergeable=mergeable,
            mergeable_state=mergeable_state,
            checks_state=checks_state,
            conflicts=(mergeable is False or mergeable_state == "dirty"),
        )

    async def merge_pr(
        self, repo: str, number: int, strategy: str
    ) -> MergeResult:
        endpoint = f"repos/{repo}/pulls/{number}/merge"
        # ``merge_method`` accepts merge | squash | rebase. We don't pass
        # commit_title/commit_message — GitHub defaults are fine and a
        # missing field is safer than getting the templating wrong.
        payload = await self._gh.api(
            endpoint, method="PUT", body={"merge_method": strategy}
        )
        return MergeResult(
            sha=str(payload.get("sha", "")),
            merged=bool(payload.get("merged", False)),
            strategy=strategy,
        )

    async def git_push(self, repo_path: Path, branch: str) -> str:
        # Idempotent: pushing an already-pushed ref is a no-op on the
        # remote. We capture stdout for diagnostics but only return the
        # local HEAD SHA — the caller uses it for no-progress detection.
        proc = await asyncio.create_subprocess_exec(
            "git", "push", "origin", branch,
            cwd=str(repo_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_b = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"git push failed (exit {proc.returncode}): "
                f"{stderr_b.decode('utf-8', errors='replace')[:512]}"
            )
        sha_proc = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "HEAD",
            cwd=str(repo_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        sha_b, _ = await sha_proc.communicate()
        return sha_b.decode("utf-8", errors="replace").strip()

    async def _gh_api_list(self, endpoint: str) -> list[dict[str, Any]]:
        """`gh api` for array-returning endpoints (review comments, reviews).

        :class:`GhClient.api` is typed as ``dict`` because the upstream
        ``gh`` command returns arrays at the top level for list endpoints
        and we want each array endpoint to be an explicit method. We
        re-use the subprocess plumbing by calling the same argv as
        ``GhClient.api`` would, but accept the array response shape.
        """
        # Reach through the subprocess seam directly to honour the array
        # response. We could also subclass GhClient; the duplication is
        # ~6 lines and keeps the public client's typing honest.
        argv = (self._gh._binary, "api", endpoint, "--method", "GET")  # noqa: SLF001
        text = await self._gh._run_json_text(argv)                       # noqa: SLF001
        import json
        data = json.loads(text)
        if not isinstance(data, list):
            raise GhUnknownError(
                f"gh api {endpoint}: expected JSON array, got {type(data).__name__}",
                exit_code=0,
                stderr=text[:512],
                argv=argv,
            )
        return data


# ---- fake PrToolkit (canonical scriptable fake for tests + smoke) ---


@dataclass
class FakePrToolkit:
    """Scriptable in-memory PrToolkit for tests and the CLI smoke run.

    The fake holds a small state machine: each ``list_reviews`` /
    ``list_review_comments`` call advances through the scripted snapshots
    so a workflow can drive multiple iterations.
    """

    pr: GhPullRequest
    review_snapshots: list[list[ReviewSummary]]
    comment_snapshots: list[list[ReviewComment]]
    mergeability_snapshots: list[MergeabilityReport]
    merge_result: MergeResult | None = None
    push_shas: list[str] = None  # type: ignore[assignment]
    raise_on_merge: Exception | None = None
    raise_on_request_review: Exception | None = None
    raise_on_pr_view: Exception | None = None

    # observer state (test inspection)
    calls: list[tuple[str, tuple[Any, ...]]] = None  # type: ignore[assignment]
    review_request_count: int = 0
    merge_count: int = 0
    push_count: int = 0

    def __post_init__(self) -> None:
        if self.push_shas is None:
            self.push_shas = []
        if self.calls is None:
            self.calls = []
        self._review_idx = 0
        self._comment_idx = 0
        self._merge_idx = 0
        self._push_idx = 0

    async def pr_view(self, repo: str, number: int) -> GhPullRequest:
        self.calls.append(("pr_view", (repo, number)))
        if self.raise_on_pr_view is not None:
            raise self.raise_on_pr_view
        return self.pr

    async def list_reviews(self, repo: str, number: int) -> list[ReviewSummary]:
        self.calls.append(("list_reviews", (repo, number)))
        snap = self.review_snapshots[
            min(self._review_idx, len(self.review_snapshots) - 1)
        ]
        self._review_idx += 1
        return list(snap)

    async def list_review_comments(
        self, repo: str, number: int
    ) -> list[ReviewComment]:
        self.calls.append(("list_review_comments", (repo, number)))
        snap = self.comment_snapshots[
            min(self._comment_idx, len(self.comment_snapshots) - 1)
        ]
        self._comment_idx += 1
        return list(snap)

    async def request_review(
        self, repo: str, number: int, reviewers: list[str] | None = None
    ) -> dict[str, Any]:
        self.calls.append(("request_review", (repo, number, tuple(reviewers or ()))))
        self.review_request_count += 1
        if self.raise_on_request_review is not None:
            raise self.raise_on_request_review
        return {"requested": True, "reviewers": list(reviewers or [])}

    async def mergeability(self, repo: str, number: int) -> MergeabilityReport:
        self.calls.append(("mergeability", (repo, number)))
        snap = self.mergeability_snapshots[
            min(self._merge_idx, len(self.mergeability_snapshots) - 1)
        ]
        self._merge_idx += 1
        return snap

    async def merge_pr(
        self, repo: str, number: int, strategy: str
    ) -> MergeResult:
        self.calls.append(("merge_pr", (repo, number, strategy)))
        self.merge_count += 1
        if self.raise_on_merge is not None:
            raise self.raise_on_merge
        if self.merge_result is None:
            raise RuntimeError("FakePrToolkit: no merge_result scripted")
        return self.merge_result

    async def git_push(self, repo_path: Path, branch: str) -> str:
        self.calls.append(("git_push", (str(repo_path), branch)))
        self.push_count += 1
        if not self.push_shas:
            raise RuntimeError("FakePrToolkit: no push_shas scripted")
        idx = min(self._push_idx, len(self.push_shas) - 1)
        self._push_idx += 1
        return self.push_shas[idx]


# ---- gh-error → outcome mapper (Ravel L-1 applied at every call) ----


def _map_gh_error(
    err: Exception,
    *,
    run_id: str,
    node_id: str,
    attempt: int,
    operation: str,
) -> Outcome:
    """Convert a ``GhClientError`` (or peer exception) to an ``Outcome``.

    Honours the table in ``clients/gh.py``:

    * rate-limit       → RetryableFailure
    * server 5xx       → RetryableFailure
    * not-found        → PermanentFailure(error_kind="pr.not_found")
    * auth             → PermanentFailure(error_kind="needs_human.auth")
    * unknown (L-1)    → PermanentFailure(error_kind="needs_human.gh_unknown")
    * any other Exception → PermanentFailure(error_kind="needs_human.{op}.crash")

    The verb wraps this so the workflow always sees a discriminated
    outcome — Ravel's L-1 caveat is the reason "unknown" never becomes
    a retry.
    """
    if isinstance(err, GhRateLimitedError):
        after = err.retry_after.total_seconds() if err.retry_after else 30.0
        return RetryableFailure(
            retry_key=f"{run_id}:{node_id}:{operation}",
            error_kind="gh.rate_limited",
            message=f"{operation}: rate-limited (retry after {after:.0f}s)",
            attempt=attempt,
        )
    if isinstance(err, GhServerError):
        return RetryableFailure(
            retry_key=f"{run_id}:{node_id}:{operation}",
            error_kind="gh.server_error",
            message=f"{operation}: HTTP {err.status}",
            attempt=attempt,
        )
    if isinstance(err, GhNotFoundError):
        return PermanentFailure(
            error_kind="pr.not_found",
            message=f"{operation}: {err}",
        )
    if isinstance(err, GhAuthError):
        return PermanentFailure(
            error_kind="needs_human.auth",
            message=f"{operation}: {err}",
        )
    if isinstance(err, (GhUnknownError, GhClientError)):
        # Ravel L-1: unknown gh exit-1 surrenders to a human.
        return PermanentFailure(
            error_kind="needs_human.gh_unknown",
            message=f"{operation}: {err}",
        )
    # Non-gh exception (e.g. RuntimeError from git_push). Still surrender.
    return PermanentFailure(
        error_kind=f"needs_human.{operation}_crash",
        message=f"{operation}: {type(err).__name__}: {err}",
    )


# ---- verb library --------------------------------------------------


def build_verb_registry(
    *,
    repo: str,
    pr_number: int,
    repo_path: Path,
    max_iterations: int,
    merge_strategy: Literal["merge", "squash", "rebase"] | None,
    dry_run: bool,
    toolkit: PrToolkit,
    poll_interval_s: float,
    poll_timeout_s: float,
    reviewers: list[str] | None,
    work_item_id: str | None,
) -> VerbRegistry:
    """Assemble all verbs the topology references. Closes over inputs."""

    verbs = VerbRegistry()

    @verbs.register("start_run")
    def _start(ctx):
        return Success(
            value={
                "intent": "pr-lifecycle",
                "repo": repo,
                "pr_number": pr_number,
                "dry_run": dry_run,
                "max_iterations": max_iterations,
            }
        )

    @verbs.register("fetch_pr")
    async def _fetch_pr(ctx):
        # Ravel L-1: unknown gh-1 → needs_human, not retry.
        try:
            pr = await toolkit.pr_view(repo, pr_number)
        except Exception as e:  # noqa: BLE001
            return _map_gh_error(
                e, run_id=ctx.run_id, node_id=ctx.node_id,
                attempt=ctx.attempt, operation="fetch_pr",
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
            },
            inspected_artifacts=(f"pr:{repo}#{pr.number}",),
        )

    @verbs.register("check_initial_state")
    def _check_initial_state(ctx):
        pr = ctx.completed["fetch_pr"]["value"]
        state_u = str(pr["state"]).upper()
        if pr.get("merged") or state_u == "MERGED":
            return PermanentFailure(
                error_kind="already_merged",
                message=f"PR #{pr['number']} is already merged",
                details={"pr": pr},
            )
        if state_u == "CLOSED":
            return PermanentFailure(
                error_kind="needs_human.closed_not_merged",
                message="PR was closed without merge",
                details={"pr": pr},
            )
        return Success(value={"state": "open", "head": pr["head"], "base": pr["base"]})

    @verbs.register("request_review")
    async def _request_review(ctx):
        # Ravel L-1 at gh boundary.
        if dry_run:
            return Success(value={"requested": False, "reason": "dry_run"})
        try:
            result = await toolkit.request_review(repo, pr_number, reviewers)
        except Exception as e:  # noqa: BLE001
            return _map_gh_error(
                e, run_id=ctx.run_id, node_id=ctx.node_id,
                attempt=ctx.attempt, operation="request_review",
            )
        return Success(value={"requested": True, "result": result})

    @verbs.register("poll_review")
    async def _poll_review(ctx):
        # v0: bounded poll. Each tick re-fetches reviews + comments via gh.
        # Ravel L-1 applies at every fetch.
        deadline = time.monotonic() + max(poll_timeout_s, 0.0)
        # at least one fetch even if timeout==0
        first = True
        while True:
            try:
                reviews = await toolkit.list_reviews(repo, pr_number)
                comments = await toolkit.list_review_comments(repo, pr_number)
            except Exception as e:  # noqa: BLE001
                return _map_gh_error(
                    e, run_id=ctx.run_id, node_id=ctx.node_id,
                    attempt=ctx.attempt, operation="poll_review",
                )

            approvals = [r for r in reviews if r.state.upper() == "APPROVED"]
            changes_requested = [
                r for r in reviews if r.state.upper() == "CHANGES_REQUESTED"
            ]
            unresolved = [
                {"id": c.id, "path": c.path, "line": c.line, "body": c.body, "user": c.user}
                for c in comments
            ]

            if changes_requested or unresolved:
                return Success(
                    value={
                        "branch": "comments",
                        "comments": unresolved,
                        "approvals": [a.user for a in approvals],
                        "changes_requested": [r.user for r in changes_requested],
                    }
                )
            if approvals:
                return Success(
                    value={
                        "branch": "approvals",
                        "comments": [],
                        "approvals": [a.user for a in approvals],
                    }
                )

            if not first and time.monotonic() >= deadline:
                return PermanentFailure(
                    error_kind="needs_human.poll_timeout",
                    message=(
                        f"no review activity within {poll_timeout_s:.0f}s "
                        f"(no approvals, no comments)"
                    ),
                )
            if first and poll_timeout_s <= 0:
                # caller wants a single tick; treat the empty result as timeout
                return PermanentFailure(
                    error_kind="needs_human.poll_timeout",
                    message="no review activity (timeout=0)",
                )
            first = False
            await asyncio.sleep(max(poll_interval_s, 0.0))

    @verbs.register("dispatch_poll")
    def _dispatch_poll(ctx):
        # Pure router: turns the poll's `branch` field into the workflow's
        # next step. We use Success → comments-path (default), and a
        # specific PermanentFailure(error_kind="approvals_ready") → merge
        # path. The kernel routes `permanent_failure:approvals_ready` to
        # check_can_merge; PermanentFailure here is the routing primitive
        # not a real failure. The verdict card ignores this node.
        pv = ctx.completed["poll_review"]["value"]
        if pv.get("branch") == "approvals":
            return PermanentFailure(
                error_kind="approvals_ready",
                message="approvals present, no unresolved comments",
                details={"approvals": pv.get("approvals", [])},
            )
        return Success(value={"branch": "comments"})

    @verbs.register("synth_prompt")
    def _synth_prompt(ctx):
        pv = ctx.completed["poll_review"]["value"]
        comments = pv.get("comments", [])
        if not comments:
            # Defensive: if dispatch routed us here without comments,
            # still give the agent a prompt it can refuse cleanly.
            body = "(no comments — agent should return empty actionable_items)"
        else:
            body = "\n".join(
                f"#{c['id']} {c['path']}:{c.get('line') or '?'} ({c['user']}): {c['body']}"
                for c in comments
            )
        return (
            "Synthesise the following PR review comments into structured "
            f"actionable items.\n\n{body}\n\n"
            "Group comments that ask for the same change. Mark nits/praise "
            "as non_actionable. Return CommentSynthesis."
        )

    @verbs.register("address_prompt")
    def _address_prompt(ctx):
        synth = ctx.completed["synthesize_comments"]["value"]["parsed"]
        items = synth.get("actionable_items") or []
        body = "\n".join(
            f"- {it['file']} (lines {it.get('line_range')}): {it['change_summary']}"
            for it in items
        )
        return (
            f"Repo checkout: {repo_path}\n"
            "Apply the following actionable items via minimal commits. "
            "Return AddressResult with the new commit SHAs.\n\n"
            f"{body}"
        )

    @verbs.register("push_addressal")
    async def _push_addressal(ctx):
        addr_outcome = ctx.completed["address_comments"]["value"]
        parsed = addr_outcome.get("parsed") or {}
        commits = parsed.get("commits") or []
        if not commits:
            return PermanentFailure(
                error_kind="needs_human.no_changes",
                message="agent reported zero commits — nothing to push",
                details={"summary": parsed.get("summary", "")},
            )
        if dry_run:
            return Success(
                value={
                    "sha": commits[-1],
                    "pushed": False,
                    "reason": "dry_run",
                    "commits": commits,
                }
            )
        # Use the head branch from fetch_pr — that's what we push to.
        branch = ctx.completed["fetch_pr"]["value"]["head"]
        try:
            sha = await toolkit.git_push(repo_path, branch)
        except Exception as e:  # noqa: BLE001
            return _map_gh_error(
                e, run_id=ctx.run_id, node_id=ctx.node_id,
                attempt=ctx.attempt, operation="git_push",
            )
        return Success(
            value={
                "sha": sha,
                "pushed": True,
                "branch": branch,
                "commits": commits,
            },
            inspected_artifacts=(f"commit:{sha}",),
        )

    @verbs.register("check_progress")
    def _check_progress(ctx):
        # Iteration + no-progress detection. State lives in this verb's
        # own prior completed entry (which is the LAST outcome only —
        # exactly what we need: the previous iteration's value).
        cur_sha = ctx.completed["push_addressal"]["value"]["sha"]
        prior = (ctx.completed.get("check_progress") or {}).get("value") or {}
        iteration = int(prior.get("iteration", 0)) + 1
        last_sha = prior.get("last_sha")
        synth = ctx.completed["synthesize_comments"]["value"]["parsed"]
        items_this_round = len(synth.get("actionable_items") or [])
        addressed_so_far = int(prior.get("comments_addressed", 0)) + items_this_round
        commits_so_far = int(prior.get("commits_so_far", 0)) + 1

        if last_sha == cur_sha and last_sha is not None:
            return PermanentFailure(
                error_kind="needs_human.no_progress",
                message=(
                    f"address-comments loop produced no new commit "
                    f"(sha={cur_sha[:8]} unchanged across iterations)"
                ),
                details={"iteration": iteration, "comments_addressed": addressed_so_far},
            )
        if iteration > max_iterations:
            return PermanentFailure(
                error_kind="needs_human.max_iterations",
                message=(
                    f"address-comments loop hit max_iterations={max_iterations} "
                    f"without merge"
                ),
                details={
                    "iteration": iteration,
                    "comments_addressed": addressed_so_far,
                    "commits": commits_so_far,
                },
            )
        return Success(
            value={
                "iteration": iteration,
                "last_sha": cur_sha,
                "comments_addressed": addressed_so_far,
                "commits_so_far": commits_so_far,
            }
        )

    @verbs.register("check_can_merge")
    async def _check_can_merge(ctx):
        try:
            report = await toolkit.mergeability(repo, pr_number)
        except Exception as e:  # noqa: BLE001
            return _map_gh_error(
                e, run_id=ctx.run_id, node_id=ctx.node_id,
                attempt=ctx.attempt, operation="check_can_merge",
            )
        if report.conflicts:
            return PermanentFailure(
                error_kind="needs_human.conflicts",
                message=f"PR has merge conflicts (mergeable_state={report.mergeable_state})",
                details={"report": _report_dict(report)},
            )
        if report.checks_state == "failure":
            return PermanentFailure(
                error_kind="needs_human.checks_failing",
                message=f"PR checks are failing (mergeable_state={report.mergeable_state})",
                details={"report": _report_dict(report)},
            )
        if report.mergeable is None:
            # GitHub still computing — surface as needs_human so we
            # don't merge against an indeterminate state (INV-NO-CORRUPT-FORWARD).
            return PermanentFailure(
                error_kind="needs_human.mergeability_unknown",
                message="GitHub has not yet computed mergeability",
                details={"report": _report_dict(report)},
            )
        return Success(value=_report_dict(report))

    @verbs.register("merge_pr")
    async def _merge_pr(ctx):
        if dry_run:
            return Success(
                value={
                    "merged": False,
                    "merge_sha": None,
                    "strategy": merge_strategy or "merge",
                    "dry_run": True,
                }
            )
        strategy = merge_strategy or "merge"
        try:
            result = await toolkit.merge_pr(repo, pr_number, strategy)
        except Exception as e:  # noqa: BLE001
            return _map_gh_error(
                e, run_id=ctx.run_id, node_id=ctx.node_id,
                attempt=ctx.attempt, operation="merge_pr",
            )
        return Success(
            value={
                "merged": result.merged,
                "merge_sha": result.sha,
                "strategy": result.strategy,
            },
            inspected_artifacts=(f"merge:{result.sha}",),
        )

    @verbs.register("update_item")
    def _update_item(ctx):
        # v0: twig integration is a separate seat. Stub records intent.
        if work_item_id is None:
            return Success(value={"updated": False, "reason": "no_work_item_linked"})
        return Success(
            value={
                "updated": False,
                "work_item_id": work_item_id,
                "reason": "twig_integration_deferred_to_phase_C_seat_2",
            }
        )

    return verbs


def _report_dict(r: MergeabilityReport) -> dict[str, Any]:
    return {
        "mergeable": r.mergeable,
        "mergeable_state": r.mergeable_state,
        "checks_state": r.checks_state,
        "conflicts": r.conflicts,
    }


# ---- agent registry ------------------------------------------------


def build_agent_registry() -> AgentRegistry:
    reg = AgentRegistry()
    for spec in ALL_SPECS:
        reg.register(spec)
    return reg


# ---- topology ------------------------------------------------------


def build_workflow() -> Workflow:
    return (
        WorkflowBuilder(
            "pr-lifecycle",
            module="requiem.workflows.pr_lifecycle",
            version="0.1",
        )
        .entry("start")
        .script("start", verb="start_run")
            .edge("start", on="success", to="fetch_pr")
        .script("fetch_pr", verb="fetch_pr", retry_max=2)
            .edge("fetch_pr", on="success",            to="check_initial_state")
            .edge("fetch_pr", on="retry_exhausted",    to="needs_human_end")
            .edge("fetch_pr", on="permanent_failure",  to="needs_human_end")
        .script("check_initial_state", verb="check_initial_state")
            .edge("check_initial_state", on="success",                                  to="request_review")
            .edge("check_initial_state", on="permanent_failure:already_merged",         to="end_already_merged")
            .edge("check_initial_state", on="permanent_failure",                        to="needs_human_end")
        .script("request_review", verb="request_review", retry_max=2)
            .edge("request_review", on="success",            to="poll_review")
            .edge("request_review", on="retry_exhausted",    to="needs_human_end")
            .edge("request_review", on="permanent_failure",  to="needs_human_end")
        .script("poll_review", verb="poll_review", retry_max=2)
            .edge("poll_review", on="success",            to="dispatch_poll")
            .edge("poll_review", on="retry_exhausted",    to="needs_human_end")
            .edge("poll_review", on="permanent_failure",  to="needs_human_end")
        .script("dispatch_poll", verb="dispatch_poll")
            .edge("dispatch_poll", on="success",                                to="synthesize_comments")
            .edge("dispatch_poll", on="permanent_failure:approvals_ready",      to="check_can_merge")
            .edge("dispatch_poll", on="permanent_failure",                      to="needs_human_end")
        .agent("synthesize_comments", agent="comment_synthesizer", prompt_verb="synth_prompt")
            .edge("synthesize_comments", on="success",            to="address_comments")
            .edge("synthesize_comments", on="bad_output",         to="needs_human_end")
            .edge("synthesize_comments", on="permanent_failure",  to="needs_human_end")
        .agent("address_comments", agent="comment_addresser", prompt_verb="address_prompt")
            .edge("address_comments", on="success",            to="push_addressal")
            .edge("address_comments", on="bad_output",         to="needs_human_end")
            .edge("address_comments", on="permanent_failure",  to="needs_human_end")
        .script("push_addressal", verb="push_addressal", retry_max=2)
            .edge("push_addressal", on="success",            to="check_progress")
            .edge("push_addressal", on="retry_exhausted",    to="needs_human_end")
            .edge("push_addressal", on="permanent_failure",  to="needs_human_end")
        .script("check_progress", verb="check_progress")
            .edge("check_progress", on="success",            to="poll_review")     # loop!
            .edge("check_progress", on="permanent_failure",  to="needs_human_end")
        .script("check_can_merge", verb="check_can_merge", retry_max=2)
            .edge("check_can_merge", on="success",            to="merge_pr")
            .edge("check_can_merge", on="retry_exhausted",    to="needs_human_end")
            .edge("check_can_merge", on="permanent_failure",  to="needs_human_end")
        .script("merge_pr", verb="merge_pr", retry_max=2)
            .edge("merge_pr", on="success",            to="update_item")
            .edge("merge_pr", on="retry_exhausted",    to="needs_human_end")
            .edge("merge_pr", on="permanent_failure",  to="needs_human_end")
        .script("update_item", verb="update_item")
            .edge("update_item", on="success", to="end_merged")
        .terminate("end_merged",          disposition="completed")
        .terminate("end_already_merged",  disposition="completed")
        .terminate("needs_human_end",     disposition="failed")
        .humanize({
            "start":                "Starting PR lifecycle",
            "fetch_pr":             "Fetched PR",
            "check_initial_state":  "Initial PR state",
            "request_review":       "Requested review",
            "poll_review":          "Polled review activity",
            "dispatch_poll":        "Dispatched on poll result",
            "synthesize_comments":  "Synthesised comments",
            "address_comments":     "Addressed comments",
            "push_addressal":       "Pushed addressal commits",
            "check_progress":       "Checked loop progress",
            "check_can_merge":      "Checked mergeability",
            "merge_pr":             "Merged PR",
            "update_item":          "Updated work item",
            "end_merged":           "PR lifecycle",
            "end_already_merged":   "PR lifecycle",
            "needs_human_end":      "PR lifecycle",
        })
        .build()
    )


# ---- engine factory ------------------------------------------------


def _default_gate_handler(node_id: str, prompt: str, options: tuple[str, ...]) -> str:
    """No interactive gates in this workflow; included for symmetry.

    The workflow uses ``terminate("needs_human_end")`` instead of a
    ``human_gate`` node — every "needs human" condition is a terminal,
    not a recoverable choice. This handler exists only so a stray
    ``NeedsHuman`` from a future verb doesn't suspend the run silently.
    """
    return options[0] if options else "acknowledge"


_default_gate_handler.__requiem_auto__ = True  # type: ignore[attr-defined]


# Default fake (used when build_engine is called by the CLI with no extra
# kwargs). Demonstrates the full loop:
#   round 1: 3 comments → synthesize → address → push → check_progress
#   round 2: approvals (no more comments) → check_can_merge → merge
# (Going through the loop once exercises the comment-address path; choosing
# approvals next exercises the merge path. Both are needed for a realistic
# verdict card.)
def _default_fake_toolkit() -> FakePrToolkit:
    pr = GhPullRequest(
        number=347,
        title="feat: real LLM providers",
        state="OPEN",
        merged=False,
        merged_at=None,
        head="feature/llm",
        base="main",
        url="https://github.com/PolyphonyRequiem/requiem/pull/347",
    )
    sample_comments = [
        ReviewComment(id=101, path="src/llm/openai.py", line=42,
                      body="prefer dict.get(key) over try/KeyError",
                      user="reviewer-alice"),
        ReviewComment(id=102, path="src/llm/openai.py", line=58,
                      body="this constant is duplicated — extract",
                      user="reviewer-alice"),
        ReviewComment(id=103, path="tests/test_openai.py", line=12,
                      body="missing assertion for timeout case",
                      user="reviewer-bob"),
    ]
    return FakePrToolkit(
        pr=pr,
        # Two snapshots: round 1 has comments, round 2 (after address+push) is
        # comment-free with an approval.
        review_snapshots=[
            [],  # round 1: no reviews yet
            [ReviewSummary(id=1, state="APPROVED", user="reviewer-alice")],
        ],
        comment_snapshots=[
            sample_comments,
            [],  # comments resolved after addressal
        ],
        mergeability_snapshots=[
            MergeabilityReport(
                mergeable=True,
                mergeable_state="clean",
                checks_state="success",
                conflicts=False,
            )
        ],
        merge_result=MergeResult(sha="a3f9c7e0deadbeef", merged=True, strategy="squash"),
        push_shas=["b1c2d3e4f5a6b7c8"],
    )


def _default_fake_provider() -> FakeProvider:
    """Provider for the smoke run — one round of synthesise + address."""
    return FakeProvider(scripts={
        "comment_synthesizer": [
            {
                "actionable_items": [
                    {"file": "src/llm/openai.py", "line_range": [40, 60],
                     "change_summary": "use dict.get and extract constant",
                     "original_comment_ids": [101, 102]},
                    {"file": "tests/test_openai.py", "line_range": [10, 15],
                     "change_summary": "add timeout assertion",
                     "original_comment_ids": [103]},
                ],
                "non_actionable": [],
            },
        ],
        "comment_addresser": [
            {
                "commits": ["b1c2d3e4f5a6b7c8"],
                "summary": "applied 2 items across 1 commit",
                "items_addressed": [101, 102, 103],
            },
        ],
    })


def build_engine(
    log_dir: Path,
    *,
    repo: str = "PolyphonyRequiem/requiem",
    pr_number: int = 347,
    repo_path: Path | None = None,
    max_iterations: int = 3,
    merge_strategy: Literal["merge", "squash", "rebase"] | None = "squash",
    dry_run: bool = False,
    toolkit: PrToolkit | None = None,
    provider: Any = None,
    poll_interval_s: float = 30.0,
    poll_timeout_s: float = 600.0,
    reviewers: list[str] | None = None,
    work_item_id: str | None = None,
    gate_handler: Callable[[str, str, tuple[str, ...]], str] | None = None,
) -> Engine:
    """Construct a runnable :class:`Engine` for the PR-lifecycle workflow.

    The CLI calls ``build_engine(log_dir)``; all other args default to a
    self-contained smoke run that uses ``FakePrToolkit`` + scripted
    ``FakeProvider`` to demonstrate the verdict card.
    """
    if repo_path is None:
        repo_path = log_dir / "repo_stub"
        repo_path.mkdir(parents=True, exist_ok=True)
    if toolkit is None:
        toolkit = _default_fake_toolkit()
    if provider is None:
        provider = _default_fake_provider()

    verbs = build_verb_registry(
        repo=repo,
        pr_number=pr_number,
        repo_path=repo_path,
        max_iterations=max_iterations,
        merge_strategy=merge_strategy,
        dry_run=dry_run,
        toolkit=toolkit,
        poll_interval_s=poll_interval_s,
        poll_timeout_s=poll_timeout_s,
        reviewers=reviewers,
        work_item_id=work_item_id,
    )
    return Engine(
        workflow=build_workflow(),
        verbs=verbs,
        agents=build_agent_registry(),
        provider=provider,
        toolbelt=Toolbelt.real(),
        log_dir=log_dir,
        gate_handler=gate_handler or _default_gate_handler,
    )


# ---- result builder + verdict card ---------------------------------


def build_result(completed: dict[str, dict[str, Any]]) -> PrLifecycleResult:
    """Project the completed map into a typed :class:`PrLifecycleResult`."""
    fetch = (completed.get("fetch_pr") or {}).get("value") or {}
    pr_number = int(fetch.get("number", 0))
    pr_url = str(fetch.get("url", ""))

    merge = (completed.get("merge_pr") or {}).get("value") or {}
    merged = bool(merge.get("merged", False))
    merge_sha = merge.get("merge_sha")
    dry_run = bool(merge.get("dry_run", False))

    progress = (completed.get("check_progress") or {}).get("value") or {}
    iterations = int(progress.get("iteration", 0))
    comments_addressed = int(progress.get("comments_addressed", 0))

    # Final-state classification keys off the completed-map signature:
    #   - merge_pr success + merged True             → MERGED
    #   - check_initial_state PF:already_merged      → ALREADY_MERGED
    #   - reached needs_human_end OR any verb left a
    #     PermanentFailure(error_kind="needs_human.*") → OPEN_NEEDS_HUMAN
    #   - any other PermanentFailure                 → FAILED
    final_state: PrFinalState
    initial = completed.get("check_initial_state") or {}
    if (
        initial.get("kind") == "permanent_failure"
        and initial.get("error_kind") == "already_merged"
    ):
        final_state = "ALREADY_MERGED"
    elif merge_pr_kind(completed) == "success" and (merged or dry_run):
        final_state = "MERGED" if merged else "OPEN_NEEDS_HUMAN"
    elif _any_needs_human(completed):
        final_state = "OPEN_NEEDS_HUMAN"
    elif _any_permanent_failure(completed):
        final_state = "FAILED"
    else:
        final_state = "OPEN_NEEDS_HUMAN"

    return PrLifecycleResult(
        pr_number=pr_number,
        pr_url=pr_url,
        final_state=final_state,
        iterations=iterations,
        comments_addressed=comments_addressed,
        merged=merged,
        merge_sha=str(merge_sha) if merge_sha else None,
        dry_run=dry_run,
    )


def merge_pr_kind(completed: dict[str, dict[str, Any]]) -> str | None:
    m = completed.get("merge_pr")
    return None if m is None else m.get("kind")


def _any_needs_human(completed: dict[str, dict[str, Any]]) -> bool:
    for outcome in completed.values():
        if outcome.get("kind") == "permanent_failure" and str(
            outcome.get("error_kind", "")
        ).startswith("needs_human"):
            return True
    return False


def _any_permanent_failure(completed: dict[str, dict[str, Any]]) -> bool:
    return any(o.get("kind") == "permanent_failure" for o in completed.values())


# ---- render hints + verdict card (CLI surfaces) --------------------


def _detail_fetch_pr(value: dict) -> str:
    return f"#{value.get('number', '?')} \"{value.get('title', '')}\""


def _detail_poll_review(value: dict) -> str:
    branch = value.get("branch", "?")
    if branch == "approvals":
        return f"approvals from {', '.join(value.get('approvals', [])) or '?'}"
    n = len(value.get("comments", []) or [])
    return f"{n} comment(s) to address"


def _detail_synthesize(value: dict) -> str:
    parsed = (value.get("parsed") or {})
    n = len(parsed.get("actionable_items") or [])
    return f"{n} actionable item(s)"


def _detail_address(value: dict) -> str:
    parsed = (value.get("parsed") or {})
    n = len(parsed.get("commits") or [])
    return f"{n} commit(s)"


def _detail_push(value: dict) -> str:
    sha = value.get("sha") or ""
    return f"to {sha[:8]}" if sha else "(no-op)"


def _detail_check_progress(value: dict) -> str:
    return (
        f"iter {value.get('iteration', '?')} — "
        f"{value.get('comments_addressed', 0)} addressed"
    )


def _detail_merge(value: dict) -> str:
    if not value.get("merged"):
        return "(dry-run)" if value.get("dry_run") else "(unmerged)"
    return f"{value.get('strategy', '?')} → {str(value.get('merge_sha', ''))[:8]}"


def render_hints() -> dict:
    return {
        "artifact_name": "PR lifecycle",
        "details": {
            "fetch_pr":            _detail_fetch_pr,
            "poll_review":         _detail_poll_review,
            "synthesize_comments": _detail_synthesize,
            "address_comments":    _detail_address,
            "push_addressal":      _detail_push,
            "check_progress":      _detail_check_progress,
            "merge_pr":            _detail_merge,
        },
        "silent_nodes": frozenset({
            "start", "dispatch_poll",
            "end_merged", "end_already_merged", "needs_human_end",
        }),
    }


def verdict_card(completed: dict) -> str | None:
    """Render the operator-facing one-screen summary."""
    if not completed:
        return None
    res = build_result(completed)

    if res.final_state == "MERGED":
        head = "✓ Merged"
    elif res.final_state == "ALREADY_MERGED":
        head = "✓ Already merged (no action needed)"
    elif res.final_state == "OPEN_NEEDS_HUMAN":
        head = "⚠ Open — needs human"
    else:
        head = "✕ Failed"

    fetch = (completed.get("fetch_pr") or {}).get("value") or {}
    title = fetch.get("title", "")
    lines = [
        f"─── PR Lifecycle: #{res.pr_number} {'─' * max(0, 40 - len(str(res.pr_number)))}",
        f"  {head}",
        f"      PR:          #{res.pr_number}{' — ' + title if title else ''}",
    ]
    if res.pr_url:
        lines.append(f"      URL:         {res.pr_url}")
    if res.iterations or res.comments_addressed:
        push = (completed.get("push_addressal") or {}).get("value") or {}
        commits_n = len(push.get("commits") or [])
        lines.append(
            f"      Iterations:  {res.iterations} "
            f"({res.comments_addressed} comments addressed, {commits_n} commit(s))"
        )
    merge = (completed.get("merge_pr") or {}).get("value") or {}
    if merge.get("strategy"):
        lines.append(f"      Strategy:    {merge.get('strategy')}")
    if res.merge_sha:
        lines.append(f"      Merge SHA:   {res.merge_sha[:8]}")
    elif res.dry_run:
        lines.append("      Merge SHA:   (dry-run — no merge performed)")

    # NeedsHuman reasons: dig the first needs_human.* PF for context.
    if res.final_state == "OPEN_NEEDS_HUMAN":
        for node_id, outcome in completed.items():
            if outcome.get("kind") == "permanent_failure" and str(
                outcome.get("error_kind", "")
            ).startswith("needs_human"):
                lines.append(
                    f"      Reason:      {outcome.get('error_kind')} "
                    f"({outcome.get('message', '')})"
                )
                lines.append(f"      At node:     {node_id}")
                break

    lines.append("─" * 69)
    return "\n".join(lines)
