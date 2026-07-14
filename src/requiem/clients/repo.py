"""Repository-platform Protocol — the trunk-topology surface (ADR-0024).

The ADR-0018 trunk topology (``trunk_bootstrap``, ``leaf_pr``,
``feature_pr``) needs a small, narrow surface on a repository platform:
create refs, find/view/open PRs, resolve the default branch. Previously
this was hard-coded against :mod:`requiem.clients.gh.GhClient` and
duck-typed at call sites (``ctx.toolbelt.gh``). That coupling makes the
topology GitHub-only by construction; ADR-0024 lifts it into a Protocol
so an ADO impl (and any future third backend) can plug in without
re-describing the topology workflows.

Two design rules to honour at every call site:

* **The Protocol is narrow.** Six methods, all required by the
  trunk-topology workflows today. Any new method requires extending the
  Protocol *and* every impl in lockstep — surface drift is precisely
  what this abstraction exists to prevent.
* **The Protocol owns vocabulary translation.** Workflows pass platform-
  neutral arguments (a ``head`` branch name; a ``repo`` opaque string)
  and receive the platform-neutral :class:`RepoPullRequest`. Impls do
  the per-backend query-syntax translation; workflows never see GitHub's
  ``"head:foo state:open"`` or ADO's ``searchCriteria.sourceRefName``.

See ADR-0024 for the full design + migration plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

# ---- value object -------------------------------------------------------


# Neutral PR-state vocabulary; impls normalise to this on the way out.
#   GitHub: OPEN → "open", CLOSED → "closed", MERGED → "merged"
#   ADO:    active → "open", abandoned → "closed", completed → "merged"
RepoPrState = Literal["open", "closed", "merged"]
RepoMergeStrategy = Literal["merge", "squash", "rebase"]
REQUIRED_TEST_STATUS_CONTEXT = "requiem/local-tests"
REQUIRED_TEST_STATUS_GENRE = "requiem"


@dataclass(frozen=True, slots=True)
class RepoPullRequest:
    """Platform-neutral pull request projection.

    Mirrors the existing :class:`requiem.clients.gh.GhPullRequest` shape so
    callers don't have to choose between "GitHub PR" and "RepoPullRequest"
    — the field set is identical, the semantics are normalised.

    ``state`` is one of the three canonical values in :data:`RepoPrState`
    (lowercase). ``merged_at`` is set iff ``state == "merged"``. ``raw``
    carries the platform's original payload for callers that need a field
    the Protocol doesn't surface (escape hatch — prefer to add a typed
    field to the Protocol if a workflow needs it more than once).

    **Back-compat construction.** Many callers across the codebase still
    write ``GhPullRequest(state="OPEN", merged=False, ...)`` (the legacy
    GhPullRequest shape that ADR-0024 normalised). :meth:`__post_init__`
    normalises any uppercase state to the neutral lowercase, and the
    constructor accepts (and ignores) the legacy ``merged`` bool kwarg via
    a ``__new__`` shim. The dataclass is frozen, so the
    state normalisation rewrites via ``object.__setattr__``.
    """

    number: int
    title: str
    state: RepoPrState
    merged_at: datetime | None
    head: str                  # bare branch name (no refs/heads/ prefix)
    base: str                  # bare branch name (no refs/heads/ prefix)
    url: str
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Accept legacy uppercase / mixed-case states from call sites
        # written against the pre-ADR-0024 GhPullRequest. The Protocol
        # contract is lowercase only; normalise once on construction.
        normalised = str(self.state).lower()
        if normalised in ("open", "closed", "merged"):
            if normalised != self.state:
                object.__setattr__(self, "state", normalised)
        else:
            # Unrecognised state — default to "open" rather than smuggling
            # a garbage value into downstream verbs.
            object.__setattr__(self, "state", "open")

    @property
    def merged(self) -> bool:
        """Convenience for ``state == "merged"`` (back-compat with the
        legacy ``GhPullRequest.merged`` field)."""
        return self.state == "merged"


# Back-compat constructor shim. Some legacy call sites still pass
# ``merged=...`` as a kwarg, which dataclasses would reject. Wrap the
# generated ``__init__`` to silently drop the legacy kwarg (the merged
# state is derived from ``state`` post-normalisation).
_orig_repo_pr_init = RepoPullRequest.__init__


def _repo_pr_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
    kwargs.pop("merged", None)
    _orig_repo_pr_init(self, *args, **kwargs)


RepoPullRequest.__init__ = _repo_pr_init  # type: ignore[method-assign]


@dataclass(frozen=True, slots=True)
class RepoMergeabilityReport:
    """Platform-neutral mergeability projection.

    Unifies the load-bearing fields the GitHub and Azure DevOps merge paths
    expose. ``mergeable`` is ``None`` when the backend has not finished
    computing a definitive answer yet. ``checks_state`` is backend-normalised
    to ``"success"``, ``"failure"``, ``"pending"``, or ``"unknown"``.
    """

    mergeable: bool | None
    mergeable_state: str
    checks_state: str
    conflicts: bool
    policies_satisfied: bool
    head_sha: str | None = None


@dataclass(frozen=True, slots=True)
class RepoCompleteResult:
    """Platform-neutral result of completing/merging a PR."""

    number: int
    merged: bool
    merge_sha: str | None
    strategy: RepoMergeStrategy


# ---- Protocol -----------------------------------------------------------


@runtime_checkable
class RepoPlatform(Protocol):
    """The narrow ref + PR surface the ADR-0018 trunk topology needs.

    Every method is async; impls MAY perform sync work behind a thread
    boundary (see :class:`~requiem.workflows.ado_pr.RealAdoPrToolkit` for
    the pattern). Errors are platform-typed exceptions; workflows
    translate to discriminated outcomes per Ravel L-1.

    Implementations MUST be safe to call concurrently from different
    workflow runs against the same repo — no shared mutable state, no
    process-global caches. (The Hermes fleet runs multiple workers; two
    leaves opening PRs in parallel must not collide on a shared client
    instance.)

    The ``repo`` string is opaque to the workflow — it is whatever the
    platform's native shape requires (``"Owner/Repo"`` for GitHub;
    ``"<org>/<project>/<repo>"`` for Azure DevOps). Each impl's docstring
    must say what it expects.
    """

    # -- ref ops (trunk_bootstrap) ----------------------------------

    async def branch_sha(self, repo: str, branch: str) -> str:
        """Return the commit SHA the branch ref currently points at.

        Raises :class:`Exception` (impl-typed: ``GhNotFoundError`` /
        ``AdoNotFoundError`` / etc.) when the branch doesn't exist —
        workflows map this to ``PermanentFailure(error_kind="...")``
        per their own conventions.
        """
        ...

    async def ensure_branch_ref(
        self, repo: str, branch: str, source_sha: str
    ) -> bool:
        """Create the branch ref at ``source_sha`` if absent; no-op if
        present. **Never force-moves an existing ref** (the
        trunk-bootstrap rationale — a re-run must not rewind a trunk
        that leaves have advanced).

        Returns ``True`` if the ref was created, ``False`` if it was
        already present at any SHA (idempotent semantics).
        """
        ...

    # -- PR ops (leaf_pr, feature_pr) -------------------------------

    async def find_open_pr_for_branch(
        self, repo: str, *, head: str, limit: int = 30
    ) -> list[RepoPullRequest]:
        """Return open PRs whose source/head branch equals ``head``.

        Multiple results are legal (a misbehaving caller may have opened
        two PRs against the same head); callers MUST handle that.

        Notes for impls:

        * GitHub: ``gh pr list --search "head:<head> state:open"``.
          GitHub's search index excludes refs that start with ``refs/``,
          so impls accept and pass bare branch names.
        * Azure DevOps: ``searchCriteria.sourceRefName=refs/heads/<head>
          &searchCriteria.status=active``. The impl adds the
          ``refs/heads/`` prefix on the wire and strips it on the way
          back, so the workflow always sees bare branch names.
        """
        ...

    async def pr_view(self, repo: str, number: int) -> RepoPullRequest:
        """Fetch a single PR by id — the canonical read for *merged*
        state (which a default ``list`` query can't reliably surface,
        since most platforms' list APIs default to open-only).
        """
        ...

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

        This is the ADR-0018 sole-writer for leaf PRs: requiem opens
        them with ``base=feature/<root>`` so the worker doesn't need a
        platform-specific ``--base`` flag.
        """
        ...

    # -- repo metadata (end_to_end._resolve_base_branch) ------------

    async def default_branch(self, repo: str) -> str:
        """Return the repo's default branch (``"main"``, ``"master"``,
        ``"develop"``, …). Used by the driver to resolve the trunk's
        base when the operator hasn't passed ``--base-branch`` and the
        repo is on a non-main default.
        """
        ...


@runtime_checkable
class MergeCapableRepoPlatform(RepoPlatform, Protocol):
    """Sibling Protocol for workflows that must both inspect and complete PRs.

    Deliberately separate from :class:`RepoPlatform`: most trunk-topology code
    needs only the narrow six-method surface above. Workflows that actually
    merge a PR must opt into this broader seam explicitly, and every impl must
    honour the head/base precondition guard on ``pr_complete``.
    """

    async def pr_mergeability(
        self, repo: str, number: int
    ) -> RepoMergeabilityReport:
        ...

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
        """Complete/merge a PR after re-fetching the live head/base/SHA.

        Supplied preconditions MUST be checked immediately before mutation.
        ``expected_head_sha`` must be enforced by the platform's atomic merge
        compare-and-swap when supported.
        """
        ...


# ---- post_commit_status (ADR-0032 follow-up) -----------------------------
#
# `AdoClient` and `GhClient` both also implement an async
# `post_commit_status(repo, sha, *, context, state, description="")` method
# (states: "success" | "failure" | "pending"). It is deliberately **not**
# part of either Protocol above: it exists to let `implementation.py`'s
# `push_branch` record the local `run_tests` result as real evidence on the
# ephemeral `feature/<root>` trunk (which has no CI wired up, so
# `pr_mergeability`'s `checks_state` would otherwise be permanently
# "unknown"). Callers reach it via `getattr(repo_client, "post_commit_status",
# None)` and treat it as fully best-effort — adding it to the narrow Protocol
# would force every fake/double in the codebase to grow a matching method for
# a capability most call sites never need.
