"""Azure DevOps REST client — RepoPlatform impl for the trunk topology (ADR-0024).

The GitHub side of the trunk topology is :class:`requiem.clients.gh.GhClient`;
this is its Azure DevOps sibling. Same Protocol (:class:`RepoPlatform`),
different transport (ADO REST instead of the ``gh`` CLI), different auth
(AAD bearer via :mod:`azure.identity` instead of ``gh auth``).

``repo`` for every method is ``"<organization>/<project>/<repository>"`` —
the same shape :class:`requiem.workflows.ado_pr.RealAdoPrToolkit` uses, so
operators see one ADO repo identifier across the toolset.

**Auth (ADR-0007 Q4 + ADR-0024):** the v0 default is an
:class:`azure.identity.AzureCliCredential` (the user runs ``az login`` once
on the host; tokens cached + refreshed transparently). PATs are NOT
supported in Daniel's primary v0 ADO org, but ``ADO_PAT`` is still honoured
as a backward-compat fallback for locked-down runners. Resolution order
(first non-empty wins):

1. an explicit ``credential=`` argument (any
   :class:`azure.identity.TokenCredential`);
2. an explicit ``pat=`` argument;
3. the ``ADO_PAT`` env var (legacy);
4. :class:`AzureCliCredential` from ``azure-identity``.

This mirrors :class:`requiem.workflows.ado_pr.RealAdoPrToolkit` exactly —
the two paths consume the same auth surface but, deliberately, duplicate
the session plumbing for now. Consolidation into a shared
``_AdoSession`` helper is a fast-follow (ADR-0024 fast-follows).

**Errors.** Every non-2xx HTTP response maps to :class:`AdoClientError`
(or a typed subclass: rate-limited, not-found, auth, server). Workflows
translate to discriminated outcomes per Ravel L-1.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from requiem.clients.repo import (
    REQUIRED_TEST_STATUS_CONTEXT,
    REQUIRED_TEST_STATUS_GENRE,
    RepoCompleteResult,
    RepoMergeStrategy,
    RepoMergeabilityReport,
    RepoPullRequest,
)

# Re-use the existing constants + helpers from ado_pr to avoid two sources
# of truth for the ADO REST audience id and the lazy-default credential
# resolver. (These are internal to the ado_pr workflow module today but are
# the right primitives to share until the consolidation fast-follow lands.)
from requiem.workflows.ado_pr import (
    ADO_RESOURCE,
    _LazyDefault,
    _resolve_default_credential,
    _to_thread,
)


# ---- typed errors -------------------------------------------------------


class AdoClientError(Exception):
    """Base for every error this client raises.

    Carries ``status`` (HTTP status code, or ``None`` for transport errors)
    and ``url`` (the endpoint we hit) so callers can emit receipts /
    event-log entries with full forensic context.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.url = url


class AdoRateLimitedError(AdoClientError):
    """ADO API rate-limited us (HTTP 429 or `Retry-After` header)."""


class AdoNotFoundError(AdoClientError):
    """404 — the resource (repo / branch / PR / item) does not exist."""


class AdoAuthError(AdoClientError):
    """401 / 403 — auth failure; operator must re-auth or fix permissions."""


class AdoServerError(AdoClientError):
    """5xx — ADO server error; verbs map to retryable failure."""


class AdoUnknownError(AdoClientError):
    """Anything else — per Ravel L-1, NeedsHuman not silent retry."""


@dataclass(frozen=True, slots=True)
class AdoBranchRef:
    """One ADO Git branch ref with its authoritative object id."""

    name: str
    sha: str


def _ado_commit_status_signal(payload: dict[str, Any]) -> str:
    """Collapse the required Requiem status into neutral vocabulary."""
    rows = payload.get("value")
    if not isinstance(rows, list) or not rows:
        return "unknown"
    saw_success = False
    saw_pending = False
    for row in rows:
        context = (row or {}).get("context")
        if not isinstance(context, dict):
            continue
        if (
            context.get("name") != REQUIRED_TEST_STATUS_CONTEXT
            or context.get("genre") != REQUIRED_TEST_STATUS_GENRE
        ):
            continue
        state = str((row or {}).get("state", "")).lower()
        if state == "succeeded":
            saw_success = True
            continue
        if state in {"pending", "notset"}:
            saw_pending = True
            continue
        if state in {"failed", "error"}:
            return "failure"
        return "unknown"
    if saw_pending:
        return "pending"
    if saw_success:
        return "success"
    return "unknown"


# ---- AdoClient (RepoPlatform impl) --------------------------------------


class AdoClient:
    """Async :class:`RepoPlatform` impl for Azure DevOps Repos.

    Implements the six-method ADR-0024 contract plus the merge-capable
    sibling surface for workflows that must complete a PR:
    ``branch_sha``, ``ensure_branch_ref``, ``find_open_pr_for_branch``,
    ``pr_view``, ``pr_create``, ``default_branch``, ``pr_mergeability``,
    ``pr_complete``.

    ``repo`` is ``"<organization>/<project>/<repository>"`` everywhere.
    """

    API_VERSION = "7.1"
    # Marker the ADO refs API uses to create or delete a ref via PUT.
    _NULL_OBJECT_ID = "0000000000000000000000000000000000000000"

    def __init__(
        self,
        *,
        credential: Any | None = None,           # azure.identity.TokenCredential
        pat: str | None = None,
        base_url: str | None = None,
    ) -> None:
        env_pat = os.environ.get("ADO_PAT", "")
        if credential is not None:
            self._credential: Any | None = credential
            self._pat: str = ""
        elif pat is not None and pat != "":
            self._credential = None
            self._pat = pat
        elif env_pat:
            self._credential = None
            self._pat = env_pat
        else:
            # Lazy default — only constructs AzureCliCredential() on first
            # auth-needing call (so non-ADO users never pay the
            # azure-identity import cost just because this module exists).
            self._credential = _LazyDefault
            self._pat = ""
        self._base = (base_url or "https://dev.azure.com").rstrip("/")

    # ---- auth + transport -------------------------------------------

    async def _auth_header(self) -> str:
        if self._credential is _LazyDefault:
            self._credential = _resolve_default_credential()
        if self._credential is not None:
            token = await _to_thread(self._credential.get_token, ADO_RESOURCE)
            return f"Bearer {token.token}"
        encoded = base64.b64encode(f":{self._pat}".encode()).decode("ascii")
        return f"Basic {encoded}"

    @staticmethod
    def _split_repo(repo: str) -> tuple[str, str, str]:
        parts = repo.split("/")
        if len(parts) != 3:
            raise AdoClientError(
                f"ADO repo must be 'org/project/repository', got {repo!r}"
            )
        return parts[0], parts[1], parts[2]

    def _repo_base(self, repo: str) -> str:
        """Build the ``…/<org>/<project>/_apis/git/repositories/<repo>`` prefix."""
        org, project, repository = self._split_repo(repo)
        return (
            f"{self._base}/{org}/{project}"
            f"/_apis/git/repositories/{repository}"
        )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        body: Any | None = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        """Issue one HTTP request to ADO and return the parsed JSON body.

        Raises a typed :class:`AdoClientError` subclass on any non-2xx.
        Performs the request on a thread (urllib is sync); we do that
        rather than introduce httpx as a dep for v0.
        """
        import urllib.error
        import urllib.parse
        import urllib.request

        # Always append api-version, plus any per-call params.
        all_params = dict(params or {})
        all_params.setdefault("api-version", self.API_VERSION)
        query = urllib.parse.urlencode(all_params)
        full_url = f"{url}{'&' if '?' in url else '?'}{query}"

        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(full_url, data=data, method=method)
        req.add_header("Authorization", await self._auth_header())
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")

        try:
            response_bytes = await _to_thread(_urlopen_read, req)
        except urllib.error.HTTPError as e:  # pragma: no cover - network path
            self._raise_classified(e, full_url)
        except urllib.error.URLError as e:  # pragma: no cover - network path
            raise AdoUnknownError(
                f"ADO REST {method} {full_url} unreachable: {e}", url=full_url
            ) from e
        if not response_bytes:
            return {}
        try:
            return json.loads(response_bytes)
        except json.JSONDecodeError as e:
            raise AdoUnknownError(
                f"ADO REST {method} {full_url}: non-JSON response: {e}",
                url=full_url,
            ) from e

    @staticmethod
    def _raise_classified(
        e: Exception, url: str
    ) -> None:  # pragma: no cover - hit only on live HTTP
        """Translate a ``urllib.error.HTTPError`` into a typed AdoClientError.
        Mirrors ``GhClient._classify`` in shape — same Ravel L-1 discipline."""
        status = getattr(e, "code", 0)
        try:
            body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""  # type: ignore[attr-defined]
        except Exception:
            body = ""
        msg = f"ADO REST {url} -> HTTP {status}: {body[:512]}"
        if status == 401 or status == 403:
            raise AdoAuthError(msg, status=status, url=url) from e
        if status == 404:
            raise AdoNotFoundError(msg, status=status, url=url) from e
        if status == 429:
            raise AdoRateLimitedError(msg, status=status, url=url) from e
        if 500 <= status < 600:
            raise AdoServerError(msg, status=status, url=url) from e
        raise AdoUnknownError(msg, status=status, url=url) from e

    # ---- RepoPlatform: ref ops --------------------------------------

    @staticmethod
    def _strip_refs_heads(ref: str) -> str:
        """ADO refs come back as ``refs/heads/<branch>``; the Protocol
        contract is bare branch names. Strip once at the boundary."""
        prefix = "refs/heads/"
        return ref[len(prefix):] if ref.startswith(prefix) else ref

    async def branch_sha(self, repo: str, branch: str) -> str:
        """Return the commit SHA the branch ref points at.

        ADO endpoint: ``GET /git/repositories/{repo}/refs?filter=heads/<branch>``.
        We use the filter form (not the directly-addressable refs endpoint)
        because it returns an empty value list on a missing branch rather
        than 404 — and we want a typed NotFound either way, so we normalise
        here.
        """
        url = f"{self._repo_base(repo)}/refs"
        payload = await self._request(
            "GET", url, params={"filter": f"heads/{branch}"}
        )
        values = payload.get("value") if isinstance(payload, dict) else None
        if not values:
            raise AdoNotFoundError(
                f"branch refs/heads/{branch} does not exist in {repo}",
                status=404, url=url,
            )
        return str(values[0].get("objectId", ""))

    async def ensure_branch_ref(
        self, repo: str, branch: str, source_sha: str
    ) -> bool:
        """Create ``refs/heads/<branch>`` at ``source_sha`` if absent.

        ADO endpoint: ``POST /git/repositories/{repo}/refs`` with a
        ``refUpdates`` body of one entry whose ``oldObjectId`` is the
        nullSha (``000…``) and ``newObjectId`` is the source SHA. A
        nullSha → real-SHA update means *create*; the reverse means
        *delete*.

        Per ADR-0018: **never force-moves an existing ref**. The
        nullSha precondition guarantees this — if the ref already
        exists, ADO returns a ``success: false`` entry with
        ``customMessage`` indicating the precondition failure, which
        we surface as ``False`` (idempotent re-run, no harm done).

        Returns ``True`` if created, ``False`` if already present at
        any SHA.
        """
        url = f"{self._repo_base(repo)}/refs"
        body = [{
            "name": f"refs/heads/{branch}",
            "oldObjectId": self._NULL_OBJECT_ID,
            "newObjectId": source_sha,
        }]
        payload = await self._request("POST", url, body=body)
        # ADO returns {"value": [{"success": bool, "customMessage": str, ...}]}.
        values = payload.get("value") if isinstance(payload, dict) else None
        if not values:
            raise AdoUnknownError(
                f"ADO refs POST returned empty value list: {payload!r}",
                url=url,
            )
        entry = values[0]
        if entry.get("success") is True:
            return True
        # The "already exists" case manifests as success=False with a
        # message that mentions the precondition. Re-check by fetching
        # the current ref — if it's there, treat as idempotent no-op.
        # (We do this rather than parse the customMessage so we're not
        # coupled to ADO's English error strings.)
        try:
            await self.branch_sha(repo, branch)
            return False  # ref already exists — no-op idempotent success
        except AdoNotFoundError:
            # Not present AND POST said success=False — something else
            # went wrong (auth, validation, network race we can't recover).
            raise AdoUnknownError(
                f"ADO refs POST failed and ref not present afterwards: "
                f"{entry.get('customMessage', '(no message)')!r}",
                url=url,
            )

    async def list_branch_refs(
        self,
        repo: str,
        *,
        prefix: str,
        limit: int = 1000,
    ) -> list[AdoBranchRef]:
        """List branch refs whose bare name starts with ``prefix``.

        Reaching ``limit`` is treated as an incomplete read rather than a
        successful truncation. Destructive callers must never act on a partial
        inventory.
        """
        url = f"{self._repo_base(repo)}/refs"
        payload = await self._request(
            "GET",
            url,
            params={"filter": f"heads/{prefix}", "$top": str(limit)},
        )
        values = payload.get("value") if isinstance(payload, dict) else None
        if not isinstance(values, list):
            raise AdoUnknownError(
                f"ADO refs GET returned an invalid value list: {payload!r}",
                url=url,
            )
        if len(values) >= limit:
            raise AdoUnknownError(
                f"ADO refs GET reached limit={limit}; refusing an incomplete read",
                url=url,
            )
        refs: list[AdoBranchRef] = []
        for entry in values:
            name = self._strip_refs_heads(str((entry or {}).get("name", "")))
            sha = str((entry or {}).get("objectId", ""))
            if not name or not sha:
                raise AdoUnknownError(
                    f"ADO refs GET returned an invalid ref entry: {entry!r}",
                    url=url,
                )
            refs.append(AdoBranchRef(name=name, sha=sha))
        return refs

    async def delete_branch_ref(
        self,
        repo: str,
        branch: str,
        *,
        expected_sha: str,
    ) -> None:
        """Compare-and-delete one branch ref at ``expected_sha``."""
        url = f"{self._repo_base(repo)}/refs"
        payload = await self._request(
            "POST",
            url,
            body=[{
                "name": f"refs/heads/{branch}",
                "oldObjectId": expected_sha,
                "newObjectId": self._NULL_OBJECT_ID,
            }],
        )
        values = payload.get("value") if isinstance(payload, dict) else None
        if not isinstance(values, list) or len(values) != 1:
            raise AdoUnknownError(
                f"ADO ref deletion returned an invalid response: {payload!r}",
                url=url,
            )
        entry = values[0]
        if entry.get("success") is not True:
            raise AdoUnknownError(
                f"ADO refused compare-and-delete for {branch!r} at "
                f"{expected_sha}: {entry.get('customMessage', '(no message)')}",
                url=url,
            )

    # ---- RepoPlatform: PR ops ---------------------------------------

    async def find_open_pr_for_branch(
        self, repo: str, *, head: str, limit: int = 30
    ) -> list[RepoPullRequest]:
        """Find active PRs whose source branch is ``head``.

        ADO endpoint: ``GET /git/repositories/{repo}/pullrequests`` with
        ``searchCriteria.sourceRefName=refs/heads/<head>`` and
        ``searchCriteria.status=active``. ``$top`` caps the result count.
        """
        url = f"{self._repo_base(repo)}/pullrequests"
        payload = await self._request(
            "GET", url,
            params={
                "searchCriteria.sourceRefName": f"refs/heads/{head}",
                "searchCriteria.status": "active",
                "$top": str(limit),
            },
        )
        values = payload.get("value", []) if isinstance(payload, dict) else []
        return [self._to_repo_pr(repo, item) for item in values]

    async def list_active_prs(
        self,
        repo: str,
        *,
        limit: int = 1000,
    ) -> list[RepoPullRequest]:
        """List every active PR in a repo, failing if the read may truncate."""
        url = f"{self._repo_base(repo)}/pullrequests"
        payload = await self._request(
            "GET",
            url,
            params={"searchCriteria.status": "active", "$top": str(limit)},
        )
        values = payload.get("value") if isinstance(payload, dict) else None
        if not isinstance(values, list):
            raise AdoUnknownError(
                f"ADO PR list returned an invalid value list: {payload!r}",
                url=url,
            )
        if len(values) >= limit:
            raise AdoUnknownError(
                f"ADO PR list reached limit={limit}; refusing an incomplete read",
                url=url,
            )
        return [self._to_repo_pr(repo, item) for item in values]

    async def abandon_pr(
        self,
        repo: str,
        number: int,
        *,
        expected_head: str,
    ) -> RepoPullRequest:
        """Abandon one active PR after re-validating its source branch."""
        live = await self.pr_view(repo, number)
        if live.state != "open":
            raise AdoUnknownError(
                f"refusing to abandon PR {number}: live state is {live.state!r}"
            )
        if live.head != expected_head:
            raise AdoUnknownError(
                f"refusing to abandon PR {number}: live head {live.head!r} "
                f"!= expected {expected_head!r}"
            )
        url = f"{self._repo_base(repo)}/pullrequests/{number}"
        payload = await self._request(
            "PATCH",
            url,
            body={"status": "abandoned"},
        )
        abandoned = self._to_repo_pr(repo, payload)
        if abandoned.state != "closed" or abandoned.head != expected_head:
            raise AdoUnknownError(
                f"ADO did not confirm PR {number} abandoned on "
                f"{expected_head!r}: {payload!r}",
                url=url,
            )
        return abandoned

    async def pr_view(self, repo: str, number: int) -> RepoPullRequest:
        """Fetch one PR by id. Canonical read for *merged* state — the
        ``find_open_pr_for_branch`` list defaults to active-only so it
        can't surface a completed PR."""
        url = f"{self._repo_base(repo)}/pullrequests/{number}"
        payload = await self._request("GET", url)
        return self._to_repo_pr(repo, payload)

    async def pr_create(
        self,
        repo: str,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> RepoPullRequest:
        """Open a PR from ``head`` into ``base`` (bare branch names).

        ADO endpoint: ``POST /git/repositories/{repo}/pullrequests``.
        ADO requires the ``refs/heads/`` prefix on the wire; we add it
        here so callers always pass bare names.
        """
        url = f"{self._repo_base(repo)}/pullrequests"
        request_body = {
            "title": title,
            "description": body,
            "sourceRefName": f"refs/heads/{head}",
            "targetRefName": f"refs/heads/{base}",
        }
        payload = await self._request("POST", url, body=request_body)
        return self._to_repo_pr(repo, payload)

    # ---- RepoPlatform: repo metadata --------------------------------

    async def default_branch(self, repo: str) -> str:
        """Return the repo's default branch (bare name)."""
        url = self._repo_base(repo)
        payload = await self._request("GET", url)
        ref = payload.get("defaultBranch") if isinstance(payload, dict) else None
        if not isinstance(ref, str) or not ref:
            raise AdoUnknownError(
                f"ADO repo {repo} missing defaultBranch field: {payload!r}",
                url=url,
            )
        return self._strip_refs_heads(ref)

    async def pr_mergeability(
        self, repo: str, number: int
    ) -> RepoMergeabilityReport:
        """Return ADO mergeability/policy state in neutral vocabulary."""
        url = f"{self._repo_base(repo)}/pullrequests/{number}"
        payload = await self._request("GET", url)
        merge_status = str(payload.get("mergeStatus", "notSet"))
        if merge_status == "succeeded":
            mergeable: bool | None = True
        elif merge_status in {"queued", "notSet"}:
            mergeable = None
        else:
            mergeable = False
        head = self._strip_refs_heads(str(payload.get("sourceRefName", "")))
        checks_state = "unknown"
        head_sha: str | None = None
        if head:
            head_sha = await self.branch_sha(repo, head)
            statuses = await self._request(
                "GET",
                f"{self._repo_base(repo)}/commits/{head_sha}/statuses",
                params={"api-version": "7.1-preview.1", "latestOnly": True},
            )
            checks_state = _ado_commit_status_signal(statuses)
        return RepoMergeabilityReport(
            mergeable=mergeable,
            mergeable_state=merge_status,
            checks_state=checks_state,
            conflicts=(merge_status == "conflicts"),
            policies_satisfied=(merge_status == "succeeded"),
            head_sha=head_sha,
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

        ADO's leaf-PR ``checks_state`` (``_ado_commit_status_signal`` above)
        is computed from this same ``commits/{sha}/statuses`` feed. Leaf PRs
        land on an ephemeral ``feature/<root>`` trunk with no build-validation
        branch policy attached — nothing else will ever post a status there —
        so without this, ``checks_state`` stays "unknown" forever and
        ``leaf_lifecycle.check_tests_passed`` can never see a green signal.
        Callers post this once local tests (``implementation.py``'s
        ``run_tests``) have actually verified the commit, so the gate stays
        genuinely fail-closed on real evidence rather than being relaxed.
        """
        state_map = {"success": "succeeded", "failure": "failed", "pending": "pending"}
        body = {
            "state": state_map[state],
            "description": description,
            "context": {"name": context, "genre": "requiem"},
        }
        await self._request(
            "POST",
            f"{self._repo_base(repo)}/commits/{sha}/statuses",
            body=body,
            params={"api-version": "7.1-preview.1"},
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
        """Complete an ADO PR after re-validating live refs and source SHA."""
        strategy_map = {
            "merge": "noFastForward",
            "squash": "squash",
            "rebase": "rebase",
        }
        merge_strategy = strategy_map[strategy]
        url = f"{self._repo_base(repo)}/pullrequests/{number}"
        live = await self._request("GET", url)
        live_head = self._strip_refs_heads(str(live.get("sourceRefName", "")))
        live_base = self._strip_refs_heads(str(live.get("targetRefName", "")))
        if expected_head is not None and live_head != expected_head:
            raise AdoUnknownError(
                f"refusing to complete PR {number}: live head {live_head!r} "
                f"!= expected {expected_head!r}",
                url=url,
            )
        if expected_base is not None and live_base != expected_base:
            raise AdoUnknownError(
                f"refusing to complete PR {number}: live base {live_base!r} "
                f"!= expected {expected_base!r}",
                url=url,
            )
        if str(live.get("status", "")).lower() == "completed":
            merge_commit = live.get("lastMergeCommit") or {}
            merge_sha = (
                str(merge_commit.get("commitId"))
                if isinstance(merge_commit, dict) and merge_commit.get("commitId")
                else None
            )
            return RepoCompleteResult(
                number=number,
                merged=True,
                merge_sha=merge_sha,
                strategy=strategy,
            )
        if expected_head_sha is not None:
            live_head_sha = await self.branch_sha(repo, live_head)
            if live_head_sha != expected_head_sha:
                raise AdoUnknownError(
                    f"refusing to complete PR {number}: live head SHA "
                    f"{live_head_sha!r} != validated {expected_head_sha!r}",
                    url=url,
                )
        # ADO's completePullRequest rejects the PATCH with HTTP 400
        # ("You must specify a valid LastMergeSourceCommit") unless we echo
        # back the source commit it already told us about. This also
        # doubles as ADO's own optimistic-concurrency check: the merge
        # only proceeds if the source ref is still at this exact commit.
        last_merge_source_commit = (
            {"commitId": expected_head_sha}
            if expected_head_sha is not None
            else live.get("lastMergeSourceCommit")
        )
        if not (
            isinstance(last_merge_source_commit, dict)
            and last_merge_source_commit.get("commitId")
        ):
            head_sha = await self.branch_sha(repo, live_head)
            last_merge_source_commit = {"commitId": head_sha}
        payload = await self._request(
            "PATCH",
            url,
            body={
                "status": "completed",
                "completionOptions": {"mergeStrategy": merge_strategy},
                "lastMergeSourceCommit": last_merge_source_commit,
            },
        )
        merge_commit = payload.get("lastMergeCommit") or {}
        merge_sha = (
            str(merge_commit.get("commitId"))
            if isinstance(merge_commit, dict) and merge_commit.get("commitId")
            else None
        )
        return RepoCompleteResult(
            number=number,
            merged=(str(payload.get("status", "")).lower() == "completed"),
            merge_sha=merge_sha,
            strategy=strategy,
        )

    # ---- Work-item reads (ADR-0031 / R4 projection) -----------------
    #
    # The projection layer needs a thin, field-selective read of one
    # work item — state + the three Microsoft.VSTS.Scheduling.* dates
    # plus title/type/parent. Twig's ``show`` covers most of this
    # already; this helper is the direct-REST path for callers that
    # don't want a subprocess hop AND that need to constrain the
    # fields list (a full work-item fetch carries hundreds of fields
    # by default — expensive at tree scale).

    async def get_work_item(
        self,
        *,
        organization: str,
        project: str,
        item_id: int,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """Fetch one ADO work item, optionally restricted to ``fields``.

        Endpoint: ``GET /{org}/{project}/_apis/wit/workitems/{id}``
        with ``?fields=`` (comma-separated reference names) when
        ``fields`` is set.

        Returns the raw ADO payload — a dict with ``id``, ``rev``,
        ``fields`` (dict keyed by reference name), and ``_links``.
        Callers project as they need; this client deliberately stays
        below the dataclass layer so consumers (R4 projection,
        future R3 roll-up) own their own shapes.

        ``fields`` MUST be a list of fully-qualified reference names
        (e.g. ``"System.Title"``, ``"Microsoft.VSTS.Scheduling.StartDate"``).
        Passing ``None`` fetches every field — the default ADO
        behaviour, kept for ad-hoc forensic reads but discouraged at
        tree scale (per ADR-0031 §scope: \"request only what you
        need\").

        Raises :class:`AdoNotFoundError` on 404 (work item doesn't
        exist or isn't in scope), :class:`AdoAuthError` on 401/403,
        and the usual typed subclasses for transport / shape errors.
        """
        url = (
            f"{self._base}/{organization}/{project}"
            f"/_apis/wit/workitems/{item_id}"
        )
        params: dict[str, str] = {}
        if fields:
            params["fields"] = ",".join(fields)
        payload = await self._request("GET", url, params=params)
        if not isinstance(payload, dict):
            raise AdoUnknownError(
                f"ADO get_work_item {item_id}: expected dict, "
                f"got {type(payload).__name__}",
                url=url,
            )
        return payload

    # ---- payload → RepoPullRequest ----------------------------------

    @staticmethod
    def _ado_status_to_neutral(status: str) -> str:
        """active → open, abandoned → closed, completed → merged."""
        s = status.lower()
        if s == "active":
            return "open"
        if s == "abandoned":
            return "closed"
        if s == "completed":
            return "merged"
        return "open"

    def _to_repo_pr(self, repo: str, payload: dict[str, Any]) -> RepoPullRequest:
        """Translate an ADO ``pullRequest`` payload into a neutral
        :class:`RepoPullRequest`. Strips ``refs/heads/`` from branch
        names; parses ``closedDate`` as merged_at when status==completed."""
        pr_id = int(payload.get("pullRequestId", 0))
        status = str(payload.get("status", "active"))
        neutral_state = self._ado_status_to_neutral(status)
        # ADO uses closedDate for both abandoned and completed PRs; we
        # only treat it as a merged_at when the PR actually merged.
        merged_at: datetime | None = None
        if neutral_state == "merged":
            closed = payload.get("closedDate")
            if isinstance(closed, str) and closed:
                merged_at = _parse_iso_datetime(closed)
        org, project, repository = self._split_repo(repo)
        # ADO doesn't expose a single canonical PR URL in the payload,
        # but the web URL is deterministic from the ids.
        url = (
            f"{self._base}/{org}/{project}/_git/{repository}/pullrequest/{pr_id}"
        )
        return RepoPullRequest(
            number=pr_id,
            title=str(payload.get("title", "")),
            state=neutral_state,
            merged_at=merged_at,
            head=self._strip_refs_heads(str(payload.get("sourceRefName", ""))),
            base=self._strip_refs_heads(str(payload.get("targetRefName", ""))),
            url=url,
            raw=dict(payload),
        )


def _parse_iso_datetime(value: str) -> datetime | None:
    """Parse the RFC3339 / ISO-8601 timestamps ADO emits. Tolerant of
    the trailing-Z form used by some endpoints."""
    s = value
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _urlopen_read(req) -> bytes:  # pragma: no cover - network path
    """Thread-safe synchronous urlopen helper; raises HTTPError on non-2xx.
    Kept module-level so ``_to_thread`` can dispatch it without binding
    self."""
    import urllib.request
    with urllib.request.urlopen(req) as response:
        return response.read()


# ---- FakeAdoClient (in-memory; for tests + harness) ---------------------


class FakeAdoClient:
    """In-memory :class:`RepoPlatform` impl for tests.

    Mirrors :class:`requiem.workflows.trunk_bootstrap._DemoGhClient` and
    :class:`requiem.workflows.leaf_pr._DemoGhClient`: callers seed the
    initial state (refs, open PRs, repo default branch) at construction
    and the methods read + mutate from that state. Faithful-fake
    discipline per ``tests/test_fake_surface_contract.py`` — every method
    on AdoClient has a matching async method here with the same
    signature.
    """

    def __init__(
        self,
        *,
        default_branches: dict[str, str] | None = None,
        refs: dict[tuple[str, str], str] | None = None,
        open_prs: list[RepoPullRequest] | None = None,
        next_pr_number: int = 8000,
        raise_on_search: Exception | None = None,
        raise_on_create: Exception | None = None,
        raise_on_sha: Exception | None = None,
        work_items: dict[int, dict[str, Any]] | None = None,
    ) -> None:
        # repo -> default branch (e.g. {"contoso/proj/repo": "main"})
        self._default_branches = dict(default_branches or {})
        # (repo, branch) -> SHA
        self._refs = dict(refs or {})
        # All PRs the fake knows about; find_open_pr_for_branch filters them.
        self._prs: list[RepoPullRequest] = list(open_prs or [])
        self._pr_by_number: dict[tuple[str, int], RepoPullRequest] = {
            (self._infer_repo(pr), pr.number): pr for pr in self._prs
        }
        self._next_pr_number = next_pr_number
        self.raise_on_search = raise_on_search
        self.raise_on_create = raise_on_create
        self.raise_on_sha = raise_on_sha
        # ADR-0031 / R4: in-memory work-item store for get_work_item.
        # Keyed by int id; values are ADO-shaped {id, rev, fields}
        # payloads. Tests seed only the fields they want to assert on.
        self._work_items = {int(k): dict(v) for k, v in (work_items or {}).items()}
        # Forensic logs for test assertions.
        self.created_refs: list[tuple[str, str, str]] = []  # (repo, branch, sha)
        self.created_prs: list[dict[str, Any]] = []
        self.completed_prs: list[dict[str, Any]] = []
        self.posted_statuses: list[dict[str, Any]] = []
        self.abandoned_prs: list[dict[str, Any]] = []
        self.deleted_refs: list[tuple[str, str, str]] = []
        # (repo, sha) -> list of ADO-shaped status rows, mirrors the real
        # commits/{sha}/statuses feed post_commit_status writes to.
        self._statuses_by_sha: dict[tuple[str, str], list[dict[str, Any]]] = {}

    @staticmethod
    def _infer_repo(pr: RepoPullRequest) -> str:
        # FakeAdoClient stores PRs without an explicit repo binding; callers
        # that need cross-repo behaviour should construct one fake per repo.
        # We use a sentinel that callers can override by passing repo on each
        # method call. For find_open_pr_for_branch we filter by branch only,
        # which is the only behaviour the trunk-topology workflows need.
        return pr.raw.get("_repo", "")

    async def branch_sha(self, repo: str, branch: str) -> str:
        if self.raise_on_sha is not None:
            raise self.raise_on_sha
        key = (repo, branch)
        if key not in self._refs:
            raise AdoNotFoundError(
                f"branch refs/heads/{branch} does not exist in {repo}",
                status=404,
            )
        return self._refs[key]

    async def ensure_branch_ref(
        self, repo: str, branch: str, source_sha: str
    ) -> bool:
        key = (repo, branch)
        if key in self._refs:
            return False  # idempotent no-op
        self._refs[key] = source_sha
        self.created_refs.append((repo, branch, source_sha))
        return True

    async def list_branch_refs(
        self,
        repo: str,
        *,
        prefix: str,
        limit: int = 1000,
    ) -> list[AdoBranchRef]:
        refs = [
            AdoBranchRef(name=branch, sha=sha)
            for (ref_repo, branch), sha in self._refs.items()
            if ref_repo == repo and branch.startswith(prefix)
        ]
        refs.sort(key=lambda ref: ref.name)
        if len(refs) >= limit:
            raise AdoUnknownError(
                f"fake ref read reached limit={limit}; refusing truncation"
            )
        return refs

    async def delete_branch_ref(
        self,
        repo: str,
        branch: str,
        *,
        expected_sha: str,
    ) -> None:
        key = (repo, branch)
        live = self._refs.get(key)
        if live != expected_sha:
            raise AdoUnknownError(
                f"refusing to delete {branch!r}: live sha {live!r} "
                f"!= expected {expected_sha!r}"
            )
        del self._refs[key]
        self.deleted_refs.append((repo, branch, expected_sha))

    async def find_open_pr_for_branch(
        self, repo: str, *, head: str, limit: int = 30
    ) -> list[RepoPullRequest]:
        if self.raise_on_search is not None:
            raise self.raise_on_search
        # Filter by head + open state. The fake stores PRs without an
        # explicit repo binding (see _infer_repo); callers wanting
        # cross-repo isolation should construct one fake per repo.
        return [
            pr for pr in self._prs
            if pr.head == head and pr.state == "open"
        ][:limit]

    async def list_active_prs(
        self,
        repo: str,
        *,
        limit: int = 1000,
    ) -> list[RepoPullRequest]:
        prs = [
            pr for pr in self._prs
            if pr.state == "open"
            and (not self._infer_repo(pr) or self._infer_repo(pr) == repo)
        ]
        if len(prs) >= limit:
            raise AdoUnknownError(
                f"fake PR read reached limit={limit}; refusing truncation"
            )
        return prs

    async def abandon_pr(
        self,
        repo: str,
        number: int,
        *,
        expected_head: str,
    ) -> RepoPullRequest:
        pr = await self.pr_view(repo, number)
        if pr.state != "open" or pr.head != expected_head:
            raise AdoUnknownError(
                f"refusing to abandon PR {number}: state={pr.state!r}, "
                f"head={pr.head!r}, expected_head={expected_head!r}"
            )
        abandoned = RepoPullRequest(
            number=pr.number,
            title=pr.title,
            state="closed",
            merged_at=None,
            head=pr.head,
            base=pr.base,
            url=pr.url,
            raw={**pr.raw, "status": "abandoned"},
        )
        self._pr_by_number[(repo, number)] = abandoned
        self._prs = [
            abandoned if candidate.number == number else candidate
            for candidate in self._prs
        ]
        self.abandoned_prs.append({
            "repo": repo,
            "number": number,
            "expected_head": expected_head,
        })
        return abandoned

    async def pr_view(self, repo: str, number: int) -> RepoPullRequest:
        pr = self._pr_by_number.get((repo, number))
        if pr is None:
            # Fall back to repo-agnostic lookup so tests that don't tag
            # PRs with a repo still work.
            for candidate in self._prs:
                if candidate.number == number:
                    return candidate
            raise AdoNotFoundError(
                f"PR {number} does not exist in {repo}", status=404,
            )
        return pr

    async def pr_create(
        self,
        repo: str,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> RepoPullRequest:
        if self.raise_on_create is not None:
            raise self.raise_on_create
        n = self._next_pr_number
        self._next_pr_number += 1
        org, project, repository = AdoClient._split_repo(repo)
        url = (
            f"https://dev.azure.com/{org}/{project}/_git/{repository}"
            f"/pullrequest/{n}"
        )
        pr = RepoPullRequest(
            number=n, title=title, state="open", merged_at=None,
            head=head, base=base, url=url,
            raw={
                "pullRequestId": n, "title": title,
                "description": body,
                "sourceRefName": f"refs/heads/{head}",
                "targetRefName": f"refs/heads/{base}",
                "_repo": repo,
            },
        )
        self._prs.append(pr)
        self._pr_by_number[(repo, n)] = pr
        self.created_prs.append({
            "repo": repo, "title": title, "body": body,
            "head": head, "base": base, "url": url,
        })
        return pr

    async def default_branch(self, repo: str) -> str:
        if repo not in self._default_branches:
            # The fake defaults to "main" if unset — matches the most
            # common repo shape and keeps tests terse.
            return "main"
        return self._default_branches[repo]

    async def pr_mergeability(
        self, repo: str, number: int
    ) -> RepoMergeabilityReport:
        pr = await self.pr_view(repo, number)
        status = str(pr.raw.get("mergeStatus", "succeeded"))
        if status == "succeeded":
            mergeable: bool | None = True
        elif status in {"queued", "notSet"}:
            mergeable = None
        else:
            mergeable = False
        # Merge test-seeded pr.raw["statuses"] with anything posted via
        # post_commit_status against the PR's head sha, mirroring how the
        # real client reads commits/{sha}/statuses independently of the
        # PR payload itself.
        head_sha = self._refs.get((repo, pr.head))
        posted = self._statuses_by_sha.get((repo, head_sha), []) if head_sha else []
        rows = list(pr.raw.get("statuses") or []) + posted
        checks_state = _ado_commit_status_signal({"value": rows})
        return RepoMergeabilityReport(
            mergeable=mergeable,
            mergeable_state=status,
            checks_state=checks_state,
            conflicts=(status == "conflicts"),
            policies_satisfied=(status == "succeeded"),
            head_sha=head_sha,
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
        state_map = {"success": "succeeded", "failure": "failed", "pending": "pending"}
        row = {
            "state": state_map[state],
            "description": description,
            "context": {"name": context, "genre": "requiem"},
        }
        self._statuses_by_sha.setdefault((repo, sha), []).append(row)
        self.posted_statuses.append({
            "repo": repo, "sha": sha, "context": context,
            "state": state, "description": description,
        })

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
        pr = await self.pr_view(repo, number)
        if expected_head is not None and pr.head != expected_head:
            raise AdoUnknownError(
                f"refusing to complete PR {number}: live head {pr.head!r} "
                f"!= expected {expected_head!r}",
            )
        if expected_base is not None and pr.base != expected_base:
            raise AdoUnknownError(
                f"refusing to complete PR {number}: live base {pr.base!r} "
                f"!= expected {expected_base!r}",
            )
        if expected_head_sha is not None:
            raw_source = pr.raw.get("lastMergeSourceCommit")
            raw_source_sha = (
                str(raw_source.get("commitId"))
                if isinstance(raw_source, dict) and raw_source.get("commitId")
                else None
            )
            live_head_sha = self._refs.get((repo, pr.head)) or raw_source_sha
            if live_head_sha != expected_head_sha:
                raise AdoUnknownError(
                    f"refusing to complete PR {number}: live head SHA "
                    f"{live_head_sha!r} != validated {expected_head_sha!r}",
                )
        self.completed_prs.append({
            "repo": repo,
            "number": number,
            "strategy": strategy,
            "expected_head": expected_head,
            "expected_base": expected_base,
            "expected_head_sha": expected_head_sha,
        })
        merged = RepoPullRequest(
            number=pr.number,
            title=pr.title,
            state="merged",
            merged_at=pr.merged_at,
            head=pr.head,
            base=pr.base,
            url=pr.url,
            raw={**pr.raw, "status": "completed"},
        )
        self._pr_by_number[(repo, number)] = merged
        self._prs = [merged if candidate.number == number else candidate for candidate in self._prs]
        return RepoCompleteResult(
            number=number,
            merged=True,
            merge_sha=str(pr.raw.get("lastMergeCommit", {}).get("commitId"))
            if isinstance(pr.raw.get("lastMergeCommit"), dict)
            and pr.raw.get("lastMergeCommit", {}).get("commitId")
            else None,
            strategy=strategy,
        )

    async def get_work_item(
        self,
        *,
        organization: str,
        project: str,
        item_id: int,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """In-memory ``get_work_item`` for tests.

        Returns the seeded payload (or raises :class:`AdoNotFoundError`
        if the id was never seeded). ``fields`` is honoured by
        narrowing the returned ``fields`` dict — useful for tests that
        want to assert their consumer requested the right subset.
        ``organization`` / ``project`` are recorded for forensics but
        do not filter (the fake is single-tenant).
        """
        if item_id not in self._work_items:
            raise AdoNotFoundError(
                f"work item {item_id} does not exist (organization="
                f"{organization!r}, project={project!r})",
                status=404,
            )
        payload = dict(self._work_items[item_id])
        if fields is not None:
            raw_fields = payload.get("fields") or {}
            payload["fields"] = {
                k: v for k, v in raw_fields.items() if k in fields
            }
        return payload
