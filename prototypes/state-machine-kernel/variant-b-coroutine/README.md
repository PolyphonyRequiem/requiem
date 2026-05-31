# Variant B — coroutine-based nodes

> **Shape:** each node is an `async def` returning an `Outcome`; the
> engine is an `async def` loop; sub-workflow calls are `await
> self.run(child, ...)`; cancellation is `asyncio.Event` + a caught
> `asyncio.CancelledError` boundary.

## Run

```pwsh
python -m pip install -r ..\requirements.txt
python demo.py
```

Same 8-scenario suite as variants A and C; same event log format.

## Files

| File | What it owns |
|---|---|
| `outcomes.py` | Discriminated outcomes (copy of variant A). |
| `events.py` | Event log (copy of variant A). |
| `engine.py` | The kernel: model, registry sugar (`agent`/`human_gate`/`route`/`subworkflow`/`terminate`), and `Engine.run` as an async loop. |
| `demo.py` | Scenarios driven by `asyncio.run`. |

## Authoring shape

```python
async def ingest(ctx): return Success(value={"text": "..."})
async def llm_step(ctx):
    text = ctx.completed["ingest"]["value"]["text"]
    out = await fake_llm(text)              # ← await inside a node body
    return Success(value={"llm_output": out})

wf = Workflow("demo", start="ingest")
wf.add(agent("ingest", ingest))
wf.add(agent("llm_step", llm_step, retry_max=2))
wf.add(human_gate("approve", "ok?", ["yes", "no"]))
wf.add(route("branch", chooser=lambda c: "fast"))
wf.add(terminate("done", "completed"))
wf.edge("ingest", "success", "llm_step")
...
```

## Invariants honoured

Same as variant A. The crash+resume scenario uses the same on-disk
event log; `_reconstruct` is line-for-line equivalent. The only
material runtime difference is the use of `asyncio.Event` for the
in-process cancel flag and `asyncio.CancelledError` as a catchable
boundary inside node bodies.

## Strengths

- **Suspension feels native** even though we don't use it for gates (gates still go through the engine's Suspended sentinel). Inside a node body, `await` for I/O is the natural pattern — no thread pools, no callbacks.
- **Sub-workflow composition is trivial.** `await self.run(child, ...)` is just a function call; no special engine path is needed beyond the wrapping events.
- **Parallel composition is free.** A future `parallel_fork` node could just do `asyncio.gather(*[self.run(child, ...) for child in shards])` — nothing in the kernel needs to change.
- **Real-world I/O fits naturally.** `httpx.AsyncClient`, `aiofiles`, async ADO/GH clients all slot in without a sync→async shim.

## Weaknesses

- **The "async-ness" of nodes is largely cosmetic** at the kernel level. Each node still runs to completion returning an `Outcome`; the engine still drives one node at a time. The big win is *inside* node bodies, not in the engine.
- **Cancellation is harder to reason about.** `asyncio.CancelledError` can fire at any `await` point inside a node, so verb authors have to think about partial-state cleanup. INV-NO-CORRUPT-FORWARD is the safety net, but it shifts cognitive load to verb authors.
- **Less serialisable.** Same as variant A — workflows are Python objects with closures.
- **Slightly harder to unit-test a single node** than variant A (you need `asyncio.run` in every test), though `pytest-asyncio` makes it tolerable.

## Constraints on adjacent seams

- **Stravinsky:** outcome contract unchanged. The kernel does add one
  expectation: `asyncio.CancelledError` thrown out of a verb is
  converted to `Cancelled(reason="task cancelled")`. Stravinsky's
  shape needs to admit that conversion.
- **Brahms:** event format unchanged from variant A.
- **Wagner:** if Wagner authors workflows in Python, the coroutine
  ergonomics are pleasant. If Wagner picks YAML, this variant gains
  no advantage over A — the YAML→model layer destroys async ergonomics
  for the author.
