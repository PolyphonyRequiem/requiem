# ADR 0015 — Process config as the type-agnostic routing seam

**Status:** ACCEPTED
**Date:** 2026-06
**Author:** Recorded during the Requiem parity push; design pressure-tested by a
rubber-duck critique that flagged the resume-fidelity footgun addressed below.
**Supersedes:** none
**Superseded by:** —
**Cross-cuts:** §9 non-negotiable #1 (type-agnostic routing from process
config), ADR-0010 (discovered-work ledger — where type→facet mapping belongs),
root_dispatch (`validate_root`).

---

## TL;DR

Polyphony's tier model is **data, not code**: which work-item types sit above
the implementable tier — and therefore still qualify a child as a dispatchable
SDLC root — lived in a per-repo process config, not in the engine. Requiem had
regressed on this: `root_dispatch.validate_root` hardcoded
`ROOT_PARENT_TYPES = {"Epic", "Feature"}`. This ADR records the
`requiem.process_config` seam that restores type-agnosticism: a per-repo
`.requiem-config/process.yaml` loaded into a frozen `ProcessConfig` that
workflows consult instead of baking ADO type literals into their verbs.

## Decision

- **`requiem.process_config`** exposes a frozen `ProcessConfig`
  (`root_parent_types` consumed today; `type_aliases`, `decomposable_types`,
  `implementable_types` reserved so the on-disk schema can grow without a
  breaking migration), plus `load_process_config`, `discover_process_config`,
  `default_process_config`, and `resolve_process_config`.
- **Resolution order is deterministic:** explicit `ProcessConfig` (tests /
  programmatic callers) → `.requiem-config/process.yaml` discovered by walking
  up from the run's `repo_path` → polyphony-equivalent `Epic`/`Feature`
  defaults. A repo with no config file behaves exactly as before.
- **Discovery is anchored to `repo_path`, never ambient cwd**, so a run cannot
  silently pick up an unrelated repo's config.
- **Fail closed, not silent:** a *missing* config is never an error; a
  *present-but-malformed* config raises `ProcessConfigError` rather than
  guessing (INV-NO-CORRUPT-FORWARD). An empty `root_parent_types` list falls
  back to defaults rather than routing every parented item to a human gate.

## Resume fidelity (the footgun)

Reading the config only at `build_engine` time would let a `process.yaml`
edited **between a crash and a resume** change a routing decision the run was
about to make. The fix: the effective config (with its `source` and a
`sha256`) is **snapshotted into the event log by the `start_run` verb**, and
`validate_root` consumes that durable snapshot — not ambient disk. Because the
kernel replays completed verbs from the log, a decision already recorded is
immune to later config edits, and a crash *before* `validate_root` re-resolves
deterministically from the snapshot `start_run` recorded first (INV-RESTART).

## Scope / not yet

This ADR's original scope closed the `root_dispatch` half of non-negotiable #1.
The planning half — consuming `decomposable_types` / `implementable_types` —
landed subsequently and is recorded in the addendum below.

## Addendum (planning tier enforcement)

`branch_decomposable` in `requiem.workflows.planning` now consults the process
config's tier sets via `ProcessConfig.tier_for_type(work_item_type)` and treats
them as **authoritative over the planner's own `decomposable` flag** — the tier
model is a `process.yaml` thing, not an LLM judgement call:

- **implementable type → forced leaf.** Even if the planner proposed a
  decomposition, the node is recorded as a leaf and the discarded child count is
  kept as a breadcrumb (`overrode_planner` / `discarded_child_count`). This is
  the *contracting* direction — we never fabricate work — so it applies silently
  rather than gating.
- **decomposable type the planner left as a leaf (or with zero children) →
  fail closed** to a new `type_policy_gate` (`config_requires_decomposition`);
  the operator proceeds (records needs-human) or aborts. We never fabricate
  children to satisfy the policy.
- **configured policy but no `work_item_type` to classify → fail closed**
  (`missing_work_item_type_for_policy`) rather than silently reverting to
  LLM-driven tiering.
- **empty tier sets (the default) → the planner's decision stands**, so every
  existing repo and test is behaviourally unchanged.
- The planner *prompt* is also seeded with the policy so a cooperating model
  complies by default; the gate is the deterministic backstop, not the
  first line of defence.

**Resume fidelity / propagation.** The effective config is snapshotted into
planning's `start_run` output (mirroring `validate_root`) and **threaded into
recursive child sub-workflow inputs** (`child_inputs`), so a child tiers with
the exact config the parent run started with. This is restart-safe by
construction: the config travels as recorded JSON inputs, never as ambient disk
state or a contextvar that a process restart would drop (INV-RESTART). The
record verb and the human-readable sidecar both reflect `branch_decomposable`'s
*effective* decision, never the raw planner flag, so the durable plan can't
contradict the routing that actually happened.

**Contradiction handling.** A type declared both decomposable and implementable
(directly or via aliases) is a contradictory policy and raises
`ProcessConfigError` at construction — covering YAML load, `from_snapshot`, and
direct construction (`__post_init__`), checked after alias normalization.

## Example `.requiem-config/process.yaml`

```yaml
# Work-item types whose children are still valid SDLC roots.
root_parent_types:
  - Epic
  - Feature
# Optional: collapse synonyms before routing.
type_aliases:
  Bug: Task
# Consumed by planning's branch_decomposable (tier enforcement).
decomposable_types: [Epic, Feature]
implementable_types: [Task, User Story]
```
