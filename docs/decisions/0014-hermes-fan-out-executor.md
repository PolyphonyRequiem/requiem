# ADR 0014 — Hermes-backed external fan-out executor

**Status:** ACCEPTED
**Date:** 2026-06
**Author:** Recorded during the Requiem×Hermes integration session; design
pressure-tested by a rubber-duck critique that flagged the idempotency,
receipts, dispatch-race, and dry-run-honesty issues addressed below.
**Supersedes:** none
**Superseded by:** —
**Cross-cuts:** ADR-0013 (fan-out executor — the gap this fills), ADR-0006
(merge-group topology — branch shape Hermes workers own), ADR-0002 (toolbelt
client seam), ADR-0011/0012 (the plan→reality transitions that produce leaves).

---

## TL;DR

ADR-0013 recorded the fan-out executor as PROPOSED-**blocked**: recursive
planning produces a tree, but nothing dispatches the implementable leaves into
real implementation. Its blocker **B1** is fatal for the *in-process* design:
the kernel forwards only flat JSON kwargs to a dispatched sub-workflow, never
the live `provider`/`toolbelt`/`gate_handler`, so an in-process `implementation`
child falls back to a **fake** LLM + fake ADO/GitHub over real git — and *looks
successful*.

This ADR takes a different route. Instead of dispatching the leaf **in
process**, Requiem dispatches it to a **real external executor**: a
[Hermes](https://hermes-agent.nousresearch.com) **kanban** worker. Hermes is a
standalone agent runtime with a durable SQLite work-delivery board (atomically
claimed tasks, parent→child dependencies, workers in isolated git worktrees,
per-task skills/model, idempotency keys, run-outcome rows, an event stream,
isolated boards). Requiem creates one kanban task per implementable leaf and a
real Hermes worker delivers it.

This **sidesteps B1** (the executor brings its own real provider/toolbelt) but
explicitly does **not** unblock in-process sub-workflow seam propagation — that
remains future work. Naming matters: this is the *Hermes-backed external
fan-out executor*, not "ADR-0013 unblocked".

## Decision

Ship `requiem.workflows.kanban_executor` plus a `KanbanClient`
(`requiem.clients.kanban`, wrapping `hermes kanban … --json`) on the `Toolbelt`,
following the established per-tool typed-client seam (ADR-0002, mirroring
`TwigClient`). The workflow is:

```
preflight → resolve_leaves → dispatch_leaves → poll_kanban → aggregate → end/fail_end
```

The executor is a **consumer of a committed implementation plan**, not a
generic ADO tree-walker. Default (no `--live`) is a real-board **dry run**.

### Leaf resolution — type-agnostic, from the plan (not the ADO type)

`resolve_leaves` does **not** classify ADO work-item types. Which types are
plannable/implementable/actionable is a process-config concern (ADR-0010), and
hardcoding it (as `root_dispatch`'s `ROOT_PARENT_TYPES` still does) is the wart
that config is meant to kill. Instead, the **plan** is the source of truth: a
leaf is a node whose plan says `decomposable == False`.

`requiem.plan_tree.load_committed_leaves` implements ADR-0013's `load_committed`
contract: read the approved `<run>.plan.tree.json` + the `<run>.plan.committed.json`
manifest, enumerate `decomposable == False` nodes **depth-first** (recursively,
not just depth-1), carry each leaf's title/body/type from its **parent's
`proposals[i]`** (the leaf node itself doesn't hold them), and map each
synthetic id through the manifest `id_map` to its real ADO id. It fails closed
(`PlanArtifactError`) on a missing/malformed/unapproved/**dry-run** manifest, a
misaligned tree, an unmapped leaf, or duplicate real ids — a malformed plan
must never silently dispatch the wrong work. The earlier `twig.list_children`
depth-1 scan was wrong twice over (depth-1 only; would dispatch decomposable
Epics/Features as implementation) and has been removed.

### End-to-end driver — "run against any ADO work item"

`requiem.end_to_end` (`python -m requiem.end_to_end --item <id> --board <b>`)
is a thin operator command that runs the three workflows as **sequential
top-level engines** — planning → `commit_plan` → `kanban_executor` — threading
concrete artifact paths between them. Running them top-level (not as nested
in-process sub-workflows) keeps each stage on its own real provider/toolbelt,
so B1 never bites the driver either. Two branches:

* **Atomic root** — planning decides the root is itself a leaf
  (`decomposable == False`; it writes `.plan.md`, and `commit_plan` rejects a
  leaf). The driver dispatches the **root item itself** as the single
  implementable leaf. Without this, "any ADO work item" would be false for
  every atomic item.
* **Decomposable root** — the driver seeds children for real via `commit_plan`
  (a faithful fan-out needs real ADO ids, so never a dry-run manifest), then
  runs the executor artifact-driven over the tree + committed manifest.

`commit` (seed ADO children) and `live` (spawn workers) both default **off**: a
decomposable root without `--commit` stops at "planned only".

## How each ADR-0013 blocker is handled

* **B1 (fake-success):** Resolved by construction — the executor is an external
  real agent, not an in-process fake-provider sub-workflow.
* **B2 (success ≠ implemented):** A leaf is "delivered" only when its latest
  `task_runs.outcome == "completed"` **and** the worker recorded a `result`
  (a *receipt*). Weak completions (blocked/crashed/no-result) are surfaced to a
  human via `aggregate`, never silently marked success.
* **B3 (branch topology):** Hermes workers run in isolated worktrees and own
  their branch (`--workspace worktree --branch impl/<root>-<leaf>`), consistent
  with ADR-0006's leaf-branch shape.
* **B4 (fresh-run idempotency):** Each leaf's task carries
  `--idempotency-key requiem:{root}:{leaf}` — **stable work identity**, not the
  transient `run_id` — so a *fresh* Requiem run over the same plan reuses tasks
  rather than duplicating them.

## Safety rails (from the design critique)

1. **Two-phase dispatch.** Tasks are created *unassigned* (not claimable),
   dependencies are linked, and only then are tasks assigned/released. This
   removes the create→claim race a worker could otherwise win before the
   dependency edges land.
2. **Dry-run is a distinct, non-delivering outcome.** A dry run never reports
   "delivered"; its verdict card says "planned only — nothing delivered".
3. **No implicit fake fallback.** If no kanban client is on the toolbelt the
   workflow fails typed (`toolbelt.missing_client`). Fakes are only injected
   explicitly (the demo's in-process `SimKanbanClient`, the harness).
4. **Conservative error posture (Ravel's L-1).** Unknown CLI failures, board
   missing, task-not-found, and bad JSON map to `NeedsHuman`, never an
   auto-retry. Only SQLite-busy maps to `RetryableFailure`.
5. **Durable, interruptible polling.** `poll_kanban` returns
   `RetryableFailure(after=interval)` while leaves are non-terminal, so the
   engine self-loops with one durable event per poll and honours
   `INV-CANCEL-SHORT-CIRCUITS-RETRY` between polls; `retry_exhausted` routes to
   `aggregate`, which reports the still-running leaves as not-delivered.
6. **Provenance recorded.** Board slug, Hermes version, task ids, idempotency
   keys, branches, and dispatch mode are written into the Requiem event log so
   resume reconciles against the durable Hermes board.

## Consequences

* The parity tracker's "fan-out executor" row moves from *blocked-by-B1/B3* to
  *shipped (Hermes-backed)*, with the honest caveat that **in-process**
  sub-workflow real-seam propagation is still unaddressed (a separate
  foundational change ADR-0013 §B1 describes).
* Requiem now has **two durable logs** (its own event log + the Hermes kanban
  DB). The Hermes board is the source of truth for worker state; Requiem
  reconciles by re-reading it each poll and by stable idempotency key on
  resume. Requiem cancellation does **not** stop in-flight Hermes workers —
  resume adopts them (documented; a `cancel-on-cancel` policy is future work).
* A genuine live run requires a configured Hermes profile that can do the ADO
  work, the Hermes dispatcher/gateway running, and credentials — i.e. Daniel's
  environment. The wiring up to (and including) real task creation/linking on a
  real board is proven against the real binary in CI (hermes-gated tests);
  spawning live workers is the operator's `--live` action.

## Follow-ups (not in this slice)

* Richer receipts: verify the branch/PR exist at the remote and tests are green
  before counting a leaf delivered (currently: run outcome + worker `result`).
* `cancel-on-cancel`: Requiem cancel issues `hermes kanban` reclaim/cancel for
  outstanding tasks.
* Process-config loader (parity non-negotiable #1): replace `root_dispatch`'s
  hardcoded `ROOT_PARENT_TYPES` with a `process.yaml` type→facet map, so root
  classification is as type-agnostic as leaf resolution now is.
* Idempotency vs. re-planning: the `requiem:{root}:{leaf}` key reuses a task
  across same-plan reruns; if the plan changes while ids stay stable, reconcile
  on `plan_id`/manifest hash rather than reusing the stale task.
