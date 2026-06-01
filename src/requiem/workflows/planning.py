"""Recursive planning workflow — Fauré (Phase C seat).

Takes a root work item and produces a plan: either a **leaf** (atomic,
implementable directly) or **decomposable** (a list of proposed child
work items). A reviewer agent critiques the plan; the planner gets up to
three iterations before the workflow escalates to a human.

## Topology (flat — sub-workflow recursion is deferred to Berlioz)

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

    branch_decomposable → record_plan → end

    depth_gate / twig_gate / bad_output_gate / escalation_gate
        (human gates with `proceed` / `abort` options)

The 3-iteration cap is a **topology fact**, not a runtime counter: three
explicit (planner, reviewer, router) triples chained on the ``revise``
edge. This honours `INV-NO-CORRUPT-FORWARD` — the cap can't drift at
runtime, and the resume cursor knows exactly which iteration it's in
because the node ids are distinct.

## Recursion (decomposable children) — DEFERRED

In polyphony, a decomposable plan recurses: each proposed child becomes
the root of a fresh sub-planning run. That requires a **sub-workflow
primitive** in the kernel (Berlioz's seat). Until it ships, this
workflow records children as *proposals* (written to the plan-tree JSON
sidecar) but does not spawn sub-runs. The `record_plan` verb is the
seam where the recursive dispatch will land; the TODO is marked inline.

## Twig integration

`fetch_item` reads the root via the injected twig client. Polyphony's
``create_child`` is **not** invoked: per the brief, children are
proposals in v0, not commitments. A future ``commit-plan`` workflow
turns proposals into real ADO items.

## Plan persistence

One sidecar per run, written under ``log_dir``:

* leaf:         ``<run_id>.plan.md``
* decomposable: ``<run_id>.plan.tree.json``

The event log remains the authoritative record — sidecars are for the
human-facing verdict card and downstream tooling.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

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
from requiem.toolbelt import Toolbelt


# ---- public dataclasses --------------------------------------------------


@dataclass(frozen=True)
class PlanResult:
    """The structured outcome a parent workflow consumes.

    `children` is non-empty only when `decomposable=True`. In v0 children
    are *proposals* (planner output) — they have not been created in ADO
    nor recursively planned. See module docstring.
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
    """

    def show(self, item_id: int) -> TwigItem: ...


@dataclass
class FakeTwigClient:
    """In-memory twig stand-in for tests and the self-contained demo.

    Maps `{item_id: TwigItem}`. Raises `TwigItemNotFoundError` for misses
    so the workflow's `fetch_item` verb exercises its real classifier path.
    """

    items: dict[int, TwigItem] = field(default_factory=dict)

    def show(self, item_id: int) -> TwigItem:
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
    twig: TwigClientProto,
    log_dir: Path,
) -> VerbRegistry:
    verbs = VerbRegistry()

    @verbs.register("start_run")
    def _start(ctx):
        return Success(
            value={
                "item_id": item_id,
                "parent_plan_id": parent_plan_id,
                "current_depth": current_depth,
                "max_depth": max_depth,
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
    def _fetch(ctx):
        try:
            item = twig.show(item_id)
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
        # Pure projection — determines which planner iteration was the
        # last approved one and stamps decomposable/children into state.
        approved_iter = _find_approved_iteration(ctx.completed)
        planner = ctx.completed[f"planner_{approved_iter}"]["value"]["parsed"]
        return Success(
            value={
                "decomposable": planner["decomposable"],
                "child_count": len(planner["children"]),
                "approved_iteration": approved_iter,
            },
        )

    @verbs.register("record_plan")
    def _record(ctx):
        approved_iter = _find_approved_iteration(ctx.completed)
        planner = ctx.completed[f"planner_{approved_iter}"]["value"]["parsed"]
        item = ctx.completed["fetch_item"]["value"]
        plan_id = f"plan-{item['item_id']}-{ctx.run_id}"
        artifact = _write_plan_sidecar(
            log_dir=log_dir,
            run_id=ctx.run_id,
            plan_id=plan_id,
            item=item,
            planner=planner,
            approved_iteration=approved_iter,
            current_depth=current_depth,
        )
        # NOTE: when Berlioz's sub-workflow primitive lands, decomposable
        # plans will recursively spawn one planning run per child here.
        # The recorded children remain *proposals* in v0.
        return Success(
            value={
                "plan_id": plan_id,
                "item_id": item["item_id"],
                "item_title": item["title"],
                "decomposable": planner["decomposable"],
                "children": [dict(c) for c in planner["children"]],
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
        artifact = _write_plan_sidecar(
            log_dir=log_dir,
            run_id=ctx.run_id,
            plan_id=plan_id,
            item=item,
            planner=planner,
            approved_iteration=approved_iter,
            current_depth=current_depth,
            needs_human=True,
        )
        return Success(
            value={
                "plan_id": plan_id,
                "item_id": item.get("item_id", item_id),
                "item_title": item.get("title", "(unknown)"),
                "decomposable": bool(planner.get("decomposable")),
                "children": [dict(c) for c in planner.get("children", [])],
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
    needs_human: bool = False,
) -> Path:
    """Write the plan to a sidecar file.

    * Leaf plans → markdown (`<run_id>.plan.md`).
    * Decomposable plans → JSON tree (`<run_id>.plan.tree.json`) so the
      future sub-workflow dispatcher can read it back deterministically.

    The event log remains authoritative; this file is for humans and for
    the (not-yet-existent) recursive driver.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    decomposable = bool(planner.get("decomposable")) if planner else False
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
                    "children": [dict(c) for c in planner.get("children", [])],
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
    """Happy-path: planner returns a decomposable plan (3 children); reviewer approves on iter 1."""
    return FakeProvider(
        scripts={
            "planner": [
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
            ],
            "plan_reviewer": [
                {"verdict": "approve", "feedback": "Children are well-scoped."},
            ],
        }
    )


def demo_twig(item_id: int = 99999) -> FakeTwigClient:
    return FakeTwigClient(
        items={
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
    )


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
            .edge("branch_decomposable", on="success", to="record_plan")
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
        "branch_decomposable": "Branched on plan shape",
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
    twig: TwigClientProto | None = None,
    provider: AgentProvider | None = None,
    gate_handler=None,
) -> Engine:
    """Construct a runnable Engine for the planning workflow.

    Defaults are self-contained (FakeProvider + FakeTwigClient) so
    ``requiem run requiem.workflows.planning`` works out of the box.
    Production callers pass a real ``TwigClient`` and a real LLM
    provider.
    """
    return Engine(
        workflow=build_workflow(),
        verbs=build_verb_registry(
            item_id=item_id,
            parent_plan_id=parent_plan_id,
            max_depth=max_depth,
            current_depth=current_depth,
            twig=twig or demo_twig(item_id),
            log_dir=log_dir,
        ),
        agents=build_agent_registry(),
        provider=provider or demo_provider(),
        toolbelt=Toolbelt.real(),
        log_dir=log_dir,
        gate_handler=gate_handler or _default_gate_handler,
    )


# ---- result projection (for tests + callers) --------------------------


def project_plan_result(completed: dict) -> PlanResult | None:
    """Lift the engine's `completed` projection into a `PlanResult`.

    Accepts the `completed` map shaped `{node_id: outcome_dict}` — same
    shape the verb context sees and the renderer streams. Tests can
    build this from the event log via :func:`completed_from_log`.

    Returns `None` if neither `record_plan` nor `record_needs_human`
    ran — i.e. the workflow failed before producing any plan record.
    Recursive `PlanResult.children` remain empty in v0 (no sub-workflow
    recursion); proposed children live in the plan-tree JSON sidecar.
    """
    rec = completed.get("record_plan") or completed.get("record_needs_human")
    if not rec:
        return None
    v = rec.get("value") or {}
    return PlanResult(
        item_id=int(v.get("item_id", 0)),
        plan_id=str(v.get("plan_id", "")),
        decomposable=bool(v.get("decomposable", False)),
        children=[],
        summary=str(v.get("summary", "")),
        review_iterations=int(v.get("review_iterations", 0)),
        final_verdict=v.get("final_verdict", "approved"),
    )


def completed_from_log(log_path: Path) -> dict[str, dict[str, Any]]:
    """Rebuild the `{node_id: outcome_dict}` map from the run's event log.

    The kernel's public `RunResult.projection` is a summary; the full
    completed map only lives in memory during a run. Tests and tooling
    that want the plan record after the fact fold the JSONL stream.
    """
    from requiem.persistence import replay

    completed: dict[str, dict[str, Any]] = {}
    for ev in replay(log_path):
        if ev.get("kind") == "verb_completed":
            node = ev.get("node_id")
            payload = ev.get("payload") or {}
            outcome = payload.get("outcome")
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
    }
    for i in range(1, ITER_CAP + 1):
        details[f"planner_{i}"] = _detail_planner
        details[f"reviewer_{i}"] = _detail_reviewer
        details[f"router_{i}"] = _detail_router
    return {
        "artifact_name": "planning",
        "details": details,
        "silent_nodes": frozenset(
            {"start", "end", "end_needs_human", "fail_end", "fail_end_not_found"}
        ),
    }


def verdict_card(completed: dict) -> str | None:
    """The Demo Contract §3 verdict card.

    Two shapes — leaf and decomposable — per the brief. Falls back to
    None if no plan was recorded (the CLI then prints nothing extra).
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

    lines = [
        f"─── Plan: AB#{item_id}"
        + (" (decomposable) " if decomposable else " ")
        + "─" * max(1, 50 - len(str(item_id)) - (15 if decomposable else 0))
    ]
    if decomposable:
        children = v.get("children") or []
        lines += [
            f"  {head_mark} {head_verdict} ({len(children)} children)",
            f"      Item:        {item_id} — \"{title}\"",
        ]
        if children:
            lines.append("      Children:")
            # Right-align the [type] tag in a tabular column so the
            # verdict card reads as a small table, not raw lines.
            title_quoted = [f'"{c.get("title", "?")}"' for c in children]
            max_w = max((len(t) for t in title_quoted), default=0)
            for c, t in zip(children, title_quoted):
                wit = c.get("work_item_type", "?")
                lines.append(f"        - {t.ljust(max_w)}   [{wit}]")
        lines.append(
            f"      Reviewer:    {verdict} on iteration {iters}"
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


# ---- async-friendly variant for embedding ------------------------------


async def run_planning(
    log_dir: Path,
    run_id: str,
    *,
    item_id: int,
    parent_plan_id: str | None = None,
    max_depth: int = 4,
    current_depth: int = 0,
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
        twig=twig,
        provider=provider,
        gate_handler=gate_handler,
    )
    return await engine.run(run_id)
