# Seam — Run-event stream schema

> **Brahms (events hat), Phase A, seam #2.**
> Status: prototypes shipped; awaiting Daniel's direction on which to advance.

---

## What this seam is

Every meaningful thing that happens inside Requiem becomes a JSON line in a
per-run append-only `<run>.events.jsonl`. The engine emits, verbs emit,
agents emit, the UI tails. Per **INV-EVENT-LOG-AUTHORITATIVE** the durable
run state is reconstructible from this file alone — the manifest, the UI's
trace view, the harness's assertions, and `polyphony reconcile`-style
diagnostics are all *projections* of it.

The schema we choose here ripples into:

- **persistence** (Bach rebuilds run state from these events)
- **the discriminated outcome contract** (Stravinsky's `Outcome` rides in
  `VerbCompleted`)
- **the UI binding** (platespinner-style tailers; eventually Requiem's own
  trace view)
- **the harness** (scenario assertions key off event shape)
- **forward compatibility** (mixed-version cohorts during dogfood)
- **operator-grade debugging** (`jq` over the file at 2 a.m.)

The UI's design is on hold this round, which paradoxically makes evolvability
and self-description **more** important — we're locking the contract before
its main consumer is prototyped.

## Invariants served

| Invariant | How the seam serves it |
|---|---|
| `INV-EVENT-LOG-AUTHORITATIVE` | The file is the source of truth; every projection is `derive(read_all(path))`. The three demos prove this with a `RunState` rebuilt from the log alone. |
| `INV-RESTART` | The writer is append-only, line-terminated; a torn final write is detectable and never silently incorporated. The reader buffers partial lines and emits a `CorruptLine` rather than a half-event. |
| `INV-NO-CORRUPT-FORWARD` | All three readers distinguish `CorruptLine` from forward-compat unknown kinds; `derive` halts on corruption so the workflow surrenders to a human gate instead of advancing on partial state. |
| `INV-DISCRIMINATED-OUTCOMES` | The `VerbCompleted` event carries Stravinsky's union (`Success | RetryableFailure | PermanentFailure | NeedsHuman | Cancelled`). In variant A it is typed all the way through; in B/C the same shape lives inside the payload. |
| `INV-SINGLE-PROCESS` | One writer per run; no cross-process locking story is required. The thread lock in `EventWriter` is sufficient for the engine + verb library + UI-backend co-tenancy. |
| `INV-CANCEL-SHORT-CIRCUITS-RETRY` | Routing concern, not schema; but the discriminated outcome makes the kernel's decision mechanical (`if outcome.kind == "cancelled": break`). |
| `INV-NO-ENGINE-ABANDONMENT` | `RunCompleted.terminal ∈ {"completed", "surrendered", "superseded"}`. There is no `"abandoned"` terminal by design. |

## What ships in this PR

```
prototypes/run-event-stream/
├── README.md               (this file)
├── requirements.txt        (pydantic >=2.6,<3)
├── run_all.py              (runs all three demos in sequence; CI-friendly)
├── variant-a-typed-discriminated/
│   ├── README.md, demo.py, events.py, outcomes.py, writer.py, reader.py, state.py
├── variant-b-envelope-loose/
│   ├── README.md, demo.py, events.py, writer.py, reader.py, state.py
└── variant-c-cloudevents/
    ├── README.md, demo.py, events.py, writer.py, reader.py, state.py
```

Each variant's `demo.py` exercises **all six required behaviours**:

1. emits the six representative kinds (`RunStarted`, `NodeEntered`,
   `VerbInvoked`, `VerbCompleted`, `GateOpened`, `RunCompleted`)
2. appends to a real `.events.jsonl` with line-buffered, lock-protected,
   optional-fsync writes
3. tails the file from a second thread, yielding typed events as they
   arrive
4. derives a `RunState` from the log alone (proves
   `INV-EVENT-LOG-AUTHORITATIVE`)
5. evolves the schema with a v2-only `RetryAttempted` kind, then reads
   the same file with both v1 and v2 readers
6. injects a truncated JSON line; the derive function halts rather than
   silently skipping (proves `INV-NO-CORRUPT-FORWARD`)

Run any one variant:

```powershell
python -m venv .venv; .\.venv\Scripts\python -m pip install -r requirements.txt
cd variant-a-typed-discriminated; python demo.py
```

Or the whole set:

```powershell
python run_all.py
```

---

## Variant summary

| | A — typed discriminated | B — envelope-loose | C — CloudEvents 1.0 |
|---|---|---|---|
| **On-disk record** | one pydantic model per kind, unionized by `event_type` discriminator | `Event(kind: str, schema_version: int, payload: dict)` | CloudEvents 1.0 *Structured Mode JSON* envelope with typed `data` |
| **Top-level typing** | full | envelope only | envelope only |
| **Payload typing** | full (same model) | opt-in via registry | opt-in via registry |
| **Unknown-kind handling** | sentinel `UnknownEvent` after catching discriminator miss | natural — `payload: dict` round-trips | natural — `data: dict` round-trips |
| **Interop signal** | none (Requiem-native shape) | none (Requiem-native shape) | high — CE 1.0 is widely adopted |
| **Line size (median)** | ~170 B | ~190 B | ~330 B |
| **`jq` ergonomics** | `select(.event_type == "verb_completed").outcome.kind` | `select(.kind == "verb_completed").payload.outcome.kind` | `select(.type == "io.requiem.verb.completed").data.outcome.kind` |

### Comparison axes

| Axis | A — typed discriminated | B — envelope-loose | C — CloudEvents 1.0 |
|------|------------------------|--------------------|---------------------|
| **Schema evolvability — adding a kind** | medium: add model + extend union + bump `SCHEMA_VERSION`; old readers need a `try/except union_tag_invalid` path | **easy**: register payload model in the consumer that cares; everyone else just sees `payload: dict` | **easy**: register type string in the consumer that cares; CE envelope itself is unchanged |
| **Schema evolvability — adding a field within a kind** | breaking by default (`extra="forbid"`); ergonomic per-kind versioning (`node_entered_v2`) | additive under `extra="ignore"`; `schema_version` lives at top level (coarse) | additive under `extra="ignore"`; `dataschema` URI gives per-type version (fine) |
| **Reader robustness — unknown kinds** | needs explicit handling; risk of accidental hard-fail if the catch is missed | trivial; envelope decodes regardless | trivial; envelope decodes regardless |
| **Writer ergonomics** | **best**: type the engine emits, pydantic refuses an out-of-shape outcome at the call site | OK: producer must remember to wrap payload in a dict that matches an unwritten contract | OK + boilerplate: `source`, `id`, `time`, `dataschema` per event |
| **JSON readability (`jq` / Vim / tail -f)** | **best**: flat one-level record per event | good: two-level (`payload.*`) | noisy: CE fields dominate the line |
| **File size** | smallest | +15% vs A | **+105% vs A** for the same payload |
| **Tooling interop (jq, otel bridges, watchers)** | jq only | jq only | jq + cloudevents SDKs + Knative/otel receivers + Hermes-style notifiers |
| **Corruption surfacing** | uniform across variants: `CorruptLine` vs `UnknownEvent` distinction is explicit in all three readers | same | same |
| **Downstream UI binding flexibility** | strongest: UI can pattern-match on the typed model | strong: UI gets envelope free, opts into payload validation per panel | strong: UI gets a standard envelope — but pays for it on every line |
| **Mixed-version cohort tolerance** | poor: producer at vN+1 will trip vN readers unless they all hold the `UnknownEvent` sentinel | **best**: payload-as-dict is the entire forward-compat story | best (same reason as B) plus type-string is reverse-DNS namespaced |
| **Producer/consumer coupling** | tightest — one repository must own the union | loose — registry per consumer | loose + standardized envelope |

---

## Recommendation

**Advance variant B (envelope-loose), with a one-line graft from variant
A.** The graft: keep Stravinsky's `Outcome` union as a typed pydantic model
nested inside the `verb_completed` payload, so that the
`INV-DISCRIMINATED-OUTCOMES` contract is mechanically enforced at the call
site even though the surrounding envelope is loose.

### Why B over A

- **Forward compatibility is the load-bearing property.** Phase A is the
  one moment to make that cheap. Variant A makes adding a kind a
  cross-cutting change that touches the producer, the engine's reader,
  every projection consumer, *and* the harness simultaneously. Variant B
  makes it a per-consumer additive change. The UI is not even prototyped
  yet — locking the schema to a tight union now is the wrong direction.
- **Mixed-version cohorts are inevitable.** Engine vN, harness vN+1, and
  a long-lived UI backend vN-1 will coexist during dogfood. Variant B
  handles this with zero ceremony; variant A handles it only if every
  reader explicitly catches `union_tag_invalid` — easy to miss.
- **The typing-at-the-call-site benefit is recoverable.** Variant B can
  ship a thin `emit_verb_completed(verb, outcome: Outcome)` helper that
  validates the payload before `EventWriter.append`. That captures 95% of
  variant A's win at 0% of its rigidity.

### Why not C

- **The interop win is hypothetical.** Requiem is single-process
  (`INV-SINGLE-PROCESS`) and Daniel is the only operator. There is no
  Knative sink, no Otel collector waiting to consume CE 1.0 events. The
  one realistic external consumer — Hermes-style notifications — can be
  served from a `domain_signal` kind under variant B with no envelope
  overhead at all.
- **The line-size tax is real.** A 10k-event run is 3.3 MB under C vs
  1.5 MB under A/B. Multiply by years of runs sitting on disk.
- **CE extension attributes can't carry the `Outcome` union.** They must
  be flat primitives; structured payloads must live under `data`. That
  defeats the "envelope as the lens" benefit you'd expect from CE.
- **Reserved**: variant C is the right answer the day Requiem grows a
  second consumer outside the operator's machine. If that day arrives,
  variant B's envelope is a strict subset of CE — we can wrap, not
  rewrite.

### Concrete proposal for the ADR

- **Adopt:** variant B envelope (`event_id`, `run_id`, `ts`, `kind`,
  `schema_version`, `node_path`, `payload`).
- **Adopt the registry pattern** for per-kind payload validation; ship the
  v0 registry in the engine repo.
- **Adopt typed-emit helpers** (`emit_verb_completed`, `emit_run_started`,
  …) so producers go through validating call sites — borrowing variant A's
  ergonomics without its rigidity.
- **Keep `extra="forbid"`** in Phase A to surface drift; relax to
  `extra="ignore"` per-kind once the shape stabilises (slice-by-slice in
  Phase C).
- **Defer CloudEvents adoption.** Revisit only when a *second*
  out-of-process consumer materialises.

---

## Domain signals — how the 20-signal envelope lives alongside this schema

(See `docs/references/error-handling-deep-dive.md` §2.3 and the
`docs/decisions/domain-signal-envelope.md` ADR that Beethoven/Bach own.)

Three options were considered. **Recommendation: option (1) — one stream,
one well-known kind.**

### Option 1 — same file, kind `domain_signal` *(recommended)*

```jsonc
{
  "event_id": 47, "run_id": "...", "ts": "...", "kind": "domain_signal",
  "schema_version": 1,
  "node_path": "/root/feature-pr/await-review",
  "payload": {
    "signal": "surface_opened",      // one of the 20 seeded values
    "signal_version": 1,             // per-signal evolution
    "envelope": { ...Beethoven's body... }
  }
}
```

- One file, one tailer, one tailing position. Bach's persistence layer
  reads one stream; platespinner-style UIs read one stream.
- Domain signals get a strict happens-before relationship with execution
  events for free (same `event_id` sequence).
- The platespinner `.notifications.jsonl` becomes a *projection*
  (`jq 'select(.kind == "domain_signal")' < run.events.jsonl`) rather
  than a separate file the engine has to keep in sync. Avoids the
  "two-file divergence" failure mode polyphony pays for today.
- The 20-signal seed catalogue continues to live in
  `docs/decisions/domain-signal-envelope.md`; this seam just transports it.

### Option 2 — sibling file `.notifications.jsonl`

- Matches polyphony's current shape and platespinner's current tailer.
- Cost: two writers must agree on event ordering for accurate
  cause-and-effect debugging. The two-file divergence under crash is real
  (we have receipts).

### Option 3 — separate kind per signal (`domain_signal.surface_opened`)

- Hurts the registry's S/N ratio (20 extra entries; rare kinds drown the
  common ones in tooling).
- Loses the natural lens "show me the domain story for this run" without
  jq-ing 20 type names.

If Daniel objects to option 1, we'd fall back to option 2 but
ship a sidecar projector that *derives* `.notifications.jsonl` from
`.events.jsonl` so the events file remains authoritative
(`INV-EVENT-LOG-AUTHORITATIVE` still holds).

---

## Constraints on adjacent seams

These are assumptions Bach (persistence) and Stravinsky (verb outcomes)
should react to:

### Bach (persistence — seam #4)

- **The events file is the durable substrate.** Manifest, journal,
  ledger, and watermark are projections derived on restart from
  `run.events.jsonl`. Whatever durable structures Bach defines must be
  fully rebuildable from the event stream alone (`INV-RESTART` +
  `INV-EVENT-LOG-AUTHORITATIVE`).
- **`event_id` is a per-run monotonic int.** Bach can index by
  `(run_id, event_id)`; we don't promise global uniqueness across runs.
- **Idempotent rebuilds.** Projections must be deterministic over the same
  event prefix — a "halt at line N because line N+1 is corrupt" rebuild
  must produce the same state as a "log was truncated at line N" rebuild.
- **No projection should write back into `.events.jsonl`.** Projections
  emit their own derived files (`run.manifest.json`, etc.).

### Stravinsky (verb outcomes — seam #1)

- **The `Outcome` discriminated union is carried as
  `VerbCompleted.payload.outcome`.** It must round-trip cleanly through
  pydantic's JSON, including the `Cancelled` variant.
- **The variant tag is the contract.** Routing/retry/UI never inspect
  inner fields to determine success. This means `Outcome` cannot grow a
  shared `success_like: bool` shortcut — adding one would corrupt the
  contract.
- **Cancellation must be losslessly representable** so the kernel's
  retry-short-circuit logic is mechanical (`INV-CANCEL-SHORT-CIRCUITS-RETRY`).
- **`VerbCompleted` carries `verb` and `node_path` so cross-run
  comparison ("this verb failed last time at the same node") is a
  jq-able operation, not a join.**

### Liszt (external-process abstraction — seam #8)

- The 5-class exit-code contract (north-star §4) maps to `Outcome` at the
  *verb library boundary*, not at the event boundary. By the time
  something is emitted as `VerbCompleted`, the translation has happened.

### Mahler/Wagner (state machine kernel — seam #3)

- **The kernel emits `NodeEntered` before invoking the node's verb** and
  emits `VerbCompleted` (or `GateOpened` / `RunCompleted`) before
  reading the next node. The event log is the order of effects, not a
  trace of intent.
- **Routes are not events.** A future `route_taken` kind is reserved but
  not in this prototype; it's a kernel decision whether routing is a
  distinct kind or implicit in the sequence `VerbCompleted → NodeEntered`.

---

## Open questions for Daniel

1. **One stream or two?** Recommendation is option 1 (single
   `.events.jsonl` with a `domain_signal` kind). Are you OK losing the
   polyphony convention of a separate `.notifications.jsonl`, given we
   can derive it as a projection?
2. **Is the CloudEvents interop signal worth its line-size tax for v0?**
   We say no; you may have a future consumer in mind (Hermes? a
   first-party Teams bridge?) that would change the calculus.
3. **`schema_version` granularity.** Top-level (variant B) is coarse but
   simple; per-payload versioning (CE-style `dataschema`) is finer but
   doubles the registry surface. Recommendation is top-level for v0,
   per-kind for kinds that prove to evolve fast in Phase C.
4. **`event_id` semantics.** We've assumed *per-run monotonic int
   starting at 1*. Confirm — alternatives are ULID (globally unique,
   sortable) and 64-bit Lamport-style clocks (useful only if we ever go
   multi-writer, which `INV-SINGLE-PROCESS` rules out for v0).
5. **Should `node_path` be a typed structure** (parent-aware
   `[("root", 0), ("feature-pr", 2), ("await-review", 0)]`) **or
   remain a string** (`/root/feature-pr[2]/await-review`)? String is
   simpler; structured is platespinner-friendly. Either way, the kernel
   owns the canonical form.
6. **Compression.** None in Phase A. At a few MB per run, a 30-day
   retention is ~1 GB and probably not worth gzip'ing — but call this
   out if you want different defaults.

---

## What's deliberately *not* in scope for this PR

- ADR text (will land separately once you've directed B vs A vs hybrid).
- The full domain-signal catalogue and channel registry — those live in
  Beethoven's seam.
- SSE/WebSocket framing for the UI — that's seam #6; this prototype only
  proves the file is tailable.
- Cross-run analytics / time-series — out of scope per the v0 line.
- Cross-process write coordination — `INV-SINGLE-PROCESS` rules it out.
