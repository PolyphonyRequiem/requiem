"""GitHub CLI client — wraps the `gh` binary for read paths.

Phase B / Chopin (seat 2 of 8). Liszt B+C hybrid: a per-tool typed client
that confines `gh`-version coupling here and hands verbs a typed outcome /
typed-error contract. Verbs map the errors below to discriminated
`Outcome` variants; this module itself returns successes and raises typed
errors — it never speaks the verb vocabulary directly.

----------------------------------------------------------------------
Exit-code → error mapping (Ravel's L-1 caveat applies)
----------------------------------------------------------------------
Per ADR 0002 row for Liszt: **an unknown `gh exit 1` is `NeedsHuman`,
NEVER `RetryableFailure`** — silently retrying an unknown failure is
exactly the "corrupt-forward" mode INV-NO-CORRUPT-FORWARD forbids.

| exit | stderr / body shape          | error raised                | verb maps to                              |
|------|------------------------------|-----------------------------|-------------------------------------------|
| 0    | success                      | — (returns parsed value)    | Success                                   |
| 1    | "rate limit" / HTTP 429      | GhRateLimitedError          | RetryableFailure(after=retry_after)       |
| 1    | "Could not resolve" / HTTP 404 | GhNotFoundError           | PermanentFailure(error_kind="not_found")  |
| 1    | "authentication" / HTTP 401  | GhAuthError                 | NeedsHuman                                |
| 1    | HTTP 5xx                     | GhServerError(status)       | RetryableFailure(after=timedelta(s=30))   |
| 1    | (anything else)              | GhUnknownError              | NeedsHuman                                |
| ≥ 2  | any                          | GhUnknownError              | NeedsHuman                                |

`X-RateLimit-Reset` is parsed out of stderr when present (gh tends to
print it on `gh api` rate-limit surfaces). Value is a unix epoch second;
`retry_after` is computed as `max(0, reset - now)`.

----------------------------------------------------------------------
Scope (v0)
----------------------------------------------------------------------
Read-mostly paths only: `pr_view`, `pr_search`, and a low-level `api`
escape hatch. Mutations are deliberately narrow and enumerated:
`pr_create` (PR open), `pr_complete` (PR merge with live head/base
precondition guard), and — per ADR-0018, ratified 2026-06-07 — the
branch-ref pair `branch_sha` / `ensure_branch_ref` (remote
`feature/<root>` trunk bootstrap, because the local git client is
read-only). No broader mutation hatch exists; comments, labels, and
other repo writes remain out of scope.

`gh` JSON output requires explicit `--json` field listing. Each method
ships a per-method `_FIELDS` tuple; we never request `*`. This shields
us from gh upstream renaming/removing fields without a corresponding
client-side decision.

The client does NOT manage `gh auth` — that is `gh`'s job. Daniel's box
runs two accounts (`dangreen_microsoft` EMU, locked out of
`PolyphonyRequiem/*`; and `PolyphonyRequiem`, active). Verbs running
against `PolyphonyRequiem/*` will only succeed when the active account
matches; we surface `GhAuthError` on the failure path and stop there.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field  # noqa: F401  (back-compat re-export)
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Literal


# ---- typed value object ------------------------------------------------


from requiem.clients.repo import (  # noqa: F401  (re-exported for callers that want to type-annotate GhClient at the Protocol)
    MergeCapableRepoPlatform,
    RepoCompleteResult,
    RepoMergeStrategy,
    RepoMergeabilityReport,
    RepoPlatform,
    RepoPullRequest,
)


# Back-compat alias — older code expected ``GhClient.pr_view`` to return a
# ``GhPullRequest``. The platform-neutral ``RepoPullRequest`` (ADR-0024) has
# the same field set, so the alias preserves source compatibility without a
# rename across the codebase. New code should reach for ``RepoPullRequest``.
GhPullRequest = RepoPullRequest


# ---- typed errors ------------------------------------------------------


class GhClientError(Exception):
    """Base for every error this client raises.

    Carries `stderr`, `exit_code`, and `argv` so verbs that wrap us can
    emit receipts / event-log entries with full forensic context.
    """

    def __init__(
        self,
        message: str,
        *,
        exit_code: int = 1,
        stderr: str = "",
        argv: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr
        self.argv = argv


class GhRateLimitedError(GhClientError):
    """GitHub API rate-limited us.

    `retry_after` is parsed from `X-RateLimit-Reset` when present; None
    when gh's stderr didn't expose a reset time (the verb should then
    fall back to its configured default backoff).
    """

    def __init__(
        self,
        message: str,
        *,
        retry_after: timedelta | None,
        **kw: Any,
    ) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class GhNotFoundError(GhClientError):
    """404 / 'Could not resolve to a ...' — the resource does not exist."""


class GhAuthError(GhClientError):
    """401 / authentication failure — operator must re-auth or switch accounts."""


class GhServerError(GhClientError):
    """HTTP 5xx from GitHub. Verbs convert to RetryableFailure."""

    def __init__(self, message: str, *, status: int, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.status = status


class GhUnknownError(GhClientError):
    """Catch-all. Per Ravel's L-1 caveat, verbs map this to NeedsHuman."""


# ---- private classification helpers -----------------------------------


# `gh pr create` prints the new PR URL on the last non-empty stdout line.
# Format observed across `gh` versions: `https://github.com/<owner>/<repo>/pull/<n>`.
_PR_URL_NUM_RE = re.compile(r"/pull/(\d+)\b")


def _extract_pr_number(stdout: str) -> int | None:
    """Pull the PR number out of `gh pr create`'s stdout, if present.

    Scans every line bottom-up so a noisy "creating draft..." preamble
    doesn't trip us up; the URL is always the last meaningful token.
    """
    for line in reversed(stdout.splitlines()):
        m = _PR_URL_NUM_RE.search(line)
        if m:
            return int(m.group(1))
    return None


# `gh api` typically prints HTTP errors as `HTTP 404: Not Found (https://...)`
# or `gh: <body> (HTTP 404)`. We match either shape.
_HTTP_STATUS_RE = re.compile(r"HTTP\s+(\d{3})", re.IGNORECASE)
_RATE_LIMIT_RESET_RE = re.compile(
    r"X-RateLimit-Reset\s*[:=]\s*(\d+)", re.IGNORECASE
)
_RATE_LIMIT_HINTS = (
    "rate limit",
    "api rate limit exceeded",
    "secondary rate limit",
)
_NOT_FOUND_HINTS = (
    "could not resolve to a",
    "not found",
    "no such",
)
_AUTH_HINTS = (
    "authentication",
    "unauthorized",
    "bad credentials",
    "401",
    "gh auth login",
)


def _github_check_signal(status_payload: dict[str, Any], check_runs_payload: dict[str, Any]) -> str:
    """Collapse GitHub combined-status + check-runs into success|failure|pending|unknown."""
    statuses = status_payload.get("statuses")
    status_signal = "unknown"
    if isinstance(statuses, list) and statuses:
        state = str(status_payload.get("state", "")).lower()
        if state == "success":
            status_signal = "success"
        elif state == "pending":
            status_signal = "pending"
        elif state in {"failure", "error"}:
            status_signal = "failure"

    runs = check_runs_payload.get("check_runs")
    runs_signal = "unknown"
    if isinstance(runs, list) and runs:
        runs_signal = "success"
        for run in runs:
            status = str((run or {}).get("status", "")).lower()
            conclusion = str((run or {}).get("conclusion", "")).lower()
            if status != "completed":
                runs_signal = "pending"
                break
            if conclusion != "success":
                runs_signal = "failure"
                break

    signals = (status_signal, runs_signal)
    if "failure" in signals:
        return "failure"
    if "pending" in signals:
        return "pending"
    if "success" in signals:
        return "success"
    return "unknown"


def _extract_http_status(blob: str) -> int | None:
    m = _HTTP_STATUS_RE.search(blob)
    return int(m.group(1)) if m else None


def _extract_rate_limit_reset(blob: str) -> timedelta | None:
    m = _RATE_LIMIT_RESET_RE.search(blob)
    if not m:
        return None
    reset_epoch = int(m.group(1))
    delta = reset_epoch - int(time.time())
    return timedelta(seconds=max(0, delta))


def _classify(
    exit_code: int,
    stdout: str,
    stderr: str,
    argv: tuple[str, ...],
) -> GhClientError:
    """Map (exit_code, stderr/stdout) → typed error per the docstring table.

    The combined blob is checked because gh sometimes prints the failing
    HTTP body to stdout (when `gh api` succeeded at the wire level but
    the response is an error) and the human-readable hint to stderr.
    """
    blob = f"{stderr}\n{stdout}"
    blob_l = blob.lower()
    status = _extract_http_status(blob)
    kw: dict[str, Any] = {
        "exit_code": exit_code,
        "stderr": stderr,
        "argv": argv,
    }

    if exit_code >= 2:
        return GhUnknownError(
            f"gh exited {exit_code}: {stderr.strip() or stdout.strip() or '(no output)'}",
            **kw,
        )

    if status == 429 or any(h in blob_l for h in _RATE_LIMIT_HINTS):
        return GhRateLimitedError(
            "gh: rate-limited by GitHub",
            retry_after=_extract_rate_limit_reset(blob),
            **kw,
        )

    if status == 404 or any(h in blob_l for h in _NOT_FOUND_HINTS):
        return GhNotFoundError(
            f"gh: not found — {stderr.strip() or stdout.strip()}",
            **kw,
        )

    if status == 401 or any(h in blob_l for h in _AUTH_HINTS):
        return GhAuthError(
            f"gh: authentication failed — {stderr.strip() or stdout.strip()}",
            **kw,
        )

    if status is not None and 500 <= status < 600:
        return GhServerError(
            f"gh: GitHub server error HTTP {status}",
            status=status,
            **kw,
        )

    # Ravel's L-1 caveat: exit 1 with unrecognized stderr is NeedsHuman, not retryable.
    return GhUnknownError(
        f"gh exited 1 (unclassified): {stderr.strip() or stdout.strip() or '(no output)'}",
        **kw,
    )


# ---- the client -------------------------------------------------------


# Explicit field list per gh's `--json` contract. Listed in the order gh
# documents them for readability. Touching this list is a deliberate
# act — do not add `*`.
_PR_FIELDS: Final[tuple[str, ...]] = (
    "number",
    "title",
    "body",
    "state",
    "mergedAt",
    "mergeCommit",
    "headRefName",
    "baseRefName",
    "url",
)


def _parse_merged_at(value: Any) -> datetime | None:
    if not value:
        return None
    # gh emits RFC3339 with `Z` suffix; Python <3.11 parses with `+00:00`.
    s = str(value)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


_GH_STATE_TO_NEUTRAL = {
    "OPEN": "open",
    "CLOSED": "closed",
    "MERGED": "merged",
}


def _to_pr(payload: dict[str, Any]) -> GhPullRequest:
    raw_state = str(payload.get("state", "")).upper()
    # Normalise to ADR-0024's neutral vocabulary so the platform-neutral
    # RepoPullRequest contract holds across both GH and ADO impls.
    neutral_state = _GH_STATE_TO_NEUTRAL.get(raw_state, "open")
    return GhPullRequest(
        number=int(payload.get("number", 0)),
        title=str(payload.get("title", "")),
        state=neutral_state,
        merged_at=_parse_merged_at(payload.get("mergedAt")),
        head=str(payload.get("headRefName", "")),
        base=str(payload.get("baseRefName", "")),
        url=str(payload.get("url", "")),
        raw=dict(payload),
    )


class GhClient:
    """Async wrapper around the `gh` CLI. Read-only methods only in v0.

    Each call is a fresh subprocess — no caching, no session reuse. The
    seam is `asyncio.create_subprocess_exec` to match Twig's client and
    to keep us out of `cmd.exe` quoting on Windows.
    """

    def __init__(self, cwd: Path | None = None, *, binary: str = "gh") -> None:
        self._cwd = cwd
        self._binary = binary

    # ---- public API ----

    async def pr_view(self, repo: str, number: int) -> GhPullRequest:
        argv = (
            self._binary, "pr", "view", str(number),
            "--repo", repo,
            "--json", ",".join(_PR_FIELDS),
        )
        stdout = await self._run_json_text(argv)
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as e:
            raise GhUnknownError(
                f"gh pr view returned non-JSON: {e}",
                exit_code=0,
                stderr=stdout[:512],
                argv=argv,
            ) from e
        if not isinstance(payload, dict):
            raise GhUnknownError(
                f"gh pr view: expected JSON object, got {type(payload).__name__}",
                exit_code=0, stderr=stdout[:512], argv=argv,
            )
        return _to_pr(payload)

    async def pr_search(
        self, repo: str, query: str, limit: int = 30
    ) -> list[GhPullRequest]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        argv = (
            self._binary, "pr", "list",
            "--repo", repo,
            "--search", query,
            "--limit", str(limit),
            "--json", ",".join(_PR_FIELDS),
        )
        stdout = await self._run_json_text(argv)
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as e:
            raise GhUnknownError(
                f"gh pr list returned non-JSON: {e}",
                exit_code=0, stderr=stdout[:512], argv=argv,
            ) from e
        if not isinstance(payload, list):
            raise GhUnknownError(
                f"gh pr list: expected JSON array, got {type(payload).__name__}",
                exit_code=0, stderr=stdout[:512], argv=argv,
            )
        return [_to_pr(item) for item in payload]

    # ---- RepoPlatform Protocol surface (ADR-0024) ----
    #
    # These are the trunk-topology methods the platform-agnostic workflows
    # call. ``pr_view`` / ``pr_create`` / ``branch_sha`` / ``ensure_branch_ref``
    # are already shape-compatible above; this section adds the two methods
    # the Protocol requires that GhClient didn't ship yet
    # (``find_open_pr_for_branch`` and ``default_branch``).

    async def find_open_pr_for_branch(
        self, repo: str, *, head: str, limit: int = 30
    ) -> list[GhPullRequest]:
        """Find open PRs whose source branch is ``head``.

        Translates to ``gh pr list --search "head:<head> state:open"``.
        Bare branch names only — GitHub's search index doesn't accept
        ``refs/heads/`` prefixes. Mirrors the ADR-0024 contract.
        """
        return await self.pr_search(
            repo, query=f"head:{head} state:open", limit=limit
        )

    async def default_branch(self, repo: str) -> str:
        """Resolve the repo's default branch via ``gh api repos/<repo>``.

        Returns the bare branch name (no ``refs/heads/`` prefix). Raises
        :class:`GhNotFoundError` if the repo doesn't exist;
        :class:`GhUnknownError` if the API returns a payload without a
        ``default_branch`` field (forbidden but worth defending — Ravel L-1
        says "unknown shape is NeedsHuman, not RetryableFailure").
        """
        payload = await self.api(f"repos/{repo}")
        branch = payload.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise GhUnknownError(
                f"gh api repos/{repo}: missing or empty default_branch field",
                exit_code=0,
                stderr=str(payload)[:512],
                argv=(self._binary, "api", f"repos/{repo}"),
            )
        return branch

    async def pr_mergeability(
        self, repo: str, number: int
    ) -> RepoMergeabilityReport:
        """Return GitHub's mergeability projection in neutral vocabulary."""
        payload = await self.api(f"repos/{repo}/pulls/{number}", method="GET")
        mergeable = payload.get("mergeable")
        mergeable_state = str(payload.get("mergeable_state", "unknown"))
        checks_state = "unknown"
        head = payload.get("head")
        head_sha = head.get("sha") if isinstance(head, dict) else None
        if isinstance(head_sha, str) and head_sha:
            status_payload = await self.api(
                f"repos/{repo}/commits/{head_sha}/status",
                method="GET",
            )
            check_runs_payload = await self.api(
                f"repos/{repo}/commits/{head_sha}/check-runs",
                method="GET",
            )
            checks_state = _github_check_signal(status_payload, check_runs_payload)
        return RepoMergeabilityReport(
            mergeable=mergeable if isinstance(mergeable, bool) or mergeable is None else None,
            mergeable_state=mergeable_state,
            checks_state=checks_state,
            conflicts=(mergeable is False or mergeable_state == "dirty"),
            policies_satisfied=(mergeable_state == "clean"),
            head_sha=str(head_sha) if isinstance(head_sha, str) and head_sha else None,
        )

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
        """Merge a PR after re-validating its live head/base branches."""
        live = await self.pr_view(repo, number)
        if expected_head is not None and live.head != expected_head:
            raise GhUnknownError(
                f"refusing to merge PR #{number}: live head {live.head!r} "
                f"!= expected {expected_head!r}",
                exit_code=0,
                stderr="head precondition failed",
                argv=(self._binary, "pr", "view", str(number)),
            )
        if expected_base is not None and live.base != expected_base:
            raise GhUnknownError(
                f"refusing to merge PR #{number}: live base {live.base!r} "
                f"!= expected {expected_base!r}",
                exit_code=0,
                stderr="base precondition failed",
                argv=(self._binary, "pr", "view", str(number)),
            )
        if live.merged:
            raw_merge = live.raw.get("mergeCommit") if isinstance(live.raw, dict) else None
            merge_sha = None
            if isinstance(raw_merge, dict):
                oid = raw_merge.get("oid")
                merge_sha = str(oid) if oid else None
            return RepoCompleteResult(
                number=number,
                merged=True,
                merge_sha=merge_sha,
                strategy=strategy,
            )
        live_head = live.raw.get("head") if isinstance(live.raw, dict) else None
        live_head_sha = (
            str(live_head.get("sha"))
            if isinstance(live_head, dict) and live_head.get("sha")
            else None
        )
        if (
            expected_head_sha is not None
            and live_head_sha is not None
            and live_head_sha != expected_head_sha
        ):
            raise GhUnknownError(
                f"refusing to merge PR #{number}: live head SHA "
                f"{live_head_sha!r} != validated {expected_head_sha!r}",
                exit_code=0,
                stderr="head SHA precondition failed",
                argv=(self._binary, "pr", "view", str(number)),
            )
        body = {"merge_method": strategy}
        if expected_head_sha is not None:
            body["sha"] = expected_head_sha
        payload = await self.api(
            f"repos/{repo}/pulls/{number}/merge",
            method="PUT",
            body=body,
        )
        return RepoCompleteResult(
            number=number,
            merged=bool(payload.get("merged", False)),
            merge_sha=str(payload.get("sha")) if payload.get("sha") else None,
            strategy=strategy,
        )

    async def post_commit_status(
        self,
        repo: str,
        sha: str,
        *,
        context: str,
        state: Literal["success", "failure", "pending"],
        description: str = "",
    ) -> None:
        """Post a commit status onto ``sha`` (ADR-0032 §self-merge evidence).

        GitHub's vocabulary already matches ours 1:1 (``success`` /
        ``failure`` / ``pending``), unlike ADO. See
        :meth:`AdoClient.post_commit_status` for why this exists: leaf PRs
        on an ephemeral trunk have no CI wired up, so ``pr_mergeability``'s
        ``checks_state`` would otherwise be permanently "unknown".
        """
        await self.api(
            f"repos/{repo}/statuses/{sha}",
            method="POST",
            body={"state": state, "context": context, "description": description},
        )

    async def api(
        self,
        endpoint: str,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Low-level escape hatch.

        Returns the parsed JSON response body. For endpoints that return
        arrays at the top level, wrap your call site (we deliberately
        keep this typed as `dict` to flush array endpoints out into
        explicit methods over time).
        """
        argv: tuple[str, ...] = (
            self._binary, "api", endpoint, "--method", method.upper(),
        )
        stdin_bytes: bytes | None = None
        if body is not None:
            argv = argv + ("--input", "-")
            stdin_bytes = json.dumps(body).encode("utf-8")
        stdout = await self._run_json_text(argv, stdin=stdin_bytes)
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as e:
            raise GhUnknownError(
                f"gh api returned non-JSON: {e}",
                exit_code=0, stderr=stdout[:512], argv=argv,
            ) from e
        if not isinstance(payload, dict):
            raise GhUnknownError(
                f"gh api: expected JSON object, got {type(payload).__name__}",
                exit_code=0, stderr=stdout[:512], argv=argv,
            )
        return payload

    # ---- mutation: PR create (added for Bizet — implementation workflow) ----
    #
    # `gh pr create` does not honour --json; it prints the new PR URL on
    # the last non-empty stdout line. We parse the number out of the URL
    # and then re-fetch via `pr_view` so callers receive a fully-typed
    # GhPullRequest. The two-call shape keeps the contract honest at the
    # cost of one extra subprocess — negligible vs the network create.
    #
    # The L-1 caveat still applies: an unclassified `gh exit 1` is
    # NeedsHuman, not a retry. Verbs translate accordingly.

    async def pr_create(
        self,
        repo: str,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> GhPullRequest:
        """Open a PR via ``gh pr create``. Returns the freshly-viewed PR.

        ``head`` is the branch the PR is from; ``base`` is what it merges
        into. Both are bare branch names (no ``owner:`` prefix needed
        because ``--repo`` is set explicitly).

        ``body`` is passed via ``--body-file -`` over stdin so titles
        and bodies with shell-special characters survive intact across
        platforms.
        """
        argv: tuple[str, ...] = (
            self._binary, "pr", "create",
            "--repo", repo,
            "--title", title,
            "--head", head,
            "--base", base,
            "--body-file", "-",
        )
        stdout = await self._run_json_text(argv, stdin=body.encode("utf-8"))
        number = _extract_pr_number(stdout)
        if number is None:
            raise GhUnknownError(
                f"gh pr create succeeded but no PR URL found in stdout: {stdout[:512]!r}",
                exit_code=0, stderr=stdout[:512], argv=argv,
            )
        # Re-fetch so callers get the canonical typed shape (state, base,
        # head, url all populated from gh's own JSON).
        return await self.pr_view(repo, number)

    # ---- mutation: branch ref create (ADR-0018 trunk bootstrap) ----
    #
    # The git client (`toolbelt.GitClient`) is read-only, so requiem cannot
    # create the `feature/<root>` integration trunk locally. Per ADR-0018
    # (ratified 2026-06-07) the driver bootstraps it *remotely* via the GitHub
    # refs API — no working tree required, idempotent, and confined to these
    # two narrow methods rather than letting orchestration code reach for raw
    # `api()` mutation. The L-1 caveat still applies: an unclassified failure
    # surfaces as GhUnknownError → NeedsHuman, never a silent retry.

    async def branch_sha(self, repo: str, branch: str) -> str:
        """Return the commit SHA a branch points at (``git/ref/heads/...``).

        Raises ``GhNotFoundError`` if the branch does not exist — callers
        treat a missing *source* branch as fail-closed (can't bootstrap a
        trunk off a base that isn't there).
        """
        payload = await self.api(f"repos/{repo}/git/ref/heads/{branch}")
        obj = payload.get("object")
        if not isinstance(obj, dict) or "sha" not in obj:
            raise GhUnknownError(
                f"git/ref/heads/{branch} returned no object.sha: {payload!r}",
                exit_code=0, stderr="", argv=(),
            )
        return str(obj["sha"])

    async def ensure_branch_ref(
        self, repo: str, branch: str, source_sha: str
    ) -> bool:
        """Idempotently ensure ``refs/heads/<branch>`` exists at ``source_sha``.

        Returns ``True`` if it created the ref, ``False`` if it already
        existed. Does **not** move an existing ref (no force-update): an
        already-present branch is left exactly as-is, so re-runs never rewind
        a trunk that leaves have advanced. A create that loses a 422 race
        (someone else created the ref first) is reconciled to ``False``.
        """
        try:
            await self.api(f"repos/{repo}/git/ref/heads/{branch}")
            return False  # already exists — leave it untouched
        except GhNotFoundError:
            pass
        try:
            await self.api(
                f"repos/{repo}/git/refs",
                method="POST",
                body={"ref": f"refs/heads/{branch}", "sha": source_sha},
            )
            return True
        except GhClientError as create_err:
            # Lost a create race? Re-read; if the ref now exists, treat as a
            # benign no-op. Otherwise the create genuinely failed — re-raise
            # the original error (don't mask it behind the recheck).
            try:
                await self.api(f"repos/{repo}/git/ref/heads/{branch}")
            except GhClientError:
                raise create_err from None
            return False

    # ---- subprocess seam ----
    async def _run_json_text(
        self, argv: tuple[str, ...], *, stdin: bytes | None = None
    ) -> str:
        """Spawn gh and return decoded stdout, or raise a typed GhClientError."""
        # `stdin` is always explicit: PIPE when we have a body to send,
        # DEVNULL otherwise. Inheriting stdin (the default) is broken on
        # Python 3.14 + Windows under pytest — pytest's captured stdin
        # isn't an inheritable handle, and the spawn fails before gh
        # ever runs. Schumann (fs seat) flagged this from their seat.
        stdin_arg: Any = (
            asyncio.subprocess.PIPE if stdin is not None
            else asyncio.subprocess.DEVNULL
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(self._cwd) if self._cwd is not None else None,
                stdin=stdin_arg,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        # Cross-platform: Windows raises NotADirectoryError on bad cwd,
        # Linux/macOS raise FileNotFoundError; missing `gh` binary raises
        # FileNotFoundError on both.
        except (FileNotFoundError, NotADirectoryError) as e:
            raise GhUnknownError(
                f"failed to spawn gh: {e}",
                exit_code=-1,
                stderr=str(e),
                argv=argv,
            ) from e

        stdout_b, stderr_b = await proc.communicate(input=stdin)
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        exit_code = proc.returncode if proc.returncode is not None else -1

        if exit_code == 0:
            return stdout

        raise _classify(exit_code, stdout, stderr, argv)
