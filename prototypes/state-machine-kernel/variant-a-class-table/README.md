# Variant A — class-based nodes + explicit transition table

> **Shape:** each node is a Python class; transitions are a `(node_id,
> outcome_key) -> node_id` dict; the engine is a synchronous loop.

## Run

```pwsh
python -m pip install -r ..\requirements.txt
python demo.py
```

The demo runs 5 in-process scenarios plus a subprocess-driven
crash+resume scenario. Exit code 0 on success. Per-run event logs land
in `.runs/<run_id>.events.jsonl`.

## Files

| File | What it owns |
|---|---|
| `outcomes.py` | The discriminated-union outcome — **placeholder for Stravinsky's seam #1**. |
| `events.py` | The append-only event log — **placeholder for Brahms's seam #2**. |
| `nodes.py` | `Node`, `AgentStep`, `ScriptStep`, `HumanGate`, `Route`, `SubworkflowCall`, `Terminate`. |
| `workflow.py` | `Workflow` — nodes dict + transitions dict + `add` / `edge` / `route` author API. |
| `engine.py` | The kernel: `Engine.run` loop, reconstruction from event log, retry, cancel, sub-workflow. |
| `demo.py` | Eight required scenarios, end-to-end. |

## Authoring shape

```python
wf = Workflow("demo", start="ingest")
wf.add(AgentStep(node_id="ingest", body=ingest_fn))
wf.add(HumanGate(node_id="approve", prompt="ok?", options=["yes", "no"]))
wf.add(Route(node_id="branch", chooser=lambda ctx: ...))
wf.add(Terminate(node_id="done", disposition="completed"))
wf.edge("ingest", "success", "approve")
wf.edge("approve", "needs_human:yes", "branch")
wf.route("branch", "fast", "done")
```

## Invariants honoured

| Invariant | Where |
|---|---|
| `INV-RESTART` | `Engine._reconstruct` rebuilds `current_node`, `completed[]`, `attempt` from the event log; supports crash mid-node (re-execute), crash mid-gate (re-suspend), crash mid-retry (resume at recorded attempt). |
| `INV-DISCRIMINATED-OUTCOMES` | The whole engine branches on `outcome.kind`; payloads are never introspected for routing decisions. |
| `INV-CANCEL-SHORT-CIRCUITS-RETRY` | `_is_cancel_pending` is checked before each node and *between retry attempts*. A durable `cancel_received` event is honoured even across restarts. |
| `INV-NO-ENGINE-ABANDONMENT` | Retry exhaustion routes on `retry_exhausted`; the engine never picks `abandoned` on its own. If the author hasn't wired `retry_exhausted`, the run terminates `failed`, not `abandoned`. |
| `INV-EVENT-LOG-AUTHORITATIVE` | Every transition, every retry, every gate appears in the log. `Completed`/`Suspended`/`RunCancelled`/`RunFailed` are derived from the log on resume. |

## Strengths

- **Conductor-shaped.** Reads exactly like the YAML model — easy mental map for anyone migrating from polyphony.
- **Most readable engine.** `_loop` is one function; the retry / cancel / route / gate / terminate decision tree is in plain sight.
- **Best unit-testability of a single node.** `node.execute(ctx)` is a one-line test: build a `NodeContext`, call execute, assert on the returned `Outcome`. No engine, no I/O.
- **Explicit transitions are auditable.** The transition table is a single dict; you can print it, validate it, render it.

## Weaknesses

- **Boilerplate for branching.** `Route` nodes feel slightly bureaucratic versus an `if`/`elif` inline in async code.
- **No native parallelism story.** A `parallel` primitive would need a new node class and engine path (whereas in B it's `asyncio.gather`).
- **Closures over Python callables.** Workflows aren't directly serialisable — you can't write a workflow to JSON and re-load it (the bodies are callables). For the UI's topology view, you'd need a parallel data-model export.

## Constraints on adjacent seams

- **Stravinsky (outcome contract):** Engine assumes `outcome.kind` is a string and `outcome.value` (for Success) is a dict. If Stravinsky picks a different shape, expect ~30 lines of engine changes.
- **Brahms (event schema):** Engine writes 11 event types (listed in `events.EVENT_TYPES`). Brahms's schema must include all of these; adding new event types is a kernel concern, not a workflow concern.
- **Wagner (DSL):** This variant **is** the DSL — workflows are authored in Python via `Workflow.add`/`edge`. If Wagner picks a YAML/decorator DSL, this variant becomes its target: parse Wagner's DSL into `Workflow` + `Node` instances.
