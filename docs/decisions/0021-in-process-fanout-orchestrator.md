# ADR 0021 — In-process fan-out orchestrator (parity #4 / #5)

**Status:** Accepted + **implemented** (2026-06-09, `requiem.workflows.fanout`).
Sequential v0 shipped; parallel + worktree (#5) deferred as noted.
**Date:** 2026-06-09
**Relates to:** ADR-0013 (fan-out executor — B1/B2/B3, now all CLOSED),
ADR-0014 (external kanban executor — the *other* dispatch path), ADR-0005
(sub-workflow log isolation), ADR-0020 (child-seam propagation), ADR-0006
(branch topology), INV-RESTART, INV-SUBWORKFLOW-LOG-ISOLATION.
**Parity:** non-negotiable **#4** (tree-walking root SDLC orchestrator with batch
dispatch + iterate-until-stable) and a stepping stone toward **#5** (per-item
worktree isolation for *parallel* dispatch).

## Context

Parity #4 wants a root orchestrator that **walks the committed plan tree and
dispatches each implementable leaf into `implementation`**, then loops until the
tree is stable. The audit called the fan-out "the missing core" and the biggest
single parity gap.

Two things changed that make this buildable now:

1. **The ADR-0013 blockers are closed** (PRs #65/#66/#67). B1 (the child-seam)
   means a dispatched `implementation` child inherits the parent's real
   provider/toolbelt instead of silently faking. B3 means the leaf branch follows
   `impl/<root>-<item>`. B2 means a leaf that *surrenders* (red tests / bad coder)
   surfaces to the parent as `NeedsHuman` instead of a false success.
2. **There is already an external dispatch path** — `kanban_executor` (ADR-0014)
   dispatches each leaf to a separate Hermes worker process. That satisfies the
   *functional* fan-out for #4/#5 today. What's missing is the **in-process**
   tree-walking orchestrator that runs `implementation` as a sub-workflow in the
   single requiem process (INV-SINGLE-PROCESS), for environments without a
   Hermes kanban fleet.

### What the kernel does and does NOT give us

- The DSL `subworkflow` node is **static**: one fixed `workflow` module + one
  `sub_run_id` per node. There is no native "dispatch N dynamic children from one
  node" primitive, and `parallel_fork` is an *agent-team* primitive, not a
  sub-workflow fan-out.
- But `kanban_executor` already shows the idiom that fits: **a single script verb
  that resolves all leaves (`plan_tree.load_committed_leaves`) and processes them
  in a loop inside the verb.** A script verb can construct and `await` an
  `implementation` engine per leaf in-process (the seam from ADR-0020 makes that
  child inherit real seams).

## Decision (proposed)

Add `requiem.workflows.fanout` — an **in-process** orchestrator workflow that
dispatches implementable leaves into `implementation`, mirroring
`kanban_executor`'s shape but running the child engines in-process rather than
handing them to an external worker.

### 1. Shape (sequential first)

```
start
  → resolve_leaves   (script · plan_tree.load_committed_leaves → ResolvedLeaf[])
  → dispatch_leaves  (script · for each not-yet-done leaf: build + run an
                       implementation engine in-process; collect per-leaf outcome)
      ├─ all leaves landed a green PR        → end_success
      ├─ ≥1 leaf surrendered (NeedsHuman)    → end_needs_human
      └─ ≥1 leaf hard-failed                 → end_failed
```

`dispatch_leaves` runs leaves **sequentially** in v0 (matching the current
sub-workflow topology; `MAX_CHILDREN`/no-parallel-fork). Each leaf gets:

- an `ImplementationInputs` carrying `root=<plan root>` and `item_id=<leaf
  real_id>` (so B3 yields `impl/<root>-<leaf>`), the leaf's title/body as the
  plan, and the shared repo/base_branch.
- its own child run id `fanout-<root>__leaf-<real_id>` so the child writes its
  own `*.events.jsonl` (INV-SUBWORKFLOW-LOG-ISOLATION).

### 2. Iterate-until-stable + resume

- **Idempotent re-entry.** Before dispatching a leaf, `dispatch_leaves` checks
  whether that leaf's child run already reached a terminal disposition (read its
  log via `plan_tree`/`completed_from_log`). A re-run skips finished leaves —
  the "iterate until stable" loop is *resume-driven*, not a busy loop. This
  reuses the kernel's own log-authoritative resume rather than inventing a new
  loop primitive.
- **Outcome roll-up.** A leaf that lands a green PR (`implementation` →
  `end_handoff`/completed) counts done. A leaf that surrenders
  (`end_needs_human`, B2) routes the orchestrator to `end_needs_human` so a human
  resolves it before re-running. A hard failure → `end_failed`.

### 3. Seam + dry-run

- The orchestrator's own engine carries the real provider/toolbelt; the kernel's
  ADR-0020 seam means each in-process `implementation` child inherits them. Tests
  inject fakes exactly as the subworkflow tests do.
- `dry_run` threads into each leaf's `ImplementationInputs.dry_run` so a dry
  orchestrator opens no PRs and touches nothing outside `log_dir` (mirrors
  `implementation`'s and `full_sdlc`'s dry-run contract).

### 4. Relationship to the external executor

`fanout` (in-process) and `kanban_executor` (external) are **siblings**, not
replacements. The driver (`end_to_end`) picks one: a Hermes-fleet deployment uses
`kanban_executor`; a single-process deployment uses `fanout`. Both consume the
same `plan_tree.load_committed_leaves` enumeration and the same `branch_model`
topology, so they can't diverge on what a "leaf" or a "branch" is.

### 5. Explicitly deferred

- **Parallel dispatch + worktree isolation (#5).** v0 is sequential. True
  parallelism needs each leaf in its own git worktree (so concurrent children
  don't fight over the working tree) **and** the seam captured per-branch (a
  contextvar is per-task — ADR-0020 §Consequences already flagged this). That's
  a separate ADR once sequential fan-out is proven.
- **Wiring `fanout` into `end_to_end`'s CLI** as a selectable dispatch backend —
  a follow-up once the workflow itself is tested.

## Consequences

**Positive:** closes the "missing core" of #4 with an in-process tree-walking
dispatch that reuses the now-unblocked seam (B1), branch model (B3), and handoff
classifier (B2); mirrors the proven `kanban_executor` resolve-then-loop idiom so
the two dispatch paths stay consistent; resume-driven iterate-until-stable needs
no new loop primitive (the event log is authoritative); per-leaf child logs keep
INV-SUBWORKFLOW-LOG-ISOLATION.

**Negative / open:** sequential v0 leaves #5 (parallel + worktree) for a follow-up
— so this advances #4 toward parity but does not by itself close #5; running
`implementation` engines inside a script verb (rather than a DSL `subworkflow`
node) means the orchestrator's *own* log records the dispatch as script steps,
not `subworkflow_started`/`completed` markers — acceptable (each child still has
its own isolated log) but a deliberate divergence from the static-subworkflow
path worth noting; the iterate-until-stable loop relies on faithfully reading
each child's terminal disposition from its log, so the per-leaf run-id scheme
must be collision-free (the `real_id` uniqueness `load_committed_leaves` already
guarantees covers this).

**Why an ADR:** the choice to dispatch in a *script verb loop* (vs. extending the
DSL with a dynamic fan-out node), the sequential-first / parallel-deferred split,
and the resume-driven iterate-until-stable design are load-bearing and worth
review before code. This is the design-doc-first checkpoint, matching the rhythm
used for ADR-0020 (B1).
