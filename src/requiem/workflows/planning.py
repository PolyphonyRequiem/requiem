"""Recursive planning workflow — Fauré (Phase C seat).

Takes a root work item and produces a plan: either a **leaf** (atomic,
implementable directly) or **decomposable** (a list of proposed child
work items). A reviewer agent critiques the plan; the planner gets up to
three iterations before the workflow escalates to a human.

## Topology

::

    start → guard_depth → fetch_item → planner_1 → reviewer_1 → router_1
        router_1 ─ approve  → branch_decomposable
        router_1 ─ revise   → planner_2 → reviewer_2 → router_2
                                                  router_2 ─ approve → branch_decomposable
                                                  router_2 ─ revise  → planner_3 → reviewer_3 → router_3
                                                                                             router_3 ─ approve → branch_decomposable
                                                                                             router_3 ─ revise  → escalation_gate
                                                                                             router_3 ─ escalate→ escalation_gate
                                                  router_2 ─ escalate→ escalation_gate
        router_1 ─ escalate → escalation_gate

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


# ---- typed agent outputs ------------------------------------------------


class ChildPlan(BaseModel):
    title: str
    description: str
    work_item_type: Literal["Task", "Bug", "User Story"]
    item_id: int | None = None
    """Optional pinned ADO id for the child.

    When omitted the workflow synthesises a deterministic id (see
    :func:`_synth_child_id`). Tests use this to engineer cycle scenarios
    (set a child's ``item_id`` equal to an ancestor's).
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
ITER_CAP = 3


def build_verb_registry(
    *,
    item_id: int,
    parent_plan_id: str | None,
    max_depth: int,
    current_depth: int,
    ancestor_item_ids: Sequence[int],
    twig: TwigClientProto,
    log_dir: Path,
) -> VerbRegistry:
    verbs = VerbRegistry()
    ancestor_set = tuple(int(a) for a in ancestor_item_ids)

    @verbs.register("start_run")
    def _start(ctx):
        return Success(
            value={
                "item_id": item_id,
                "parent_plan_id": parent_plan_id,
                "current_depth": current_depth,
                "max_depth": max_depth,
                "ancestor_item_ids": list(ancestor_set),
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
        return Success(
            value={
                "item_id": item.id,
                "title": item.title,
                "state": item.state,
                "work_item_type": item.work_item_type,
                "area_path": item.area_path,
            },
            inspected_artifacts=(f"twig:item/{item.id}",),
        )

    def _planner_prompt(iteration: int):
        def _prompt(ctx):
            item = ctx.completed["fetch_item"]["value"]
            base = (
                f"Plan work item AB#{item['item_id']} — \"{item['title']}\" "
                f"(type={item['work_item_type']}, state={item['state']}).\n"
                f"Current planning depth: {current_depth} of {max_depth}.\n"
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
            return (
                f"Review the following plan for AB#{item['item_id']} "
                f"(\"{item['title']}\"):\n\n"
                f"  summary: {planner['summary']}\n"
                f"  decomposable: {planner['decomposable']}\n"
                f"  estimated_complexity: {planner['estimated_complexity']}\n"
                f"  rationale: {planner['rationale']}\n"
                f"  children: {len(planner['children'])} proposed\n\n"
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

        Using PermanentFailure variants as branch selectors is the
        established convention in this workflow (see ``router_i``).
        """
        approved_iter = _find_approved_iteration(ctx.completed)
        planner = ctx.completed[f"planner_{approved_iter}"]["value"]["parsed"]
        decomposable = bool(planner.get("decomposable"))
        children = planner.get("children", [])
        child_count = len(children)
        if not decomposable:
            return Success(
                value={
                    "decomposable": False,
                    "branch": "leaf",
                    "child_count": 0,
                    "approved_iteration": approved_iter,
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
        return {
            "item_id": child_id,
            "current_depth": current_depth + 1,
            "max_depth": max_depth,
            "parent_plan_id": parent_handle,
            "ancestor_item_ids": new_ancestors,
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

        artifact = _write_plan_sidecar(
            log_dir=log_dir,
            run_id=ctx.run_id,
            plan_id=plan_id,
            item=item,
            planner=planner,
            approved_iteration=approved_iter,
            current_depth=current_depth,
            recursive_children=children,
        )
        return Success(
            value={
                "plan_id": plan_id,
                "item_id": item["item_id"],
                "item_title": item["title"],
                "decomposable": bool(planner.get("decomposable")),
                "children": children,
                "proposals": [dict(c) for c in planner.get("children", [])],
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
            },
            inspected_artifacts=(f"file:{artifact}",),
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
        "children": [_plan_result_to_dict(c) for c in plan.children],
    }


def _find_approved_iteration(completed: dict) -> int:
    """Walk router_1..router_3 to find which one returned `approve` (Success).

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
) -> Path:
    """Write the plan to a sidecar file.

    * Leaf plans → markdown (`<run_id>.plan.md`).
    * Decomposable plans → JSON tree (`<run_id>.plan.tree.json`) with the
      full recursive sub-plan tree (each child is a serialised
      :class:`PlanResult` with its own ``children`` list).

    The event log remains authoritative; this file is for humans and for
    downstream tooling that wants the tree without folding the log.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    decomposable = bool(planner.get("decomposable")) if planner else False
    rec_children = list(recursive_children or [])
    if decomposable and not needs_human:
        path = log_dir / f"{run_id}.plan.tree.json"
        path.write_text(
            json.dumps(
                {
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
    if planner and planner.get("children"):
        body.append("")
        body.append("## Proposed children (v0: proposals only, not yet committed)")
        body.append("")
        for c in planner["children"]:
            body.append(f"- [{c['work_item_type']}] {c['title']} — {c['description']}")
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


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
        .script("guard_depth", verb="guard_depth")
            .edge("guard_depth", on="success", to="fetch_item")
            .edge(
                "guard_depth",
                on="permanent_failure:depth_exceeded",
                to="depth_gate",
            )
        .human_gate(
            "depth_gate",
            prompt="Max planning depth exceeded. Proceed manually?",
            options=["proceed", "abort"],
        )
            .edge("depth_gate", on="needs_human:proceed", to="fetch_item")
            .edge("depth_gate", on="needs_human:abort", to="fail_end")
        .script("fetch_item", verb="fetch_item")
            .edge("fetch_item", on="success", to="planner_1")
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

    # Three planner/reviewer/router iterations, wired left-to-right.
    for i in range(1, ITER_CAP + 1):
        b = (
            b.agent(
                f"planner_{i}",
                agent="planner",
                prompt_verb=f"planner_prompt_{i}",
            )
                .edge(f"planner_{i}", on="success", to=f"reviewer_{i}")
                .edge(f"planner_{i}", on="bad_output", to="bad_output_gate")
            .agent(
                f"reviewer_{i}",
                agent="plan_reviewer",
                prompt_verb=f"reviewer_prompt_{i}",
            )
                .edge(f"reviewer_{i}", on="success", to=f"router_{i}")
                .edge(f"reviewer_{i}", on="bad_output", to="bad_output_gate")
            .script(f"router_{i}", verb=f"router_{i}")
                .edge(f"router_{i}", on="success", to="branch_decomposable")
                .edge(
                    f"router_{i}",
                    on="permanent_failure:escalate",
                    to="escalation_gate",
                )
        )
        if i < ITER_CAP:
            b = b.edge(
                f"router_{i}",
                on="permanent_failure:revise",
                to=f"planner_{i + 1}",
            )
        # router_3's revise is rerouted to escalation_gate by the verb
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
        .script("record_needs_human", verb="record_needs_human")
            .edge("record_needs_human", on="success", to="end_needs_human")
        .terminate("end", disposition="completed")
        .terminate("end_needs_human", disposition="completed")
        .terminate("fail_end", disposition="failed")
        .terminate("fail_end_not_found", disposition="failed")
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
        "branch_decomposable": "Branched on plan shape",
        "aggregate_children": "Aggregated child plans",
        "record_plan": "Recorded plan",
        "record_needs_human": "Recorded (needs-human) plan",
        "end": "planning",
        "end_needs_human": "planning",
        "fail_end": "planning",
        "fail_end_not_found": "planning",
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
) -> Engine:
    """Construct a runnable Engine for the planning workflow.

    Defaults are self-contained (FakeProvider + FakeTwigClient) so
    ``requiem run requiem.workflows.planning`` works out of the box.
    Production callers pass a real ``TwigClient`` and a real LLM
    provider.

    ``ancestor_item_ids`` carries the chain of ``item_id``s from the
    root planning run down to this invocation (cycle detection input).
    For the root invocation, leave it empty.

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

    # Install for any recursive child invocation in this asyncio task.
    _active_twig_cv.set(final_twig)
    _active_provider_cv.set(final_provider)
    _active_gate_handler_cv.set(final_gate)

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
        ),
        agents=build_agent_registry(),
        provider=final_provider,
        toolbelt=Toolbelt.real(),
        log_dir=log_dir,
        gate_handler=final_gate,
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
    rec = completed.get("record_plan") or completed.get("record_needs_human")
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
    silent = {"start", "end", "end_needs_human", "fail_end", "fail_end_not_found"}
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

    Three shapes — leaf, decomposable (flat single-level), and
    decomposable+recursive (multi-level tree). Falls back to None if no
    plan was recorded (the CLI then prints nothing extra).
    """
    rec = completed.get("record_plan") or completed.get("record_needs_human")
    if not rec:
        return None
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
