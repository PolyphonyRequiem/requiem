# ADR 0026 — Per-type process config: facets, decomposition guidance, and depth caps

**Status:** Proposed (2026-06-17)
**Date:** 2026-06-17
**Relates to:**
ADR-0010 (planning tier model — defines decomposable vs implementable),
ADR-0015 (process config as the type-agnostic routing seam — declared
`decomposable_types` and `implementable_types` as reserved fields for
this growth),
ADR-0025 (dogfood delivery path — Gap A added implementable-types
short-circuit using the flat tier model; this ADR upgrades the schema
to enable richer hierarchy enforcement).
**Supersedes:** the flat-tier model from ADR-0015 + ADR-0025 Gap A
(but stays backward compatible with old configs — see migration).

## Context

The 2026-06-17 SKU-fallback dogfood (CVAPI Scenario `#62759077`)
proved the flat tier model from ADR-0015 was structurally inadequate:

```yaml
# ~/.config/requiem/cvapi-process.yaml (pre-this-ADR)
decomposable_types: [Objective, Key Result, Epic, Scenario, Feature]
implementable_types: [Task, Bug, User Story]
```

Under this config the LLM planner, when handed a Scenario, decomposed
it into Tasks directly — bypassing the intermediate Feature layer.
That's technically legal per the config (Task IS in the implementable
set, and decomposable+implementable types are the only options for a
ChildPlan), but it's **wrong for the CVAPI hierarchy**: the operator
expects Scenarios to decompose into Features, and Features to
decompose into Tasks. The flat schema can't express the difference.

The operator's pushback was explicit: *"the usage of tasks directly
under scenario is a violation as well in my mind, that shouldn't have
happened."*

### Why flat tiers can't express this

The flat model has two sets:

- `decomposable_types`: items the planner MUST decompose
- `implementable_types`: items the planner MUST treat as leaves

There's no representation of **which** implementable type a given
decomposable type should produce. Once the planner knows it must
decompose, the only constraint on child types is the `ChildPlan`
schema's `work_item_type: Literal[...]` enum — and that's tuned for
parseability, not policy.

### Polyphony already solved this

Polyphony's `.polyphony-config/process-config.yaml` uses a per-type
schema with facets and decomposition guidance:

```yaml
types:
  Epic:
    facets: [plannable]
    decomposition_guidance: |
      Always decompose into Issues. Epics are never implemented directly.
    max_nesting_depth: 1
  Issue:
    facets: [plannable, implementable]
    decomposition_guidance: |
      Decompose into Tasks when scope exceeds a single PG (~2000 LoC).
      Implement directly when the change is focused and fits one PG.
    max_nesting_depth: 1
  Task:
    facets: [implementable, actionable]
    facet_order: [actionable, implementable]
    actionable_executor: polyphony
```

And requiem's own `.requiem/prototypes/faure-router/process.yaml`
prototype already adopted the facets model — it just never got
promoted out of prototype.

This ADR records the decision to **promote that schema to production
requiem**, with CVAPI-specific specifics, and rewrites ADR-0025 §1's
config to match.

## Decision

### New schema: `types` is the source of truth

`ProcessConfig` gains a `types: dict[str, TypeConfig]` field where
each `TypeConfig` carries:

| Field | Required | Purpose |
|---|---|---|
| `facets: list[str]` | yes | Tier classification by capability. Valid values: `plannable` (planner is invoked, child decomposition allowed), `implementable` (workflow may implement directly, no decomposition required), `actionable` (executor backend dispatches). A type can carry multiple facets (e.g. Feature is both plannable AND implementable). |
| `decomposition_guidance: str` | when `plannable` | Free-form instruction injected into the planner prompt for items of this type. The single most powerful lever for steering decomposition shape. |
| `max_nesting_depth: int` | optional | Cap on how deep recursion can go from a plannable type. `1` means "decompose once, then children must be implementable leaves." Omitted → unbounded (subject to global `max_depth`). |
| `actionable_executor: str` | when `actionable` | Names the executor backend that picks up actionable items. Today only `requiem` (the in-process fanout); future: kanban worker, ado-pipeline, etc. |

### Flat-set fields are derived, not authoritative

`decomposable_types` and `implementable_types` continue to exist on
`ProcessConfig` for back-compat with verb code, but they are
**derived** from `types`:

- `decomposable_types = {t for t, cfg in types.items() if "plannable" in cfg.facets}`
- `implementable_types = {t for t, cfg in types.items() if "implementable" in cfg.facets and "plannable" not in cfg.facets}`

A type with BOTH `plannable` AND `implementable` facets (e.g. Feature)
appears in `decomposable_types` only — the planner is invoked, but the
planner is free to return `decomposable=false` and the workflow
accepts it as a leaf.

### Migration: legacy configs keep working

If a config defines the old flat `decomposable_types` / `implementable_types`
keys **without** a `types` map, the loader synthesises a minimal
`types` map from them:

```python
for t in decomposable_types:
    types[t] = TypeConfig(facets=["plannable"])
for t in implementable_types:
    types[t] = TypeConfig(facets=["implementable", "actionable"])
```

Existing tests and configs continue to work unchanged. A config that
provides BOTH new-shape `types` AND legacy flat sets is a
`ProcessConfigError` (ambiguous source of truth).

### Planner prompt: inject decomposition_guidance

`planning.py`'s `_planner_prompt` already constructs a policy line for
"this type must decompose" / "this type is a leaf." Extend it: when
the type's `TypeConfig` has `decomposition_guidance`, append it to
the prompt. The planner now knows the target child type AND any
domain-specific rules ("Decompose into Features; Tasks come from
Features, not directly from Scenarios").

### branch_decomposable: enforce max_nesting_depth

Today the workflow has a global `max_depth`. Add a per-type cap:
when the parent's `TypeConfig.max_nesting_depth` is set, the
recursion from THAT type cannot exceed it. Routes overflow to
`recursion_depth_gate` (the existing gate). Useful for "Epic
decomposes once and that's it."

### CVAPI config under the new schema

```yaml
types:
  Objective:
    facets: [plannable]
    decomposition_guidance: |
      Decompose into Key Results. Objectives are strategic outcomes;
      Key Results are the measurable milestones that ladder up to them.
    max_nesting_depth: 1
  "Key Result":
    facets: [plannable]
    decomposition_guidance: |
      Decompose into Epics. A Key Result represents a measurable
      milestone; Epics group the work needed to hit it.
    max_nesting_depth: 1
  Epic:
    facets: [plannable]
    decomposition_guidance: |
      Decompose into Scenarios. Epics describe customer-facing
      capabilities; Scenarios describe the user journeys / workflows
      that deliver them.
    max_nesting_depth: 1
  Scenario:
    facets: [plannable]
    decomposition_guidance: |
      Decompose into Features. A Scenario is a user journey; a Feature
      is an intermediate planning unit that groups related Tasks under
      a single deliverable theme. NEVER decompose a Scenario directly
      into Tasks — Features always sit between.
    max_nesting_depth: 1
  Feature:
    facets: [plannable, implementable]
    decomposition_guidance: |
      Decompose into Tasks (the concrete implementation units).
      Implement directly only when the Feature is small enough to fit
      in a single PR (~500 LoC of net change).
    max_nesting_depth: 1
  Task:
    facets: [implementable, actionable]
    actionable_executor: requiem
  Bug:
    facets: [implementable, actionable]
    actionable_executor: requiem
  "User Story":
    facets: [implementable, actionable]
    actionable_executor: requiem
```

The "NEVER decompose a Scenario directly into Tasks" sentence is the
critical one for CVAPI — it's the operator's hard rule, made
machine-readable by being in the prompt every iteration.

## Why not adjacency edges?

A natural alternative is an explicit `child_types: [...]` field per
type, encoding the parent→child graph as a hard adjacency matrix.
Rejected for now because:

1. **Polyphony didn't need it.** Their experience showed
   `decomposition_guidance` (a string the planner reads) plus
   `max_nesting_depth` (a structural cap) is enough to produce the
   right shape. The LLM is good at following clear textual guidance;
   making it a hard graph adds enforcement complexity without
   removing the need for the prompt anyway.
2. **`decomposition_guidance` is more expressive.** "Decompose into
   Features unless the Scenario is trivial, then into Tasks
   directly" is one sentence. The same rule as an adjacency matrix
   needs a `child_types_when_trivial` field or similar — more schema,
   less clarity.
3. **Validation in `branch_decomposable` is the safety net.** When
   the planner produces an unexpected child type, `branch_decomposable`
   can compare against the guidance and route to `type_policy_gate`
   for human resolution. We don't need an adjacency matrix to detect
   the violation; we just need to look at the parent's expected
   children (from guidance) vs what came back.
4. **It's reversible.** If `decomposition_guidance` proves
   insufficient in practice (planners keep going off the rails), we
   can add a `child_types` adjacency in a follow-up ADR without
   breaking the schema.

## Scope of this ADR

**In scope:**
- New `TypeConfig` dataclass + `types: dict[str, TypeConfig]` field on `ProcessConfig`
- YAML parser for the new schema
- Back-compat shim: legacy flat configs synthesise a `types` map
- `decomposable_types` / `implementable_types` derived from facets
- Planner prompt includes `decomposition_guidance`
- `branch_decomposable` enforces `max_nesting_depth` per type
- Rewrite of `~/.config/requiem/cvapi-process.yaml` and the example doc
- Tests for all of the above

**Out of scope (deferred):**
- Cross-run commit_plan idempotency (was the planned Bug B from
  the session — defer to a follow-up ADR once the new schema lands
  so the manifest format can be designed against the new shape)
- Executor failure investigation from run 11 (separate bug, separate session)
- Adjacency-matrix enforcement (`child_types` field) — see "Why not"
  above; defer until evidence shows guidance + max_depth is insufficient
- Hard enforcement that planner-proposed child types match guidance
  (today's policy: log + route to `type_policy_gate` if they don't;
  do NOT hard-reject because that would loop the planner)

## STATUS log

- **2026-06-17 PROPOSED.** Plan written; no code committed against
  this ADR yet. Implementation order:
  1. `process_config.py` schema + back-compat shim + tests
  2. `planning.py` prompt integration + tests
  3. `planning.py` max_nesting_depth enforcement + tests
  4. Operator config rewrite + example doc
  5. Broad sweep + commit + push
  6. Re-run #62759077 dogfood and inspect the proposed children
