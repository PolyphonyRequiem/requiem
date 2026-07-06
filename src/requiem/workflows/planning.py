"""Recursive planning workflow — Fauré (Phase C seat).

Takes a root work item and produces a plan: either a **leaf** (atomic,
implementable directly) or **decomposable** (a list of proposed child
work items). A reviewer agent critiques the plan; the planner gets up to
three iterations before the workflow escalates to a human.

## Topology

::

    start → guard_depth → fetch_item → policy_classifier
        policy_classifier ─ success    → planner_1 → reviewer_1 → router_1
        policy_classifier ─ short_circuit_implementable → record_leaf_from_policy → end
        router_1 ─ approve  → branch_decomposable
        router_1 ─ revise   → planner_2 → reviewer_2 → router_2
            ...continues up to planner_{ITER_CAP} → reviewer_{ITER_CAP} → router_{ITER_CAP}
            router_{ITER_CAP} ─ approve  → branch_decomposable
            router_{ITER_CAP} ─ revise   → escalation_gate
            router_{ITER_CAP} ─ escalate → escalation_gate
        router_i (i < ITER_CAP) ─ escalate → escalation_gate

(``ITER_CAP`` is defined as a module constant; the topology is generated
by a loop, so bumping the constant scales the planner/reviewer chain
automatically. ``policy_classifier`` short-circuits implementable types
out of the LLM loop entirely per ADR-0025 Gap A.)

    branch_decomposable
        ─ leaf                          → record_plan → end
        ─ decomposable & depth_ok       → prep_child_1 → child_1 → prep_child_2 → child_2 → …
                                              → prep_child_N (no_more_children)
                                              → aggregate_children → record_plan → end
        ─ decomposable & depth_exceeded → recursion_depth_gate
        ─ decomposable & too_many       → too_many_children_gate
    prep_child_i
        ─ has child → child_i (sub-workflow)
        ─ no more children → aggregate_children
        ─ cycle detected → cycle_gate
    child_i (sub-workflow)
        ─ success → prep_child_{i+1}    (or aggregate_children if i==MAX_CHILDREN)
        ─ permanent_failure → escalation_gate
        ─ needs_human → escalation_gate (gate fires on the parent's behalf)
        ─ cancelled → fail_end

    depth_gate / twig_gate / bad_output_gate / escalation_gate /
    recursion_depth_gate / too_many_children_gate / cycle_gate
        (human gates with `proceed` / `abort` options)

The 3-iteration cap is a **topology fact**, not a runtime counter: three
explicit (planner, reviewer, router) triples chained on the ``revise``
edge. This honours `INV-NO-CORRUPT-FORWARD` — the cap can't drift at
runtime, and the resume cursor knows exactly which iteration it's in
because the node ids are distinct.

## Recursion (decomposable children) — IMPLEMENTED (Fauré seat 2)

When the planner produces a decomposable plan, each proposed child is
spawned as its own ``planning`` sub-workflow via the kernel's
``SubWorkflowNode`` primitive (ADR 0005). Children are processed
**sequentially** — one node per child, pre-built up to ``MAX_CHILDREN``.
v0 picks sequential over ``parallel_fork`` to keep the resume cursor
trivially per-child; parallel fanout is a Phase D optimisation.

Inputs (``item_id``, ``current_depth + 1``, ``max_depth``,
``parent_plan_id``, ``ancestor_item_ids``) are threaded via the
sub-workflow's ``inputs_verb``: the kernel records the dict in the
``subworkflow_started.inputs_summary`` event and re-reads it on the
child engine's construction (ADR 0005 addendum). No sidecar.

## Recursion safety

* ``guard_depth`` at the top of every invocation enforces ``current_depth
  <= max_depth`` (depth_gate fires otherwise).
* ``branch_decomposable`` enforces ``current_depth + 1 <= max_depth``
  BEFORE spawning children — otherwise routes to ``recursion_depth_gate``.
* Each ``prep_child_i`` enforces a **cycle check**: if the child's
  proposed (or synthesised) ``item_id`` appears in the parent's
  ``ancestor_item_ids`` tuple, routes to ``cycle_gate``.
* If the planner proposes more than ``MAX_CHILDREN`` children, routes to
  ``too_many_children_gate``. v0 cap is 8 — bump in v1 with care.

## Twig integration

`fetch_item` reads the root via the injected twig client. Polyphony's
``create_child`` is **not** invoked: per the brief, children are
proposals in v0, not commitments. A future ``commit-plan`` workflow
turns proposals into real ADO items.

## Plan persistence

One sidecar per run, written under ``log_dir``:

* leaf:         ``<run_id>.plan.md``
* decomposable: ``<run_id>.plan.tree.json`` (recursive tree with
  grandchildren / great-grandchildren as nested objects)

The event log remains the authoritative record — sidecars are for the
human-facing verdict card and downstream tooling.

## Test-fake threading across recursion layers

Production callers pass a real ``TwigClient`` + real ``AgentProvider``
to the root ``build_engine``. Tests need the same fakes to apply to
*recursively-spawned* child engines. We use module-level
``contextvars`` (``_active_twig_cv``, ``_active_provider_cv``,
``_active_gate_handler_cv``): the root ``build_engine`` installs the
operator-supplied seams into the context, and child ``build_engine``
calls (invoked by the kernel inside the same asyncio task) read them as
the default. This avoids serialising un-pickleable objects through the
``inputs_summary`` JSON.
"""
from __future__ import annotations

import contextvars
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, Sequence

from pydantic import BaseModel, Field

from requiem.agent import AgentProvider, AgentSpec, FakeProvider
from requiem.clients.twig import (
    TwigClient,
    TwigClientError,
    TwigItem,
    TwigItemNotFoundError,
    TwigRateLimitedError,
)
from requiem.dsl import AgentRegistry, VerbRegistry, Workflow, WorkflowBuilder
from requiem.kernel import Engine
from requiem.outcomes import NeedsHuman, PermanentFailure, RetryableFailure, Success
from requiem.persistence import replay
from requiem.process_config import ProcessConfig, default_process_config
from requiem.toolbelt import Toolbelt


# Maximum number of children we pre-build subworkflow nodes for. If the
# planner proposes more than this, the workflow routes to a human gate
# rather than silently truncating. Sequential per-child topology (no
# parallel_fork yet) keeps the resume cursor trivially per-child.
MAX_CHILDREN = 8


# Module-level contextvars used to thread test fakes (twig / provider /
# gate_handler) into recursively-spawned child engines. The kernel calls
# `build_engine(log_dir, **recorded_inputs)` for the child without any
# twig / provider — those objects aren't JSON-serialisable into
# `inputs_summary`. So the root caller's `build_engine` installs the
# active seams here, and child invocations (running in the same asyncio
# task) inherit. See module docstring §"Test-fake threading".
_active_twig_cv: contextvars.ContextVar["TwigClientProto | None"] = (
    contextvars.ContextVar("requiem.planning.active_twig", default=None)
)
_active_provider_cv: contextvars.ContextVar["AgentProvider | None"] = (
    contextvars.ContextVar("requiem.planning.active_provider", default=None)
)
_active_gate_handler_cv: contextvars.ContextVar[Any] = (
    contextvars.ContextVar("requiem.planning.active_gate_handler", default=None)
)
# The process config IS recorded into child sub-workflow inputs (durable,
# restart-safe — see ``child_inputs``), so unlike the seams above it never
# depends on this contextvar for correctness across a restart. The contextvar
# is only a convenience for in-process recursive construction; recorded inputs
# (and the ``start_run`` snapshot) remain authoritative (INV-RESTART).
_active_process_config_cv: contextvars.ContextVar["ProcessConfig | None"] = (
    contextvars.ContextVar("requiem.planning.active_process_config", default=None)
)


# ---- public dataclasses --------------------------------------------------


@dataclass(frozen=True)
class PlanResult:
    """The structured outcome a parent workflow consumes.

    `children` is non-empty only when `decomposable=True`. After Fauré
    seat 2, children are themselves `PlanResult` instances reconstructed
    from each sub-workflow run — the tree is fully recursive.
    """

    item_id: int
    plan_id: str
    decomposable: bool
    children: list["PlanResult"]
    summary: str
    review_iterations: int
    final_verdict: Literal["approved", "needs_human"]
    proposals: list[dict[str, Any]] = field(default_factory=list)
    """The planner's raw child proposals for *this* node (creatable
    metadata: title / description / work_item_type / optional pinned
    ``item_id`` / optional ``review_group``).

    Carried through the recursive serialisation so the ``.plan.tree.json``
    artifact is self-describing at every depth — the downstream
    ``commit_plan`` workflow can seed ADO children at any level without
    folding each sub-run's event log. ``children[i]`` aligns with
    ``proposals[i]`` by index (planning spawns one sub-workflow per
    proposal in order); each child's ``item_id`` is the synthesised id
    derived from this node, so alignment is verifiable.
    """


# ---- typed agent outputs ------------------------------------------------


class ChildPlan(BaseModel):
    title: str
    description: str
    # ADR-0015 §9 non-negotiable #1: requiem is TYPE-AGNOSTIC. The
    # engine must not name ADO work-item types in code. The Literal
    # whitelist `["Task", "Bug", "User Story"]` was wrong (ChildPlan
    # could never produce a Feature/Deliverable/Story even when the
    # operator's process.yaml listed them); widening it to a bigger
    # Literal (commit 6f68888) was wrong in the same direction. The
    # correct shape: ``str`` at the Pydantic layer, validated at the
    # workflow layer against ``ProcessConfig.types`` (the operator's
    # source of truth). A planner-emitted type the operator hasn't
    # declared routes to ``type_policy_gate`` for human resolution
    # rather than being silently accepted or hard-rejected at parse.
    work_item_type: str
    item_id: int | None = None
    """Optional pinned ADO id for the child.

    When omitted the workflow synthesises a deterministic id (see
    :func:`_synth_child_id`). Tests use this to engineer cycle scenarios
    (set a child's ``item_id`` equal to an ancestor's).
    """

    review_group: str | None = None
    """Optional planner-assigned grouping label for related leaves.

    The planner *may* set this when it perceives a natural grouping among
    implementable children (e.g. ``"data-layer"`` / ``"ui-layer"``); a
    review surface can then cluster the corresponding impl PRs by group.
    Absence is a valid no-op and the planner is never required to assign
    one. The value is deliberately **not** validated against a closed
    enum in v0 — it is a free-form curation hint, not a contract.

    This is the lightweight seed of the agent-driven review-surface
    curation policy (ADR-0006 §Q7); the richer cut / gate / persist /
    review-surface decisions live in ADR-0008. It has **no**
    branch-topology impact — it is a planner-output schema field plus a
    downstream render hint.
    """

    depends_on: list[int] | None = None
    """Optional 0-based indices into THIS SAME children list, naming sibling
    children whose implementation must exist first.

    This is a real dispatch-ordering contract, not a narrative "logically
    follows" hint: both fan-out backends will not dispatch a leaf until
    every declared dependency has landed (and, on the in-process backend,
    merged into the trunk) — see ``requiem.workflows.leaf_deps``. A leaf
    whose dependency never lands is reported ``blocked``, not silently
    attempted with stale/missing context.

    Use this ONLY for a genuine build-time prerequisite: the dependent
    child's code would not compile, or would have nothing correct to
    reference, without the dependency's changes already present in the
    worktree (e.g. child 2 defines a shared config schema that child 5
    migrates existing overrides onto; child 0 authors a service-resource
    that child 3 wires output-chaining from). Do NOT use it to express a
    preferred review/read order — most children should leave it unset.
    Self-references, out-of-range indices, and references to a sibling
    that is itself decomposed (not a leaf) are rejected.
    """


class PlannerOutput(BaseModel):
    summary: str
    decomposable: bool
    children: list[ChildPlan] = Field(default_factory=list)
    estimated_complexity: Literal["trivial", "small", "medium", "large", "unknown"]
    rationale: str


class ReviewerOutput(BaseModel):
    verdict: Literal["approve", "revise", "escalate"]
    feedback: str = ""


# ---- agent specs --------------------------------------------------------


PLANNER = AgentSpec(
    name="planner",
    charter=(
        "You take a work item and produce an actionable plan. Decide if "
        "the item is atomic (a leaf — `decomposable=False`) or needs to "
        "be broken into child work items (`decomposable=True` with a "
        "non-empty `children` list). Be conservative: items only "
        "decompose when there are concrete, distinct sub-tasks."
    ),
    response_model=PlannerOutput,
)

REVIEWER = AgentSpec(
    name="plan_reviewer",
    charter=(
        "You critique a plan produced by the planner. Verdict is "
        "`approve` if the plan is sound, `revise` if the planner can fix "
        "it with one more pass given feedback, `escalate` if the issue "
        "is outside the planner's authority (scope, policy, ambiguity)."
    ),
    response_model=ReviewerOutput,
)

ALL_SPECS = [PLANNER, REVIEWER]


# ---- twig seam ---------------------------------------------------------


class TwigClientProto(Protocol):
    """Subset of `TwigClient` the planning workflow uses.

    Declared as a Protocol so tests can substitute fakes without touching
    the real subprocess seam. The real client satisfies this structurally.

    NOTE: the protocol requires the *async* surface. The sync `show()`
    wrapper on the real `TwigClient` internally calls `asyncio.run`, which
    explodes when invoked from a verb running under the kernel's own
    event loop (see `tests/test_bugbash_regressions.py` —
    `test_planning_fetch_item_does_not_call_sync_twig`). Verbs must
    `await twig.show_async(...)` so the call stays on the kernel loop.
    """

    async def show_async(self, item_id: int) -> TwigItem: ...


@dataclass
class FakeTwigClient:
    """In-memory twig stand-in for tests and the self-contained demo.

    Maps `{item_id: TwigItem}`. Raises `TwigItemNotFoundError` for misses
    so the workflow's `fetch_item` verb exercises its real classifier path.
    """

    items: dict[int, TwigItem] = field(default_factory=dict)

    async def show_async(self, item_id: int) -> TwigItem:
        if item_id not in self.items:
            raise TwigItemNotFoundError(f"fake: item {item_id} not found")
        return self.items[item_id]


# ---- verb library ------------------------------------------------------


# Iteration cap — change this only by editing the topology in `build_workflow`.
ITER_CAP = 8
"""Max planner+reviewer iterations before the workflow escalates to a
human gate. Iteration history (all on the SKU-fallback dogfood):

* 3 (original): converged on simple Tasks but ran out for complex Scenarios
* 5 (commit 2b1979e): bumped after reviewer-prompt fix unlocked
  substantive feedback the planner needed more rounds to address.
  Still insufficient on plannable Features with cross-cutting concerns
  (security review timing, observability ownership, dependency chains).
* 8 (this commit): bumped after ADR-0026 dogfood retry showed even
  Features under Scenarios need more rounds to converge against
  high-quality reviewer feedback. The right long-term answer is
  `--on-escalate=accept-last` policy (where the workflow ships the
  last planner output and routes the reviewer's escalation feedback
  to a side-channel rather than blocking) — tracked separately.

This is a temporary lever. The right shape is a configurable per-run
``iter_cap`` parameter on ``build_engine`` / ``--max-plan-iterations``
CLI flag plus the escalation policy. See ADR-0025 open questions.
"""

# Version stamp for the `.plan.tree.json` sidecar. Bumped to 2 when each
# recursive node gained its own `proposals` list (making the artifact
# self-describing for the downstream `commit_plan` seeding workflow).
# `commit_plan` refuses artifacts below this version — older trees lack the
# per-node creatable metadata it needs.
PLAN_TREE_SCHEMA_VERSION = 2


def build_verb_registry(
    *,
    item_id: int,
    parent_plan_id: str | None,
    max_depth: int,
    current_depth: int,
    ancestor_item_ids: Sequence[int],
    twig: TwigClientProto,
    log_dir: Path,
    config: ProcessConfig | None = None,
    child_proposal: dict[str, Any] | None = None,
) -> VerbRegistry:
    verbs = VerbRegistry()
    ancestor_set = tuple(int(a) for a in ancestor_item_ids)
    closed_config = config or default_process_config()

    def _effective_config(ctx) -> ProcessConfig:
        """The tier-routing config for this run.

        Prefer the durable snapshot recorded by ``start_run`` so a resume
        re-reads the routing facts the run was started with, not ambient disk
        (INV-RESTART). Fall back to the closed-over config for direct-verb unit
        tests that invoke a verb without running ``start_run`` first. This
        mirrors root_dispatch's ``validate_root``.
        """
        snap = (ctx.completed.get("start") or {}).get("value", {}).get(
            "process_config"
        )
        return ProcessConfig.from_snapshot(snap) if snap else closed_config

    @verbs.register("start_run")
    def _start(ctx):
        return Success(
            value={
                "item_id": item_id,
                "parent_plan_id": parent_plan_id,
                "current_depth": current_depth,
                "max_depth": max_depth,
                "ancestor_item_ids": list(ancestor_set),
                # Snapshot the effective tier-routing config so a resume reads
                # the durable facts rather than re-reading disk (INV-RESTART).
                "process_config": closed_config.to_snapshot(),
                # Recursive-child proposal carried over from the parent (see
                # build_engine docstring + fetch_item). Top-level runs leave
                # this None and fetch_item calls twig; recursive children
                # receive the parent's already-resolved ChildPlan and skip the
                # twig fetch (the child's id is a synthesised
                # parent_id*100+slot that does NOT exist in ADO until
                # commit_plan seeds it).
                "child_proposal": child_proposal,
            },
        )

    @verbs.register("guard_depth")
    def _guard(ctx):
        if current_depth > max_depth:
            return PermanentFailure(
                error_kind="depth_exceeded",
                message=(
                    f"planning depth {current_depth} exceeds cap {max_depth} "
                    f"for item {item_id}"
                ),
                details={"current_depth": current_depth, "max_depth": max_depth},
            )
        return Success(value={"current_depth": current_depth, "max_depth": max_depth})

    @verbs.register("fetch_item")
    async def _fetch(ctx):
        # Recursive-child path: the parent already resolved this child's
        # title / description / work_item_type from its planner output and
        # passed it through `child_inputs` → `start_run`. The child's
        # synthesised id (parent_id*100+slot) does NOT exist in ADO until
        # `commit_plan` seeds it, so a twig lookup would fail. Use the
        # parent-supplied proposal directly and skip twig.
        start_val = (ctx.completed.get("start") or {}).get("value") or {}
        proposal = start_val.get("child_proposal")
        if proposal:
            return Success(
                value={
                    "item_id": item_id,
                    "title": proposal.get("title") or "",
                    "state": proposal.get("state") or "Proposed",
                    "work_item_type": proposal.get("work_item_type") or "",
                    "area_path": proposal.get("area_path") or "",
                    "description": proposal.get("description") or "",
                },
                inspected_artifacts=(
                    f"planner:proposal/{parent_plan_id or 'recursive'}@{item_id}",
                ),
            )
        try:
            item = await twig.show_async(item_id)
        except TwigItemNotFoundError as e:
            return PermanentFailure(
                error_kind="twig_not_found",
                message=f"work item {item_id} not found: {e}",
            )
        except TwigRateLimitedError as e:
            # The classifier path exists; surface it as retryable. The
            # demo workflow doesn't wire a retry budget on fetch_item,
            # so this currently falls through to permanent_failure.
            return RetryableFailure(
                retry_key=f"{ctx.run_id}:fetch_item",
                error_kind="rate_limited",
                message=str(e),
                attempt=ctx.attempt,
            )
        except TwigClientError as e:
            # Ravel's L-1 caveat: unknown twig failure -> human gate.
            return PermanentFailure(
                error_kind="twig_unknown",
                message=f"twig error fetching {item_id}: {e}",
            )
        # ADR (gap fix, 2026-07-05 dogfood run #34 postmortem): the planner
        # and reviewer previously only ever saw title/type/state — never
        # the work item's actual description. On AB#62759077 the title
        # ("Capacity-aware VMSS SKU fallback for regional cluster
        # deployments") was terse enough that the planner filled the gap
        # by inventing a whole "provision brand-new regions" subtree, even
        # though the real description explicitly scoped this to EXISTING
        # regions/existing bicep integration points. Threading the real
        # description through closes the grounding gap at its source.
        return Success(
            value={
                "item_id": item.id,
                "title": item.title,
                "state": item.state,
                "work_item_type": item.work_item_type,
                "area_path": item.area_path,
                "description": (item.raw.get("fields") or {}).get(
                    "System.Description"
                )
                or "",
            },
            inspected_artifacts=(f"twig:item/{item.id}",),
        )

    @verbs.register("policy_classifier")
    def _policy_classify(ctx):
        """ADR-0025 Gap A: short-circuit planning for implementable types.

        When the process-config tier policy classifies the work item's
        type as ``implementable``, the planner and reviewer have no
        useful work to do — the planner can only restate the title,
        and the reviewer's "escalate" verdict on a vague leaf summary
        cascades into a tree-killing failure (see ADR-0025).

        Routing (using the established convention of returning
        ``PermanentFailure`` to drive non-default workflow edges; same
        trick branch_decomposable uses for the ``recurse`` branch):

        * ``Success({"tier": ...})`` → planner_1 (legacy path,
          unchanged) — fires for ``decomposable``, ``unknown``, and
          unset policy.
        * ``PermanentFailure("short_circuit_implementable")`` →
          record_leaf_from_policy (no LLM calls).
        """
        item = ctx.completed["fetch_item"]["value"]
        work_item_type = (item.get("work_item_type") or "").strip()
        policy = _effective_config(ctx).tier_for_type(work_item_type)
        if policy == "implementable":
            return PermanentFailure(
                error_kind="short_circuit_implementable",
                message=(
                    f"type {work_item_type!r} is in implementable_types; "
                    f"skipping planner+reviewer (ADR-0025 Gap A)"
                ),
                details={
                    "tier": policy,
                    "work_item_type": work_item_type,
                },
            )
        return Success(
            value={
                "tier": policy or "unset",
                "work_item_type": work_item_type,
            },
        )

    @verbs.register("record_leaf_from_policy")
    def _record_policy_leaf(ctx):
        """Record a synthesised leaf plan when policy_classifier short-
        circuited (tier=='implementable').

        Produces a ``record_plan``-shape Success so downstream consumers
        (`project_plan_result`, the renderer, the executor) read it
        identically to a planner-driven leaf. The inspected_artifact
        ``policy:implementable/<type>`` makes the policy-driven origin
        legible in the event log.
        """
        item = ctx.completed["fetch_item"]["value"]
        work_item_type = (item.get("work_item_type") or "").strip()
        plan_id = f"plan-{item['item_id']}-{ctx.run_id}"

        # Synthesise a planner-shaped dict so the sidecar writer can
        # serialise it without special-casing.
        synthetic_planner = {
            "summary": item.get("title") or "",
            "decomposable": False,
            "children": [],
            "estimated_complexity": "unknown",
            "rationale": (
                f"Forced leaf per repo process config: type "
                f"{work_item_type!r} is in implementable_types. "
                f"Planner and reviewer skipped (ADR-0025 Gap A)."
            ),
        }
        artifact = _write_plan_sidecar(
            log_dir=log_dir,
            run_id=ctx.run_id,
            plan_id=plan_id,
            item=item,
            planner=synthetic_planner,
            approved_iteration=0,         # no iteration; no review happened
            current_depth=current_depth,
            recursive_children=[],
            effective_decomposable=False,
            policy_tier="implementable",
            discarded_child_count=0,
        )
        return Success(
            value={
                "plan_id": plan_id,
                "item_id": item["item_id"],
                "item_title": item["title"],
                "decomposable": False,
                "children": [],
                "proposals": [],
                "policy_tier": "implementable",
                "overrode_planner": False,
                "discarded_child_count": 0,
                "summary": synthetic_planner["summary"],
                "estimated_complexity": "unknown",
                "review_iterations": 0,
                "final_verdict": "policy-forced-leaf",
                "plan_artifact": str(artifact),
            },
            inspected_artifacts=(
                f"policy:implementable/{work_item_type}",
                f"file:{artifact}",
            ),
        )

    def _planner_prompt(iteration: int):
        def _prompt(ctx):
            item = ctx.completed["fetch_item"]["value"]
            cfg = _effective_config(ctx)
            wit = item.get("work_item_type")
            policy = cfg.tier_for_type(wit)
            policy_line = ""
            if policy == "implementable":
                policy_line = (
                    f"Per repo process policy, work items of type "
                    f"'{item['work_item_type']}' are IMPLEMENTABLE leaves: do NOT "
                    "decompose. Set decomposable=false and propose no children.\n"
                )
            elif policy == "decomposable":
                policy_line = (
                    f"Per repo process policy, work items of type "
                    f"'{item['work_item_type']}' MUST be decomposed: set "
                    "decomposable=true and propose at least one child.\n"
                )

            # ADR-0026 step 2: inject per-type decomposition_guidance
            # AFTER the generic policy line. This is the domain-specific
            # lever ("Decompose into Features. NEVER directly into Tasks.")
            # that steers the LLM toward the right child types. Only
            # injected for plannable types (implementable types short-
            # circuit out of the planner via Gap A and never reach this
            # prompt). Falls back to an empty string when no guidance is
            # configured — the generic policy line still applies.
            guidance = cfg.decomposition_guidance_for(wit)
            guidance_line = ""
            if guidance and policy == "decomposable":
                guidance_line = (
                    f"Repo-specific decomposition guidance for "
                    f"'{item['work_item_type']}':\n  {guidance.strip()}\n"
                )

            # Description grounding fix (2026-07-05): without this the
            # planner only ever sees the bare title and hallucinates scope
            # to fill the gap. No truncation — see ADR-0025's "trust the
            # input, show it complete" precedent; a truncated description
            # invites the same false-signal problem truncated children
            # descriptions caused for the reviewer.
            desc = (item.get("description") or "").strip()
            description_line = (
                f"Work item description (ground every proposed child in "
                f"this, not just the title):\n{desc}\n"
                if desc
                else ""
            )

            # Dependency-declaration fix (2026-07-06 dogfood run #36): 3 of 4
            # needs_human leaves were a coder correctly refusing to invent a
            # sibling's schema/producer that hadn't landed in the worktree
            # yet — the plan text implied a build order the dispatcher never
            # enforced. Give the planner an explicit, indexable way to say
            # so, instead of leaving it as narrative prose dispatch can't act
            # on.
            depends_on_line = (
                "If a child you are proposing can only be correctly "
                "implemented once ANOTHER child in this same list has "
                "landed (e.g. it needs a shared schema/type/service-resource "
                "that sibling defines), set that child's `depends_on` to the "
                "0-based index/indices of the prerequisite sibling(s) in "
                "THIS children list. Only use this for a real build-time "
                "prerequisite — not a preferred read/review order. Most "
                "children should leave it unset.\n"
            )

            base = (
                f"Plan work item AB#{item['item_id']} — \"{item['title']}\" "
                f"(type={item['work_item_type']}, state={item['state']}).\n"
                f"Current planning depth: {current_depth} of {max_depth}.\n"
                + description_line
                + policy_line
                + guidance_line
                + depends_on_line
            )
            if iteration == 1:
                return base + "Produce a first plan."
            # On revise: re-read the prior reviewer's feedback.
            prior = ctx.completed.get(f"reviewer_{iteration - 1}", {})
            feedback = (prior.get("value", {}).get("parsed") or {}).get("feedback", "")
            return (
                base
                + f"This is revision attempt {iteration}. Reviewer feedback to address:\n"
                + (feedback or "(no feedback recorded — produce a tightened plan)")
            )

        return _prompt

    def _reviewer_prompt(iteration: int):
        def _prompt(ctx):
            planner = ctx.completed[f"planner_{iteration}"]["value"]["parsed"]
            item = ctx.completed["fetch_item"]["value"]
            children = planner.get("children") or []
            # ADR-0025 Gap A* fix: render the actual children so the
            # reviewer can evaluate the decomposition, not just see a
            # count. Pre-fix the prompt showed "children: N proposed"
            # which forced reviewers to escalate ("cannot evaluate
            # without seeing the children") — see 2026-06-17 #62759077
            # dogfood run 4. Each child rendered as a numbered bullet
            # with title, type, and description.
            if children:
                child_block = "\n  proposed children:\n"
                for i, c in enumerate(children, 1):
                    title = c.get("title", "(no title)")
                    wit = c.get("work_item_type", "(no type)")
                    desc = c.get("description", "") or ""
                    # 0-based slot, matching the planner's `depends_on`
                    # index convention (requiem.plan_tree._synth_of).
                    child_block += f"    {i}. [slot {i - 1}] [{wit}] {title}\n"
                    if desc:
                        # Indent the description so it visually nests under
                        # the title. NO truncation — see ADR-0025 dogfood
                        # run 7 (2026-06-17): truncating at 400 chars made
                        # the reviewer think the PLANNER produced truncated
                        # descriptions ("description is truncated mid-
                        # sentence at 'recommended SKU p…'"). It kept
                        # revising to "fix" what was actually a rendering
                        # artifact, never converging. Trust the planner's
                        # output — show it complete.
                        desc_clean = desc.strip()
                        child_block += f"        {desc_clean}\n"
                    deps = c.get("depends_on") or []
                    if deps:
                        child_block += f"        depends_on: slot(s) {deps}\n"
            else:
                child_block = "\n  proposed children: none (leaf plan)\n"

            # Description grounding fix (2026-07-05): give the reviewer the
            # same source text the planner had, so it can catch scope
            # drift (children that don't match the actual ask) — not just
            # internal inconsistency within the planner's own output.
            desc = (item.get("description") or "").strip()
            desc_block = (
                f"\n  original work item description:\n    {desc}\n"
                if desc
                else ""
            )

            return (
                f"Review the following plan for AB#{item['item_id']} "
                f"(\"{item['title']}\"):\n"
                f"{desc_block}\n"
                f"  summary: {planner['summary']}\n"
                f"  decomposable: {planner['decomposable']}\n"
                f"  estimated_complexity: {planner['estimated_complexity']}\n"
                f"  rationale: {planner['rationale']}"
                f"{child_block}\n"
                "Check that the children are grounded in the original "
                "description above (not just the title) before approving — "
                "escalate or request revision if a child invents scope "
                "(e.g. work that already exists) not supported by it.\n"
                "Also sanity-check any `depends_on` slot references: they "
                "must point to a real sibling slot in THIS list (not "
                "itself), and should only be set for a genuine build-time "
                "prerequisite (the dependent child needs code/schema the "
                "referenced sibling produces) — not merely a preferred "
                "review order. Request revision if a dependency looks "
                "wrong, missing, or spurious.\n"
                f"Iteration: {iteration} of {ITER_CAP}. "
                "Approve, request revision, or escalate."
            )

        return _prompt

    for i in range(1, ITER_CAP + 1):
        verbs.register(f"planner_prompt_{i}")(_planner_prompt(i))
        verbs.register(f"reviewer_prompt_{i}")(_reviewer_prompt(i))

    def _router(iteration: int):
        last = iteration == ITER_CAP

        def _route(ctx):
            verdict = (
                ctx.completed[f"reviewer_{iteration}"]["value"]["parsed"]["verdict"]
            )
            if verdict == "approve":
                return Success(
                    value={"verdict": "approve", "iteration": iteration},
                )
            if verdict == "escalate":
                return PermanentFailure(
                    error_kind="escalate",
                    message=f"reviewer escalated on iteration {iteration}",
                    details={"iteration": iteration},
                )
            # revise
            current_planner = ctx.completed[f"planner_{iteration}"]["value"]["parsed"]
            if iteration > 1:
                prev_planner = (
                    ctx.completed.get(f"planner_{iteration - 1}", {})
                    .get("value", {})
                    .get("parsed")
                )
                if prev_planner is not None and current_planner == prev_planner:
                    return PermanentFailure(
                        error_kind="escalate",
                        message=(
                            f"planner output did not change after revision request "
                            f"on iteration {iteration}; escalating to human review"
                        ),
                        details={
                            "iteration": iteration,
                            "stalled": True,
                            "previous_iteration": iteration - 1,
                        },
                    )
            if last:
                return PermanentFailure(
                    error_kind="escalate",
                    message=(
                        f"reviewer requested revision on iteration {iteration} "
                        f"but iteration cap {ITER_CAP} reached"
                    ),
                    details={"iteration": iteration, "cap_reached": True},
                )
            return PermanentFailure(
                error_kind="revise",
                message=f"reviewer requested revision on iteration {iteration}",
                details={"iteration": iteration},
            )

        return _route

    for i in range(1, ITER_CAP + 1):
        verbs.register(f"router_{i}")(_router(i))

    @verbs.register("branch_decomposable")
    def _branch(ctx):
        """Branch between leaf, recurse, depth-exceeded, and too-many-children.

        Returns:
        * ``Success``                         → leaf path (record_plan)
        * ``PermanentFailure("recurse")``     → prep_child_1 (kick off
                                                  sequential child spawning)
        * ``PermanentFailure("recursion_depth_exceeded")`` → recursion_depth_gate
        * ``PermanentFailure("too_many_children")``        → too_many_children_gate
        * ``PermanentFailure("config_requires_decomposition")`` → type_policy_gate
        * ``PermanentFailure("missing_work_item_type_for_policy")`` → type_policy_gate
        * ``PermanentFailure("unknown_child_work_item_type")`` → type_policy_gate

        The process config's tier policy (``implementable_types`` /
        ``decomposable_types``) is *authoritative* over the planner's
        ``decomposable`` flag: config owns the tier model (§9 #1). An
        implementable type is forced to a leaf (the safe, contracting
        direction — we never fabricate work); a decomposable type the planner
        failed to break down fails closed to a human gate. When neither tier
        set names the type, the planner's judgment stands unchanged.

        Using PermanentFailure variants as branch selectors is the
        established convention in this workflow (see ``router_i``).
        """
        approved_iter = _find_approved_iteration(ctx.completed)
        planner = ctx.completed[f"planner_{approved_iter}"]["value"]["parsed"]
        planner_decomposable = bool(planner.get("decomposable"))
        children = planner.get("children", [])
        child_count = len(children)

        cfg = _effective_config(ctx)
        work_item_type = ctx.completed["fetch_item"]["value"].get("work_item_type")
        policy = cfg.tier_for_type(work_item_type)

        # Fail closed: a configured tier policy cannot be applied to an item
        # whose type is missing/blank — surface it rather than silently
        # falling back to LLM-driven tiering (the very thing §9 #1 forbids).
        if cfg.has_tier_policy() and not (work_item_type or "").strip():
            return PermanentFailure(
                error_kind="missing_work_item_type_for_policy",
                message=(
                    "process config declares a tier policy but the work item "
                    "has no work_item_type to classify"
                ),
                details={
                    "approved_iteration": approved_iter,
                    "planner_decomposable": planner_decomposable,
                },
            )

        # Implementable types are forced to leaves regardless of the planner.
        if policy == "implementable":
            return Success(
                value={
                    "decomposable": False,
                    "branch": "leaf",
                    "child_count": 0,
                    "approved_iteration": approved_iter,
                    "policy_tier": "implementable",
                    "overrode_planner": planner_decomposable,
                    "planner_decomposable": planner_decomposable,
                    "discarded_child_count": child_count if planner_decomposable else 0,
                },
            )

        # ADR-0015 §9 #1 + ADR-0026: when the operator's process.yaml
        # declares `types`, every planner-proposed child must have a
        # work_item_type the operator has declared. The planner is free-
        # text (ChildPlan.work_item_type is `str`, not Literal — see
        # the docstring there); this is where we enforce the operator
        # contract. Unknown types route to type_policy_gate for human
        # resolution rather than being silently accepted (which would
        # let the planner invent types the seed step couldn't create
        # on ADO) or hard-rejected at parse (which would block typo
        # recovery via the gate).
        if cfg.has_tier_policy() and planner_decomposable and child_count > 0:
            known_types = set(cfg.types.keys())
            if known_types:
                unknown: list[tuple[int, str]] = []
                for i, ch in enumerate(children, start=1):
                    raw = (ch.get("work_item_type") or "").strip()
                    norm = cfg.normalize_type(raw)
                    if norm not in known_types:
                        unknown.append((i, raw))
                if unknown:
                    return PermanentFailure(
                        error_kind="unknown_child_work_item_type",
                        message=(
                            f"planner proposed {len(unknown)} child(ren) with "
                            f"work_item_type not declared in the operator's "
                            f"process config; known types: {sorted(known_types)}; "
                            f"unknown: {[(i, t) for i, t in unknown]}"
                        ),
                        details={
                            "approved_iteration": approved_iter,
                            "unknown_children": [
                                {"slot": i, "work_item_type": t} for i, t in unknown
                            ],
                            "known_types": sorted(known_types),
                            "work_item_type": work_item_type,
                        },
                    )

        # Decomposable types MUST break down; a planner leaf (or zero proposed
        # children) is an unsatisfiable policy → fail closed to a human gate.
        #
        # EXCEPTION (ADR-0026): a type with BOTH `plannable` AND
        # `implementable` facets (e.g. Feature in CVAPI) explicitly
        # allows the planner to choose either path. A leaf verdict from
        # such a type is honoured as a leaf rather than treated as a
        # policy violation. This is the "Implement directly when the
        # change fits one PR" escape hatch that polyphony's Issue type
        # has and CVAPI's Feature type inherits.
        type_is_also_implementable = cfg.has_facet(work_item_type, "implementable")
        if (
            policy == "decomposable"
            and (not planner_decomposable or child_count == 0)
            and not type_is_also_implementable
        ):
            return PermanentFailure(
                error_kind="config_requires_decomposition",
                message=(
                    f"process config requires type '{work_item_type}' to be "
                    f"decomposed, but the planner produced "
                    f"{'a leaf plan' if not planner_decomposable else 'no children'}"
                ),
                details={
                    "approved_iteration": approved_iter,
                    "policy_tier": "decomposable",
                    "planner_decomposable": planner_decomposable,
                    "child_count": child_count,
                    "work_item_type": work_item_type,
                },
            )

        # ADR-0026 bi-facet leaf: a plannable+implementable type that
        # the planner chose to leaf — return Success(leaf) so the run
        # routes to record_plan. The type's "decomposable" tier brought
        # the planner in; the type's "implementable" facet lets the
        # leaf verdict stand.
        if (
            policy == "decomposable"
            and not planner_decomposable
            and type_is_also_implementable
        ):
            return Success(
                value={
                    "decomposable": False,
                    "branch": "leaf",
                    "child_count": 0,
                    "approved_iteration": approved_iter,
                    "policy_tier": "decomposable",
                    "type_facets_allowed_leaf": True,
                    "overrode_planner": False,
                    "planner_decomposable": False,
                },
            )

        # No tier override applies; the planner's decision stands.
        decomposable = planner_decomposable
        if not decomposable:
            return Success(
                value={
                    "decomposable": False,
                    "branch": "leaf",
                    "child_count": 0,
                    "approved_iteration": approved_iter,
                    "policy_tier": policy,
                    "overrode_planner": False,
                },
            )
        if child_count > MAX_CHILDREN:
            return PermanentFailure(
                error_kind="too_many_children",
                message=(
                    f"planner proposed {child_count} children; "
                    f"workflow cap is {MAX_CHILDREN}"
                ),
                details={
                    "child_count": child_count,
                    "max_children": MAX_CHILDREN,
                    "approved_iteration": approved_iter,
                },
            )
        if current_depth + 1 > max_depth:
            return PermanentFailure(
                error_kind="recursion_depth_exceeded",
                message=(
                    f"decomposable plan at depth {current_depth} would recurse "
                    f"to depth {current_depth + 1}, exceeding max_depth={max_depth}"
                ),
                details={
                    "current_depth": current_depth,
                    "would_recurse_to": current_depth + 1,
                    "max_depth": max_depth,
                    "child_count": child_count,
                },
            )

        # ADR-0026 step 3 NOTE (2026-06-17): per-type max_nesting_depth
        # enforcement is INTENTIONALLY deferred to a follow-up. The
        # semantic ("starting from an X, the recursion may go at most N
        # levels deep") requires cap propagation through child_inputs so
        # each child subworkflow knows its remaining budget. The
        # decomposition_guidance text lever (step 2) handles 90% of the
        # steering need without the wiring complexity; the structural
        # cap can land later if guidance proves insufficient in practice.
        # The TypeConfig field is parsed and snapshotted — it's just not
        # yet consumed here.
        return PermanentFailure(
            error_kind="recurse",
            message=f"will recurse into {child_count} sub-plans",
            details={
                "decomposable": True,
                "child_count": child_count,
                "approved_iteration": approved_iter,
            },
        )

    def _approved_children_from_completed(completed: dict) -> list[dict]:
        approved_iter = _find_approved_iteration(completed)
        planner = completed[f"planner_{approved_iter}"]["value"]["parsed"]
        return list(planner.get("children", []))

    def _make_prep_child(index: int):
        """Returns the prep verb for child slot ``index`` (1-based).

        * If no child exists at this slot → ``PermanentFailure("no_more_children")``
          (routes to aggregate_children).
        * If the child's proposed/synthesised id is in ancestor_set →
          ``PermanentFailure("cycle_detected")`` (routes to cycle_gate).
        * Otherwise Success with metadata describing this child.
        """

        def _prep(ctx):
            children = _approved_children_from_completed(ctx.completed)
            if index > len(children):
                return PermanentFailure(
                    error_kind="no_more_children",
                    message=(
                        f"no child at slot {index}; "
                        f"planner proposed {len(children)}"
                    ),
                    details={"slot": index, "child_count": len(children)},
                )
            child = children[index - 1]
            child_id = _resolve_child_id(item_id, index - 1, child)
            if child_id in ancestor_set or child_id == item_id:
                return PermanentFailure(
                    error_kind="cycle_detected",
                    message=(
                        f"child {index} proposed item_id {child_id} which "
                        f"matches an ancestor in this planning chain"
                    ),
                    details={
                        "slot": index,
                        "proposed_item_id": child_id,
                        "ancestors": list(ancestor_set) + [item_id],
                    },
                )
            return Success(
                value={
                    "slot": index,
                    "child_item_id": child_id,
                    "child_title": child.get("title"),
                    "child_work_item_type": child.get("work_item_type"),
                    "child_description": child.get("description"),
                },
            )

        return _prep

    for i in range(1, MAX_CHILDREN + 1):
        verbs.register(f"prep_child_{i}")(_make_prep_child(i))

    @verbs.register("child_inputs")
    def _child_inputs(ctx):
        """Compute the inputs dict the kernel records on subworkflow_started.

        ``ctx.node_id`` is the sub-workflow node id (``child_i``). We
        derive the slot from the corresponding ``prep_child_i`` outcome
        already in ``ctx.completed`` — that's the source of truth for
        which child this slot represents and what id was assigned to it.
        """
        node_id = ctx.node_id
        # node_id is "child_<i>". The prep verb wrote everything we need.
        prep_key = node_id.replace("child_", "prep_child_")
        prep = (ctx.completed.get(prep_key) or {}).get("value") or {}
        child_id = prep.get("child_item_id")
        # parent_plan_id for the child is this run's eventual plan_id.
        # We don't have the parent's plan_id yet (record_plan runs LATER),
        # but ``parent_plan_id`` is informational; build a stable handle
        # from the parent's item_id + run_id.
        parent_handle = f"plan-{item_id}-{ctx.run_id}"
        new_ancestors = list(ancestor_set) + [item_id]
        # Carry the durable config snapshot into the child's recorded inputs so
        # the recursive sub-workflow tiers with the SAME config the parent run
        # started with — restart-safe, never re-reading ambient disk and never
        # depending on a contextvar surviving a process restart (INV-RESTART).
        snap = (ctx.completed.get("start") or {}).get("value", {}).get(
            "process_config"
        )
        # Carry the parent's already-resolved child proposal so the child's
        # `fetch_item` can skip twig (the synthesised id doesn't exist in ADO
        # until commit_plan seeds it). This is the load-bearing fix for
        # decomposable dry-runs: without this, the recursive `fetch_item`
        # would call twig with `parent_id*100+slot` and hit twig_not_found,
        # cascading into the escalation_gate.
        child_proposal = {
            "title": prep.get("child_title") or "",
            "description": prep.get("child_description") or "",
            "work_item_type": prep.get("child_work_item_type") or "",
            "state": "Proposed",
        }
        return {
            "item_id": child_id,
            "current_depth": current_depth + 1,
            "max_depth": max_depth,
            "parent_plan_id": parent_handle,
            "ancestor_item_ids": new_ancestors,
            "process_config": snap,
            "child_proposal": child_proposal,
        }

    @verbs.register("aggregate_children")
    def _aggregate(ctx):
        """Walk completed ``child_i`` sub-workflow outcomes and rebuild PlanResults.

        Each ``child_i`` whose outcome is ``Success`` has a recorded
        ``sub_run_id`` we can read the child's log from. We reconstruct
        each child's ``PlanResult`` via :func:`project_plan_result`
        (which is itself recursive over nested child sub-workflows).

        Children that were never spawned (``no_more_children`` routed
        straight to aggregate) are simply absent from ``completed``.
        """
        approved_iter = _find_approved_iteration(ctx.completed)
        children_proposals = _approved_children_from_completed(ctx.completed)
        aggregated: list[dict[str, Any]] = []
        for i in range(1, MAX_CHILDREN + 1):
            entry = ctx.completed.get(f"child_{i}")
            if entry is None:
                continue
            if entry.get("kind") != "success":
                # Defensive: a failure should have routed to escalation
                # before we reach aggregate_children. If we DO see one,
                # surface it explicitly rather than silently truncating.
                return PermanentFailure(
                    error_kind="child_aggregation_inconsistent",
                    message=(
                        f"child_{i} had outcome kind={entry.get('kind')!r}; "
                        "aggregate_children expects only successful slots"
                    ),
                    details={"slot": i, "child_outcome": entry},
                )
            sub_run_id = (entry.get("value") or {}).get("sub_run_id")
            if not sub_run_id:
                return PermanentFailure(
                    error_kind="child_aggregation_missing_sub_run_id",
                    message=f"child_{i} success outcome missing sub_run_id",
                    details={"slot": i, "child_outcome": entry},
                )
            child_log = log_dir / f"{sub_run_id}.events.jsonl"
            child_completed = completed_from_log(child_log)
            child_plan = project_plan_result(child_completed)
            if child_plan is None:
                # Child completed but produced no plan record — should not
                # happen given the workflow always records something before
                # `end`/`end_needs_human`. Treat as inconsistent.
                return PermanentFailure(
                    error_kind="child_aggregation_no_plan_record",
                    message=(
                        f"child_{i} (sub_run_id={sub_run_id!r}) produced "
                        "no plan record"
                    ),
                    details={"slot": i, "sub_run_id": sub_run_id},
                )
            aggregated.append(_plan_result_to_dict(child_plan))
        return Success(
            value={
                "approved_iteration": approved_iter,
                "child_count": len(aggregated),
                "proposed_count": len(children_proposals),
                "children": aggregated,
            },
        )

    @verbs.register("record_plan")
    def _record(ctx):
        approved_iter = _find_approved_iteration(ctx.completed)
        planner = ctx.completed[f"planner_{approved_iter}"]["value"]["parsed"]
        item = ctx.completed["fetch_item"]["value"]
        plan_id = f"plan-{item['item_id']}-{ctx.run_id}"

        # If we recursed, aggregate_children produced the recursive tree;
        # otherwise we're a leaf (or a decomposable plan that didn't
        # recurse, which the topology forbids — defensive empty list).
        aggregate = (ctx.completed.get("aggregate_children") or {}).get("value") or {}
        children = aggregate.get("children") or []

        # branch_decomposable is the authority on the effective tier decision:
        # a process-config tier override (e.g. implementable type → forced leaf)
        # must win over the planner's own ``decomposable`` flag in the durable
        # record and sidecar, or the recorded plan would contradict the routing
        # that actually happened. On the recurse path branch_decomposable routed
        # via permanent_failure (no Success value recorded); aggregate_children's
        # presence means we decomposed.
        branch_entry = ctx.completed.get("branch_decomposable") or {}
        if branch_entry.get("kind") == "success":
            bval = branch_entry.get("value") or {}
            effective_decomposable = bool(bval.get("decomposable"))
            overrode_planner = bool(bval.get("overrode_planner"))
            policy_tier = bval.get("policy_tier")
            discarded_child_count = int(bval.get("discarded_child_count") or 0)
        else:
            # Recurse path: the planner decomposed and we aggregated children.
            effective_decomposable = True
            overrode_planner = False
            policy_tier = _effective_config(ctx).tier_for_type(
                item.get("work_item_type")
            )
            discarded_child_count = 0

        # When config forced a leaf over a planner that proposed children, the
        # proposals are discarded (not committed) — keep only a diagnostic count.
        proposals = (
            [] if overrode_planner else [dict(c) for c in planner.get("children", [])]
        )

        artifact = _write_plan_sidecar(
            log_dir=log_dir,
            run_id=ctx.run_id,
            plan_id=plan_id,
            item=item,
            planner=planner,
            approved_iteration=approved_iter,
            current_depth=current_depth,
            recursive_children=children,
            effective_decomposable=effective_decomposable,
            policy_tier=policy_tier,
            discarded_child_count=discarded_child_count,
        )
        return Success(
            value={
                "plan_id": plan_id,
                "item_id": item["item_id"],
                "item_title": item["title"],
                "decomposable": effective_decomposable,
                "children": children,
                "proposals": proposals,
                "policy_tier": policy_tier,
                "overrode_planner": overrode_planner,
                "discarded_child_count": discarded_child_count,
                "summary": planner["summary"],
                "estimated_complexity": planner["estimated_complexity"],
                "review_iterations": approved_iter,
                "final_verdict": "approved",
                "plan_artifact": str(artifact),
            },
            inspected_artifacts=(f"file:{artifact}",),
        )

    @verbs.register("record_needs_human")
    def _record_nh(ctx):
        # Reached when an escalation gate routed `abort`. The plan record
        # still captures the planner's last output so the operator can act.
        approved_iter = _last_planner_iteration(ctx.completed)
        planner_block = ctx.completed.get(f"planner_{approved_iter}", {})
        planner = (planner_block.get("value") or {}).get("parsed") or {}
        item = ctx.completed.get("fetch_item", {}).get("value", {})
        plan_id = f"plan-{item.get('item_id', item_id)}-{ctx.run_id}"
        # If we got far enough to aggregate any children, preserve them in
        # the needs-human record so the operator sees the partial tree.
        aggregate = (ctx.completed.get("aggregate_children") or {}).get("value") or {}
        children = aggregate.get("children") or []
        artifact = _write_plan_sidecar(
            log_dir=log_dir,
            run_id=ctx.run_id,
            plan_id=plan_id,
            item=item,
            planner=planner,
            approved_iteration=approved_iter,
            current_depth=current_depth,
            recursive_children=children,
            needs_human=True,
        )
        # ADR-0027 Shape B: always write the escalation-feedback sidecar
        # when record_needs_human fires from an escalation_gate route.
        # The reviewer at the same iteration as the planner is the one
        # whose verdict drove the escalation (escalation_gate routes
        # `proceed`→record_needs_human after the last reviewer escalated).
        reviewer_block = ctx.completed.get(f"reviewer_{approved_iter}", {})
        reviewer_parsed = (reviewer_block.get("value") or {}).get("parsed") or {}
        escalation_artifact: Path | None = None
        try:
            escalation_artifact = _write_escalation_sidecar(
                log_dir=log_dir,
                run_id=ctx.run_id,
                item=item,
                planner=planner,
                iteration=approved_iter,
                reviewer_feedback=reviewer_parsed.get("feedback", "") or "",
                reviewer_verdict=reviewer_parsed.get("verdict", "unknown"),
                recursive_children=children,
            )
        except OSError:
            # Sidecar is best-effort durability; the plan record itself
            # is the authoritative output. Don't fail the run if the
            # filesystem is unhappy.
            pass

        inspected: tuple[str, ...] = (f"file:{artifact}",)
        if escalation_artifact is not None:
            inspected = inspected + (f"file:{escalation_artifact}",)
        return Success(
            value={
                "plan_id": plan_id,
                "item_id": item.get("item_id", item_id),
                "item_title": item.get("title", "(unknown)"),
                "decomposable": bool(planner.get("decomposable")),
                "children": children,
                "proposals": [dict(c) for c in planner.get("children", [])],
                "summary": planner.get("summary", "(no plan recorded)"),
                "estimated_complexity": planner.get(
                    "estimated_complexity", "unknown"
                ),
                "review_iterations": approved_iter,
                "final_verdict": "needs_human",
                "plan_artifact": str(artifact),
                "escalation_artifact": (
                    str(escalation_artifact)
                    if escalation_artifact is not None
                    else None
                ),
            },
            inspected_artifacts=inspected,
        )

    return verbs


def _synth_child_id(parent_id: int, index: int) -> int:
    """Deterministic synthesised id for a proposed child without an ADO id.

    Convention: ``parent_id * 100 + (index + 1)``. Predictable for tests
    and easy to recognise as synthetic at a glance. Real ADO ids are
    plausible up to ~10^7 and could in principle collide once this gets
    above MAX_CHILDREN per layer, which v0 doesn't exercise.
    """
    return parent_id * 100 + (index + 1)


def _resolve_child_id(parent_id: int, index: int, child: dict) -> int:
    """Use the child's pinned ``item_id`` if provided; else synthesise."""
    pinned = child.get("item_id")
    if isinstance(pinned, int):
        return pinned
    return _synth_child_id(parent_id, index)


def _plan_result_to_dict(plan: "PlanResult") -> dict[str, Any]:
    """Recursive serialisation — children are themselves PlanResults."""
    return {
        "item_id": plan.item_id,
        "plan_id": plan.plan_id,
        "decomposable": plan.decomposable,
        "summary": plan.summary,
        "review_iterations": plan.review_iterations,
        "final_verdict": plan.final_verdict,
        "proposals": [dict(p) for p in plan.proposals],
        "children": [_plan_result_to_dict(c) for c in plan.children],
    }


def _find_approved_iteration(completed: dict) -> int:
    """Walk router_1..router_{ITER_CAP} to find which one returned `approve` (Success).

    `Success` routers go to `branch_decomposable`/`record_plan`; any
    `PermanentFailure` router routes elsewhere (revise/escalate). So the
    approved iteration is the one whose router entry has `kind=success`
    and a `verdict=approve` value.
    """
    for i in range(1, ITER_CAP + 1):
        entry = completed.get(f"router_{i}")
        if entry and entry.get("kind") == "success":
            v = (entry.get("value") or {}).get("verdict")
            if v == "approve":
                return i
    # Shouldn't happen — record_plan is unreachable without a Success router.
    return _last_planner_iteration(completed)


def _last_planner_iteration(completed: dict) -> int:
    last = 1
    for i in range(1, ITER_CAP + 1):
        if f"planner_{i}" in completed:
            last = i
    return last


def _write_plan_sidecar(
    *,
    log_dir: Path,
    run_id: str,
    plan_id: str,
    item: dict,
    planner: dict,
    approved_iteration: int,
    current_depth: int,
    recursive_children: list[dict[str, Any]] | None = None,
    needs_human: bool = False,
    effective_decomposable: bool | None = None,
    policy_tier: str | None = None,
    discarded_child_count: int = 0,
) -> Path:
    """Write the plan to a sidecar file.

    * Leaf plans → markdown (`<run_id>.plan.md`).
    * Decomposable plans → JSON tree (`<run_id>.plan.tree.json`) with the
      full recursive sub-plan tree (each child is a serialised
      :class:`PlanResult` with its own ``children`` list).

    ``effective_decomposable`` lets the caller override the planner's own
    ``decomposable`` flag — used when a process-config tier policy forced a
    different tier (e.g. an implementable type forced to a leaf). When ``None``
    the planner's flag is used (backward-compatible default).

    The event log remains authoritative; this file is for humans and for
    downstream tooling that wants the tree without folding the log.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    if effective_decomposable is not None:
        decomposable = effective_decomposable
    else:
        decomposable = bool(planner.get("decomposable")) if planner else False
    overrode = bool(
        planner
        and planner.get("decomposable")
        and effective_decomposable is False
    )
    rec_children = list(recursive_children or [])
    # ADR-0027 (accept-last) + ADR-0006 (decomposable trees): when the
    # planner produced a decomposable plan, ALWAYS write the JSON tree
    # sidecar — even when the operator escalation routed through
    # record_needs_human. The verdict field captures the operator's
    # decision; commit_plan's load_tree accepts both `approved` and
    # `needs_human` verdicts (the escalation policy already gated entry).
    # The pre-ADR-0027 behavior of suppressing the tree on needs_human
    # broke the `--on-escalate accept-last` dogfood path because commit_plan
    # received a `.plan.md` and rejected it with `bad_artifact` (not
    # JSON). Writing the tree unconditionally for decomposable plans
    # preserves the operator's audit (verdict carries the escalation)
    # while restoring commit_plan's input.
    if decomposable:
        path = log_dir / f"{run_id}.plan.tree.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": PLAN_TREE_SCHEMA_VERSION,
                    "plan_id": plan_id,
                    "item_id": item.get("item_id"),
                    "item_title": item.get("title"),
                    "decomposable": True,
                    "current_depth": current_depth,
                    "approved_iteration": approved_iteration,
                    "summary": planner.get("summary"),
                    "estimated_complexity": planner.get("estimated_complexity"),
                    "rationale": planner.get("rationale"),
                    # Proposals as the planner originally produced them.
                    "proposals": [dict(c) for c in planner.get("children", [])],
                    # Recursive sub-plan tree — empty until the recursion
                    # leg has actually run.
                    "children": rec_children,
                    "verdict": "needs_human" if needs_human else "approved",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    path = log_dir / f"{run_id}.plan.md"
    head = "needs human" if needs_human else "approved"
    body = [
        f"# Plan: {plan_id}",
        "",
        f"- **Item:** AB#{item.get('item_id', '?')} — "
        f"\"{item.get('title', '(unknown)')}\"",
        f"- **Type:** {item.get('work_item_type', '?')}",
        f"- **Verdict:** {head}",
        f"- **Reviewer iteration:** {approved_iteration}",
        f"- **Decomposable:** {decomposable}",
        f"- **Estimated complexity:** "
        f"{planner.get('estimated_complexity', 'unknown') if planner else 'unknown'}",
    ]
    if policy_tier and policy_tier != "unspecified":
        body.append(f"- **Tier policy:** {policy_tier}")
    if overrode:
        body.append(
            f"- **Process policy override:** type is implementable; "
            f"discarded {discarded_child_count} planner-proposed child(ren)."
        )
    body += [
        "",
        "## Summary",
        "",
        planner.get("summary", "(no plan recorded — escalated to human)")
        if planner
        else "(no plan recorded — escalated to human)",
        "",
        "## Rationale",
        "",
        planner.get("rationale", "(no rationale recorded)") if planner else "(no rationale recorded)",
    ]
    # Only list proposed children for a genuine decomposable-but-not-yet-
    # committed leaf record; when config forced a leaf the proposals are
    # discarded, not pending, so we don't present them as live proposals.
    if planner and planner.get("children") and not overrode:
        body.append("")
        body.append("## Proposed children (v0: proposals only, not yet committed)")
        body.append("")
        for c in planner["children"]:
            line = f"- [{c['work_item_type']}] {c['title']} — {c['description']}"
            group = c.get("review_group")
            if group:
                line += f"  _(review group: {group})_"
            body.append(line)
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


# ---- escalation sidecar -------------------------------------------------


def _write_escalation_sidecar(
    *,
    log_dir: Path,
    run_id: str,
    item: dict,
    planner: dict,
    iteration: int,
    reviewer_feedback: str,
    reviewer_verdict: str,
    recursive_children: list[dict[str, Any]] | None,
) -> Path:
    """Write a human-readable escalation summary alongside the plan tree.

    ADR-0027 Shape B: ALWAYS written on escalation regardless of
    ``--on-escalate`` policy. This is the durable artifact the
    operator reads to either (a) act on the reviewer's blocking
    questions and re-run, or (b) confirm the plan that ``accept-last``
    auto-shipped is the one they want.

    Path: ``<log_dir>/<run_id>.escalation-feedback.md``. Co-located
    with ``<run_id>.events.jsonl`` and ``<run_id>.plan.tree.json`` so
    a single ``ls`` shows the full forensic set.
    """
    sidecar_path = log_dir / f"{run_id}.escalation-feedback.md"

    children_block = "\n".join(
        f"  {i}. [{c.get('work_item_type', '?')}] {c.get('title', '?')}"
        for i, c in enumerate(planner.get("children") or [], 1)
    ) or "  (none — planner returned a leaf plan)"

    recursive_block = ""
    if recursive_children:
        recursive_block = "\n".join(
            f"  {i}. [{c.get('work_item_type', '?')}] {c.get('title', '?')} "
            f"(final_verdict={c.get('final_verdict', '?')!r})"
            for i, c in enumerate(recursive_children, 1)
        )

    body = (
        f"# Escalation feedback — run `{run_id}`\n\n"
        f"**Root work item:** AB#{item.get('item_id', '?')} — "
        f"{item.get('title', '(no title)')!r} "
        f"(type={item.get('work_item_type', '?')})\n\n"
        f"**Escalated at:** iteration {iteration} of {ITER_CAP}\n"
        f"**Reviewer verdict:** {reviewer_verdict}\n"
        f"**Plan disposition:** "
        f"`needs_human` (the last planner output is recorded as the plan; "
        f"the operator must decide whether to ship it as-is or revise the "
        f"work item and re-run)\n\n"
        f"## Reviewer feedback (open questions)\n\n"
        f"{reviewer_feedback or '(empty — reviewer emitted no feedback text)'}\n\n"
        f"## Last planner output\n\n"
        f"**Decomposable:** {planner.get('decomposable', '?')}\n\n"
        f"**Summary:** {planner.get('summary', '(none)')}\n\n"
        f"**Estimated complexity:** "
        f"{planner.get('estimated_complexity', 'unknown')}\n\n"
        f"**Proposed children ({len(planner.get('children') or [])}):**\n\n"
        f"{children_block}\n"
    )
    if recursive_block:
        body += (
            f"\n## Recursive sub-plans (already produced)\n\n"
            f"{recursive_block}\n"
        )
    body += (
        f"\n## Forensic links\n\n"
        f"- Event log: `{log_dir / f'{run_id}.events.jsonl'}`\n"
        f"- Plan tree (if decomposable): "
        f"`{log_dir / f'{run_id}.plan.tree.json'}`\n"
        f"\n## What to do next\n\n"
        f"1. Read the reviewer feedback above.\n"
        f"2. If the open questions need product/PM input, resolve them in "
        f"the work item description (or a comment) and re-run.\n"
        f"3. If the plan as proposed is good enough to ship, re-run with "
        f"`--on-escalate=accept-last` (the sidecar will still be written "
        f"for audit but the run will proceed past planning).\n"
        f"4. If the plan is fundamentally wrong, file follow-up items and "
        f"start fresh against a tighter scope.\n"
    )
    sidecar_path.write_text(body, encoding="utf-8")
    return sidecar_path


# ---- agent registry -----------------------------------------------------


def build_agent_registry() -> AgentRegistry:
    reg = AgentRegistry()
    for spec in ALL_SPECS:
        reg.register(spec)
    return reg


# ---- default scripted FakeProvider for the demo -----------------------


def demo_provider() -> FakeProvider:
    """Happy-path 2-level recursive demo.

    Root is decomposable into 3 children. The first child is itself
    decomposable into 2 leaf grandchildren; children 2 and 3 are leaves.
    Every reviewer approves on iteration 1, so the verdict card shows
    a clean recursive tree (root → 3 children, one with 2 grandchildren).
    """
    return FakeProvider(
        scripts={
            "planner": [
                # Root planner: 3 children, first one decomposable.
                {
                    "summary": "Decompose error-handling refactor into three sub-tasks.",
                    "decomposable": True,
                    "children": [
                        {
                            "title": "Define ErrorKind enum",
                            "description": "Lift the closed taxonomy from ADR 0004 into a typed enum.",
                            "work_item_type": "Task",
                        },
                        {
                            "title": "Migrate verbs to ErrorKind",
                            "description": "Update each verb to construct outcomes with the enum.",
                            "work_item_type": "Task",
                        },
                        {
                            "title": "Update tests",
                            "description": "Tighten outcome assertions to use the enum.",
                            "work_item_type": "Task",
                        },
                    ],
                    "estimated_complexity": "medium",
                    "rationale": "Three distinct, independently testable changes.",
                },
                # Child 1 planner: decomposable into 2 grandchildren.
                {
                    "summary": "Split ErrorKind into the enum and its serialiser.",
                    "decomposable": True,
                    "children": [
                        {
                            "title": "Add ErrorKind dataclass",
                            "description": "Declare the closed-set enum values.",
                            "work_item_type": "Task",
                        },
                        {
                            "title": "Wire ErrorKind into outcome JSON",
                            "description": "Make outcomes serialise with the enum tag.",
                            "work_item_type": "Task",
                        },
                    ],
                    "estimated_complexity": "small",
                    "rationale": "Enum definition and serialisation are separable.",
                },
                # Grandchild 1 (leaf).
                {
                    "summary": "Add a dataclass-style enum to outcomes.py.",
                    "decomposable": False,
                    "children": [],
                    "estimated_complexity": "small",
                    "rationale": "Localised change.",
                },
                # Grandchild 2 (leaf).
                {
                    "summary": "Extend OutcomeEncoder to emit the enum.",
                    "decomposable": False,
                    "children": [],
                    "estimated_complexity": "small",
                    "rationale": "Localised change.",
                },
                # Child 2 planner (leaf).
                {
                    "summary": "Sweep verbs.py to swap message-strings for ErrorKind.",
                    "decomposable": False,
                    "children": [],
                    "estimated_complexity": "small",
                    "rationale": "Mechanical rewrite.",
                },
                # Child 3 planner (leaf).
                {
                    "summary": "Tighten outcome assertions in test_outcomes.py.",
                    "decomposable": False,
                    "children": [],
                    "estimated_complexity": "small",
                    "rationale": "Tests-only change.",
                },
            ],
            "plan_reviewer": [
                {"verdict": "approve", "feedback": "Children are well-scoped."},
                {"verdict": "approve", "feedback": "Grandchildren are separable."},
                {"verdict": "approve", "feedback": "Leaf — fine."},
                {"verdict": "approve", "feedback": "Leaf — fine."},
                {"verdict": "approve", "feedback": "Leaf — fine."},
                {"verdict": "approve", "feedback": "Leaf — fine."},
            ],
        }
    )


def demo_twig(item_id: int = 99999) -> FakeTwigClient:
    """Twig that stubs the root plus all synthesised child / grandchild ids.

    The demo provider scripts a 2-level recursion: root → 3 children,
    child 1 → 2 grandchildren. Synthesised ids follow
    ``parent * 100 + slot``, so we pre-populate all 6 stub items.
    """
    items: dict[int, TwigItem] = {
        item_id: TwigItem(
            id=item_id,
            title="Restructure error handling",
            state="Active",
            area_path="PolyphonyRequiem\\v0",
            work_item_type="User Story",
            parent_id=None,
            raw={},
        ),
    }
    child_ids = [item_id * 100 + i for i in (1, 2, 3)]
    grand_ids = [child_ids[0] * 100 + i for i in (1, 2)]
    for cid in child_ids:
        items[cid] = TwigItem(
            id=cid,
            title=f"Demo child {cid}",
            state="New",
            area_path="PolyphonyRequiem\\v0",
            work_item_type="Task",
            parent_id=item_id,
            raw={},
        )
    for gid in grand_ids:
        items[gid] = TwigItem(
            id=gid,
            title=f"Demo grandchild {gid}",
            state="New",
            area_path="PolyphonyRequiem\\v0",
            work_item_type="Task",
            parent_id=child_ids[0],
            raw={},
        )
    return FakeTwigClient(items=items)


# ---- workflow ----------------------------------------------------------


def build_workflow() -> Workflow:
    b = (
        WorkflowBuilder(
            "planning",
            module="requiem.workflows.planning",
            version="0.1",
        )
        .entry("start")
        .script("start", verb="start_run")
            .edge("start", on="success", to="guard_depth")
            # Catch-all so a crash in start_run narrates instead of
            # stranding the run with `route.missing` (issue #31).
            .edge("start", on="permanent_failure", to="fail_end_crash")
        .script("guard_depth", verb="guard_depth")
            .edge("guard_depth", on="success", to="fetch_item")
            .edge(
                "guard_depth",
                on="permanent_failure:depth_exceeded",
                to="depth_gate",
            )
            .edge("guard_depth", on="permanent_failure", to="fail_end_crash")
        .human_gate(
            "depth_gate",
            prompt="Max planning depth exceeded. Proceed manually?",
            options=["proceed", "abort"],
        )
            .edge("depth_gate", on="needs_human:proceed", to="fetch_item")
            .edge("depth_gate", on="needs_human:abort", to="fail_end")
        .script("fetch_item", verb="fetch_item")
            .edge("fetch_item", on="success", to="policy_classifier")
            .edge(
                "fetch_item",
                on="permanent_failure:twig_not_found",
                to="fail_end_not_found",
            )
            .edge(
                "fetch_item",
                on="permanent_failure:twig_unknown",
                to="twig_gate",
            )
            # Any other permanent_failure (verb.crash from an unexpected
            # exception in the twig seam, etc.) → narrated crash terminal.
            .edge("fetch_item", on="permanent_failure", to="fail_end_crash")
        .script("policy_classifier", verb="policy_classifier")
            # ADR-0025 Gap A: implementable types short-circuit to a
            # synthesised leaf without any LLM call. All other tiers
            # (decomposable, unknown, unset) flow into the planner as
            # before — branch_decomposable retains authority over the
            # decomp-vs-leaf decision in those cases.
            .edge("policy_classifier", on="success", to="planner_1")
            .edge(
                "policy_classifier",
                on="permanent_failure:short_circuit_implementable",
                to="record_leaf_from_policy",
            )
            # Any other permanent_failure (verb crash from a malformed
            # config snapshot, etc.) → narrated terminal.
            .edge("policy_classifier", on="permanent_failure", to="fail_end_crash")
        .script("record_leaf_from_policy", verb="record_leaf_from_policy")
            .edge("record_leaf_from_policy", on="success", to="end")
            .edge(
                "record_leaf_from_policy",
                on="permanent_failure",
                to="fail_end_crash",
            )
        .human_gate(
            "twig_gate",
            prompt="Twig returned an unrecognised failure. Proceed or abort?",
            options=["proceed", "abort"],
        )
            # `proceed` here means the operator has resolved the twig
            # condition by hand; we re-fetch from the top.
            .edge("twig_gate", on="needs_human:proceed", to="fetch_item")
            .edge("twig_gate", on="needs_human:abort", to="fail_end")
        .human_gate(
            "bad_output_gate",
            prompt=(
                "An agent produced invalid output. The BadOutput contract "
                "forbids auto-retry; an operator must intervene."
            ),
            options=["abort"],
        )
            .edge("bad_output_gate", on="needs_human:abort", to="fail_end")
        .human_gate(
            "escalation_gate",
            prompt=(
                "Planner/reviewer loop escalated. Accept the last planner "
                "output (proceed) or abort?"
            ),
            options=["proceed", "abort"],
        )
            .edge("escalation_gate", on="needs_human:proceed", to="record_needs_human")
            .edge("escalation_gate", on="needs_human:abort", to="fail_end")
    )

    # `ITER_CAP` planner/reviewer/router iterations, wired left-to-right.
    for i in range(1, ITER_CAP + 1):
        b = (
            b.agent(
                f"planner_{i}",
                agent="planner",
                prompt_verb=f"planner_prompt_{i}",
            )
                .edge(f"planner_{i}", on="success", to=f"reviewer_{i}")
                .edge(f"planner_{i}", on="bad_output", to="bad_output_gate")
                # Catch-all (verb.crash, provider failure, etc.) → narrated
                # crash terminal (issue #31).
                .edge(f"planner_{i}", on="permanent_failure", to="fail_end_crash")
            .agent(
                f"reviewer_{i}",
                agent="plan_reviewer",
                prompt_verb=f"reviewer_prompt_{i}",
            )
                .edge(f"reviewer_{i}", on="success", to=f"router_{i}")
                .edge(f"reviewer_{i}", on="bad_output", to="bad_output_gate")
                .edge(f"reviewer_{i}", on="permanent_failure", to="fail_end_crash")
            .script(f"router_{i}", verb=f"router_{i}")
                .edge(f"router_{i}", on="success", to="branch_decomposable")
                .edge(
                    f"router_{i}",
                    on="permanent_failure:escalate",
                    to="escalation_gate",
                )
                .edge(f"router_{i}", on="permanent_failure", to="fail_end_crash")
        )
        if i < ITER_CAP:
            b = b.edge(
                f"router_{i}",
                on="permanent_failure:revise",
                to=f"planner_{i + 1}",
            )
        # router_{ITER_CAP}'s revise is rerouted to escalation_gate by the verb
        # itself (it returns escalate on revise when the cap is hit), so
        # no extra edge is needed at the topology level.

    b = (
        b
        .script("branch_decomposable", verb="branch_decomposable")
            # Leaf path: planner said not decomposable → straight to record.
            .edge("branch_decomposable", on="success", to="record_plan")
            # Decomposable + depth OK + within MAX_CHILDREN → start spawning.
            .edge(
                "branch_decomposable",
                on="permanent_failure:recurse",
                to="prep_child_1",
            )
            # Decomposable but would recurse past max_depth → gate.
            .edge(
                "branch_decomposable",
                on="permanent_failure:recursion_depth_exceeded",
                to="recursion_depth_gate",
            )
            # Planner proposed more children than we can pre-build for → gate.
            .edge(
                "branch_decomposable",
                on="permanent_failure:too_many_children",
                to="too_many_children_gate",
            )
            # Process-config tier policy could not be satisfied
            # deterministically (config demands decomposition the planner
            # didn't produce, or a configured policy with no work_item_type
            # to classify) → fail closed to a human gate.
            .edge(
                "branch_decomposable",
                on="permanent_failure:config_requires_decomposition",
                to="type_policy_gate",
            )
            .edge(
                "branch_decomposable",
                on="permanent_failure:missing_work_item_type_for_policy",
                to="type_policy_gate",
            )
            # ADR-0015 §9 #1: planner proposed a child with a type not
            # in the operator's process.yaml — fail closed to the same
            # human gate so the operator can either correct the planner
            # output OR add the type to their process config.
            .edge(
                "branch_decomposable",
                on="permanent_failure:unknown_child_work_item_type",
                to="type_policy_gate",
            )
            # Catch-all (verb.crash, unexpected errors) → narrated terminal.
            .edge(
                "branch_decomposable",
                on="permanent_failure",
                to="fail_end_crash",
            )
        .human_gate(
            "recursion_depth_gate",
            prompt=(
                "Decomposable plan would exceed max recursion depth. "
                "Accept as needs-human (proceed) or abort?"
            ),
            options=["proceed", "abort"],
        )
            .edge(
                "recursion_depth_gate",
                on="needs_human:proceed",
                to="record_needs_human",
            )
            .edge("recursion_depth_gate", on="needs_human:abort", to="fail_end")
        .human_gate(
            "too_many_children_gate",
            prompt=(
                f"Planner proposed more than {MAX_CHILDREN} children. "
                "Accept as needs-human (proceed) or abort?"
            ),
            options=["proceed", "abort"],
        )
            .edge(
                "too_many_children_gate",
                on="needs_human:proceed",
                to="record_needs_human",
            )
            .edge(
                "too_many_children_gate", on="needs_human:abort", to="fail_end"
            )
        .human_gate(
            "type_policy_gate",
            prompt=(
                "Process config tier policy could not be applied "
                "deterministically (config requires decomposition the planner "
                "did not produce, or the item has no type to classify). "
                "Accept as needs-human (proceed) or abort?"
            ),
            options=["proceed", "abort"],
        )
            .edge(
                "type_policy_gate",
                on="needs_human:proceed",
                to="record_needs_human",
            )
            .edge("type_policy_gate", on="needs_human:abort", to="fail_end")
        .human_gate(
            "cycle_gate",
            prompt=(
                "Cycle detected: a child's proposed item_id matches an "
                "ancestor in this planning chain. Accept as needs-human "
                "(proceed) or abort?"
            ),
            options=["proceed", "abort"],
        )
            .edge("cycle_gate", on="needs_human:proceed", to="record_needs_human")
            .edge("cycle_gate", on="needs_human:abort", to="fail_end")
    )

    # Pre-build MAX_CHILDREN sequential subworkflow slots. Each slot is
    # (prep_child_i → child_i) where prep is a guard / no-more-children
    # selector. Sequential per ADR 0005-compatible scheme — parallel_fork
    # is a Phase D optimisation.
    for i in range(1, MAX_CHILDREN + 1):
        is_last = i == MAX_CHILDREN
        next_prep_or_aggregate = (
            "aggregate_children" if is_last else f"prep_child_{i + 1}"
        )
        b = (
            b
            .script(f"prep_child_{i}", verb=f"prep_child_{i}")
                .edge(f"prep_child_{i}", on="success", to=f"child_{i}")
                # No child at this slot → skip ahead to aggregate.
                .edge(
                    f"prep_child_{i}",
                    on="permanent_failure:no_more_children",
                    to="aggregate_children",
                )
                # Cycle detected → cycle gate.
                .edge(
                    f"prep_child_{i}",
                    on="permanent_failure:cycle_detected",
                    to="cycle_gate",
                )
                # Catch-all (verb.crash, etc.) → narrated terminal.
                .edge(
                    f"prep_child_{i}",
                    on="permanent_failure",
                    to="fail_end_crash",
                )
            .subworkflow(
                f"child_{i}",
                workflow="requiem.workflows.planning",
                inputs_verb="child_inputs",
            )
                # Child completed cleanly → next slot (or aggregate if last).
                .edge(
                    f"child_{i}",
                    on="success",
                    to=next_prep_or_aggregate,
                )
                # Child returned permanent_failure → bubble to escalation.
                .edge(
                    f"child_{i}",
                    on="permanent_failure",
                    to="escalation_gate",
                )
                # Child bubbled NeedsHuman → also escalate at parent.
                .edge(
                    f"child_{i}",
                    on="needs_human",
                    to="escalation_gate",
                )
                # Child was cancelled → propagate as cancel by ending failed.
                .edge(f"child_{i}", on="cancelled", to="fail_end")
        )

    b = (
        b
        .script("aggregate_children", verb="aggregate_children")
            .edge("aggregate_children", on="success", to="record_plan")
            # Aggregation invariants only fail under workflow misuse;
            # escalate to a human if they ever do.
            .edge(
                "aggregate_children",
                on="permanent_failure",
                to="escalation_gate",
            )
        .script("record_plan", verb="record_plan")
            .edge("record_plan", on="success", to="end")
            .edge("record_plan", on="permanent_failure", to="fail_end_crash")
        .script("record_needs_human", verb="record_needs_human")
            .edge("record_needs_human", on="success", to="end_needs_human")
            .edge(
                "record_needs_human",
                on="permanent_failure",
                to="fail_end_crash",
            )
        .terminate("end", disposition="completed")
        .terminate("end_needs_human", disposition="completed")
        .terminate("fail_end", disposition="failed")
        .terminate("fail_end_not_found", disposition="failed")
        # Narrated crash terminal — every script/agent verb routes its
        # catch-all `permanent_failure` here so a `verb.crash` (or any
        # un-handled error_kind) hits a terminate node and the verdict
        # card can name the crashed verb instead of stranding the run
        # with `route.missing` (issue #31).
        .terminate("fail_end_crash", disposition="failed")
        .humanize(_humanize_map())
    )
    return b.build()


def _humanize_map() -> dict[str, str]:
    m: dict[str, str] = {
        "start": "Starting planning",
        "guard_depth": "Depth check",
        "depth_gate": "depth exceeded — proceed?",
        "fetch_item": "Fetched work item",
        "twig_gate": "twig error — proceed?",
        "bad_output_gate": "agent produced bad output",
        "escalation_gate": "planner loop escalated",
        "recursion_depth_gate": "decomposable + would exceed max_depth — proceed?",
        "too_many_children_gate": (
            f"planner proposed > {MAX_CHILDREN} children — proceed?"
        ),
        "cycle_gate": "cycle detected in plan tree — proceed?",
        "type_policy_gate": "config tier policy unsatisfiable — proceed?",
        "branch_decomposable": "Branched on plan shape",
        "aggregate_children": "Aggregated child plans",
        "record_plan": "Recorded plan",
        "record_needs_human": "Recorded (needs-human) plan",
        "end": "planning",
        "end_needs_human": "planning",
        "fail_end": "planning",
        "fail_end_not_found": "planning",
        "fail_end_crash": "planning",
    }
    for i in range(1, ITER_CAP + 1):
        m[f"planner_{i}"] = f"Planner (iteration {i})"
        m[f"reviewer_{i}"] = f"Reviewer (iteration {i})"
        m[f"router_{i}"] = f"Route after review {i}"
    for i in range(1, MAX_CHILDREN + 1):
        m[f"prep_child_{i}"] = f"Prep child slot {i}"
        m[f"child_{i}"] = f"Plan child {i} (sub-workflow)"
    return m


# ---- engine factory ----------------------------------------------------


def _default_gate_handler(node_id: str, prompt: str, options: tuple[str, ...]) -> str:
    """Demo handler: aborts on every gate.

    The happy-path demo never hits a gate (planner approves on iter 1,
    twig is a fake that always answers). If a gate fires under the demo
    factory, that's a defect we want surfaced, so we pick `abort`. Test
    suites supply their own handlers.
    """
    return "abort"


_default_gate_handler.__requiem_auto__ = True  # type: ignore[attr-defined]


# ---- ADR-0027 escalation-policy gate handler ---------------------------


VALID_ESCALATION_POLICIES = ("escalate", "accept-last", "abort")
"""Allowed values for the `--on-escalate` CLI flag. Default is `escalate`
(current behavior pre-ADR-0027); `accept-last` answers `proceed` to
ship the last planner output as needs-human; `abort` answers `abort`."""


def make_escalation_policy_handler(
    policy: str,
    *,
    fallback: Any = None,
) -> Any:
    """Build a gate handler that enforces an escalation policy.

    Per ADR-0027 Shape B, the policy ONLY affects ``escalation_gate``;
    all other gates (``bad_output_gate``, ``type_policy_gate``,
    ``recursion_depth_gate``, etc.) are delegated to ``fallback`` (the
    operator's interactive handler, or a test-supplied stub).

    ``policy=escalate`` (default): delegate the escalation gate too —
    behavior is identical to today, except the sidecar gets written by
    ``record_needs_human`` whenever the operator picks ``proceed``.

    ``policy=accept-last``: auto-answer ``proceed`` at escalation_gate.
    The workflow records the last planner output as needs-human, the
    sidecar captures the reviewer's escalation rationale, and the run
    continues (run_pipeline must allow needs_human past the planning
    phase when policy is accept-last — see end_to_end.py).

    ``policy=abort``: auto-answer ``abort`` at escalation_gate. Run
    terminates; sidecar is NOT written (no record_needs_human path).
    Useful for batch / CI contexts where you'd rather fail fast than
    ship a needs-human plan.
    """
    if policy not in VALID_ESCALATION_POLICIES:
        raise ValueError(
            f"invalid escalation policy {policy!r}; valid: "
            f"{VALID_ESCALATION_POLICIES}"
        )
    fallback_fn = fallback or _default_gate_handler

    def _handler(node_id: str, prompt: str, options: tuple[str, ...]) -> str:
        if node_id != "escalation_gate":
            return fallback_fn(node_id, prompt, options)
        if policy == "accept-last":
            if "proceed" not in options:
                # Defensive: if the gate's option set ever changes,
                # don't silently pick a wrong option.
                return fallback_fn(node_id, prompt, options)
            return "proceed"
        if policy == "abort":
            if "abort" not in options:
                return fallback_fn(node_id, prompt, options)
            return "abort"
        # policy == "escalate": delegate to fallback (interactive
        # prompt for the operator, or test stub).
        return fallback_fn(node_id, prompt, options)

    _handler.__requiem_auto__ = True  # type: ignore[attr-defined]
    _handler.__requiem_escalation_policy__ = policy  # type: ignore[attr-defined]
    return _handler


def build_engine(
    log_dir: Path,
    *,
    item_id: int = 99999,
    parent_plan_id: str | None = None,
    max_depth: int = 4,
    current_depth: int = 0,
    ancestor_item_ids: Sequence[int] = (),
    twig: TwigClientProto | None = None,
    provider: AgentProvider | None = None,
    gate_handler=None,
    process_config: "ProcessConfig | dict[str, Any] | None" = None,
    child_proposal: dict[str, Any] | None = None,
) -> Engine:
    """Construct a runnable Engine for the planning workflow.

    Defaults are self-contained (FakeProvider + FakeTwigClient) so
    ``requiem run requiem.workflows.planning`` works out of the box.
    Production callers pass a real ``TwigClient`` and a real LLM
    provider.

    ``ancestor_item_ids`` carries the chain of ``item_id``s from the
    root planning run down to this invocation (cycle detection input).
    For the root invocation, leave it empty.

    ``child_proposal`` carries the parent's already-resolved ChildPlan
    (``{title, description, work_item_type, state}``) when this engine
    is the recursive child of another planning run. When set, the
    workflow's ``fetch_item`` verb uses this proposal directly and
    skips the twig lookup — the synthesised child id
    (``parent_id*100+slot``) does NOT exist in ADO until ``commit_plan``
    seeds it, so a twig fetch is guaranteed to fail. Top-level callers
    always leave this ``None``; the recursive ``child_inputs`` verb
    populates it from the parent's ``prep_child_i`` outcome.

    Test-fake threading: any non-None ``twig`` / ``provider`` /
    ``gate_handler`` arguments are installed in module-level contextvars
    so that recursively-spawned child engines (whose factory is invoked
    by the kernel with only the recorded JSON inputs) pick up the same
    seams. See module docstring §"Test-fake threading".
    """
    # Resolve seams from contextvars when the caller didn't supply one
    # (covers the recursive-child case where the kernel constructs the
    # engine with no twig/provider kwargs).
    resolved_twig = twig if twig is not None else _active_twig_cv.get()
    resolved_provider = provider if provider is not None else _active_provider_cv.get()
    resolved_gate = (
        gate_handler if gate_handler is not None else _active_gate_handler_cv.get()
    )

    final_twig = resolved_twig or demo_twig(item_id)
    final_provider = resolved_provider or demo_provider()
    final_gate = resolved_gate or _default_gate_handler

    # Resolve the tier-routing config. ``process_config`` may arrive as a live
    # ProcessConfig (programmatic caller) or as a JSON snapshot dict (recorded
    # child-workflow inputs reconstructed by the kernel). Recorded inputs are
    # authoritative; the contextvar is only a convenience for in-process calls
    # that didn't pass one. Fall back to the documented defaults.
    if isinstance(process_config, ProcessConfig):
        resolved_config: ProcessConfig | None = process_config
    elif isinstance(process_config, dict):
        resolved_config = ProcessConfig.from_snapshot(process_config)
    else:
        resolved_config = _active_process_config_cv.get()
    final_config = resolved_config or default_process_config()

    # Install for any recursive child invocation in this asyncio task.
    _active_twig_cv.set(final_twig)
    _active_provider_cv.set(final_provider)
    _active_gate_handler_cv.set(final_gate)
    _active_process_config_cv.set(final_config)

    return Engine(
        workflow=build_workflow(),
        verbs=build_verb_registry(
            item_id=item_id,
            parent_plan_id=parent_plan_id,
            max_depth=max_depth,
            current_depth=current_depth,
            ancestor_item_ids=ancestor_item_ids,
            twig=final_twig,
            log_dir=log_dir,
            config=final_config,
            child_proposal=child_proposal,
        ),
        agents=build_agent_registry(),
        provider=final_provider,
        toolbelt=Toolbelt.real(),
        log_dir=log_dir,
        gate_handler=final_gate,
        # ADR-0030 §2: thread the operator's ProcessConfig into the
        # planning Engine so role-tagged AgentSpecs (planner/reviewer
        # — not currently tagged but the wiring should be ready) can
        # pick up `models.<role>` from process.yaml. Without this the
        # kernel's `_invoke_with_resolved_model` always sees
        # `self.process_config is None`. Closing the same gap as the
        # implementation workflow (run #28).
        process_config=final_config,
    )


# ---- result projection (for tests + callers) --------------------------


def project_plan_result(completed: dict) -> PlanResult | None:
    """Lift the engine's `completed` projection into a `PlanResult`.

    Accepts the `completed` map shaped `{node_id: outcome_dict}` — same
    shape the verb context sees and the renderer streams. Tests can
    build this from the event log via :func:`completed_from_log`.

    Returns `None` if neither `record_plan` nor `record_needs_human`
    ran — i.e. the workflow failed before producing any plan record.
    Recursive children (whose serialised tree the parent's
    ``record_plan`` value carries) are reconstructed as nested
    ``PlanResult`` instances; the tree is fully recursive.
    """
    rec = (
        completed.get("record_plan")
        or completed.get("record_needs_human")
        or completed.get("record_leaf_from_policy")
    )
    if not rec:
        return None
    v = rec.get("value") or {}
    return PlanResult(
        item_id=int(v.get("item_id", 0)),
        plan_id=str(v.get("plan_id", "")),
        decomposable=bool(v.get("decomposable", False)),
        children=[_plan_result_from_dict(c) for c in (v.get("children") or [])],
        summary=str(v.get("summary", "")),
        review_iterations=int(v.get("review_iterations", 0)),
        final_verdict=v.get("final_verdict", "approved"),
        proposals=[dict(p) for p in (v.get("proposals") or [])],
    )


def _plan_result_from_dict(d: dict[str, Any]) -> PlanResult:
    """Inverse of :func:`_plan_result_to_dict`. Recursive over ``children``."""
    return PlanResult(
        item_id=int(d.get("item_id", 0)),
        plan_id=str(d.get("plan_id", "")),
        decomposable=bool(d.get("decomposable", False)),
        children=[_plan_result_from_dict(c) for c in (d.get("children") or [])],
        summary=str(d.get("summary", "")),
        review_iterations=int(d.get("review_iterations", 0)),
        final_verdict=d.get("final_verdict", "approved"),
        proposals=[dict(p) for p in (d.get("proposals") or [])],
    )


def completed_from_log(log_path: Path) -> dict[str, dict[str, Any]]:
    """Rebuild the `{node_id: outcome_dict}` map from the run's event log.

    The kernel's public `RunResult.projection` is a summary; the full
    completed map only lives in memory during a run. Tests and tooling
    that want the plan record after the fact fold the JSONL stream.

    Folds both ``verb_completed`` (script/agent nodes) and
    ``subworkflow_completed`` (sub-workflow nodes per ADR 0005) — the
    latter doesn't emit a separate ``verb_completed`` event, so the
    completed map would otherwise miss it and break
    :func:`project_plan_result` for recursive plans.
    """
    completed: dict[str, dict[str, Any]] = {}
    for ev in replay(log_path):
        kind = ev.get("kind")
        node = ev.get("node_id")
        payload = ev.get("payload") or {}
        outcome = payload.get("outcome")
        if kind in ("verb_completed", "subworkflow_completed"):
            if node and outcome is not None:
                completed[node] = outcome
    return completed


# ---- render hooks (consumed by `requiem.cli.render`) ------------------


def _detail_guard_depth(value: dict) -> str:
    return f"depth {value.get('current_depth', '?')} ≤ {value.get('max_depth', '?')}"


def _detail_fetch_item(value: dict) -> str:
    return f"AB#{value.get('item_id', '?')} — \"{value.get('title', '?')}\""


def _detail_router(value: dict) -> str:
    return f"verdict={value.get('verdict', '?')} (iter {value.get('iteration', '?')})"


def _detail_branch(value: dict) -> str:
    head = "decomposable" if value.get("decomposable") else "leaf"
    n = value.get("child_count", 0)
    return f"{head}" + (f" ({n} children)" if value.get("decomposable") else "")


def _detail_record(value: dict) -> str:
    return f"to {value.get('plan_artifact', '?')}"


def _detail_planner(value: dict) -> str:
    p = (value or {}).get("parsed") or {}
    head = "decomposable" if p.get("decomposable") else "leaf"
    return f"{head} — {p.get('estimated_complexity', '?')}"


def _detail_reviewer(value: dict) -> str:
    p = (value or {}).get("parsed") or {}
    return p.get("verdict", "?")


def render_hints() -> dict:
    details: dict[str, Any] = {
        "guard_depth": _detail_guard_depth,
        "fetch_item": _detail_fetch_item,
        "branch_decomposable": _detail_branch,
        "record_plan": _detail_record,
        "record_needs_human": _detail_record,
        "aggregate_children": _detail_aggregate,
    }
    for i in range(1, ITER_CAP + 1):
        details[f"planner_{i}"] = _detail_planner
        details[f"reviewer_{i}"] = _detail_reviewer
        details[f"router_{i}"] = _detail_router
    for i in range(1, MAX_CHILDREN + 1):
        details[f"prep_child_{i}"] = _detail_prep_child
    silent = {
        "start", "end", "end_needs_human",
        "fail_end", "fail_end_not_found", "fail_end_crash",
    }
    # The prep_child nodes that returned no_more_children are noise for
    # most users; the topology pre-builds MAX_CHILDREN slots even when the
    # planner proposed fewer. They still fire as nodes though, so we
    # leave them out of silent_nodes — the renderer can decide whether
    # to elide PermanentFailure("no_more_children") visually.
    return {
        "artifact_name": "planning",
        "details": details,
        "silent_nodes": frozenset(silent),
    }


def _detail_aggregate(value: dict) -> str:
    n = value.get("child_count", 0)
    proposed = value.get("proposed_count", n)
    if n == proposed:
        return f"{n} child plan{'s' if n != 1 else ''}"
    return f"{n} of {proposed} child plan(s) aggregated"


def _detail_prep_child(value: dict) -> str:
    return (
        f"slot {value.get('slot', '?')} → AB#{value.get('child_item_id', '?')} "
        f"({value.get('child_work_item_type', '?')})"
    )


def verdict_card(completed: dict) -> str | None:
    """The Demo Contract §3 verdict card.

    Three success shapes — leaf, decomposable (flat single-level), and
    decomposable+recursive (multi-level tree) — plus a fourth
    "crashed" shape for runs where a verb crashed (or returned an
    un-routed `PermanentFailure`) and the catch-all edge sent the run to
    ``fail_end_crash``. Without that fourth shape the operator would
    see only ``route.missing`` and have no narrative for the failure
    (issue #31).

    Falls back to None if no plan was recorded *and* no crash trace can
    be reconstructed from ``completed`` (the CLI then prints nothing
    extra).
    """
    rec = completed.get("record_plan") or completed.get("record_needs_human")
    if not rec:
        # No plan was recorded → either we never reached planning, or a
        # verb crashed mid-flight. Render a narrated "did not plan"
        # card from the last failure in ``completed`` so the operator
        # sees the crashed verb and its error_kind.
        return _card_crashed(completed)
    v = rec.get("value") or {}
    decomposable = bool(v.get("decomposable"))
    item_id = v.get("item_id", "?")
    title = v.get("item_title", "?")
    iters = v.get("review_iterations", 1)
    verdict = v.get("final_verdict", "approved")
    head_mark = "✓" if verdict == "approved" else "⚠"
    head_verdict = "Approved" if verdict == "approved" else "Needs human"

    if decomposable:
        children = v.get("children") or []
        depth, leaves = _tree_stats(children)
        header_kind = f" ({depth} level{'s' if depth != 1 else ''}, {leaves} leaves) "
    else:
        header_kind = " "

    header = (
        f"─── Plan: AB#{item_id}"
        + header_kind
        + "─" * max(1, 50 - len(str(item_id)) - len(header_kind))
    )
    lines = [header]
    if decomposable:
        children = v.get("children") or []
        lines += [
            f"  {head_mark} {head_verdict}",
            f"      Root:        {item_id} — \"{title}\"",
            f"      Tree:        {depth} level"
            f"{'s' if depth != 1 else ''} deep, {leaves} leaves",
        ]
        # Render the tree (depth-truncated at 3 levels for legibility;
        # full tree lives in the sidecar JSON).
        if children:
            tree_lines = _render_tree(children, max_depth=3, prefix="        ")
            lines.extend(tree_lines)
        # Aggregate approval line (counts plans, not iterations).
        approved_count, total_count = _approval_counts(children)
        # Include this plan itself in the totals (its own iteration).
        approved_count += 1 if verdict == "approved" else 0
        total_count += 1
        first_iter_count = _first_iter_approval_count(v, children)
        if first_iter_count == total_count:
            lines.append(
                f"      Approvals:   {total_count}/{total_count} plans "
                f"approved on first iteration"
            )
        else:
            lines.append(
                f"      Approvals:   {approved_count}/{total_count} plans "
                f"approved (root iter {iters})"
            )
        lines.append(f"  → plan tree: {v.get('plan_artifact', '?')}")
    else:
        lines += [
            f"  {head_mark} {head_verdict} (leaf)",
            f"      Item:        {item_id} — \"{title}\"",
            f"      Estimated:   {v.get('estimated_complexity', '?')}",
            f"      Reviewer:    {verdict} on iteration {iters}",
            f"      Summary:     {v.get('summary', '?')}",
            f"  → plan file: {v.get('plan_artifact', '?')}",
        ]
    lines.append("─" * 69)
    return "\n".join(lines)


def _card_crashed(completed: dict) -> str | None:
    """Render a "did not plan" card for a run that crashed before recording.

    Walks ``completed`` for non-success outcomes (the kernel records the
    crashed verb's outcome dict in ``completed`` before the router fires,
    so a ``verb.crash`` is always findable here). Picks the last failure
    and names the crashed verb plus its ``error_kind`` / message.

    Returns None if there is no failure to narrate — the renderer treats
    that the same as "no card", preserving the pre-fix silence for
    truly empty runs.

    Companion to the catch-all ``permanent_failure`` edges added per
    issue #31: those edges ensure the run terminates cleanly at
    ``fail_end_crash`` instead of stranding with ``route.missing``, and
    this helper turns the resulting ``completed`` map into a narrative.
    """
    failures: list[tuple[str, dict[str, Any]]] = []
    for nid, payload in completed.items():
        if not isinstance(payload, dict):
            continue
        kind = payload.get("kind")
        if kind in ("permanent_failure", "needs_human", "bad_output"):
            failures.append((nid, payload))
    if not failures:
        return None
    nid, outcome = failures[-1]
    error_kind = outcome.get("error_kind") or outcome.get("kind", "?")
    message = (
        outcome.get("message")
        or outcome.get("prompt")
        or outcome.get("kind", "?")
    )
    header = "─── Plan: crashed " + "─" * (69 - len("─── Plan: crashed "))
    return "\n".join([
        header,
        "  ✕ Did not plan",
        f"      Stopped at:  {nid}",
        f"      Error kind:  {error_kind}",
        f"      Reason:      {message}",
        "─" * 69,
    ])


def _tree_stats(children: list[dict[str, Any]]) -> tuple[int, int]:
    """Return ``(max_depth, leaf_count)`` of the recursive child tree.

    Depth counts the root as 1. A single-level tree (root + leaves) has
    depth 2. Leaves are nodes whose ``decomposable`` is False or whose
    ``children`` list is empty.
    """
    if not children:
        return 1, 0
    max_d = 1
    leaves = 0
    for c in children:
        grand = c.get("children") or []
        if not grand or not c.get("decomposable"):
            leaves += 1
            max_d = max(max_d, 2)
        else:
            d, l = _tree_stats(grand)
            leaves += l
            max_d = max(max_d, 1 + d)
    return max_d, leaves


def _render_tree(
    children: list[dict[str, Any]], *, max_depth: int, prefix: str, depth: int = 1
) -> list[str]:
    """Render a unicode-tree slice of the recursive children list."""
    lines: list[str] = []
    last_i = len(children) - 1
    for i, c in enumerate(children):
        is_last = i == last_i
        branch = "└─" if is_last else "├─"
        # We don't carry work_item_type through the PlanResult round-trip
        # (the planner output had it, but PlanResult doesn't preserve it
        # — kept on the proposal block). Use a generic tag.
        wit = "Story" if (c.get("children") or []) else "Task"
        item_id = c.get("item_id", "?")
        grand = c.get("children") or []
        if c.get("decomposable") and grand:
            d, l = _tree_stats(grand)
            tag = f"(decomposable, {l} leaves)"
        else:
            tag = "(leaf)"
        lines.append(f"{prefix}{branch} AB#{item_id} [{wit}] {tag}")
        if depth < max_depth and grand:
            sub_prefix = prefix + ("    " if is_last else "│   ")
            lines.extend(
                _render_tree(grand, max_depth=max_depth, prefix=sub_prefix, depth=depth + 1)
            )
    return lines


def _approval_counts(children: list[dict[str, Any]]) -> tuple[int, int]:
    """Count (approved, total) plans across a recursive tree."""
    approved = 0
    total = 0
    for c in children:
        total += 1
        if c.get("final_verdict") == "approved":
            approved += 1
        a, t = _approval_counts(c.get("children") or [])
        approved += a
        total += t
    return approved, total


def _first_iter_approval_count(
    root_value: dict[str, Any], children: list[dict[str, Any]]
) -> int:
    """Total plans across tree (including root) that were approved on iter 1."""
    count = 1 if root_value.get("review_iterations", 1) == 1 else 0

    def _walk(nodes):
        nonlocal count
        for n in nodes:
            if n.get("review_iterations", 1) == 1 and n.get("final_verdict") == "approved":
                count += 1
            _walk(n.get("children") or [])

    _walk(children)
    return count


# ---- async-friendly variant for embedding ------------------------------


async def run_planning(
    log_dir: Path,
    run_id: str,
    *,
    item_id: int,
    parent_plan_id: str | None = None,
    max_depth: int = 4,
    current_depth: int = 0,
    ancestor_item_ids: Sequence[int] = (),
    twig: TwigClientProto | None = None,
    provider: AgentProvider | None = None,
    gate_handler=None,
):
    """Convenience: build an engine and run it.

    Returns the engine's `RunResult`. Callers needing the `PlanResult`
    projection should call `project_plan_result(result.projection)` on a
    `Completed` result.
    """
    engine = build_engine(
        log_dir,
        item_id=item_id,
        parent_plan_id=parent_plan_id,
        max_depth=max_depth,
        current_depth=current_depth,
        ancestor_item_ids=ancestor_item_ids,
        twig=twig,
        provider=provider,
        gate_handler=gate_handler,
    )
    return await engine.run(run_id)
