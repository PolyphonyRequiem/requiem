"""requiem.workflows.ado_pr — the Azure DevOps PR lifecycle (parity #10).

The GitHub side of #10 is `pr_lifecycle.py`. This is its **Azure DevOps** sibling:
drive an ADO pull request from open → mergeable → completed, then transition the
linked work item. Platform-specific lifecycles are non-negotiable #10 ("GitHub
*and* ADO"); GitHub uses the `gh` CLI, ADO uses the Azure DevOps REST API
authenticated with a PAT (``ADO_PAT``).

Shape (mirrors pr_lifecycle's core merge lifecycle; ADO has no separate
review-comment agent loop in v0 — its parity bar is the lifecycle + work-item
linkage)::

    start
      → fetch_pr        (toolkit.pr_view)
      → check_state     (active? abandoned? already completed?)
          ├─ already completed → end_completed
          └─ active            → check_mergeable
      → check_mergeable (no merge conflicts + required policies green?)
          ├─ not ready → needs_human_end
          └─ ready     → complete_pr
      → complete_pr     (toolkit.complete_pr — squash/merge per repo policy)
      → update_item     (twig.set_state linked work item → the closed state)
      → end_completed

The toolkit is a Protocol seam (like pr_lifecycle's `PrToolkit`): `RealAdoPrToolkit`
wraps the ADO REST API (PAT-authenticated); `FakeAdoPrToolkit` is an in-memory
double for tests. Per the project's faithful-fake discipline, every Fake method
mirrors the Real method's async shape (enforced by
`tests/test_fake_surface_contract.py` when wired in). **Live ADO validation needs
a real `ADO_PAT` + org/project and is a deploy-time step** — the workflow logic is
exercised here against `FakeAdoPrToolkit`, exactly as `pr_lifecycle` is exercised
against `FakePrToolkit`.
"""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from requiem.dsl import AgentRegistry, VerbRegistry, Workflow, WorkflowBuilder
from requiem.kernel import Engine
from requiem.outcomes import NeedsHuman, PermanentFailure, Success
from requiem.toolbelt import Toolbelt

MODULE = "requiem.workflows.ado_pr"


# ---- value objects ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdoPullRequest:
    """An Azure DevOps pull request, projected to the fields the lifecycle needs."""

    pull_request_id: int
    title: str
    status: str            # active | completed | abandoned
    source_branch: str     # refs/heads/impl/<root>-<item>
    target_branch: str     # refs/heads/feature/<root> (or the trunk)
    merge_status: str      # succeeded | conflicts | queued | rejected | notSet
    is_draft: bool = False
    work_item_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class AdoMergeabilityReport:
    """Whether an ADO PR can complete: no conflicts + policies satisfied."""

    merge_status: str          # succeeded | conflicts | queued | rejected | notSet
    conflicts: bool
    policies_satisfied: bool


@dataclass(frozen=True, slots=True)
class AdoCompleteResult:
    """The outcome of completing (merging) an ADO PR."""

    pull_request_id: int
    completed: bool
    merge_strategy: str        # squash | rebase | noFastForward | rebaseMerge


@dataclass(frozen=True, slots=True)
class AdoPrLifecycleResult:
    """What a caller can pluck out of a finished ado_pr run."""

    pull_request_id: int
    verdict: str               # completed | already_completed | needs_human | abandoned
    merged: bool
    work_item_ids: tuple[int, ...] = field(default_factory=tuple)


# ---- toolkit seam (Protocol; Real wraps ADO REST, Fake for tests) -------


class AdoPrToolkit(Protocol):
    """The ADO-PR boundary the workflow calls. Tests substitute a fake.

    The real implementation issues Azure DevOps REST calls authenticated with a
    PAT. The Protocol is the seam so the workflow has no static dependency on the
    transport (mirrors pr_lifecycle.PrToolkit).
    """

    async def pr_view(self, repo: str, pr_id: int) -> AdoPullRequest: ...
    async def mergeability(self, repo: str, pr_id: int) -> AdoMergeabilityReport: ...
    async def complete_pr(
        self, repo: str, pr_id: int, *, strategy: str
    ) -> AdoCompleteResult: ...


class AdoPrError(Exception):
    """An ADO REST failure, classified by the verb into an Outcome."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# ---- credential abstraction ---------------------------------------------

# The ADO REST resource (audience) bearer tokens must target — same audience
# `az`, `twig`, and AzureCliCredential all request for ADO API calls. The id is
# stable across tenants; verified against Daniel's live `twig auth status` and
# the AzureCliCredential docs.
ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"


def _resolve_default_credential():
    """Try to construct an :class:`AzureCliCredential` (the documented v0
    default for ADO auth per ADR-0024 + ADR-0007 Q4).

    Lazy-imports ``azure.identity`` so non-ADO users don't pay for the
    dependency. Raises :class:`AdoPrError` with a clear remediation hint if
    the package isn't installed.
    """
    try:
        from azure.identity import AzureCliCredential
    except ImportError as e:
        raise AdoPrError(
            "AzureCliCredential default requires the `azure-identity` "
            "package. Install with `pip install requiem[ado]`, or pass "
            "`pat=...` / set `ADO_PAT` for backward-compat PAT auth."
        ) from e
    return AzureCliCredential()


# Sentinel marking "defer credential construction to first auth-needing call."
# Lets us keep `azure-identity` a lazy import without forcing a try/except on
# every Toolkit construction.
_LazyDefault = object()


# ---- real toolkit (Azure DevOps REST via TokenCredential or PAT) --------


class RealAdoPrToolkit:
    """Production ADO PR toolkit. Azure DevOps REST.

    ``repo`` is ``"<organization>/<project>/<repository>"``.

    **Auth (per ADR-0007 Q4 + ADR-0024):** the v0 default is OIDC via an
    :class:`azure.identity.TokenCredential` (typically
    :class:`AzureCliCredential` — the user authenticates once with
    ``az login`` and tokens are refreshed transparently). PAT auth via
    ``ADO_PAT`` remains supported as a backward-compat fallback for
    locked-down runners that cannot run ``az login``; PATs are NOT
    supported in the primary v0 ADO org and should not be the default.

    **Credential resolution order** (first non-empty wins):

    1. an explicit ``credential=`` argument (any
       :class:`azure.identity.TokenCredential`);
    2. an explicit ``pat=`` argument;
    3. the ``ADO_PAT`` env var (legacy);
    4. :class:`AzureCliCredential` from ``azure-identity``.

    Every method maps a non-2xx to :class:`AdoPrError`; the verbs translate
    those to Outcomes — this wrapper never swallows errors.

    NOTE: live execution requires either an authenticated ``az login`` (for
    OIDC) or a real ``ADO_PAT`` + reachable org/project; that is a
    deploy-time concern. The unit suite exercises the workflow via
    :class:`FakeAdoPrToolkit`.
    """

    API_VERSION = "7.1"

    def __init__(
        self,
        *,
        credential: Any | None = None,           # azure.identity.TokenCredential
        pat: str | None = None,
        base_url: str | None = None,
    ) -> None:
        # Capture explicit args; defer AzureCliCredential() construction until
        # we know we'll actually need it (lazy-import path in
        # _resolve_default_credential).
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
            # Lazy default: defer to first call so import-time of this module
            # never requires azure-identity. _auth_header() resolves it.
            self._credential = _LazyDefault
            self._pat = ""
        # https://dev.azure.com by default; on-prem ADO Server overrides base_url.
        self._base = (base_url or "https://dev.azure.com").rstrip("/")

    async def _auth_header(self) -> str:
        """Resolve to either a Bearer or Basic header, depending on which
        credential surface is active. Async so we can call
        ``credential.get_token`` without blocking the event loop.
        """
        if self._credential is _LazyDefault:
            self._credential = _resolve_default_credential()
        if self._credential is not None:
            # TokenCredential.get_token is a sync call inside azure-identity;
            # run it on a thread to keep the event loop unblocked.
            token = await _to_thread(self._credential.get_token, ADO_RESOURCE)
            return f"Bearer {token.token}"
        # PAT path: Basic auth, empty username.
        encoded = base64.b64encode(f":{self._pat}".encode()).decode("ascii")
        return f"Basic {encoded}"

    def _split_repo(self, repo: str) -> tuple[str, str, str]:
        parts = repo.split("/")
        if len(parts) != 3:
            raise AdoPrError(
                f"ADO repo must be 'org/project/repository', got {repo!r}"
            )
        return parts[0], parts[1], parts[2]

    async def _request(
        self, method: str, url: str, *, body: dict | None = None
    ) -> dict[str, Any]:
        # Imported lazily so the module has no hard urllib-at-import cost and the
        # transport is easy to monkeypatch in a focused test.
        import urllib.error
        import urllib.request

        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", await self._auth_header())
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            loop_resp = await _to_thread(_urlopen_read, req)
        except urllib.error.HTTPError as e:  # pragma: no cover - network
            raise AdoPrError(f"ADO REST {method} {url} failed: {e}", status=e.code) from e
        except urllib.error.URLError as e:  # pragma: no cover - network
            raise AdoPrError(f"ADO REST {method} {url} unreachable: {e}") from e
        return json.loads(loop_resp) if loop_resp else {}

    def _pr_url(self, repo: str, pr_id: int) -> str:
        org, project, repository = self._split_repo(repo)
        return (
            f"{self._base}/{org}/{project}/_apis/git/repositories/{repository}"
            f"/pullrequests/{pr_id}?api-version={self.API_VERSION}"
        )

    async def pr_view(self, repo: str, pr_id: int) -> AdoPullRequest:  # pragma: no cover - network
        payload = await self._request("GET", self._pr_url(repo, pr_id))
        return _ado_pr_from_payload(payload)

    async def mergeability(self, repo: str, pr_id: int) -> AdoMergeabilityReport:  # pragma: no cover - network
        payload = await self._request("GET", self._pr_url(repo, pr_id))
        merge_status = str(payload.get("mergeStatus", "notSet"))
        return AdoMergeabilityReport(
            merge_status=merge_status,
            conflicts=(merge_status == "conflicts"),
            policies_satisfied=(merge_status == "succeeded"),
        )

    async def complete_pr(self, repo: str, pr_id: int, *, strategy: str) -> AdoCompleteResult:  # pragma: no cover - network
        body = {
            "status": "completed",
            "completionOptions": {"mergeStrategy": strategy},
        }
        # ADO completes a PR via PATCH on the PR with status=completed.
        org, project, repository = self._split_repo(repo)
        url = (
            f"{self._base}/{org}/{project}/_apis/git/repositories/{repository}"
            f"/pullrequests/{pr_id}?api-version={self.API_VERSION}"
        )
        payload = await self._request("PATCH", url, body=body)
        return AdoCompleteResult(
            pull_request_id=pr_id,
            completed=(str(payload.get("status")) == "completed"),
            merge_strategy=strategy,
        )


def _ado_pr_from_payload(payload: dict[str, Any]) -> AdoPullRequest:
    refs = payload.get("workItemRefs") or payload.get("_links", {}).get("workItems", [])
    wids: list[int] = []
    if isinstance(refs, list):
        for r in refs:
            rid = (r or {}).get("id")
            if rid is not None:
                try:
                    wids.append(int(rid))
                except (TypeError, ValueError):
                    pass
    return AdoPullRequest(
        pull_request_id=int(payload.get("pullRequestId", 0)),
        title=str(payload.get("title", "")),
        status=str(payload.get("status", "active")),
        source_branch=str(payload.get("sourceRefName", "")),
        target_branch=str(payload.get("targetRefName", "")),
        merge_status=str(payload.get("mergeStatus", "notSet")),
        is_draft=bool(payload.get("isDraft", False)),
        work_item_ids=tuple(wids),
    )


def _urlopen_read(req) -> str:  # pragma: no cover - network
    import urllib.request
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


async def _to_thread(fn, *args):  # pragma: no cover - thin shim
    import asyncio
    return await asyncio.to_thread(fn, *args)


# ---- fake toolkit (in-memory; the unit-test double) ---------------------


@dataclass
class FakeAdoPrToolkit:
    """In-memory AdoPrToolkit for unit tests (mirrors FakePrToolkit).

    Seed it with a PR; ``complete_pr`` flips it to completed and records the call.
    """

    pr: AdoPullRequest
    merge_status: str = "succeeded"
    policies_satisfied: bool = True
    raise_on_view: Exception | None = None
    raise_on_complete: Exception | None = None
    completed_calls: list[dict[str, Any]] = field(default_factory=list)

    async def pr_view(self, repo: str, pr_id: int) -> AdoPullRequest:
        if self.raise_on_view is not None:
            raise self.raise_on_view
        return self.pr

    async def mergeability(self, repo: str, pr_id: int) -> AdoMergeabilityReport:
        return AdoMergeabilityReport(
            merge_status=self.merge_status,
            conflicts=(self.merge_status == "conflicts"),
            policies_satisfied=self.policies_satisfied,
        )

    async def complete_pr(self, repo: str, pr_id: int, *, strategy: str) -> AdoCompleteResult:
        if self.raise_on_complete is not None:
            raise self.raise_on_complete
        self.completed_calls.append({"repo": repo, "pr_id": pr_id, "strategy": strategy})
        # Reflect the new state for any subsequent view.
        from dataclasses import replace as _dc_replace
        self.pr = _dc_replace(self.pr, status="completed")
        return AdoCompleteResult(pull_request_id=pr_id, completed=True, merge_strategy=strategy)


# ---- workflow inputs ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdoPrInputs:
    """Everything the ADO PR lifecycle needs, stamped once at start_run."""

    repo: str                  # "org/project/repository"
    pull_request_id: int
    merge_strategy: str = "squash"
    closed_state: str = "Closed"   # the ADO work-item state on completion
    dry_run: bool = True


# ---- verb registry ------------------------------------------------------


def build_verb_registry(inputs: AdoPrInputs, toolkit: AdoPrToolkit) -> VerbRegistry:
    verbs = VerbRegistry()

    @verbs.register("start_run")
    async def _start_run(ctx):
        return Success(value={
            "repo": inputs.repo,
            "pull_request_id": inputs.pull_request_id,
            "dry_run": inputs.dry_run,
        })

    @verbs.register("fetch_pr")
    async def _fetch_pr(ctx):
        try:
            pr = await toolkit.pr_view(inputs.repo, inputs.pull_request_id)
        except AdoPrError as e:
            return PermanentFailure(
                error_kind="ado.pr_fetch_failed",
                message=f"could not fetch ADO PR {inputs.pull_request_id}: {e}",
            )
        return Success(value={
            "status": pr.status,
            "merge_status": pr.merge_status,
            "is_draft": pr.is_draft,
            "source_branch": pr.source_branch,
            "target_branch": pr.target_branch,
            "work_item_ids": list(pr.work_item_ids),
        })

    @verbs.register("check_state")
    async def _check_state(ctx):
        fetched = (ctx.completed.get("fetch_pr") or {}).get("value") or {}
        status = str(fetched.get("status", "active"))
        if status == "completed":
            return PermanentFailure(
                error_kind="already_completed",
                message="ADO PR is already completed; nothing to do.",
            )
        if status == "abandoned":
            return PermanentFailure(
                error_kind="abandoned",
                message="ADO PR is abandoned; a human must reopen or supersede it.",
            )
        if fetched.get("is_draft"):
            return NeedsHuman(
                gate="check_state",
                prompt="ADO PR is a draft; publish it before completing.",
                options=("approve", "abort"),
                context={"pull_request_id": inputs.pull_request_id},
            )
        return Success(value={"status": status})

    @verbs.register("check_mergeable")
    async def _check_mergeable(ctx):
        report = await toolkit.mergeability(inputs.repo, inputs.pull_request_id)
        if report.conflicts:
            return PermanentFailure(
                error_kind="ado.merge_conflicts",
                message="ADO PR has merge conflicts; a human must resolve them.",
            )
        if not report.policies_satisfied or report.merge_status != "succeeded":
            return PermanentFailure(
                error_kind="ado.policies_unsatisfied",
                message=(f"ADO PR not ready to complete "
                         f"(merge_status={report.merge_status!r}, "
                         f"policies_satisfied={report.policies_satisfied})."),
            )
        return Success(value={
            "merge_status": report.merge_status,
            "policies_satisfied": report.policies_satisfied,
        })

    @verbs.register("complete_pr")
    async def _complete_pr(ctx):
        if inputs.dry_run:
            # Genuinely side-effect-free: never complete a real PR on a dry run.
            return Success(value={
                "completed": False, "dry_run": True,
                "strategy": inputs.merge_strategy,
            })
        try:
            result = await toolkit.complete_pr(
                inputs.repo, inputs.pull_request_id, strategy=inputs.merge_strategy,
            )
        except AdoPrError as e:
            return PermanentFailure(
                error_kind="ado.complete_failed",
                message=f"could not complete ADO PR {inputs.pull_request_id}: {e}",
            )
        if not result.completed:
            return PermanentFailure(
                error_kind="ado.complete_not_confirmed",
                message="ADO complete call did not confirm a completed status.",
            )
        return Success(value={"completed": True, "strategy": result.merge_strategy})

    @verbs.register("update_item")
    async def _update_item(ctx):
        # Transition the linked work item(s) to the closed state via twig. Best
        # effort + dry-run-aware: the merge already happened (or was previewed);
        # a twig hiccup must not fail the run after a successful completion.
        fetched = (ctx.completed.get("fetch_pr") or {}).get("value") or {}
        wids = [int(w) for w in fetched.get("work_item_ids", [])]
        twig = ctx.toolbelt.twig
        updated: list[int] = []
        if inputs.dry_run or twig is None:
            return Success(value={"updated": [], "dry_run": inputs.dry_run,
                                  "linked_work_items": wids})
        for wid in wids:
            try:
                await twig.set_state_async(wid, inputs.closed_state)
                updated.append(wid)
            except Exception:  # noqa: BLE001 - best effort; merge already done
                pass
        return Success(value={"updated": updated, "linked_work_items": wids})

    return verbs


# ---- workflow assembly --------------------------------------------------


def build_workflow() -> Workflow:
    return (
        WorkflowBuilder("ado-pr", module=MODULE, version="1")
        .entry("start")
        .script("start", verb="start_run")
            .edge("start", on="success", to="fetch_pr")
        .script("fetch_pr", verb="fetch_pr", retry_max=2)
            .edge("fetch_pr", on="success", to="check_state")
            .edge("fetch_pr", on="retry_exhausted", to="needs_human_end")
            .edge("fetch_pr", on="permanent_failure", to="needs_human_end")
        .script("check_state", verb="check_state")
            .edge("check_state", on="success", to="check_mergeable")
            .edge("check_state", on="permanent_failure:already_completed", to="end_already_completed")
            .edge("check_state", on="permanent_failure:abandoned", to="needs_human_end")
            .edge("check_state", on="needs_human", to="needs_human_end")
            .edge("check_state", on="permanent_failure", to="needs_human_end")
        .script("check_mergeable", verb="check_mergeable", retry_max=2)
            .edge("check_mergeable", on="success", to="complete_pr")
            .edge("check_mergeable", on="retry_exhausted", to="needs_human_end")
            .edge("check_mergeable", on="permanent_failure", to="needs_human_end")
        .script("complete_pr", verb="complete_pr", retry_max=2)
            .edge("complete_pr", on="success", to="update_item")
            .edge("complete_pr", on="retry_exhausted", to="needs_human_end")
            .edge("complete_pr", on="permanent_failure", to="needs_human_end")
        .script("update_item", verb="update_item")
            .edge("update_item", on="success", to="end_completed")
        .terminate("end_completed", disposition="completed")
        .terminate("end_already_completed", disposition="completed")
        .terminate("needs_human_end", disposition="needs_human")
        .humanize({
            "start": "Starting ADO PR lifecycle",
            "fetch_pr": "Fetched ADO PR",
            "check_state": "Checked PR state",
            "check_mergeable": "Checked mergeability",
            "complete_pr": "Completed PR",
            "update_item": "Updated work item",
            "end_completed": "ADO PR lifecycle",
            "end_already_completed": "ADO PR lifecycle",
            "needs_human_end": "ADO PR lifecycle",
        })
        .build()
    )


def build_agent_registry() -> AgentRegistry:
    # No agents in the v0 ADO lifecycle (no review-addressal loop).
    return AgentRegistry()


def build_engine(
    log_dir,
    *,
    inputs: AdoPrInputs | None = None,
    toolkit: AdoPrToolkit | None = None,
    toolbelt: Toolbelt | None = None,
    gate_handler=None,
) -> Engine:
    """Construct a runnable ADO PR lifecycle engine.

    With no extras, a self-contained demo: a fake toolkit over a ready-to-complete
    PR, dry-run. Programmatic callers (the driver, tests) supply ``inputs`` and a
    ``toolkit`` (``RealAdoPrToolkit`` in production, ``FakeAdoPrToolkit`` in tests).
    """
    if inputs is None:
        inputs = AdoPrInputs(repo="org/project/repo", pull_request_id=42, dry_run=True)
    if toolkit is None:
        toolkit = FakeAdoPrToolkit(pr=AdoPullRequest(
            pull_request_id=inputs.pull_request_id, title="demo PR", status="active",
            source_branch="refs/heads/impl/9000-1", target_branch="refs/heads/feature/9000",
            merge_status="succeeded", work_item_ids=(9001,),
        ))
    if toolbelt is None:
        toolbelt = Toolbelt.real()
    return Engine(
        workflow=build_workflow(),
        verbs=build_verb_registry(inputs, toolkit),
        agents=build_agent_registry(),
        provider=None,
        toolbelt=toolbelt,
        log_dir=log_dir,
        gate_handler=gate_handler,
    )


# ---- result projection --------------------------------------------------


def ado_pr_result(completed: dict, final_node: str) -> AdoPrLifecycleResult:
    start = (completed.get("start") or {}).get("value") or {}
    fetched = (completed.get("fetch_pr") or {}).get("value") or {}
    comp = (completed.get("complete_pr") or {}).get("value") or {}
    pr_id = int(start.get("pull_request_id", 0))
    wids = tuple(int(w) for w in fetched.get("work_item_ids", []))

    if final_node == "end_already_completed":
        verdict = "already_completed"
    elif final_node == "end_completed":
        verdict = "completed"
    elif final_node == "needs_human_end":
        verdict = "needs_human"
    else:
        verdict = "unknown"

    return AdoPrLifecycleResult(
        pull_request_id=pr_id,
        verdict=verdict,
        merged=bool(comp.get("completed", False)),
        work_item_ids=wids,
    )
