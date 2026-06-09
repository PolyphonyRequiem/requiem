"""Close-out workflow — Phase B / Sibelius (seat 6 of 8).

The first real composed workflow in Requiem. Touches every Phase B seam:

* ``twig`` (Mendelssohn) — fetch the item, fetch its acceptance-criteria
  children, transition state to ``Closed``.
* ``gh`` (Chopin) — fetch the merged PR.
* ``fs`` (Schumann) — atomically write the close-out markdown into the
  repo.
* ``agent`` (Mahler) — invoke a verifier agent that compares the PR
  against the criteria and returns structured output.

Node structure (the brief, as drawn):

::

    start
      → fetch_item                # twig.show
      → resolve_pr                # pr_number, else item.raw, else gh search
      → fetch_pr                  # gh.pr_view; assert merged
         ├─ merged → fetch_criteria
         └─ not_merged → needs_human
      → fetch_criteria            # twig children where type==Acceptance Criteria
      → verifier_agent            # LLM call, structured VerifierOutput
         ├─ all_met → write_closeout
         └─ gaps → needs_human
      → write_closeout            # fs.write_text docs/closeouts/AB-<id>.md
      → close_item                # twig.set_state(<id>, "Closed")
      → end_success

Two hard rules apply to every Toolbelt call:

* **Ravel's L-1 caveat.** When a client raises an *unclassified* failure
  (``TwigUnknownError`` / ``GhUnknownError`` / ``FsClientError`` catch-all,
  or ``BadOutput`` from the verifier) we surface ``NeedsHuman``. The
  kernel never auto-retries past an unclassified signal — that's
  INV-NO-CORRUPT-FORWARD in workflow form.
* **Closed ``error_kind`` enum** (ADR 0004 §4.2). Verbs pick from the
  short fixed list at the top of this module. New kinds require an ADR;
  emergencies use ``PermanentFailure.details['extensibility_escape']``.

Dry-run mode short-circuits the two mutations (``write_closeout`` and
``close_item``) — they emit a ``Success`` with ``dry_run=True`` and never
touch ``twig.set_state_async`` or ``fs.write_text``. Everything before
that runs identically so the operator sees the same narration and the
same verdict-card data.

Public entry points (the contract ``requiem run`` consumes):

* ``build_workflow() -> Workflow``
* ``build_engine(log_dir, *, …) -> Engine``
* ``render_hints() -> dict``
* ``verdict_card(completed) -> str | None``
* ``close_out_result(completed, final_node) -> CloseOutResult``

For real ADO/GH runs use the ``__main__`` argparse block::

    python -m requiem.workflows.close_out --item 12345 --repo acme/widgets

For the demo (canned data, no network)::

    requiem run requiem.workflows.close_out --run-id closeout-smoke
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from requiem.agent import AgentSpec, FakeProvider
from requiem.clients.fs import (
    FilesystemClient,
    FsClientError,
    FsNotFoundError,
    FsPermissionError,
)
from requiem.clients.gh import (
    GhAuthError,
    GhClient,
    GhClientError,
    GhNotFoundError,
    GhPullRequest,
    GhRateLimitedError,
    GhServerError,
)
from requiem.clients.twig import (
    TwigClient,
    TwigClientError,
    TwigItem,
    TwigItemNotFoundError,
    TwigRateLimitedError,
    TwigUnknownError,
)
from requiem.dsl import AgentRegistry, VerbRegistry, Workflow, WorkflowBuilder
from requiem.kernel import Engine
from requiem.outcomes import (
    NeedsHuman,
    PermanentFailure,
    RetryableFailure,
    Success,
)
from requiem.toolbelt import RealFileClient, RealGitClient, Toolbelt


# ---- closed `error_kind` vocabulary (ADR 0004 §4.2) -----------------


EK_NOT_FOUND       = "not_found"
EK_INVALID_STATE   = "invalid_state"
EK_PR_NOT_MERGED   = "pr_not_merged"
EK_NO_CRITERIA     = "no_criteria"
EK_RATE_LIMITED    = "rate_limited"
EK_AUTH            = "auth"
EK_SERVER          = "server"
EK_SCHEMA          = "schema_mismatch"
EK_WRITE_FAILED    = "write_failed"
EK_INTERNAL        = "internal"


# ---- gate identifiers (free-form but namespaced) -------------------


GATE_PR_NOT_LINKED   = "close_out.pr_not_linked"
GATE_PR_AMBIGUOUS    = "close_out.pr_ambiguous"
GATE_PR_NOT_MERGED   = "close_out.pr_not_merged"
GATE_CRITERIA_GAPS   = "close_out.criteria_gaps"
GATE_BAD_OUTPUT      = "close_out.verifier_bad_output"
GATE_UNKNOWN_TWIG    = "close_out.unknown_twig_failure"
GATE_UNKNOWN_GH      = "close_out.unknown_gh_failure"
GATE_UNKNOWN_FS      = "close_out.unknown_fs_failure"
GATE_GH_AUTH         = "close_out.gh_auth_failure"


# ---- typed agent output (the verifier's contract) ------------------


class CriterionGap(BaseModel):
    """One unmet criterion, surfaced to the operator if the verifier finds gaps."""
    criterion_id: int
    criterion_title: str
    gap: str


class VerifierOutput(BaseModel):
    """Structured response from the verifier agent.

    ``overall`` is the single-line verdict the workflow routes on:

    * ``all_met``  — every criterion is satisfied by PR content; proceed.
    * ``partial``  — some criteria unmet; surface ``NeedsHuman`` with gaps.
    * ``none_met`` — no criteria satisfied; surface ``NeedsHuman`` with gaps.

    ``met_criteria`` and ``unmet_criteria`` must partition the criterion-id
    set the verifier was asked about. The workflow asserts the partition
    holds; mismatch is a ``schema_mismatch`` ``PermanentFailure``.
    """
    overall: Literal["all_met", "partial", "none_met"]
    met_criteria: list[int] = Field(default_factory=list)
    unmet_criteria: list[CriterionGap] = Field(default_factory=list)
    notes: str = ""


# ---- public result type (the brief's `CloseOutResult`) -------------


@dataclass(frozen=True, slots=True)
class CloseOutResult:
    """Workflow-level result the brief calls for.

    Derived from the event-log projection at run end via
    :func:`close_out_result`. Pure data; the verdict card formats it; the
    operator pipeline (``requiem events`` / agents) consumes it.
    """
    item_id: int
    pr_number: int
    verdict: Literal["closed", "needs_human"]
    closeout_path: Path | None
    gaps: list[str]
    dry_run: bool


# ---- workflow inputs ------------------------------------------------


@dataclass(frozen=True, slots=True)
class CloseOutInputs:
    """Resolved-at-build-time inputs. Recorded in the ``start`` verb's
    payload so a resumed run reads identical inputs from the log even if
    the engine factory is invoked with different defaults."""
    item_id: int
    repo: str
    pr_number: int | None
    dry_run: bool
    closeout_dir: Path

    def to_payload(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "repo": self.repo,
            "pr_number": self.pr_number,
            "dry_run": self.dry_run,
            "closeout_dir": str(self.closeout_dir),
        }


# ---- helpers --------------------------------------------------------


def _require_twig(ctx) -> TwigClient:
    twig = getattr(ctx.toolbelt, "twig", None)
    if twig is None:
        raise RuntimeError(
            "close-out requires toolbelt.twig — none configured. "
            "Pass twig= to build_engine() or use Toolbelt.real()."
        )
    return twig


def _require_gh(ctx) -> GhClient:
    gh = getattr(ctx.toolbelt, "gh", None)
    if gh is None:
        raise RuntimeError(
            "close-out requires toolbelt.gh — none configured. "
            "Pass gh= to build_engine() or use Toolbelt.real()."
        )
    return gh


def _require_fs(ctx) -> FilesystemClient:
    fs = getattr(ctx.toolbelt, "fs", None)
    if fs is None:
        raise RuntimeError(
            "close-out requires toolbelt.fs — none configured. "
            "Pass fs= to build_engine() or bind FilesystemClient(repo_root)."
        )
    return fs


def _pr_to_dict(pr: GhPullRequest) -> dict[str, Any]:
    """Lift a ``GhPullRequest`` into a JSON-safe dict that round-trips
    through the event log. The merge SHA is fished out of ``raw`` because
    not every ``_PR_FIELDS`` revision surfaces it as a named field."""
    merge_sha = None
    raw_merge = pr.raw.get("mergeCommit") if isinstance(pr.raw, dict) else None
    if isinstance(raw_merge, dict):
        oid = raw_merge.get("oid")
        if oid:
            merge_sha = str(oid)
    return {
        "number": pr.number,
        "title": pr.title,
        "state": pr.state,
        "merged": pr.merged,
        "merged_at": pr.merged_at.isoformat() if pr.merged_at else None,
        "merge_sha": merge_sha,
        "url": pr.url,
        "head": pr.head,
        "base": pr.base,
    }


def _extract_linked_prs(item: TwigItem, repo: str) -> list[dict[str, Any]]:
    """Pull linked-PR stubs out of ``item.raw`` for the requested repo.

    Convention used by both the FakeTwigClient and the real twig wrapper:
    ``item.raw['pullRequests']`` is a list of dicts shaped
    ``{"repo": "owner/name", "number": int, "title": str|None}``. Real
    twig sets this from ADO's "Pull Request" link relations. Unknown
    keys are tolerated; missing fields fall through to be omitted.
    """
    raw = item.raw or {}
    linked = raw.get("pullRequests") or []
    if not isinstance(linked, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in linked:
        if not isinstance(entry, dict):
            continue
        if entry.get("repo") and entry.get("repo") != repo:
            continue
        num = entry.get("number")
        if num is None:
            continue
        try:
            out.append(
                {
                    "repo": str(entry.get("repo", repo)),
                    "number": int(num),
                    "title": str(entry.get("title", "")),
                }
            )
        except (TypeError, ValueError):
            continue
    return out


# ---- verb registry --------------------------------------------------


def build_verb_registry(
    inputs: CloseOutInputs,
    *,
    now_fn: Callable[[], datetime] = lambda: datetime.now(tz=timezone.utc),
) -> VerbRegistry:
    """Verbs for the close-out workflow.

    ``now_fn`` is injected so tests can pin the close-out timestamp in the
    markdown; defaults to ``datetime.now(UTC)``.
    """
    verbs = VerbRegistry()

    # ---- start: stamp inputs into completed[] so every other verb
    # reads them from the projection (and INV-RESTART carries them
    # identically across a resume).
    @verbs.register("start_run")
    def _start(ctx) -> Success:
        return Success(value=inputs.to_payload())

    # ---- fetch_item ------------------------------------------------
    @verbs.register("fetch_item")
    async def _fetch_item(ctx):
        twig = _require_twig(ctx)
        try:
            item = await twig.show_async(inputs.item_id)
        except TwigItemNotFoundError as e:
            return PermanentFailure(
                error_kind=EK_NOT_FOUND,
                message=f"twig has no work item {inputs.item_id}: {e}",
                details={"item_id": inputs.item_id},
            )
        except TwigRateLimitedError as e:
            return RetryableFailure(
                retry_key=f"{ctx.run_id}:fetch_item",
                error_kind=EK_RATE_LIMITED,
                message=f"twig rate-limited: {e}",
                attempt=ctx.attempt,
            )
        except TwigUnknownError as e:
            return NeedsHuman(
                gate=GATE_UNKNOWN_TWIG,
                prompt=(
                    f"twig returned an unclassified failure fetching item "
                    f"{inputs.item_id}. Retry or abort?"
                ),
                options=("retry", "abort"),
                context={
                    "item_id": inputs.item_id,
                    "exit_code": e.exit_code,
                    "stderr": (e.stderr or "")[:512],
                },
            )
        except TwigClientError as e:
            return NeedsHuman(
                gate=GATE_UNKNOWN_TWIG,
                prompt=f"twig client error fetching item {inputs.item_id}.",
                options=("retry", "abort"),
                context={"item_id": inputs.item_id, "error": str(e)[:512]},
            )

        if item.state.lower() == "closed":
            return PermanentFailure(
                error_kind=EK_INVALID_STATE,
                message=f"item {item.id} is already Closed",
                details={"item_id": item.id, "state": item.state},
            )

        linked = _extract_linked_prs(item, inputs.repo)
        return Success(
            value={
                "item_id": item.id,
                "title": item.title,
                "state": item.state,
                "area_path": item.area_path,
                "work_item_type": item.work_item_type,
                "parent_id": item.parent_id,
                "linked_prs": linked,
            },
            inspected_artifacts=(f"twig:item:{item.id}",),
        )

    # ---- resolve_pr ------------------------------------------------
    @verbs.register("resolve_pr")
    async def _resolve_pr(ctx):
        item_value = ctx.completed["fetch_item"]["value"]
        if inputs.pr_number is not None:
            return Success(
                value={"pr_number": int(inputs.pr_number), "source": "explicit"},
            )

        linked = item_value.get("linked_prs") or []
        if len(linked) == 1:
            return Success(
                value={
                    "pr_number": int(linked[0]["number"]),
                    "source": "linked",
                },
            )
        if len(linked) == 0:
            # Issue #30: real twig JSON frequently omits the `pullRequests`
            # link relation, so an absent linked-PR list is not proof there
            # is no PR. Before escalating, fall back to a gh search by the
            # implementation branch convention (`feature/<item_id>`).
            search_hits = await _search_prs_for_item(ctx)
            if len(search_hits) == 1:
                return Success(
                    value={
                        "pr_number": int(search_hits[0].number),
                        "source": "gh_search",
                    },
                )
            if len(search_hits) > 1:
                return NeedsHuman(
                    gate=GATE_PR_AMBIGUOUS,
                    prompt=(
                        f"{len(search_hits)} PRs in {inputs.repo} match "
                        f"AB#{inputs.item_id}'s branch convention. "
                        f"Pass --pr <number> to disambiguate, or abort."
                    ),
                    options=("abort",),
                    context={
                        "item_id": inputs.item_id,
                        "source": "gh_search",
                        "candidates": [
                            {"number": int(p.number), "title": p.title}
                            for p in search_hits
                        ],
                    },
                )
            return NeedsHuman(
                gate=GATE_PR_NOT_LINKED,
                prompt=(
                    f"No PR linked to AB#{inputs.item_id} in {inputs.repo}, "
                    f"and a gh search found none. "
                    f"Pass --pr <number> or abort."
                ),
                options=("abort",),
                context={
                    "item_id": inputs.item_id,
                    "repo": inputs.repo,
                    "searched": True,
                },
            )
        # Multiple linked PRs — out of v0 scope to auto-pick; ask the operator.
        return NeedsHuman(
            gate=GATE_PR_AMBIGUOUS,
            prompt=(
                f"{len(linked)} PRs linked to AB#{inputs.item_id}. "
                f"Pass --pr <number> to disambiguate, or abort."
            ),
            options=("abort",),
            context={
                "item_id": inputs.item_id,
                "candidates": [
                    {"number": int(p["number"]), "title": p.get("title", "")}
                    for p in linked
                ],
            },
        )

    async def _search_prs_for_item(ctx) -> list:
        """Best-effort gh-search fallback for a PR linked to the item.

        Real twig JSON omits the ``pullRequests`` field (issue #30), so when the
        relation is absent we search GitHub for the PR ourselves. We search the
        PR **body** for the standard ADO link syntax ``AB#<item_id>`` — which is
        branch-topology-agnostic (it survives the B3 move from ``feature/<item>``
        to ``impl/<root>-<item>`` leaf branches, and matches however the PR was
        opened). A search failure is swallowed — the caller then escalates to a
        human, the same safe destination as before the fallback existed.
        """
        gh = _require_gh(ctx)
        # `in:body AB#<id>` finds PRs whose description carries the ADO work-item
        # link. gh's --search passes this straight to GitHub's PR search.
        query = f"in:body AB#{inputs.item_id}"
        try:
            return await gh.pr_search(inputs.repo, query)
        except GhClientError:
            return []

    # ---- fetch_pr --------------------------------------------------
    @verbs.register("fetch_pr")
    async def _fetch_pr(ctx):
        gh = _require_gh(ctx)
        pr_number = int(ctx.completed["resolve_pr"]["value"]["pr_number"])
        try:
            pr = await gh.pr_view(inputs.repo, pr_number)
        except GhRateLimitedError as e:
            return RetryableFailure(
                retry_key=f"{ctx.run_id}:fetch_pr",
                error_kind=EK_RATE_LIMITED,
                message=f"gh rate-limited: {e}",
                attempt=ctx.attempt,
            )
        except GhServerError as e:
            return RetryableFailure(
                retry_key=f"{ctx.run_id}:fetch_pr",
                error_kind=EK_SERVER,
                message=f"gh server error {e.status}",
                attempt=ctx.attempt,
            )
        except GhAuthError as e:
            return NeedsHuman(
                gate=GATE_GH_AUTH,
                prompt="gh authentication failed. Check `gh auth status`.",
                options=("retry", "abort"),
                context={"repo": inputs.repo, "error": str(e)[:512]},
            )
        except GhNotFoundError as e:
            return PermanentFailure(
                error_kind=EK_NOT_FOUND,
                message=f"gh: PR #{pr_number} not found in {inputs.repo} ({e})",
                details={"repo": inputs.repo, "pr_number": pr_number},
            )
        except GhClientError as e:
            return NeedsHuman(
                gate=GATE_UNKNOWN_GH,
                prompt=f"gh returned an unclassified failure viewing PR.",
                options=("retry", "abort"),
                context={
                    "repo": inputs.repo, "pr_number": pr_number,
                    "error": str(e)[:512],
                },
            )

        if not pr.merged:
            return NeedsHuman(
                gate=GATE_PR_NOT_MERGED,
                prompt=(
                    f"PR #{pr.number} is {pr.state} (not merged). "
                    f"Close-out is only valid for merged PRs. Abort?"
                ),
                options=("abort",),
                context={
                    "pr_number": pr.number,
                    "state": pr.state,
                    "url": pr.url,
                },
            )

        return Success(
            value=_pr_to_dict(pr),
            inspected_artifacts=(f"gh:pr:{inputs.repo}#{pr.number}@merged",),
        )

    # ---- fetch_criteria --------------------------------------------
    @verbs.register("fetch_criteria")
    async def _fetch_criteria(ctx):
        twig = _require_twig(ctx)
        try:
            children = await twig.list_children_async(inputs.item_id)
        except TwigRateLimitedError as e:
            return RetryableFailure(
                retry_key=f"{ctx.run_id}:fetch_criteria",
                error_kind=EK_RATE_LIMITED,
                message=f"twig rate-limited: {e}",
                attempt=ctx.attempt,
            )
        except TwigUnknownError as e:
            return NeedsHuman(
                gate=GATE_UNKNOWN_TWIG,
                prompt=(
                    f"twig returned an unclassified failure listing "
                    f"children of AB#{inputs.item_id}."
                ),
                options=("retry", "abort"),
                context={
                    "item_id": inputs.item_id,
                    "exit_code": e.exit_code,
                    "stderr": (e.stderr or "")[:512],
                },
            )
        except TwigClientError as e:
            return NeedsHuman(
                gate=GATE_UNKNOWN_TWIG,
                prompt=f"twig client error listing children.",
                options=("retry", "abort"),
                context={"item_id": inputs.item_id, "error": str(e)[:512]},
            )

        criteria = [
            {
                "id": c.id,
                "title": c.title,
                "state": c.state,
            }
            for c in children
            if c.work_item_type.lower() == "acceptance criteria"
        ]
        if not criteria:
            # An item with zero acceptance criteria can't be auto-verified;
            # surface to a human rather than rubber-stamp.
            return NeedsHuman(
                gate=GATE_CRITERIA_GAPS,
                prompt=(
                    f"AB#{inputs.item_id} has no Acceptance Criteria children. "
                    f"Close anyway, or abort?"
                ),
                options=("abort",),
                context={"item_id": inputs.item_id},
            )
        return Success(
            value={"criteria": criteria},
            inspected_artifacts=tuple(f"twig:item:{c['id']}" for c in criteria),
        )

    # ---- verifier_prompt -------------------------------------------
    @verbs.register("verifier_prompt")
    def _verifier_prompt(ctx) -> str:
        item = ctx.completed["fetch_item"]["value"]
        pr = ctx.completed["fetch_pr"]["value"]
        criteria = ctx.completed["fetch_criteria"]["value"]["criteria"]
        body = ["Item under close-out:", ""]
        body.append(f"- AB#{item['item_id']} — {item['title']}")
        body.append("")
        body.append(f"Merged PR: #{pr['number']} — {pr['title']}")
        body.append(f"  head={pr['head']} base={pr['base']}")
        if pr.get("merge_sha"):
            body.append(f"  merge_sha={pr['merge_sha']}")
        body.append("")
        body.append("Acceptance criteria to verify:")
        for c in criteria:
            body.append(f"- AB#{c['id']} ({c['state']}): {c['title']}")
        body.append("")
        body.append(
            "Decide whether the PR satisfies each criterion. Respond with "
            "a VerifierOutput. Partition every criterion id into either "
            "met_criteria or unmet_criteria — never both, never neither."
        )
        return "\n".join(body)

    # ---- route_verdict (consumes verifier_agent.parsed) ------------
    @verbs.register("route_verdict")
    def _route_verdict(ctx):
        agent_value = ctx.completed["verifier_agent"]["value"]
        parsed = agent_value.get("parsed") or {}
        criteria = ctx.completed["fetch_criteria"]["value"]["criteria"]
        criterion_ids = {int(c["id"]) for c in criteria}

        overall = parsed.get("overall")
        met = [int(x) for x in (parsed.get("met_criteria") or [])]
        unmet = list(parsed.get("unmet_criteria") or [])

        # Partition check: every criterion id is in exactly one bucket.
        # A drifted partition is a schema mismatch, not a "needs human" —
        # the agent produced output that *parsed* but is semantically
        # inconsistent. Surface it as bad-output for the same routing.
        unmet_ids = {int(g.get("criterion_id")) for g in unmet if g.get("criterion_id") is not None}
        partition = set(met) | unmet_ids
        if partition != criterion_ids:
            missing = criterion_ids - partition
            extra = partition - criterion_ids
            return NeedsHuman(
                gate=GATE_BAD_OUTPUT,
                prompt=(
                    "verifier output does not partition the criterion set "
                    "(missing/extra ids). Treating as bad output."
                ),
                options=("abort",),
                context={
                    "missing": sorted(missing),
                    "extra": sorted(extra),
                    "raw_overall": overall,
                },
            )

        if overall == "all_met" and not unmet:
            return Success(
                value={
                    "verdict": "all_met",
                    "met_criteria": met,
                    "notes": parsed.get("notes", ""),
                },
            )

        # partial / none_met / inconsistent (overall=all_met but unmet nonempty)
        gap_lines = [
            f"AB#{g['criterion_id']} — {g.get('gap', '?')}"
            for g in unmet
        ]
        return NeedsHuman(
            gate=GATE_CRITERIA_GAPS,
            prompt=(
                f"Verifier found {len(unmet)} unmet criteria. "
                f"Resume with --decision close-anyway|reject."
            ),
            options=("close_anyway", "reject"),
            context={
                "overall": overall,
                "met_count": len(met),
                "unmet_count": len(unmet),
                "unmet": [
                    {
                        "criterion_id": int(g["criterion_id"]),
                        "criterion_title": g.get("criterion_title", ""),
                        "gap": g.get("gap", ""),
                    }
                    for g in unmet
                ],
                "gaps": gap_lines,
                "notes": parsed.get("notes", ""),
            },
        )

    # ---- verifier_bad_output (script wired off `bad_output` edge) ---
    @verbs.register("verifier_bad_output")
    def _verifier_bad_output(ctx):
        # `BadOutput` from the agent routes here. Surface NeedsHuman with
        # the validation errors so the operator can decide; no auto-retry.
        outcome = ctx.completed["verifier_agent"]
        errs = outcome.get("validation_errors") or ()
        return NeedsHuman(
            gate=GATE_BAD_OUTPUT,
            prompt=(
                "Verifier agent returned output that did not validate against "
                "VerifierOutput. No auto-retry (Ravel L-1). Abort?"
            ),
            options=("abort",),
            context={
                "error_kind": outcome.get("error_kind", EK_SCHEMA),
                "validation_errors": list(errs)[:10],
                "raw_output": (outcome.get("raw_output") or "")[:512],
            },
        )

    # ---- write_closeout --------------------------------------------
    @verbs.register("write_closeout")
    def _write_closeout(ctx):
        item = ctx.completed["fetch_item"]["value"]
        pr = ctx.completed["fetch_pr"]["value"]
        criteria = ctx.completed["fetch_criteria"]["value"]["criteria"]
        # `route_verdict` returns Success on the happy path and NeedsHuman
        # on the gap path (operator overrode with `close_anyway`). Either
        # way the canonical met/notes data comes from the verifier agent's
        # parsed output — `route_verdict` is just routing.
        agent_value = ctx.completed["verifier_agent"]["value"]
        parsed = agent_value.get("parsed") or {}
        met = set(int(x) for x in parsed.get("met_criteria") or [])
        notes = parsed.get("notes", "")
        # When the operator overrode a partial verdict via `close_anyway`,
        # surface that in the notes so the markdown is honest about the
        # gap rather than implying everything was clean.
        rv_outcome = ctx.completed.get("route_verdict") or {}
        if rv_outcome.get("kind") == "needs_human":
            unmet = rv_outcome.get("context", {}).get("unmet") or []
            if unmet:
                override_note = "Operator override (close-anyway). Unmet criteria at close:\n"
                override_note += "\n".join(
                    f"- AB#{u.get('criterion_id', '?')}: {u.get('gap', '?')}"
                    for u in unmet
                )
                notes = (
                    f"{notes}\n\n{override_note}".strip()
                    if notes else override_note
                )

        now_iso = now_fn().strftime("%Y-%m-%dT%H:%M:%SZ")
        closeout_path = inputs.closeout_dir / f"AB-{item['item_id']}.md"
        body = _render_closeout_markdown(
            item=item,
            pr=pr,
            criteria=criteria,
            met=met,
            notes=notes,
            run_id=ctx.run_id,
            closed_at_iso=now_iso,
        )

        if inputs.dry_run:
            return Success(
                value={
                    "dry_run": True,
                    "path": str(closeout_path),
                    "bytes": len(body.encode("utf-8")),
                },
            )

        fs = _require_fs(ctx)
        try:
            fs.write_text(closeout_path, body)
        except FsNotFoundError as e:
            return PermanentFailure(
                error_kind=EK_NOT_FOUND,
                message=f"fs: parent missing for {e.path}",
                details={"path": str(closeout_path)},
            )
        except FsPermissionError as e:
            return NeedsHuman(
                gate=GATE_UNKNOWN_FS,
                prompt=f"fs refused write to {e.path}.",
                options=("retry", "abort"),
                context={"path": str(e.path)},
            )
        except FsClientError as e:
            return NeedsHuman(
                gate=GATE_UNKNOWN_FS,
                prompt="fs client returned an unclassified error writing closeout.",
                options=("retry", "abort"),
                context={"path": str(closeout_path), "error": str(e)[:512]},
            )

        return Success(
            value={
                "dry_run": False,
                "path": str(closeout_path),
                "bytes": len(body.encode("utf-8")),
            },
            inspected_artifacts=(f"file:{closeout_path}",),
        )

    # ---- close_item ------------------------------------------------
    @verbs.register("close_item")
    async def _close_item(ctx):
        item = ctx.completed["fetch_item"]["value"]
        from_state = item["state"]

        if inputs.dry_run:
            return Success(
                value={
                    "dry_run": True,
                    "item_id": inputs.item_id,
                    "from_state": from_state,
                    "to_state": "Closed",
                },
            )

        twig = _require_twig(ctx)
        try:
            updated = await twig.set_state_async(inputs.item_id, "Closed")
        except TwigItemNotFoundError as e:
            return PermanentFailure(
                error_kind=EK_NOT_FOUND,
                message=f"twig lost item {inputs.item_id}: {e}",
                details={"item_id": inputs.item_id},
            )
        except TwigRateLimitedError as e:
            return RetryableFailure(
                retry_key=f"{ctx.run_id}:close_item",
                error_kind=EK_RATE_LIMITED,
                message=f"twig rate-limited: {e}",
                attempt=ctx.attempt,
            )
        except TwigUnknownError as e:
            return NeedsHuman(
                gate=GATE_UNKNOWN_TWIG,
                prompt=(
                    f"twig returned an unclassified failure closing item "
                    f"{inputs.item_id}. Closeout file is already written; "
                    f"retry the state transition or abort."
                ),
                options=("retry", "abort"),
                context={
                    "item_id": inputs.item_id,
                    "exit_code": e.exit_code,
                    "stderr": (e.stderr or "")[:512],
                },
            )
        except TwigClientError as e:
            return NeedsHuman(
                gate=GATE_UNKNOWN_TWIG,
                prompt="twig client error closing item.",
                options=("retry", "abort"),
                context={"error": str(e)[:512]},
            )

        return Success(
            value={
                "dry_run": False,
                "item_id": updated.id,
                "from_state": from_state,
                "to_state": updated.state,
            },
            inspected_artifacts=(f"twig:item:{updated.id}@{updated.state}",),
        )

    return verbs


# ---- closeout markdown renderer ------------------------------------


def _render_closeout_markdown(
    *,
    item: dict[str, Any],
    pr: dict[str, Any],
    criteria: list[dict[str, Any]],
    met: set[int],
    notes: str,
    run_id: str,
    closed_at_iso: str,
) -> str:
    """The format from the brief:

    ::

        # AB#<id> — <title>
        **Closed:** <ISO-8601 UTC>
        **PR:** #<pr-number> — <pr-title>
        **Merge SHA:** <sha>
        **Run:** <requiem-run-id>

        ## Acceptance criteria
        - [x] AB#<crit-id> — <crit-title>

        ## Notes from verifier
        <notes>
    """
    merge_sha = pr.get("merge_sha") or "—"
    lines = [
        f"# AB#{item['item_id']} — {item['title']}",
        "",
        f"**Closed:** {closed_at_iso}  ",
        f"**PR:** #{pr['number']} — {pr['title']}  ",
        f"**Merge SHA:** {merge_sha}  ",
        f"**Run:** {run_id}",
        "",
        "## Acceptance criteria",
        "",
    ]
    for c in criteria:
        mark = "x" if int(c["id"]) in met else " "
        lines.append(f"- [{mark}] AB#{c['id']} — {c['title']}")
    lines += ["", "## Notes from verifier", "", notes or "_(no notes)_", ""]
    return "\n".join(lines)


# ---- agent registry + default scripted provider --------------------


VERIFIER = AgentSpec(
    name="verifier",
    charter=(
        "You are the close-out verifier. Read the merged PR summary and "
        "the work-item's acceptance criteria. Decide for each criterion "
        "whether the PR satisfies it. Partition the criterion ids between "
        "met_criteria and unmet_criteria — never both, never neither. "
        "`overall` is `all_met` iff unmet_criteria is empty."
    ),
    response_model=VerifierOutput,
)


def build_agent_registry() -> AgentRegistry:
    reg = AgentRegistry()
    reg.register(VERIFIER)
    return reg


def default_scripted_provider() -> FakeProvider:
    """A FakeProvider that produces ``all_met`` against the demo data.

    Tests override this with their own ``FakeProvider``; the CLI demo
    uses it as the default so ``requiem run requiem.workflows.close_out``
    completes happily without network or real LLM access.
    """
    return FakeProvider(
        scripts={
            "verifier": [
                {
                    "overall": "all_met",
                    "met_criteria": [22001, 22002, 22003],
                    "unmet_criteria": [],
                    "notes": "Demo: all three criteria observed in PR diff and tests.",
                },
            ],
        },
    )


# ---- workflow topology ----------------------------------------------


def build_workflow() -> Workflow:
    return (
        WorkflowBuilder(
            "close-out",
            module="requiem.workflows.close_out",
            version="0.1",
        )
            .entry("start")
            .script("start", verb="start_run")
                .edge("start", on="success", to="fetch_item")
            .script("fetch_item", verb="fetch_item")
                .edge("fetch_item", on="success", to="resolve_pr")
                .edge("fetch_item", on="permanent_failure:not_found", to="end_failed")
                .edge("fetch_item", on="permanent_failure:invalid_state", to="end_failed")
                .edge("fetch_item", on="permanent_failure", to="end_failed")
                .edge("fetch_item", on="needs_human:retry", to="fetch_item")
                .edge("fetch_item", on="needs_human:abort", to="end_human")
            .script("resolve_pr", verb="resolve_pr")
                .edge("resolve_pr", on="success", to="fetch_pr")
                .edge("resolve_pr", on="needs_human:abort", to="end_human")
            .script("fetch_pr", verb="fetch_pr")
                .edge("fetch_pr", on="success", to="fetch_criteria")
                .edge("fetch_pr", on="permanent_failure:not_found", to="end_failed")
                .edge("fetch_pr", on="permanent_failure", to="end_failed")
                .edge("fetch_pr", on="needs_human:retry", to="fetch_pr")
                .edge("fetch_pr", on="needs_human:abort", to="end_human")
            .script("fetch_criteria", verb="fetch_criteria")
                .edge("fetch_criteria", on="success", to="verifier_agent")
                .edge("fetch_criteria", on="needs_human:retry", to="fetch_criteria")
                .edge("fetch_criteria", on="needs_human:abort", to="end_human")
            .agent(
                "verifier_agent",
                agent="verifier",
                prompt_verb="verifier_prompt",
            )
                .edge("verifier_agent", on="success", to="route_verdict")
                .edge("verifier_agent", on="bad_output", to="verifier_bad_output")
                .edge("verifier_agent", on="permanent_failure", to="end_failed")
            .script("verifier_bad_output", verb="verifier_bad_output")
                .edge("verifier_bad_output", on="needs_human:abort", to="end_human")
            .script("route_verdict", verb="route_verdict")
                .edge("route_verdict", on="success", to="write_closeout")
                .edge("route_verdict", on="needs_human:close_anyway", to="write_closeout")
                .edge("route_verdict", on="needs_human:reject", to="end_human")
                .edge("route_verdict", on="needs_human:abort", to="end_human")
            .script("write_closeout", verb="write_closeout")
                .edge("write_closeout", on="success", to="close_item")
                .edge("write_closeout", on="permanent_failure", to="end_failed")
                .edge("write_closeout", on="needs_human:retry", to="write_closeout")
                .edge("write_closeout", on="needs_human:abort", to="end_human")
            .script("close_item", verb="close_item")
                .edge("close_item", on="success", to="end_success")
                .edge("close_item", on="permanent_failure", to="end_failed")
                .edge("close_item", on="needs_human:retry", to="close_item")
                .edge("close_item", on="needs_human:abort", to="end_human")
            .terminate("end_success", disposition="completed")
            .terminate("end_failed",  disposition="failed")
            .terminate("end_human",   disposition="needs_human")
            .humanize({
                "start":               "Starting close-out",
                "fetch_item":          "Fetched work item",
                "resolve_pr":          "Resolved PR",
                "fetch_pr":            "Fetched PR",
                "fetch_criteria":      "Fetched acceptance criteria",
                "verifier_agent":      "Verifier",
                "verifier_bad_output": "Verifier bad-output gate",
                "route_verdict":       "Routed verdict",
                "write_closeout":      "Wrote close-out record",
                "close_item":          "Closed work item",
                "end_success":         "close-out",
                "end_failed":          "close-out",
                "end_human":           "close-out",
            })
            .build()
    )


# ---- gate handler ---------------------------------------------------


def _default_gate_handler(node_id: str, prompt: str, options: tuple[str, ...]) -> str:
    """Demo gate handler.

    Default policy is the safe one: when a gate offers ``abort``, take it
    (the operator must consciously override via ``--interactive`` or
    ``requiem resume <run-id> --decision <choice>``). The only auto-yes
    is ``close_anyway`` and only when ``reject`` is not also offered —
    we never silently close past a rejection-shaped gate.
    """
    if "abort" in options:
        return "abort"
    if "retry" in options:
        return "retry"
    return options[0]


_default_gate_handler.__requiem_auto__ = True  # type: ignore[attr-defined]


# ---- render hints ---------------------------------------------------


def _detail_fetch_item(value: dict) -> str:
    return f"#{value.get('item_id', '?')} ({value.get('state', '?')})"


def _detail_resolve_pr(value: dict) -> str:
    src = value.get("source", "?")
    return f"#{value.get('pr_number', '?')} via {src}"


def _detail_fetch_pr(value: dict) -> str:
    sha = (value.get("merge_sha") or "")[:7] or "—"
    return f"#{value.get('number', '?')} merged {sha}"


def _detail_fetch_criteria(value: dict) -> str:
    return f"{len(value.get('criteria') or [])} criteria"


def _detail_route_verdict(value: dict) -> str:
    return f"{value.get('verdict', '?')} ({len(value.get('met_criteria') or [])} met)"


def _detail_write_closeout(value: dict) -> str:
    if value.get("dry_run"):
        return f"DRY-RUN would write {value.get('path', '?')}"
    return f"to {value.get('path', '?')}"


def _detail_close_item(value: dict) -> str:
    if value.get("dry_run"):
        return f"DRY-RUN would set {value.get('from_state', '?')} → Closed"
    return f"{value.get('from_state', '?')} → {value.get('to_state', '?')}"


def _gate_context_pr_not_linked(completed: dict) -> str:
    start = completed.get("start", {}).get("value", {})
    return f"item AB#{start.get('item_id', '?')} in {start.get('repo', '?')}"


def _gate_context_pr_not_merged(completed: dict) -> str:
    pr = completed.get("fetch_pr", {}).get("value", {}) or {}
    return f"PR #{pr.get('number', '?')} state={pr.get('state', '?')}"


def _gate_context_criteria_gaps(completed: dict) -> str:
    rv = completed.get("route_verdict", {}).get("context", {}) or {}
    if not rv:
        # Some gate emitters store context on the outcome dict itself.
        rv = completed.get("route_verdict", {})
    n = rv.get("unmet_count")
    return f"{n} unmet criteria" if n is not None else "criteria gaps"


def _gate_context_bad_output(completed: dict) -> str:
    ag = completed.get("verifier_agent", {}) or {}
    errs = ag.get("validation_errors") or []
    return f"{len(errs)} validation error(s)"


def render_hints() -> dict:
    return {
        "artifact_name": "work item",
        "details": {
            "fetch_item":      _detail_fetch_item,
            "resolve_pr":      _detail_resolve_pr,
            "fetch_pr":        _detail_fetch_pr,
            "fetch_criteria":  _detail_fetch_criteria,
            "route_verdict":   _detail_route_verdict,
            "write_closeout":  _detail_write_closeout,
            "close_item":      _detail_close_item,
        },
        "gate_contexts": {
            "resolve_pr":          _gate_context_pr_not_linked,
            "fetch_pr":            _gate_context_pr_not_merged,
            "route_verdict":       _gate_context_criteria_gaps,
            "verifier_bad_output": _gate_context_bad_output,
        },
        "silent_nodes": frozenset({
            "start", "verifier_bad_output", "route_verdict",
            "end_success", "end_failed", "end_human",
        }),
    }


# ---- verdict card ---------------------------------------------------


_DIVIDER = "─" * 69


def _title_bar(item_id: int | str) -> str:
    head = f"─── Close-out: AB#{item_id} "
    return head + "─" * max(3, 69 - len(head))


def _summary_seed(completed: dict) -> dict[str, Any]:
    item = completed.get("fetch_item", {}).get("value", {}) or {}
    pr = completed.get("fetch_pr", {}).get("value", {}) or {}
    crit = completed.get("fetch_criteria", {}).get("value", {}) or {}
    rv_outcome = completed.get("route_verdict", {}) or {}
    rv = rv_outcome.get("value", {}) or {}
    rv_ctx = rv_outcome.get("context", {}) or {}
    # On the happy path route_verdict returned Success with met_criteria
    # populated.  On the gap path it returned NeedsHuman and the met count
    # is on the gate context.  Fall back to the verifier agent's parsed
    # output as a last resort so the card is never wrong by silence.
    if rv.get("met_criteria"):
        criteria_met = len(rv["met_criteria"])
    elif rv_ctx.get("met_count") is not None:
        criteria_met = int(rv_ctx["met_count"])
    else:
        agent_parsed = (
            completed.get("verifier_agent", {}).get("value", {}) or {}
        ).get("parsed") or {}
        criteria_met = len(agent_parsed.get("met_criteria") or [])
    wc = completed.get("write_closeout", {}).get("value", {}) or {}
    ci = completed.get("close_item", {}).get("value", {}) or {}
    return {
        "item_id": item.get("item_id"),
        "title": item.get("title"),
        "from_state": item.get("state"),
        "pr_number": pr.get("number"),
        "pr_merged": pr.get("merged"),
        "pr_state": pr.get("state"),
        "merge_sha": pr.get("merge_sha"),
        "criteria_total": len(crit.get("criteria") or []),
        "criteria_met": criteria_met,
        "closeout_path": wc.get("path"),
        "dry_run": bool(wc.get("dry_run") or ci.get("dry_run")),
        "new_state": ci.get("to_state"),
        "rv_gate": rv_outcome,
    }


def verdict_card(completed: dict) -> str | None:
    """Post-run verdict card per Demo Contract §3.4.

    Three shapes:

    * Happy / dry-run close — ``✓ Closed`` (or ``◐ Dry run``) with the
      mutation summary and the closeout path.
    * Needs human (gaps / not-merged / not-linked / bad-output) —
      ``🚦 Needs human`` with the operator's next move.
    * Catastrophic failure (no ``fetch_item`` succeeded) — short
      "Did not close" card.
    """
    seed = _summary_seed(completed)
    item_id = seed.get("item_id")
    if item_id is None:
        return _card_catastrophic(completed)

    ci = completed.get("close_item", {}).get("value")
    if ci is not None:
        return _card_closed(seed)

    # We didn't reach close_item — surface the last needs-human gate.
    return _card_needs_human(seed, completed)


def _card_closed(seed: dict[str, Any]) -> str:
    item_id = seed["item_id"]
    title = seed.get("title", "?")
    pr_num = seed.get("pr_number")
    sha = (seed.get("merge_sha") or "")[:7]
    crit_total = seed.get("criteria_total", 0)
    crit_met = seed.get("criteria_met", 0)
    closeout_path = seed.get("closeout_path") or "—"
    from_state = seed.get("from_state", "?")
    new_state = seed.get("new_state", "Closed")
    dry_run = bool(seed.get("dry_run"))

    head = "  ◐ Dry run" if dry_run else "  ✓ Closed"
    pr_line = (
        f"      PR:          #{pr_num} (merged {sha})"
        if pr_num is not None and sha
        else f"      PR:          #{pr_num} (merged)"
        if pr_num is not None
        else "      PR:          —"
    )
    state_arrow = (
        f"      State:       {from_state} → {new_state} (dry-run)"
        if dry_run
        else f"      State:       {from_state} → {new_state}"
    )
    closeout_line = (
        f"      Closeout:    {closeout_path} (dry-run)"
        if dry_run
        else f"      Closeout:    {closeout_path}"
    )
    return "\n".join([
        _title_bar(item_id),
        head,
        f"      Item:        {item_id} — \"{title}\"",
        pr_line,
        f"      Criteria:    {crit_met}/{crit_total} met",
        closeout_line,
        state_arrow,
        _DIVIDER,
    ])


def _card_needs_human(seed: dict[str, Any], completed: dict) -> str:
    item_id = seed["item_id"]
    title = seed.get("title", "?")
    pr_num = seed.get("pr_number")
    pr_merged = seed.get("pr_merged")
    pr_state = seed.get("pr_state")
    crit_total = seed.get("criteria_total", 0)
    crit_met = seed.get("criteria_met", 0)

    if pr_num is not None:
        if pr_merged:
            pr_line = f"      PR:          #{pr_num} (merged)"
        else:
            pr_line = f"      PR:          #{pr_num} ({pr_state})"
    else:
        pr_line = "      PR:          (not resolved)"

    gaps = _gaps_from_completed(completed)
    gap_lines: list[str] = []
    if gaps:
        gap_lines.append("      Gaps:")
        for g in gaps[:5]:
            gap_lines.append(f"        ✕ {g}")
        if len(gaps) > 5:
            gap_lines.append(f"        … +{len(gaps) - 5} more")

    crit_line = (
        f"      Criteria:    {crit_met}/{crit_total} met"
        if crit_total
        else "      Criteria:    (not fetched)"
    )

    resume_hint = (
        "      Resume:      requiem resume <run-id> "
        "--decision close-anyway|reject"
    )

    lines = [
        _title_bar(item_id),
        "  🚦 Needs human",
        f"      Item:        {item_id} — \"{title}\"",
        pr_line,
        crit_line,
    ]
    lines.extend(gap_lines)
    lines.append(resume_hint)
    lines.append(_DIVIDER)
    return "\n".join(lines)


def _card_catastrophic(completed: dict) -> str:
    failures: list[tuple[str, dict[str, Any]]] = []
    for nid, payload in completed.items():
        if not isinstance(payload, dict):
            continue
        kind = payload.get("kind")
        if kind in ("permanent_failure", "needs_human", "bad_output"):
            failures.append((nid, payload))
    if not failures:
        return _title_bar("?") + "\n  ✕ No close-out attempted\n" + _DIVIDER
    nid, outcome = failures[-1]
    msg = outcome.get("message") or outcome.get("prompt") or outcome.get("kind", "?")
    return "\n".join([
        _title_bar("?"),
        "  ✕ Did not close",
        f"      Stopped at:  {nid}",
        f"      Reason:      {msg}",
        _DIVIDER,
    ])


def _gaps_from_completed(completed: dict) -> list[str]:
    rv = completed.get("route_verdict") or {}
    # NeedsHuman outcomes carry the context dict on the outcome dict.
    ctx = rv.get("context") or {}
    gaps = ctx.get("gaps") or []
    if gaps:
        return [str(g) for g in gaps]
    unmet = ctx.get("unmet") or []
    return [f"AB#{u.get('criterion_id', '?')} — {u.get('gap', '?')}" for u in unmet]


# ---- CloseOutResult derivation -------------------------------------


def close_out_result(completed: dict, final_node: str) -> CloseOutResult:
    """Derive the brief's ``CloseOutResult`` from the event-log projection.

    ``verdict`` is ``"closed"`` iff the run reached ``end_success``;
    everything else is ``"needs_human"`` (we deliberately collapse
    ``end_failed`` into ``"needs_human"`` here because the operator's
    move is the same: read the verdict card and decide).
    """
    seed = _summary_seed(completed)
    closeout_value = completed.get("write_closeout", {}).get("value") or {}
    cp = closeout_value.get("path")
    return CloseOutResult(
        item_id=int(seed.get("item_id") or 0),
        pr_number=int(seed.get("pr_number") or 0),
        verdict="closed" if final_node == "end_success" else "needs_human",
        closeout_path=Path(cp) if cp else None,
        gaps=_gaps_from_completed(completed),
        dry_run=bool(seed.get("dry_run")),
    )


# ---- engine factory -------------------------------------------------


def build_engine(
    log_dir: Path,
    *,
    item_id: int | None = None,
    repo: str | None = None,
    pr_number: int | None = None,
    dry_run: bool | None = None,
    closeout_dir: Path | None = None,
    toolbelt: Toolbelt | None = None,
    provider=None,
    gate_handler=_default_gate_handler,
    now_fn: Callable[[], datetime] | None = None,
) -> Engine:
    """Build an Engine for ``close-out``.

    The CLI calls ``build_engine(log_dir)``. With no arguments we ship a
    canned demo: ``--dry-run``, item 12345, a fully-scripted verifier,
    and an in-process Toolbelt — so ``requiem run
    requiem.workflows.close_out --run-id smoke`` works key-free and
    side-effect-free, matching the ``code_review_demo`` shape.

    Environment-variable overrides (read once, here, so CLI users can flip
    mode without editing source):

    * ``REQUIEM_CLOSE_OUT_ITEM``    — int
    * ``REQUIEM_CLOSE_OUT_REPO``    — str
    * ``REQUIEM_CLOSE_OUT_PR``      — int
    * ``REQUIEM_CLOSE_OUT_DRY_RUN`` — "1" / "true" / "yes"
    """
    item_id = _env_or(item_id, "REQUIEM_CLOSE_OUT_ITEM", int, 12345)
    repo = _env_or(repo, "REQUIEM_CLOSE_OUT_REPO", str, "acme/widgets")
    pr_number = _env_or(pr_number, "REQUIEM_CLOSE_OUT_PR", int, None)
    if dry_run is None:
        env = os.environ.get("REQUIEM_CLOSE_OUT_DRY_RUN")
        dry_run = (env or "").strip().lower() in ("1", "true", "yes") if env else True
    closeout_dir = closeout_dir or Path("docs/closeouts")

    inputs = CloseOutInputs(
        item_id=item_id,
        repo=repo,
        pr_number=pr_number,
        dry_run=dry_run,
        closeout_dir=closeout_dir,
    )

    if toolbelt is None:
        toolbelt = _demo_toolbelt(item_id=item_id, repo=repo, pr_number=pr_number or 347)
    if provider is None:
        provider = default_scripted_provider()

    return Engine(
        workflow=build_workflow(),
        verbs=build_verb_registry(
            inputs,
            now_fn=now_fn or (lambda: datetime.now(tz=timezone.utc)),
        ),
        agents=build_agent_registry(),
        provider=provider,
        toolbelt=toolbelt,
        log_dir=log_dir,
        gate_handler=gate_handler,
    )


def _env_or(passed: Any, env_name: str, cast: Any, default: Any) -> Any:
    if passed is not None:
        return passed
    raw = os.environ.get(env_name)
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


# ---- canned-data demo toolbelt -------------------------------------


def _demo_toolbelt(*, item_id: int, repo: str, pr_number: int) -> Toolbelt:
    """In-module fakes so the standalone demo runs without network access.

    Tests should prefer the richer fakes in ``tests/fakes/clients.py`` —
    these exist only so ``requiem run requiem.workflows.close_out``
    delivers a complete narration + verdict card with no setup.
    """
    parent_id = item_id
    criteria = [
        TwigItem(
            id=22001, title="kernel routes BadOutput to a remediation branch",
            state="Resolved", area_path="Requiem\\Phase B",
            work_item_type="Acceptance Criteria", parent_id=parent_id, raw={},
        ),
        TwigItem(
            id=22002, title="verb `route` is covered by tests for all 6 outcomes",
            state="Resolved", area_path="Requiem\\Phase B",
            work_item_type="Acceptance Criteria", parent_id=parent_id, raw={},
        ),
        TwigItem(
            id=22003, title="dispatch table grows without code in `_route`",
            state="Resolved", area_path="Requiem\\Phase B",
            work_item_type="Acceptance Criteria", parent_id=parent_id, raw={},
        ),
    ]
    parent = TwigItem(
        id=parent_id,
        title="Refactor outcome dispatch in kernel",
        state="In Review",
        area_path="Requiem\\Phase B",
        work_item_type="Task",
        parent_id=None,
        raw={
            "id": parent_id,
            "title": "Refactor outcome dispatch in kernel",
            "pullRequests": [{"repo": repo, "number": pr_number, "title": ""}],
            "children": [{"id": c.id} for c in criteria],
        },
    )
    pr = GhPullRequest(
        number=pr_number,
        title="Refactor outcome dispatch in kernel",
        state="MERGED",
        merged=True,
        merged_at=datetime(2026, 5, 31, 14, 22, 0, tzinfo=timezone.utc),
        head="feature/refactor-outcomes",
        base="main",
        url=f"https://github.com/{repo}/pull/{pr_number}",
        raw={"mergeCommit": {"oid": "a3f9c7e1234567890abcdef0123456789abcdef0"}},
    )

    twig = _DemoTwig({parent_id: parent, **{c.id: c for c in criteria}})
    gh = _DemoGh({(repo, pr_number): pr})
    fs = _DemoFs()

    return Toolbelt(
        git=RealGitClient(),
        files=RealFileClient(),
        gh=gh,        # type: ignore[arg-type]
        twig=twig,    # type: ignore[arg-type]
        fs=fs,        # type: ignore[arg-type]
    )


@dataclass
class _DemoTwig:
    items: dict[int, TwigItem]

    async def show_async(self, item_id: int) -> TwigItem:
        if item_id not in self.items:
            raise TwigItemNotFoundError(f"demo: no item {item_id}")
        return self.items[item_id]

    async def set_state_async(self, item_id: int, new_state: str) -> TwigItem:
        item = self.items[item_id]
        updated = TwigItem(
            id=item.id, title=item.title, state=new_state,
            area_path=item.area_path, work_item_type=item.work_item_type,
            parent_id=item.parent_id, raw=item.raw,
        )
        self.items[item_id] = updated
        return updated

    async def list_children_async(self, parent_id: int) -> list[TwigItem]:
        parent = await self.show_async(parent_id)
        stubs = parent.raw.get("children") or []
        return [
            self.items[int(s["id"])]
            for s in stubs
            if isinstance(s, dict) and int(s.get("id", -1)) in self.items
        ]


@dataclass
class _DemoGh:
    pr_by_number: dict[tuple[str, int], GhPullRequest]

    async def pr_view(self, repo: str, number: int) -> GhPullRequest:
        key = (repo, number)
        if key not in self.pr_by_number:
            raise GhNotFoundError(f"demo: no PR {number} in {repo}")
        return self.pr_by_number[key]


@dataclass
class _DemoFs:
    files: dict[Path, str] = field(default_factory=dict)

    def write_text(self, path: Path, content: str) -> None:
        self.files[Path(path)] = content

    def exists(self, path: Path) -> bool:
        return Path(path) in self.files

    def read_text(self, path: Path) -> str:
        return self.files[Path(path)]


# ---- standalone CLI entry point ------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m requiem.workflows.close_out",
        description="Close-out workflow — verify PR satisfies criteria + close item.",
    )
    p.add_argument("--item", type=int, default=12345, help="Work-item id to close")
    p.add_argument("--repo", default="acme/widgets", help="owner/repo of the PR")
    p.add_argument(
        "--pr", type=int, default=None,
        help="PR number (default: look up from item's linked PRs)",
    )
    p.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Inspect-only: no fs.write, no twig.set_state",
    )
    p.add_argument(
        "--live", action="store_true", default=False,
        help="Use real Toolbelt.real() instead of canned-data fakes",
    )
    p.add_argument("--run-id", default=None)
    p.add_argument("--log-dir", default=".runs")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    log_dir = Path(args.log_dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    run_id = args.run_id or f"closeout-{int(time.time())}"

    if args.live:
        toolbelt = Toolbelt(
            git=RealGitClient(),
            files=RealFileClient(),
            gh=GhClient(),
            twig=TwigClient(),
            fs=FilesystemClient(Path.cwd()),
        )
    else:
        toolbelt = None

    engine = build_engine(
        log_dir,
        item_id=args.item,
        repo=args.repo,
        pr_number=args.pr,
        dry_run=args.dry_run,
        toolbelt=toolbelt,
    )

    from requiem.cli.render import render_event
    from requiem.cli.main import _render_context_for, _print_verdict_card

    mod = sys.modules[__name__]
    cx = _render_context_for(mod, engine.workflow.name, engine.workflow.humanize)

    print(f"requiem.workflows.close_out — run_id={run_id}")
    print(f"log: {engine.log_path(run_id)}")
    print("─" * 72)

    def _observer(envelope: dict[str, Any]) -> None:
        for line in render_event(envelope, cx):
            print(line)
    engine.on_event = _observer

    result = asyncio.run(engine.run(run_id))

    print("─" * 72)
    _print_verdict_card(mod, cx)
    print(f"result: {type(result).__name__}")
    print(f"log: {engine.log_path(run_id)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
