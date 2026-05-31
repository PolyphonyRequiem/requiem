# Seam #3 — State Machine Kernel

**Seam owner:** Beethoven
**Phase:** A (seam-shaping)
**Branch:** `seam/state-machine-kernel`
**Status:** 3 runnable variants; recommendation = **Variant C with
Variant A's authoring sugar layered on top**.

---

## What this seam is

The state-machine kernel is Requiem's *engine*: the primitives that
turn a workflow definition into a run, drive nodes to completion,
suspend on human gates, retry on transient failures, short-circuit
on cancel, and recursively invoke sub-workflows. Everything else
in Requiem either *defines* primitives the kernel consumes (Wagner's
DSL, Stravinsky's outcome contract) or *projects* what the kernel
emits (Brahms's event schema, the UI, the harness, the reconcile
verb).

The kernel **owns** these primitives:

- `Node` — kinds: `agent`, `script`, `human_gate`, `route`,
  `subworkflow`, `terminate`.
- `Transition` — `(node_id, outcome_key) -> node_id`.
- `Outcome` — discriminated over `Success | RetryableFailure |
  PermanentFailure | NeedsHuman | Cancelled` (stubbed here; real
  contract is Stravinsky's #1).
- The **run loop**: enter → execute → emit outcome → route → repeat
  until a terminal.
- **Suspension** on a human gate (cooperatively pause; surface a
  `Suspended` result; await `resolve_gate`).
- **Resume** from event log without re-executing already-completed
  nodes (INV-RESTART).
- **Retry budget** per node with cancel short-circuit
  (INV-CANCEL-SHORT-CIRCUITS-RETRY).
- **Sub-workflow invocation** with a clearly-bounded event log
  segment.
- Terminal disposition tagging: `completed | abandoned | superseded
  | failed | cancelled`.

The kernel **does NOT own**: outcome payload shape (Stravinsky),
event JSON schema beyond the closed type list (Brahms), workflow
authoring syntax (Wagner), agent invocation (Stravinsky), persistence
beyond the event log (Brahms).

## Which invariants this seam serves

| Invariant | How the kernel honours it |
|---|---|
| **INV-RESTART** | `_reconstruct` rebuilds `current_node`, `completed[]`, `attempt` from the event log. Crash mid-node → re-execute (idempotency contract). Crash mid-gate → re-suspend. Crash mid-retry → resume at recorded attempt. |
| **INV-NO-CORRUPT-FORWARD** | The kernel never auto-retries past a `PermanentFailure`; never auto-rolls a hallucination; never resumes past a `cancel_received` event into another attempt. Receipts and `verify-then-act` are workflow-author concerns *above* the kernel — but the kernel guarantees those concerns can be expressed. |
| **INV-EVENT-LOG-AUTHORITATIVE** | Every node entry, completion, route, retry, gate, sub-workflow boundary, cancel, and termination is logged. All `RunResult` values are derived from the log on resume — there is no parallel in-memory authoritative state. |
| **INV-DISCRIMINATED-OUTCOMES** | The engine branches on `outcome.kind` only. It does not introspect `outcome.value`, `outcome.reason`, etc. for routing decisions. |
| **INV-CANCEL-SHORT-CIRCUITS-RETRY** | `_is_cancel_pending` is checked at three points: before entering a node, before deciding to retry, and during sub-workflow result handling. A durable `cancel_received` event short-circuits a fresh `run()` call before any node executes. |
| **INV-NO-ENGINE-ABANDONMENT** | Retry exhaustion routes on `retry_exhausted` (a *workflow-author* concern). If the author hasn't wired it, the run terminates `failed`, never `abandoned`. The engine treats `abandoned`, `superseded`, and `cancelled` as terminals chosen by *workflows or operators*, never by itself. |
| **INV-SINGLE-PROCESS** | Sub-workflow invocation is a recursive `engine.run` call — same process, same memory, same event-log directory. No IPC. |

## How to run the demos

Each variant ships a runnable `demo.py` that exercises **all 8
required scenarios** end-to-end and asserts on the result:

1. tiny workflow (start → agent → gate → route → end)
2. retry budget (2 fails then success)
3. cancel short-circuits retry
4. sub-workflow invocation
5. crash mid-run + resume from event log
6. (variant C only) static JSON round-trip + topology validation
7. human-gate suspension with `resolve_gate`-driven resume
8. discriminated-outcome routing on `success`, `success:<label>`,
   `permanent_failure[:<error_kind>]`, `retry_exhausted`,
   `needs_human:<choice>`

```pwsh
# from any variant directory
python -m pip install -r ..\requirements.txt
python demo.py
```

Each run drops per-run event logs in `.runs/<run_id>.events.jsonl`.
The crash+resume scenario uses a subprocess that calls `os._exit(99)`
mid-node to prove the on-disk log alone is sufficient for INV-RESTART.

## Variant comparison

| Axis | Variant A (class+table) | Variant B (coroutine) | Variant C (data-driven) |
|---|---|---|---|
| **Workflow-author ergonomics** | Cleanest for sync verbs; class-per-node is verbose for trivial nodes | Async-native; lovely if your verbs do I/O; clunky if they're pure compute | Most verbose by line-count; most explicit; most JSON-shaped |
| **Engine readability** | Best — `_loop` is one synchronous switch | Good but `await` boundaries multiply | Good — interpreter pattern is well-known |
| **Suspension/resume mechanics** | Synchronous `Suspended` return; resume via log replay; trivial to reason about | Same return pattern; `await` complicates nothing because gates are still engine-bounded | Same as A; cleanest reconstruction because state is all data |
| **Sub-workflow composability** | `SubworkflowCall` node + recursive engine call | `await self.run(child, ...)` — natural | Recursive interpreter call; sub-run id is parent-derived |
| **Cancellation cleanliness** | Cooperative flag + durable event; no surprises | `asyncio.CancelledError` can fire anywhere → verb authors must handle partial-state cleanup | Cooperative flag + durable event; same as A |
| **Testability (unit test a node in isolation)** | Best — call `node.execute(ctx)` and assert | Good but every test is `asyncio.run` | Good — call the registered verb directly with a `VerbContext` |
| **Introspection (UI renders topology without running)** | Hard — workflow holds closures; need a parallel data export | Hard — same as A | **Trivial** — `wf.model_dump_json()` gives the whole graph |
| **Retry + gate semantics** | Identical across all three | Identical | Identical |
| **Lines of engine code** | ~430 | ~330 | ~310 (model.py) + ~310 (engine.py) |
| **Lines of authoring boilerplate (5-node demo)** | ~25 | ~30 | ~50 |
| **Static analysis** | Possible but ad-hoc | Possible but ad-hoc | **First-class** — `validate_topology()` is 20 LOC and catches real bugs |
| **Adding a new node kind** | Subclass `Node`, add a branch in `_loop` | Add a `kind=` string, add a branch in `_loop` | Add a pydantic class, register in the `Union`, add a branch in `_execute` |
| **Composition with the harness** | Inject fakes via `body=` callables | Inject fakes via `fn=` async callables | Swap the `VerbRegistry` wholesale — cleanest test seam |

## Recommendation

**Adopt Variant C as the kernel; layer Variant A's authoring sugar on
top.**

The hybrid is two parts:

1. **Runtime kernel:** Variant C — `WorkflowModel` is the
   in-memory representation the engine interprets. This decouples
   *what a workflow is* from *how it's authored*, gives the UI a
   topology to render without any execution, and lets the harness
   swap verbs with a one-line `VerbRegistry` replacement. It also
   sets up the cleanest landing zone for Wagner's DSL seam
   (whatever the DSL ends up being — Python fluent, YAML+schema,
   decorators — it compiles to a `WorkflowModel`).

2. **Authoring sugar:** Variant A — a thin Python fluent builder
   (`wf.add(...)`, `wf.edge(...)`, `wf.route(...)`) that produces
   a `WorkflowModel`. Authors who want to write workflows in Python
   get the lighter syntax. Authors writing YAML get the JSON shape
   directly. Both end up at the same kernel.

Variant B's async-inside-verbs is valuable but **belongs in the verb
library**, not the kernel. A verb can be implemented async and
exposed through the registry via a sync→async shim
(`asyncio.run(...)`) or a richer dual-shaped registry. The kernel
itself doesn't need to be async to let verbs be async; pushing
async into the kernel forces every test to be async and every
non-async verb to live behind a wrapper.

**Why not pure Variant A?** Because workflows-as-data is too
valuable to give up. The UI's topology view, the harness's
scenario assertions, the migration story from conductor YAML, and
the static-analysis story all benefit from a serialisable
`WorkflowModel`. The fluent builder is a 5-minute conversion away;
the data model is fundamental.

**Why not pure Variant B?** Because the cognitive load of "any
`await` can be cancelled and might leave you mid-mutation" pushes
INV-NO-CORRUPT-FORWARD discipline onto every verb author. The
kernel should make the failure-safety story easier, not harder.
Async-in-verbs gives us most of B's wins without the cost.

**Why not pure Variant C?** Because the boilerplate is real
(2×–2.5× lines for a 5-node workflow). The fluent builder from A
is essentially free to add.

## Open questions for Daniel

These are the seam-shaping decisions the kernel can't make alone.
Defaults shown; absent input we proceed on default.

1. **Q-K1: Sub-workflow event log split.** Do child runs get their
   own `.events.jsonl` file (current prototype behaviour, easy
   replay isolation) or are they interleaved in the parent's log
   with a `scope:[parent_node]` field (already in the schema, just
   unused)? **Default: child gets its own file** *and* parent
   records `subworkflow_started` / `subworkflow_completed` with the
   child run id, so the UI can show either view. The recursive
   `polyphony` workflow can spawn dozens of sub-runs; separate
   files keep individual logs small and grep-able.

2. **Q-K2: Per-node retry budget vs. workflow-default retry
   budget.** The prototype puts `retry_max` on each node. Should
   there also be a workflow-level default? **Default: yes —
   workflow-level default of 2 (per the deep-dive R3.1), per-node
   override.** Lint warning if `provider.max_attempts × (1 +
   retry_max) > 6`.

3. **Q-K3: Cancel granularity.** Today cancel is run-scoped. Do
   we want to support cancelling a specific sub-workflow without
   killing the parent? **Default: no for v0.** Single-operator
   target; complicates reasoning. Re-evaluate post-v0.

4. **Q-K4: Gate timeout.** A workflow suspended on a gate can
   sit there forever. Do we want an engine-level "this gate has
   been pending for N hours" notion? **Default: no engine-level
   timeout.** This is a *domain signal* concern (a `surface_aged`
   notification fired by the channel layer), not a kernel concern.
   Honours INV-NO-ENGINE-ABANDONMENT.

5. **Q-K5: Crash-during-event-write recovery.** If the process
   crashes between `node_completed` being written and `route_taken`
   being written, `_reconstruct` re-takes the route — correct.
   What if it crashes *between* `node_entered` being written and
   the verb starting? `_reconstruct` re-executes the node — also
   correct. What if it crashes *during* a `node_completed` write
   such that the JSONL line is malformed (partial write)?
   **Default proposal:** event log readers must tolerate a
   malformed *last line only* — drop and proceed. Brahms's seam
   should formalise this.

6. **Q-K6: Reconstruction performance.** Today every `engine.run`
   reads the full event log. For a long-running root run with
   thousands of events, this is O(n) on every resume. Do we want
   a periodic *checkpoint event* that snapshots `completed[]`?
   **Default: no for v0.** Even 100 events × 100 resumes is fast.
   Re-evaluate if dogfood shows it. (See Bach's deep-dive note on
   journal-as-source-of-truth.)

7. **Q-K7: Parallel composition.** Polyphony has parallel
   batch-dispatch in `polyphony.yaml`. None of the three variants
   ship a `parallel_fork` primitive. Should the kernel ship one,
   or should it be a workflow-author pattern (sub-workflow per
   shard, wait on all)? **Default: kernel-shipped primitive
   (`parallel_fork` node + `join`).** Eight LOC in variant B,
   ~30 in variants A and C. Drops the 60-line PowerShell
   aggregator the deep-dive flagged.

8. **Q-K8: Workflow versioning.** A run launched against
   `polyphony@v1` workflow shouldn't suddenly behave like `v2` if
   the workflow definition changes mid-run. The prototype is
   silent on this. **Default proposal:** `WorkflowModel` includes a
   `version` field; `workflow_started` event records it; resume
   refuses if the registered version doesn't match the logged
   version. Wagner's seam can co-own this.

## Constraints on adjacent seams

### From Stravinsky (#1 — outcome contract)

The kernel assumes:

- An `Outcome` exposes a string `kind` field that is one of:
  `success`, `retryable_failure`, `permanent_failure`,
  `needs_human`, `cancelled`. Stravinsky may change names but
  must preserve the closed five-element set.
- `RetryableFailure` and `PermanentFailure` expose an `error_kind`
  string. The kernel uses `permanent_failure:<error_kind>` as a
  routing key, so `error_kind` is part of the *public* contract,
  not just diagnostics.
- `NeedsHuman` exposes `prompt: str` and `options: list[str]`.
  The kernel logs both and uses option labels for routing
  (`needs_human:<choice>`).
- `Success` exposes a `value: dict[str, Any]`. For `Route` nodes
  the kernel reads `value["route"]` and forms `success:<label>`.
- Outcomes are pydantic models with `.model_dump()` — the event
  log stores the dict.

### From Brahms (#2 — event schema)

The kernel writes 11 distinct event types (see
`events.EVENT_TYPES`). Brahms's schema must include all of them
with at minimum these fields:

- All events: `event_id` (monotone int), `ts` (ISO-8601 UTC),
  `type`, `run_id`, `scope` (list of parent node ids for
  sub-workflows; `[]` at root).
- `workflow_started`: `workflow`, `inputs`.
- `node_entered`: `node_id`, `attempt`.
- `node_completed`: `node_id`, `outcome` (the full dumped Outcome).
- `route_taken`: `from_node`, `key`, `to_node`.
- `retry_attempted`: `node_id`, `attempt`, `next_attempt`,
  `retry_max`, `reason`, `error_kind`.
- `human_gate_presented`: `node_id`, `prompt`, `options`.
- `human_gate_resolved`: `choice`.
- `subworkflow_started` / `_completed`: `parent_node`,
  `child_workflow`, `child_run_id`, `result` (on completed).
- `cancel_received`: `reason`.
- `workflow_terminated`: `node_id`, `disposition`, `reason`,
  `error_kind`.

The kernel writes the log with `fsync` after every append. The
read API is full replay (no random access yet). Brahms is free
to add indexes, fanout, SSE projections, etc., as long as the
append/replay contract is preserved.

### From Wagner (#7 — DSL)

The kernel doesn't care which surface Wagner picks — Python
fluent builder (variant A), `async def` decorators (variant B),
YAML schema, typed dicts — *provided the surface compiles to a
`WorkflowModel`-equivalent* (the recommendation above). Wagner's
choices the kernel needs:

- A node has a `kind` discriminator and a `node_id` string. Both
  are first-class.
- An edge has `from_node`, `outcome_key`, `to_node`. The key
  language is closed: `<kind>` or `<kind>:<label>`.
- Verbs are addressable by string name (the `VerbRegistry`
  pattern). Wagner can sugar this with type-checked references,
  but the underlying identity is a name.

### From the harness (#9)

The harness can drive the kernel directly in-process — no
subprocess needed. The fake-LLM and fake-script seams are *verb*
concerns: swap the `VerbRegistry` (variant C) or pass different
callables to `wf.add(...)` (variants A/B). The kernel itself
needs no harness-specific knobs. The `.events.jsonl` log is the
harness's assertion substrate.

## Things this seam deliberately leaves OPEN

- **Time, sleep, delays.** No `wait` primitive. If a workflow needs to
  poll, it's a verb that returns `RetryableFailure` and the route
  loops back to itself with a retry budget. The kernel does not
  embed a scheduler.
- **Workflow timeouts.** No engine-level "this run has been going for
  N hours, terminate it." Surfaces as a domain signal, not a kernel
  decision (INV-NO-ENGINE-ABANDONMENT).
- **Persistence of in-flight state beyond the event log.** No
  durable retry-counter, no checkpoint-on-suspend, no
  manifest-as-state. The event log alone is sufficient for
  reconstruction (see deep-dive §3.4 resolution).
- **Cross-run cancellation cascades.** A cancel on a parent does
  not auto-cancel children. Workflows that want this behaviour
  invoke children with explicit cancel propagation in a route.

## Files in this prototype tree

```
prototypes/state-machine-kernel/
├── README.md                     # this document
├── requirements.txt              # pydantic >= 2
├── variant-a-class-table/
│   ├── README.md                 # variant write-up
│   ├── demo.py                   # runnable, asserts on all 8 scenarios
│   ├── outcomes.py
│   ├── events.py
│   ├── nodes.py
│   ├── workflow.py
│   └── engine.py
├── variant-b-coroutine/
│   ├── README.md
│   ├── demo.py
│   ├── outcomes.py
│   ├── events.py
│   └── engine.py
└── variant-c-data-driven/
    ├── README.md
    ├── demo.py
    ├── outcomes.py
    ├── events.py
    ├── model.py
    └── engine.py
```

Run each variant's `demo.py` to see scenario output and event log
projections.
