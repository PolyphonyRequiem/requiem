# Example: CloudVault Service API process config

This is the type policy I (Daniel) use when running requiem against
the `cloudvault-service-api` ADO repo. **It is not committed to that
repo** by design — see ADR-0025 §1 for the disposition reasoning.
This file is here so other operators can see the shape and adapt it
for their own per-machine config.

## Usage

Save this somewhere outside the target repo (e.g. `~/.config/requiem/cvapi-process.yaml`)
and pass it explicitly:

```bash
requiem-end-to-end \
  --item 62759077 \
  --board requiem-62759077-commit \
  --ado-repo microsoft/CloudVault/cloudvault-service-api \
  --process-config ~/.config/requiem/cvapi-process.yaml \
  --commit
```

The `--process-config <path>` flag (ADR-0025) overrides the default
walking-up discovery of `.requiem-config/process.yaml` from `--repo`.
A missing file or malformed YAML raises a clear error instead of
silently falling back to defaults.

## The config

```yaml
# Requiem process config for the CloudVault Service API repo.
#
# Requiem discovers this file by walking up from the run's repo_path; without
# it, requiem falls back to its built-in polyphony-equivalent defaults
# (root_parent_types=[Epic, Feature], no decomposable/implementable tier
# enforcement) — which lets the LLM planner recurse to arbitrary depth on
# any work-item type. That's wrong for CVAPI: our ADO hierarchy is
#
#   Objective → Key Result → Epic → Scenario → Task | Bug | User Story
#
# and our Tasks/Bugs/User Stories are always meant to be executable leaves,
# never decomposed further. Without this file the planner happily produced
# a 4-level recursive Task tree on a single Scenario (run #62759077, see
# the SKU-fallback dogfood notes from 2026-06-17), with the reviewer
# correctly escalating the depth-4 leaves as too vague.
#
# Reference: requiem ADR-0015 (process_config), src/requiem/process_config.py
# Reference: requiem ADR-0010 (planning tier model)

# Which parent-type roots qualify a work item as a dispatchable SDLC root.
# A Task whose parent is one of these is a valid root for requiem; a Task
# whose parent isn't on this list routes to a human gate. We list the full
# ADO hierarchy above Task because CVAPI dispatches deliverables under all
# of these levels in practice.
root_parent_types:
  - Objective
  - Key Result
  - Epic
  - Scenario
  - Feature

# Work-item types the planner MUST decompose (planner leaf → escalation_gate).
# These are the strategic/intermediate types; ALL of them should produce
# at least one child plan.
decomposable_types:
  - Objective
  - Key Result
  - Epic
  - Scenario
  - Feature

# Work-item types the planner MUST treat as implementable leaves regardless
# of the planner's `decomposable` flag. This is the structural cap on
# recursion — when the planner proposes children for a Scenario, those
# children become Tasks, and Task children of the recursion are then forced
# to leaves WITHOUT calling the LLM again (branch_decomposable short-circuit).
#
# Skipping the LLM call at the Task level is what prevents the propagation
# of vague depth-N+ planning that broke the SKU-fallback dogfood.
implementable_types:
  - Task
  - Bug
  - User Story
```
