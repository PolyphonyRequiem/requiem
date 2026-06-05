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

This closes the `root_dispatch` half of non-negotiable #1. `decomposable_types`
/ `implementable_types` are parsed and snapshotted but not yet consumed by
planning (planning decides decomposability per-node today, ADR-0010); wiring
those into planning's tier hints is a follow-up. This is **process-config-backed
root classification**, not "type-agnostic routing fully solved."

## Example `.requiem-config/process.yaml`

```yaml
# Work-item types whose children are still valid SDLC roots.
root_parent_types:
  - Epic
  - Feature
# Optional: collapse synonyms before routing.
type_aliases:
  Bug: Task
# Reserved (parsed + snapshotted; not yet consumed by planning).
decomposable_types: [Epic, Feature]
implementable_types: [Task, Bug, User Story]
```
