# ADR 0005 — Sub-workflow Invocation Primitive

**Status:** Accepted (Berlioz Phase B seat, PR pending)
**Date:** 2026-06-04
**Supersedes:** none
**Superseded by:** —

## Context

Phase A delivered the kernel with five node kinds (`script`, `agent`,
`team`, `human_gate`, `terminate`). Phase B added the real toolbelt
clients and CLI polish. The next-up planning seat (Faure) needs
**recursive planning** — a planning workflow that, when it identifies a
sub-plan, dispatches another planning workflow without the parent
hand-rolling YAML-style "inline subgraph" tricks. The kernel needs a
sub-workflow node kind.

The Brahms-harness PR #6 surfaced a related concern as an "open invariant
candidate" in ADR 0002: *"Sub-workflows must filter `last_completed_node()`
by `run_id`"*. Now that sub-workflows exist as a primitive, that candidate
becomes a hard invariant — INV-SUBWORKFLOW-LOG-ISOLATION.

## Decision

Add a single new node kind, `SubWorkflowNode`, that invokes another
workflow (by importable module path) as a node within the parent. The
child runs in its own `Engine` instance and writes to its own
`{sub_run_id}.events.jsonl` log file in the same `log_dir`. The parent's
log records only `subworkflow_started` / `subworkflow_completed` /
`subworkflow_cancelled` markers — enough to resume after a crash, and
nothing more.

### Surface

```python
WorkflowBuilder("parent")
    .entry("plan")
    .subworkflow(
        "plan",
        workflow="requiem.workflows.planning",
        inputs_verb="planning_inputs",   # optional
    )
    .edge("plan", on="success",            to="implement")
    .edge("plan", on="permanent_failure",  to="surrender")
    .edge("plan", on="needs_human",        to="…")
    .edge("plan", on="cancelled",          to="…")
    ...
```

Standard edge keys — no new routing path. Child outcomes map to parent
outcomes:

| Child `RunResult`            | Parent outcome                                    |
|------------------------------|---------------------------------------------------|
| `Completed("completed")`     | `Success(value=child_projection)`                 |
| `Completed("failed")`        | `PermanentFailure("subworkflow.failed", …)`       |
| `Completed("cancelled")`     | `Cancelled(cause="operator", …)`                  |
| `Suspended(…)`               | `NeedsHuman(prompt, options, …)` — *bubbled up*   |
| `Failed("cancelled", …)`     | `Cancelled(cause="operator", …)`                  |
| `Failed(other, …)`           | `PermanentFailure("subworkflow.<kind>", …)`       |

### Three new event kinds

* `subworkflow_started` — parent emits; payload `{sub_run_id,
  sub_workflow_module, inputs_summary}`.
* `subworkflow_completed` — parent emits; payload `{sub_run_id,
  disposition, outcome, outcome_summary}`. **The full outcome dict is
  stored on the event** so a crash between this event and the parent's
  next route step resumes without re-invoking the (now-finished) child
  engine.
* `subworkflow_cancelled` — parent emits when propagating a cancel into
  a sub-workflow (the child also gets a `cancel_requested` written to
  its log).

### Resume protocol

`_reconstruct` adds two arms:

* `subworkflow_started` → cursor becomes `_AwaitingSubworkflow(node_id,
  sub_run_id, module, attempt)`. On resume, the kernel re-attaches by
  calling `child_engine.run(sub_run_id)` — the child does its own
  `_reconstruct` over its own log per INV-RESTART. **No second
  `subworkflow_started` event is emitted on resume.**
* `subworkflow_completed` → cursor jumps straight to
  `_AwaitingRoute(node_id, outcome, attempt)`. The child engine is not
  re-invoked; the route is taken from the stored outcome.

### The invariant: INV-SUBWORKFLOW-LOG-ISOLATION

`_reconstruct` now takes a `run_id` parameter; events whose envelope
`run_id` does not match are skipped. This means that even in the
defensive case where a child workflow's events somehow bled into the
parent's log file, the parent's cursor would not be advanced by them.

Test pinning the invariant: `tests/test_subworkflow.py::
test_reconstruct_filters_foreign_run_id_events`.

This is the Brahms-harness PR #6 finding promoted to law.

### Cancel propagation (INV-CANCEL bridges layers)

When a parent's `_pending_cancel` check fires (either at top-of-`run`
or in-loop) and the cursor is at a `SubWorkflowNode`, the kernel:

1. Writes a `cancel_requested` event into the child's log (with
   `requested_by="parent"`) — idempotent, so it's safe even if the
   child already cancelled.
2. Emits `subworkflow_cancelled` in the parent's log.
3. Emits `run_completed("cancelled")` and returns `Failed`.

Tests: `test_cancel_propagates_to_child` (cancel before child starts)
and `test_cancel_during_subworkflow_node_propagates_marker` (cancel
intercepts at the node).

### Path safety

Default `sub_run_id` is `f"{parent_run_id}__{node_id}"` — double
underscore, NOT `::`. Windows paths cannot contain `::`; Beethoven's
Phase A notes flagged this.

Three-level nesting yields `g`, `g__p`, `g__p__c` — all distinct log
files in the same directory.

## Rationale

* **Author surface stays tiny** — one new builder method, four standard
  edge keys, the same routing model as every other node. The author
  does not learn a new vocabulary to compose workflows.
* **Re-entrancy is free** — the child engine already honours
  INV-RESTART. The parent re-invokes `child_engine.run(sub_run_id)`
  and the child resumes itself. The parent does not need to know what
  step the child was at.
* **Log purity** — each engine owns its own log file. There is one
  source of truth per engine; there is no need to interleave events
  from different runs into one file and then re-separate them on read.
* **Cancel composability** — a cancel at any layer propagates downward
  by writing into the next layer's log. The next-layer engine sees
  the cancel through its own `_pending_cancel` check. No cross-layer
  process-signal machinery is required.

## Variants rejected

* **In-process subgraph (no child Engine).** A `SubWorkflowNode` whose
  child runs in the parent's own loop, by merging the child's nodes
  into the parent's node-map at runtime. Rejected: the child's events
  and the parent's events would share an `event_id` space and a log
  file, breaking INV-SUBWORKFLOW-LOG-ISOLATION by construction and
  re-introducing the "filter by run_id" hazard. The whole point of
  this primitive is the separation.
* **Cross-process sub-workflows.** Out of scope for v0
  (INV-SINGLE-PROCESS). A future ADR could lift this restriction with
  an explicit out-of-process child engine — but everything we need for
  recursive planning fits in a single process.
* **Streamed sub-workflow output.** Parent receives the full child
  projection at child completion; we do not stream intermediate events
  up. The child's log is the source of truth for child detail — any
  observer wanting live narration tails the child's `.events.jsonl`.
* **Recursive-workflow detection.** v0 trusts the author not to
  configure a self-referential cycle. The foot-gun is documented in
  the cookbook; v1 may add a depth limit or cycle detector if it bites.

## Consequences

### Positive

* Faure's recursive-planning seat is unblocked: a planning workflow can
  invoke itself (or a peer planning workflow) as a node.
* The kernel's per-node dispatch arm is a four-line `isinstance` check;
  the heavy lifting is in `_run_subworkflow` (helper) and
  `_AwaitingSubworkflow` (cursor variant).
* The renderer-registry exhaustiveness test catches any future event
  kind added without a renderer — no special exemption for sub-workflow
  events.

### Negative / load-bearing follow-ups

* **Inputs handling is best-effort for v0.** ~~`inputs_verb` returns a
  dict that is recorded in the event payload, but the child's
  `build_engine` is invoked with the standard `(log_dir, …)` signature.
  Authors who need parameterised child workflows are responsible for
  threading inputs via the toolbelt or filesystem.~~ **Resolved
  2026-06-XX (Fauré seat 2):** the kernel now reads
  `subworkflow_started.inputs_summary` from the parent's log and
  forwards values to the child's `build_engine` as kwargs, filtered by
  `inspect.signature` so factories that don't accept them are
  unaffected. Reading from the log (not from in-memory cursor state)
  means resume after a crash recovers the same inputs the original
  invocation used — INV-EVENT-LOG-AUTHORITATIVE applies.
* **Mid-flight cancel is honoured between loop iterations, not inside
  `await child_engine.run()`.** If a cancel arrives while the parent
  is awaiting the child, the parent waits for the child to finish
  before honouring it. For v0 demo-scale runs this is acceptable; a
  future ADR could introduce a cooperative cancel `asyncio.Event`
  shared across the layers.
* **Renderer suppresses the child's narration in the parent's CLI
  stream.** The parent's `on_event` observer only fires for parent
  events. Operators wanting the full child story should tail the
  child's `.events.jsonl` (or post-hoc `requiem events <sub_run_id>`).

## References

* PR pending (this seat)
* ADR 0001 — single-process architecture (INV-SINGLE-PROCESS)
* ADR 0002 §"Open invariant candidates" — the candidate this ADR strikes
* Brahms-harness PR #6 — original surface of the run_id filter finding
* `docs/north-star.md` — INV-RESTART, INV-EVENT-LOG-AUTHORITATIVE,
  INV-SUBWORKFLOW-LOG-ISOLATION (added by this ADR)
* `tests/test_subworkflow.py` — the ten scenarios pinning the primitive
