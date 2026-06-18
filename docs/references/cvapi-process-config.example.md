# Example: CloudVault Service API process config

This is the type policy I (Daniel) use when running requiem against
the `cloudvault-service-api` ADO repo. **It is not committed to that
repo** by design (see ADR-0025 §1 "Operator-only vs team-committed
config"); it lives at `~/.config/requiem/cvapi-process.yaml` and is
passed in explicitly:

```bash
requiem-end-to-end \
  --item <id> \
  --ado-repo microsoft/CloudVault/cloudvault-service-api \
  --process-config ~/.config/requiem/cvapi-process.yaml \
  ...
```

## How I discovered the right shape (anti-pattern alert)

My first attempt encoded `Feature` as the Scenario→child intermediate
because I copied polyphony's `Epic → Issue → Task` pattern and
assumed Feature was the standard ADO equivalent of Issue. **That was
wrong.** CVAPI's convention is **Deliverable**, not Feature.

Discovery query that produced the right hierarchy (run BEFORE
encoding type names into your process.yaml):

```bash
# Count items of each candidate type in the area path:
twig query --type "Deliverable" \
  --area-path "OS\Azure Core\Azure Core CTO\Azure Engineering Systems\CloudVault"
# → count: 25+ (the conventional intermediate)

twig query --type "Feature" \
  --area-path "OS\Azure Core\Azure Core CTO\Azure Engineering Systems\CloudVault"
# → count: 7 (mostly synthetic / outliers)
```

When the count for one type is an order of magnitude larger than
alternatives in your team's area path, that's the convention. Use it.

Reference: requiem ADR-0026 establishes that requiem itself is
type-agnostic; type names live exclusively in your process.yaml.
ADR-0015 §9 #1 codifies that the engine never names ADO types in code.

## Why this shape

CVAPI's ADO work-item hierarchy (per `twig process` + the area-path
discovery query above):

```
Objective → Key Result → Epic → Scenario → Deliverable → Task | Bug | User Story
```

The flat-tier model from ADR-0015 (and ADR-0025 Gap A) was structurally
inadequate to encode the **Scenario → Deliverable → Task** chain: it
could say "Task is implementable" but couldn't say "Scenarios produce
Deliverables, not Tasks directly." ADR-0026 introduces the per-type
schema with facets and `decomposition_guidance` to make hierarchical
intent machine-readable.

## The config (current)

```yaml
root_parent_types:
  - Objective
  - Key Result
  - Epic
  - Scenario
  - Deliverable

types:
  Objective:
    facets: [plannable]
    decomposition_guidance: |
      Decompose into Key Results. ...
    max_nesting_depth: 1

  Key Result:
    facets: [plannable]
    decomposition_guidance: |
      Decompose into Epics. ...
    max_nesting_depth: 1

  Epic:
    facets: [plannable]
    decomposition_guidance: |
      Decompose into Scenarios. ...
    max_nesting_depth: 1

  Scenario:
    facets: [plannable]
    decomposition_guidance: |
      Decompose into Deliverables. NEVER decompose a Scenario directly
      into Tasks — Deliverables always sit between. Do NOT use 'Feature'
      as the child type; CVAPI's convention is Deliverable.
    max_nesting_depth: 1

  Deliverable:
    facets: [plannable, implementable]
    decomposition_guidance: |
      Decompose into Tasks (the concrete implementation units).
      Implement directly only when the Deliverable is small enough to
      fit in a single PR (~500 LoC of net change).
    max_nesting_depth: 1

  Task:
    facets: [implementable, actionable]
    actionable_executor: requiem

  Bug:
    facets: [implementable, actionable]
    actionable_executor: requiem

  User Story:
    facets: [implementable, actionable]
    actionable_executor: requiem
```

The full file lives at `~/.config/requiem/cvapi-process.yaml` (with
header commentary preserved).

## What each facet does

| Facet | Effect on workflow |
|---|---|
| `plannable` | Item invokes the planner. Type appears in derived `decomposable_types`. |
| `implementable` | Item may be implemented directly. Type appears in derived `implementable_types` (unless also `plannable` — then it lives in `decomposable_types` and the planner is invoked, but a leaf verdict is honoured). |
| `actionable` | Marks the item as something the executor backend named in `actionable_executor` picks up. Today only `requiem` is implemented (the in-process fanout). |

## How Deliverable being bi-facet works (the escape hatch)

CVAPI's Deliverable carries both `plannable` AND `implementable`.
This means:

1. When a Deliverable is the work item, the planner IS invoked
   (because it has the `plannable` facet → tier_for_type returns
   `decomposable`).
2. The planner SEES the `decomposition_guidance` (the prompt tells
   it "Decompose into Tasks; implement directly only when the
   Deliverable is small enough to fit in a single PR").
3. The planner is FREE to return `decomposable=false`. The workflow
   accepts that as a leaf because the type has the `implementable`
   facet — no `config_requires_decomposition` violation.

This matches polyphony's Issue type and gives operators a clean way
to say "this tier is the planner's discretion."

## What's NOT yet enforced

`max_nesting_depth` is parsed and snapshotted, but
`branch_decomposable` does not yet consume it for enforcement
(ADR-0026 step 3 "deferred" note). The text in `decomposition_guidance`
already steers the LLM toward the right shape; the structural cap
becomes useful as a backstop only if guidance proves insufficient in
practice. When that happens, the field is already in the data model
ready for a follow-up commit to wire it through child_inputs.

## How to apply it

```bash
# Verify config loads:
python -c "
from requiem.process_config import load_process_config
cfg = load_process_config(r'$HOME/.config/requiem/cvapi-process.yaml')
print('decomposable:', sorted(cfg.decomposable_types))
print('implementable:', sorted(cfg.implementable_types))
"

# Use in a run:
requiem-end-to-end \
  --item <scenario_id> \
  --ado-repo microsoft/CloudVault/cloudvault-service-api \
  --base-branch main \
  --process-config $HOME/.config/requiem/cvapi-process.yaml \
  --commit
```
