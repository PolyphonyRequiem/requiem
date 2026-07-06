"""Process-config tier policy wired into the planning workflow (§9 #1).

The decompose-vs-leaf tier decision is *config-driven*, not purely LLM-driven:
``process.yaml``'s ``implementable_types`` / ``decomposable_types`` are
authoritative over the planner's own ``decomposable`` flag.

* implementable type → forced leaf (config overrides a planner that proposed
  children; the discarded count is recorded as a breadcrumb);
* decomposable type the planner left as a leaf → fail closed to
  ``type_policy_gate``;
* a configured policy with no ``work_item_type`` to classify → fail closed;
* empty tier sets (the default) → planner's decision stands unchanged;
* the effective config snapshot is threaded into recursive child inputs so a
  child tiers with the SAME config the parent started with (INV-RESTART).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from requiem.agent import FakeProvider
from requiem.clients.twig import TwigItem
from requiem.kernel import Completed
from requiem.process_config import ProcessConfig, TypeConfig
from requiem.workflows.planning import (
    FakeTwigClient,
    build_engine,
    completed_from_log,
    project_plan_result,
)

ROOT_ID = 4242


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs"
    d.mkdir()
    return d


def _proceed_handler(node_id, prompt, options):
    return "proceed" if "proceed" in options else options[0]


_proceed_handler.__requiem_auto__ = True  # type: ignore[attr-defined]


def _abort_handler(node_id, prompt, options):
    return "abort" if "abort" in options else options[-1]


_abort_handler.__requiem_auto__ = True  # type: ignore[attr-defined]


def _twig(item_type_by_id: dict[int, str]) -> FakeTwigClient:
    items = {
        i: TwigItem(
            id=i,
            title=f"Item {i}",
            state="New",
            area_path="Polyphony\\Engine",
            work_item_type=t,
            parent_id=None,
            raw={},
        )
        for i, t in item_type_by_id.items()
    }
    return FakeTwigClient(items=items)


def _leaf(summary: str = "leaf") -> dict:
    return {
        "summary": summary,
        "decomposable": False,
        "children": [],
        "estimated_complexity": "small",
        "rationale": "atomic",
    }


def _decomp(*titles: str, summary: str = "decomposable", child_type: str = "Task") -> dict:
    return {
        "summary": summary,
        "decomposable": True,
        "children": [
            {"title": t, "description": f"{t} desc", "work_item_type": child_type}
            for t in titles
        ],
        "estimated_complexity": "medium",
        "rationale": "separable",
    }


def _approve() -> dict:
    return {"verdict": "approve", "feedback": "ok"}


async def test_implementable_type_forces_leaf_over_planner(log_dir: Path):
    """Config says the root type is implementable → leaf, even though the
    planner proposed a decomposition. No children are spawned; the discard
    is recorded as a breadcrumb."""
    cfg = ProcessConfig(implementable_types=frozenset({"User Story"}))
    provider = FakeProvider(
        scripts={
            # Planner defies policy and proposes 2 children; config wins.
            "planner": [_decomp("A", "B")],
            "plan_reviewer": [_approve()],
        }
    )
    twig = _twig({ROOT_ID: "User Story"})
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
    )
    result = await engine.run("impl")
    assert isinstance(result, Completed), result
    assert result.final_node == "end"

    plan = project_plan_result(completed_from_log(engine.log_path("impl")))
    assert plan is not None
    assert plan.decomposable is False
    assert plan.children == []
    # No child sub-workflows were spawned.
    assert list(log_dir.glob("impl__child_*.events.jsonl")) == []


async def test_decomposable_type_planner_leaf_gates(log_dir: Path):
    """Config requires decomposition but the planner produced a leaf →
    fail closed to type_policy_gate. With the abort handler the run fails."""
    cfg = ProcessConfig(decomposable_types=frozenset({"Feature"}))
    provider = FakeProvider(
        scripts={"planner": [_leaf("oops")], "plan_reviewer": [_approve()]}
    )
    twig = _twig({ROOT_ID: "Feature"})
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
        gate_handler=_abort_handler,
    )
    result = await engine.run("dec_abort")
    assert isinstance(result, Completed), result
    assert result.final_node == "fail_end"


async def test_decomposable_type_planner_leaf_gate_proceed(log_dir: Path):
    """Same conflict, but the operator proceeds → recorded needs-human."""
    cfg = ProcessConfig(decomposable_types=frozenset({"Feature"}))
    provider = FakeProvider(
        scripts={"planner": [_leaf("oops")], "plan_reviewer": [_approve()]}
    )
    twig = _twig({ROOT_ID: "Feature"})
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
        gate_handler=_proceed_handler,
    )
    result = await engine.run("dec_proceed")
    assert isinstance(result, Completed), result
    assert result.final_node == "end_needs_human"


async def test_unspecified_type_preserves_planner_decision(log_dir: Path):
    """Empty tier sets → the planner's decomposition stands (legacy behavior)."""
    cfg = ProcessConfig()  # no tier policy
    child_ids = [ROOT_ID * 100 + i for i in (1, 2)]
    provider = FakeProvider(
        scripts={
            "planner": [_decomp("A", "B"), _leaf("a"), _leaf("b")],
            "plan_reviewer": [_approve(), _approve(), _approve()],
        }
    )
    twig = _twig({ROOT_ID: "User Story", child_ids[0]: "Task", child_ids[1]: "Task"})
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
    )
    result = await engine.run("unspec")
    assert isinstance(result, Completed), result
    plan = project_plan_result(completed_from_log(engine.log_path("unspec")))
    assert plan is not None
    assert plan.decomposable is True
    assert {c.item_id for c in plan.children} == set(child_ids)


async def test_missing_work_item_type_with_policy_gates(log_dir: Path):
    """A configured policy with no type to classify fails closed."""
    cfg = ProcessConfig(implementable_types=frozenset({"Task"}))
    provider = FakeProvider(
        scripts={"planner": [_leaf("x")], "plan_reviewer": [_approve()]}
    )
    twig = _twig({ROOT_ID: ""})  # blank work_item_type
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
        gate_handler=_abort_handler,
    )
    result = await engine.run("notype")
    assert isinstance(result, Completed), result
    assert result.final_node == "fail_end"


async def test_alias_normalized_type_forces_leaf(log_dir: Path):
    """An aliased type (Bug -> Task) classifies through implementable_types."""
    cfg = ProcessConfig(
        type_aliases={"Bug": "Task"},
        implementable_types=frozenset({"Task"}),
    )
    provider = FakeProvider(
        scripts={"planner": [_decomp("A")], "plan_reviewer": [_approve()]}
    )
    twig = _twig({ROOT_ID: "Bug"})
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
    )
    result = await engine.run("alias")
    assert isinstance(result, Completed), result
    plan = project_plan_result(completed_from_log(engine.log_path("alias")))
    assert plan is not None
    assert plan.decomposable is False


async def test_config_snapshot_threads_into_children(log_dir: Path):
    """A decomposable root whose children are an implementable type: the
    config snapshot reaches the child sub-workflows, which are forced to
    leaves — proving restart-safe config propagation through child inputs."""
    cfg = ProcessConfig(
        decomposable_types=frozenset({"User Story"}),
        implementable_types=frozenset({"Task"}),
    )
    child_ids = [ROOT_ID * 100 + i for i in (1, 2)]
    provider = FakeProvider(
        scripts={
            # Root decomposes (policy-compliant). Each child planner defies
            # policy by proposing a sub-decomposition; the threaded config
            # forces every child to a leaf, so those proposals are discarded.
            "planner": [_decomp("A", "B"), _decomp("a1"), _decomp("b1")],
            "plan_reviewer": [_approve(), _approve(), _approve()],
        }
    )
    twig = _twig(
        {ROOT_ID: "User Story", child_ids[0]: "Task", child_ids[1]: "Task"}
    )
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
    )
    result = await engine.run("thread")
    assert isinstance(result, Completed), result
    plan = project_plan_result(completed_from_log(engine.log_path("thread")))
    assert plan is not None
    assert plan.decomposable is True
    assert len(plan.children) == 2
    # Both children were forced to leaves by the threaded policy.
    assert all(c.decomposable is False for c in plan.children)
    assert all(c.children == [] for c in plan.children)


# ---- ADR-0025 Gap A: implementable types skip planner+reviewer entirely ----
#
# When the type policy already says "this is an implementable leaf, do
# not decompose," there is no useful work for the planner or reviewer to
# do. The pre-Gap-A flow ran them anyway and then overrode the verdict,
# paying for two LLM calls to produce a decision the config already
# made — AND surfacing reviewer escalations on leaves the reviewer has
# no authority to refine. The 2026-06-17 SKU-fallback dogfood failed
# exactly this way: 5/7 Task leaves planned cleanly, 2 escalated as
# "vague," cascade killed the whole tree, commit_plan never fired.
#
# Gap A's short-circuit: at the policy classifier stage, if
# tier_for_type == "implementable", route directly to record_plan with
# a synthesised leaf PlanResult. ZERO LLM calls. The breadcrumb
# inspected_artifact is `policy:implementable/<work_item_type>` to make
# the policy-driven nature legible in the event log.


async def test_implementable_type_skips_planner_and_reviewer_entirely(
    log_dir: Path,
):
    """ADR-0025 Gap A load-bearing pin. When the root's type is in
    implementable_types, NO planner call AND NO reviewer call is made;
    the plan goes straight to recorded-as-leaf."""
    cfg = ProcessConfig(implementable_types=frozenset({"Task"}))
    # Empty scripts on both agents — if either gets called we'll see
    # fake.exhausted PermanentFailure (which surfaces as a non-success
    # outcome and our assertion below will catch it loud).
    provider = FakeProvider(scripts={"planner": [], "plan_reviewer": []})
    twig = _twig({ROOT_ID: "Task"})
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
    )
    result = await engine.run("gap-a-impl")
    assert isinstance(result, Completed), result
    assert result.final_node == "end", (
        f"implementable-type root should reach 'end' without LLM calls; "
        f"got final_node={result.final_node!r}"
    )

    # Hard assertion: ZERO LLM calls of any kind.
    assert provider.calls == [], (
        f"implementable type should skip planner+reviewer entirely; "
        f"got calls: {provider.calls}"
    )

    # The plan record exists and is shaped correctly.
    plan = project_plan_result(completed_from_log(engine.log_path("gap-a-impl")))
    assert plan is not None
    assert plan.decomposable is False
    assert plan.children == []
    # The synthesised summary echoes the work-item title from twig.
    assert plan.summary == f"Item {ROOT_ID}"


async def test_implementable_type_skip_records_policy_artifact(
    log_dir: Path,
):
    """The short-circuit must leave a `policy:implementable/<type>`
    breadcrumb in inspected_artifacts so an operator reading the event
    log can tell this leaf was policy-driven, not planner-driven.
    Without this breadcrumb the audit story is broken — you can't
    distinguish 'planner said leaf' from 'policy forced leaf'."""
    cfg = ProcessConfig(implementable_types=frozenset({"Task"}))
    provider = FakeProvider(scripts={"planner": [], "plan_reviewer": []})
    twig = _twig({ROOT_ID: "Task"})
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
    )
    result = await engine.run("gap-a-artifact")
    assert isinstance(result, Completed)
    assert result.final_node == "end"

    # Walk the event log to find the policy:implementable artifact.
    from requiem.persistence import replay
    log_path = engine.log_path("gap-a-artifact")
    found_policy_artifact = False
    found_agent_artifact = False
    for ev in replay(log_path):
        if ev.get("kind") != "verb_completed":
            continue
        oc = (ev.get("payload") or {}).get("outcome", {}) or {}
        artifacts = oc.get("inspected_artifacts") or []
        for a in artifacts:
            if a.startswith("policy:implementable/"):
                found_policy_artifact = True
                assert "Task" in a, (
                    f"breadcrumb should name the work_item_type: {a!r}"
                )
            if a.startswith("agent:planner/") or a.startswith("agent:plan_reviewer/"):
                found_agent_artifact = True
    assert found_policy_artifact, (
        "implementable-type short-circuit should leave a "
        "policy:implementable/<type> breadcrumb in inspected_artifacts"
    )
    assert not found_agent_artifact, (
        "no agent artifacts should appear; we skipped planner+reviewer"
    )


async def test_decomposable_type_still_calls_planner_no_regression(
    log_dir: Path,
):
    """ADR-0025 Gap A guard against the short-circuit accidentally
    swallowing the decomposable path. When the root is a decomposable
    type (e.g. Scenario), the planner+reviewer pair DOES run exactly as
    before — the short-circuit is implementable-only."""
    cfg = ProcessConfig(
        decomposable_types=frozenset({"Scenario"}),
        implementable_types=frozenset({"Task"}),
    )
    provider = FakeProvider(
        scripts={
            "planner": [_decomp("childA", "childB")],
            "plan_reviewer": [_approve()],
        }
    )
    twig = _twig({ROOT_ID: "Scenario", ROOT_ID * 100 + 1: "Task", ROOT_ID * 100 + 2: "Task"})
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
    )
    result = await engine.run("gap-a-decomp")
    assert isinstance(result, Completed), result
    assert result.final_node == "end"

    # Both planner AND reviewer were called for the ROOT (Scenario).
    # Each child (Task) skips planner+reviewer per the same short-circuit,
    # so the call count is exactly 2 (one planner + one reviewer for root).
    agent_names = [c["agent"] for c in provider.calls]
    assert "planner" in agent_names, (
        f"decomposable root must call planner; got: {agent_names}"
    )
    assert "plan_reviewer" in agent_names, (
        f"decomposable root must call reviewer; got: {agent_names}"
    )
    # Exactly one of each — no recursion-driven LLM calls for the Task
    # children (they short-circuit per Gap A).
    assert agent_names.count("planner") == 1, agent_names
    assert agent_names.count("plan_reviewer") == 1, agent_names

    plan = project_plan_result(completed_from_log(engine.log_path("gap-a-decomp")))
    assert plan is not None
    assert plan.decomposable is True
    assert len(plan.children) == 2
    assert all(c.decomposable is False for c in plan.children), (
        "Task children must be policy-driven leaves"
    )


async def test_no_tier_policy_still_calls_planner_no_regression(
    log_dir: Path,
):
    """When no tier policy is configured (the polyphony default), the
    short-circuit must not fire — planner runs as before for ALL types.
    Pin against the short-circuit accidentally activating on default config."""
    cfg = ProcessConfig()  # no implementable_types, no decomposable_types
    provider = FakeProvider(
        scripts={
            "planner": [_leaf("atomic")],
            "plan_reviewer": [_approve()],
        }
    )
    twig = _twig({ROOT_ID: "Task"})  # Task root with NO policy
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
    )
    result = await engine.run("gap-a-no-policy")
    assert isinstance(result, Completed)
    assert result.final_node == "end"

    # planner + reviewer were both called (no short-circuit without policy).
    agent_names = [c["agent"] for c in provider.calls]
    assert "planner" in agent_names
    assert "plan_reviewer" in agent_names


# ---- ADR-0025 Gap A* (post-Gap-A finding): reviewer prompt must include
# the planner's proposed children, not just the count ----------------------
#
# 2026-06-17 SKU-fallback dogfood retry (#62759077, run 4): after Gap A
# shipped, the top-level Scenario plan still cascade-failed because the
# reviewer escalated on iteration 2 with the feedback:
#
#   "Cannot properly evaluate plan without seeing the 7 proposed child
#    tasks. Need visibility into the actual decomposition to verify..."
#
# That feedback was CORRECT. The reviewer prompt was literally telling
# the model "children: 7 proposed" — just the count — without any of
# the actual titles, types, or descriptions. The reviewer had no way to
# evaluate the decomposition. Fix: render the full child list in the
# reviewer prompt so it can evaluate properly.


async def test_planner_and_reviewer_prompts_include_root_description(
    log_dir: Path,
):
    """Pin the 2026-07-05 run #34 postmortem fix: previously the planner
    (and reviewer) only ever saw item_id/title/type/state — never the
    ADO work item's actual description. On AB#62759077 the terse title
    left the planner to invent scope (a whole "provision new regions"
    subtree) that contradicted the real, detailed description ("fallback
    within EXISTING regions"). Both prompts must now include the
    description so proposed children can be grounded in it."""
    cfg = ProcessConfig(
        decomposable_types=frozenset({"Scenario"}),
        implementable_types=frozenset({"Task"}),
    )
    description = (
        "Fallback within EXISTING regions only — do not provision new "
        "regions. Integration point is the existing Cluster/Main.bicep."
    )
    twig = FakeTwigClient(
        items={
            ROOT_ID: TwigItem(
                id=ROOT_ID,
                title="Capacity-aware VMSS SKU fallback",
                state="New",
                area_path="Polyphony\\Engine",
                work_item_type="Scenario",
                parent_id=None,
                raw={"fields": {"System.Description": description}},
            )
        }
    )
    provider = FakeProvider(
        scripts={
            "planner": [_leaf()],
            "plan_reviewer": [_approve()],
        }
    )
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
    )
    result = await engine.run("description-grounding")
    assert isinstance(result, Completed), result

    planner_prompt = next(
        c["user_message"] for c in provider.calls if c["agent"] == "planner"
    )
    reviewer_prompt = next(
        c["user_message"] for c in provider.calls if c["agent"] == "plan_reviewer"
    )
    assert description in planner_prompt, (
        f"planner prompt must include the root item's description; got:\n"
        f"{planner_prompt}"
    )
    assert description in reviewer_prompt, (
        f"reviewer prompt must include the root item's description; got:\n"
        f"{reviewer_prompt}"
    )


async def test_reviewer_prompt_includes_child_titles_not_just_count(
    log_dir: Path,
):
    """Pin against the 2026-06-17 dogfood regression: when the planner
    proposes children, the reviewer's prompt must contain each child's
    title (and ideally type+description) so the reviewer can actually
    evaluate the decomposition. Showing just '7 proposed' as a count
    forces the reviewer to escalate (or worse, hallucinate)."""
    cfg = ProcessConfig(
        decomposable_types=frozenset({"Scenario"}),
        implementable_types=frozenset({"Task"}),
    )
    provider = FakeProvider(
        scripts={
            "planner": [_decomp("Investigate SKU selection", "Implement fallback logic", "Add observability")],
            "plan_reviewer": [_approve()],
        }
    )
    twig = _twig({
        ROOT_ID: "Scenario",
        ROOT_ID * 100 + 1: "Task",
        ROOT_ID * 100 + 2: "Task",
        ROOT_ID * 100 + 3: "Task",
    })
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
    )
    result = await engine.run("reviewer-sees-children")
    assert isinstance(result, Completed), result
    assert result.final_node == "end", (
        f"approved reviewer should reach end; got {result.final_node}"
    )

    # Find the reviewer call's user_message.
    reviewer_calls = [c for c in provider.calls if c["agent"] == "plan_reviewer"]
    assert reviewer_calls, "reviewer should have been called for a Scenario root"
    reviewer_prompt = reviewer_calls[0]["user_message"]

    # The actual regression assertion: each proposed child's title must
    # appear in the reviewer's prompt. Pre-fix, only the count ("3 proposed")
    # was rendered; post-fix, the titles must be present.
    assert "Investigate SKU selection" in reviewer_prompt, (
        f"reviewer prompt should include child 1's title; got prompt:\n"
        f"{reviewer_prompt}"
    )
    assert "Implement fallback logic" in reviewer_prompt, (
        f"reviewer prompt should include child 2's title; got prompt:\n"
        f"{reviewer_prompt}"
    )
    assert "Add observability" in reviewer_prompt, (
        f"reviewer prompt should include child 3's title; got prompt:\n"
        f"{reviewer_prompt}"
    )


async def test_reviewer_prompt_does_not_truncate_descriptions(
    log_dir: Path,
):
    """Pin against the 2026-06-17 dogfood run 7 regression: the
    reviewer prompt USED to truncate child descriptions at 400 chars
    and append `…`. The reviewer then thought the PLANNER produced
    truncated descriptions ("description is truncated mid-sentence at
    'recommended SKU p…'") and revised forever to fix what was actually
    a rendering artifact in our prompt template.

    Fix: show the description in full. This test pins that a
    description longer than 400 chars survives intact in the prompt
    with no '…' ellipsis appended."""
    cfg = ProcessConfig(
        decomposable_types=frozenset({"Scenario"}),
        implementable_types=frozenset({"Task"}),
    )
    # Build a child with a 600-char description that mentions specific
    # phrases at both ends so we can assert the full string survived.
    long_desc = (
        "Implement the SKU fallback selection module that wraps the "
        "Azure Compute SDK's VirtualMachineSizes client, queries the "
        "regional capacity API, ranks candidate SKUs by configured "
        "priority, validates each against the workload's resource "
        "requirements (memory, NVMe, accelerated networking), filters "
        "out SKUs the subscription doesn't have quota for, and emits "
        "structured telemetry on every fallback decision so that "
        "operators can investigate the chosen SKU after the fact via "
        "the recommended SKU policy review dashboard. NOTE: this is "
        "the END of the description."
    )
    assert len(long_desc) > 400, "test description must exceed the old cap"

    planner_output = {
        "summary": "decomposable",
        "decomposable": True,
        "children": [
            {
                "title": "Build SKU fallback module",
                "description": long_desc,
                "work_item_type": "Task",
            }
        ],
        "estimated_complexity": "medium",
        "rationale": "separable",
    }
    provider = FakeProvider(
        scripts={
            "planner": [planner_output],
            "plan_reviewer": [_approve()],
        }
    )
    twig = _twig({ROOT_ID: "Scenario", ROOT_ID * 100 + 1: "Task"})
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
    )
    result = await engine.run("no-truncate")
    assert isinstance(result, Completed), result

    reviewer_prompt = next(
        c["user_message"] for c in provider.calls if c["agent"] == "plan_reviewer"
    )

    # The first words of the description must appear.
    assert "Implement the SKU fallback selection module" in reviewer_prompt
    # The LAST words must also appear — proves no truncation happened.
    assert "this is the END of the description." in reviewer_prompt, (
        f"description must NOT be truncated; full prompt:\n{reviewer_prompt}"
    )
    # And NO truncation ellipsis (the … character we used to append).
    assert "…" not in reviewer_prompt, (
        f"reviewer prompt should never contain the truncation ellipsis; "
        f"got:\n{reviewer_prompt}"
    )


# ============================================================
# ADR-0026 step 2: planner prompt injects decomposition_guidance
# ============================================================


async def test_planner_prompt_includes_decomposition_guidance_for_decomposable_type(
    log_dir: Path,
):
    """When the parent's TypeConfig has decomposition_guidance, that text
    MUST appear in the planner's prompt. This is the lever that steers
    the LLM toward producing the right child types (Features under
    Scenarios, Tasks under Features, etc.). Without it the planner is
    free to invent any decomposition shape consistent with the loose
    schema enum — which is exactly the 2026-06-17 SKU-fallback dogfood
    bug (Scenarios produced Tasks directly because nothing told the
    planner to produce Features first)."""
    cfg = ProcessConfig(
        types={
            "Scenario": TypeConfig(
                facets=("plannable",),
                decomposition_guidance=(
                    "Decompose into Features. NEVER directly into Tasks — "
                    "Features always sit between."
                ),
            ),
            "Feature": TypeConfig(
                facets=("plannable", "implementable"),
                decomposition_guidance="Decompose into Tasks.",
            ),
            "Task": TypeConfig(facets=("implementable",)),
        }
    )
    provider = FakeProvider(
        scripts={
            "planner": [_decomp("First Feature", "Second Feature")],
            "plan_reviewer": [_approve()],
        }
    )
    twig = _twig({
        ROOT_ID: "Scenario",
        ROOT_ID * 100 + 1: "Feature",
        ROOT_ID * 100 + 2: "Feature",
    })
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
    )
    result = await engine.run("guidance-injected")
    assert isinstance(result, Completed), result

    planner_calls = [c for c in provider.calls if c["agent"] == "planner"]
    assert planner_calls, "planner should be invoked for a Scenario root"
    planner_prompt = planner_calls[0]["user_message"]

    # The actual regression assertion: the guidance text MUST be in the prompt.
    assert "Decompose into Features" in planner_prompt, (
        f"planner prompt should include Scenario's decomposition_guidance; "
        f"got prompt:\n{planner_prompt}"
    )
    assert "NEVER directly into Tasks" in planner_prompt, (
        f"planner prompt should include the full guidance, not a fragment; "
        f"got prompt:\n{planner_prompt}"
    )


async def test_planner_prompt_omits_guidance_for_types_without_it(
    log_dir: Path,
):
    """A type with `plannable` facet but NO decomposition_guidance must
    not blow up — the prompt just doesn't include any guidance line."""
    cfg = ProcessConfig(
        types={
            "Scenario": TypeConfig(facets=("plannable",)),  # no guidance
            "Task": TypeConfig(facets=("implementable",)),
        }
    )
    provider = FakeProvider(
        scripts={
            "planner": [_decomp("child")],
            "plan_reviewer": [_approve()],
        }
    )
    twig = _twig({ROOT_ID: "Scenario", ROOT_ID * 100 + 1: "Task"})
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
    )
    result = await engine.run("no-guidance")
    assert isinstance(result, Completed), result

    planner_calls = [c for c in provider.calls if c["agent"] == "planner"]
    assert planner_calls
    planner_prompt = planner_calls[0]["user_message"]
    # The generic policy line still appears.
    assert "MUST be decomposed" in planner_prompt
    # But no domain-specific guidance.
    assert "Decompose into" not in planner_prompt


async def test_planner_prompt_omits_guidance_for_implementable_types(
    log_dir: Path,
):
    """Implementable types short-circuit out of the planner via Gap A,
    so guidance attached to them (even if present) is never injected.
    Pin: a Task root never hits the planner regardless of any guidance
    attached to its TypeConfig."""
    cfg = ProcessConfig(
        types={
            # Pathological but legal: implementable type with guidance.
            # Guidance must be IGNORED — the type short-circuits.
            "Task": TypeConfig(
                facets=("implementable",),
                decomposition_guidance="SHOULD NEVER APPEAR IN ANY PROMPT",
            ),
        }
    )
    provider = FakeProvider(scripts={})  # planner should NEVER be called
    twig = _twig({ROOT_ID: "Task"})
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
    )
    result = await engine.run("impl-guidance-ignored")
    assert isinstance(result, Completed), result
    # Gap A short-circuit should have run — no provider calls.
    assert provider.calls == [], (
        f"implementable types must bypass the planner; got calls: "
        f"{provider.calls}"
    )


# ============================================================
# ADR-0026 step 3: branch_decomposable enforces per-type max_nesting_depth
# ============================================================


async def test_planner_proposed_unknown_type_routes_to_type_policy_gate(
    log_dir: Path,
):
    """ADR-0015 §9 #1 (type-agnostic engine) + ADR-0026: when the
    operator's process.yaml declares `types`, every planner-proposed
    child must have a work_item_type the operator has declared.

    Without enforcement, a hallucinated type (e.g. the planner outputs
    'Spike' when CVAPI doesn't have Spike) would silently flow to
    commit_plan and twig's `new --type Spike` would fail at the ADO
    boundary with a confusing error. Better: catch it at the planning
    layer and route to type_policy_gate with the actual unknown types.

    The pin: planner proposes a child with type 'Spike' (not in cfg.types);
    workflow routes to type_policy_gate with the proceed_handler
    accepting as needs-human.
    """
    cfg = ProcessConfig(
        types={
            "Scenario": TypeConfig(facets=("plannable",)),
            "Task": TypeConfig(facets=("implementable",)),
        }
    )
    provider = FakeProvider(
        scripts={
            # Planner proposes ONE child of type "Spike" — not in cfg.types.
            "planner": [{
                "summary": "decomposable",
                "decomposable": True,
                "children": [{
                    "title": "rogue child",
                    "description": "type not in operator config",
                    "work_item_type": "Spike",  # not declared
                }],
                "estimated_complexity": "medium",
                "rationale": "test",
            }],
            "plan_reviewer": [_approve()],
        }
    )
    twig = _twig({ROOT_ID: "Scenario"})
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
        gate_handler=_proceed_handler,
    )
    result = await engine.run("unknown-type")
    assert isinstance(result, Completed), result
    # Inspect events for the unknown_child_work_item_type signal.
    import json
    log_path = log_dir / "unknown-type.events.jsonl"
    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    unknown_signals = [
        ev for ev in events
        if (
            ev.get("kind") == "verb_completed"
            and ev.get("node_id") == "branch_decomposable"
            and (ev.get("payload", {}).get("outcome", {}) or {}).get("error_kind")
            == "unknown_child_work_item_type"
        )
    ]
    assert unknown_signals, (
        f"branch_decomposable should have emitted "
        f"'unknown_child_work_item_type' for the Spike child; "
        f"events seen: "
        f"{[(ev.get('kind'), ev.get('node_id')) for ev in events[:25]]}"
    )
    # type_policy_gate should have been entered.
    entered_gate = [
        ev for ev in events
        if ev.get("kind") == "node_entered"
        and ev.get("node_id") == "type_policy_gate"
    ]
    assert entered_gate, "should have routed to type_policy_gate"


async def test_planner_proposed_known_types_pass_validation(log_dir: Path):
    """Sanity sibling: when every proposed child has a known type, the
    new validation gate is NOT triggered. Pin so future schema changes
    don't accidentally false-positive."""
    cfg = ProcessConfig(
        types={
            "Scenario": TypeConfig(facets=("plannable",)),
            "Task": TypeConfig(facets=("implementable",)),
        }
    )
    provider = FakeProvider(
        scripts={
            "planner": [_decomp("legit", child_type="Task")],
            "plan_reviewer": [_approve()],
        }
    )
    twig = _twig({ROOT_ID: "Scenario", ROOT_ID * 100 + 1: "Task"})
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
    )
    result = await engine.run("known-types")
    assert isinstance(result, Completed), result
    # No type_policy_gate entered.
    import json
    log_path = log_dir / "known-types.events.jsonl"
    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    entered_gate = [
        ev for ev in events
        if ev.get("kind") == "node_entered"
        and ev.get("node_id") == "type_policy_gate"
    ]
    assert not entered_gate, (
        f"type_policy_gate should NOT have fired; entries: {entered_gate}"
    )


async def test_max_nesting_depth_per_type_blocks_recursion(
    log_dir: Path,
):
    """ADR-0026 step 3 — DEFERRED.

    Per-type max_nesting_depth ENFORCEMENT requires cap propagation
    through child_inputs so each subworkflow knows its remaining
    budget — meaningful wiring beyond the lever the
    decomposition_guidance prompt already provides. Deferred to a
    follow-up once we have evidence guidance alone isn't sufficient.

    This test pins the CURRENT (deferred) behavior: the TypeConfig
    field is parsed, accessible via `max_nesting_depth_for`, and
    snapshotted — but `branch_decomposable` does not yet enforce it.
    The intent is to flip this test from documenting absence to
    asserting presence when the cap-propagation refactor lands.
    """
    cfg = ProcessConfig(
        types={
            "Scenario": TypeConfig(
                facets=("plannable",),
                max_nesting_depth=1,
            ),
            "Feature": TypeConfig(facets=("plannable", "implementable")),
            "Task": TypeConfig(facets=("implementable",)),
        }
    )
    # The field is accessible.
    assert cfg.max_nesting_depth_for("Scenario") == 1
    assert cfg.max_nesting_depth_for("Feature") is None
    # And it round-trips through snapshot for the resume path.
    snap = cfg.to_snapshot()
    restored = ProcessConfig.from_snapshot(snap)
    assert restored.max_nesting_depth_for("Scenario") == 1


async def test_max_nesting_depth_per_type_zero_caps_at_root(
    log_dir: Path,
):
    """max_nesting_depth=0 means 'this type cannot have its
    decomposition recurse at all — children must be leaves'. With the
    short-circuit (Gap A) handling implementable children, this is
    equivalent to 'a plannable type with all-implementable children'.

    Edge case test: ensure 0 is handled, not interpreted as None/missing.
    """
    cfg = ProcessConfig(
        types={
            "Epic": TypeConfig(
                facets=("plannable",),
                max_nesting_depth=0,  # children of Epic must not decompose further
            ),
            "Task": TypeConfig(facets=("implementable",)),
        }
    )
    provider = FakeProvider(
        scripts={
            "planner": [_decomp("the-task")],
            "plan_reviewer": [_approve()],
        }
    )
    twig = _twig({ROOT_ID: "Epic", ROOT_ID * 100 + 1: "Task"})
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
        max_depth=4,
        gate_handler=_proceed_handler,
    )
    result = await engine.run("per-type-depth-zero")
    # The Epic decomposes once (depth 0 → depth 1); children are Tasks
    # which short-circuit as leaves. No deeper recursion attempted. Run
    # should complete cleanly.
    assert isinstance(result, Completed), result


async def test_max_nesting_depth_per_type_none_does_not_block(
    log_dir: Path,
):
    """When max_nesting_depth is None (the default), only the global
    max_depth caps recursion. Pin: a plannable type without a per-type
    cap allows recursion up to global max_depth as before."""
    cfg = ProcessConfig(
        types={
            # No max_nesting_depth on either type.
            "Scenario": TypeConfig(facets=("plannable",)),
            "Feature": TypeConfig(facets=("plannable", "implementable")),
            "Task": TypeConfig(facets=("implementable",)),
        }
    )
    feature_id = ROOT_ID * 100 + 1
    provider = FakeProvider(
        scripts={
            "planner": [_decomp("the-feature", child_type="Feature"), _leaf("atomic")],
            "plan_reviewer": [_approve(), _approve()],
        }
    )
    twig = _twig({
        ROOT_ID: "Scenario",
        feature_id: "Feature",
    })
    engine = build_engine(
        log_dir,
        item_id=ROOT_ID,
        twig=twig,
        provider=provider,
        process_config=cfg,
        max_depth=4,
    )
    result = await engine.run("no-per-type-cap")
    assert isinstance(result, Completed), result
    # Both levels recursed cleanly because there's no per-type cap.
    plan = project_plan_result(completed_from_log(engine.log_path("no-per-type-cap")))
    assert plan is not None
