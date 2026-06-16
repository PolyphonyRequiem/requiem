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
from datetime import datetime, timezone
from typing import Any

from requiem.clients.repo import RepoPullRequest

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


# ---- AdoClient (RepoPlatform impl) --------------------------------------


class AdoClient:
    """Async :class:`RepoPlatform` impl for Azure DevOps Repos.

    Implements the six-method ADR-0024 contract:
    ``branch_sha``, ``ensure_branch_ref``, ``find_open_pr_for_branch``,
    ``pr_view``, ``pr_create``, ``default_branch``.

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
        # Forensic logs for test assertions.
        self.created_refs: list[tuple[str, str, str]] = []  # (repo, branch, sha)
        self.created_prs: list[dict[str, Any]] = []

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
