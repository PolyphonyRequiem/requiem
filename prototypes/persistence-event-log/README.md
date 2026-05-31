# Phase A Seam #4 — Persistence + Manifest-as-Event-Log

**Seat:** Bach (persistence + manifest)
**Branch:** `seam/persistence-event-log`
**Invariants in scope:** `INV-EVENT-LOG-AUTHORITATIVE`, `INV-NO-CORRUPT-FORWARD`, `INV-RESTART`

---

## The seam in one sentence

> The polyphony `RunManifest` lumped together fields that genuinely cannot
> be rebuilt from the world (topology, plan-generations, retired MGs,
> human approvals) with fields that obviously can (rebases, plan-PR
> ledger). Requiem's `INV-EVENT-LOG-AUTHORITATIVE` forces us to commit:
> **the durable artifact is the event log; the manifest is a projection.**
> The remaining question is whether we *also* keep a fast-query view —
> and if so, under what guarantees.

---

## Authoritative vs derived — moved across the line

Polyphony's `RunManifest` ([source][polyphony-manifest]) carries seven
groups of fields. Re-classified for Requiem:

| Field (polyphony name)        | Polyphony classification | Requiem classification | How it's reconstructed                          |
|-------------------------------|--------------------------|------------------------|-------------------------------------------------|
| `Schema`                      | authoritative            | **derived**            | First-event field on `run_started`              |
| `RootId`, `PlatformProject`, `CreatedAt`, `CreatedBy`, `BranchModelVersion` | authoritative | **derived** | Fold of `run_started`                           |
| `PlanGenerations`             | authoritative            | **derived**            | Count of `plan_generation_bumped` per `item_key` |
| `MergeGroups`                 | authoritative            | **derived**            | Fold of `mg_declared`                           |
| `TopologyHash`                | authoritative            | **derived (computed)** | `sha256(canonicalize(merge_groups))` on demand  |
| `RetiredMergeGroupIds`        | authoritative            | **derived**            | Set of `mg_retired`                             |
| `HumanApprovals`              | authoritative            | **derived**            | Fold of `human_approval_recorded`               |
| `Rebases`                     | log-shaped (audit)       | derived (audit events) | Fold of `rebase_recorded`                       |
| `MergedPlanPrs`               | log-shaped (idempotency) | derived                | Fold of `plan_generation_bumped` (carries `pr_number`+`merge_commit` as its own idempotency proof) |

**Every field moves to derived.** The polyphony manifest's "authoritative"
status was a *consequence of the .NET/Python split* (the engine couldn't
read C# events efficiently, so polyphony had to write a separate
YAML manifest). Requiem dissolves that split, so the manifest can
collapse back into the log.

### What we couldn't put in the log, and why

Two things must persist *outside* the event log because they're meta to
the log itself:

1. **A per-run write-lock / owner-pid file** (`<run>.lock`).
   The log can't be its own gatekeeper: the rule "only one writer at a
   time" must be enforceable *before* the first append. This is a tiny
   sidecar (`{pid, hostname, acquired_at}`), reset on graceful shutdown,
   diagnosable on crash via PID-aliveness check.

2. **The "where the log ends" pointer for crash recovery** —
   specifically, the byte offset at which the engine last issued a
   completed `fsync`. Without it we cannot distinguish "torn write from
   a crash mid-append" from "schema-evolution event we don't understand"
   at byte-level. The prototypes don't ship this (they over-approximate
   with "the first un-decodable line is corruption") but it is the
   right answer for production. Likely shape: `<run>.fsync-watermark`
   updated alongside each append.

Both are *operational metadata about the log*, not run state. They do
not violate INV-EVENT-LOG-AUTHORITATIVE — they enforce the conditions
that make the log trustworthy in the first place.

A third candidate — **provider/secret material** (PATs, API keys) —
is config, not run state, and is explicitly out of scope here.

---

## Variants

| | A. Pure log | B. Log + snapshots | C. SQLite view + JSONL truth |
|---|---|---|---|
| Truth substrate | `.events.jsonl` | `.events.jsonl` | `.events.jsonl` |
| Hot store | in-memory `Projection` | in-mem + snapshot files | `.view.sqlite` (rebuildable) |
| Restart latency, small run (≤100 ev) | trivial (<5 ms) | trivial (snapshot may not exist yet) | trivial (<20 ms) |
| Restart latency, medium (1k ev) | ~10–30 ms | ~10–30 ms | ~5–20 ms (warm), tail-replay if cold |
| Restart latency, large (100k ev) | ~1–3 s | ~30–100 ms | <50 ms (warm) |
| Targeted query latency (`is_mg_retired?`) | O(1) dict lookup | O(1) dict lookup | O(1) indexed SQL — wins for UI scans |
| Schema evolvability (add a kind) | add class + projector | add class + projector | add class + projector + SQL schema migration |
| Corruption surfacing | first bad line ⇒ `CorruptLogError` w/ byte offset | log error **plus** snapshot-divergence detection | log error **plus** `ViewAheadOfLogError` |
| Operational debuggability (human reads the log?) | `jq` over JSONL — perfect | JSONL + opaque snapshot blob | JSONL + needs `sqlite3` CLI for view |
| Test ergonomics | assert directly against `Projection` | same + snapshot interval to tune | same + DB lifecycle to manage |
| INV-EVENT-LOG-AUTHORITATIVE strictness | strictest — no other state | strict (snapshots verified) | strict (view is explicitly derived; reconcile-on-open) |
| Lines of code (this prototype) | ~290 | ~360 | ~430 |

---

## Recommendation

**Start with Variant A. Hold Variant C in reserve as a Phase C upgrade.**

Defence:

1. **Right invariant pressure for v0.** Variant A is the only one of the
   three where there is no second source of state that could lie. The
   manifest is literally `fold(events, init)`. Every other variant
   spends design budget reconciling a derived store with the truth.
   That budget is better spent elsewhere in v0.

2. **Right performance envelope for v0.** Daniel's expected runs are
   hundreds to low-thousands of events. Variant A's full-log replay on
   restart at that size is single-digit milliseconds. Pre-optimising
   for 100k-event runs is YAGNI in the absence of dogfood evidence.

3. **Right cost curve.** A SQLite view (Variant C) is a *per-event-kind
   tax*: every new kind needs a projector AND a SQL writer. Snapshots
   (Variant B) get most of B's benefit with the verifier in place, but
   pay for it by introducing a second corruption mode that earns its
   keep only at sizes we don't have yet.

4. **C is a clean upgrade path, not a fork.** Variant C's contract
   ("SQLite is a rebuildable view; JSONL is truth") is *additive* to
   Variant A. We can graft C onto an A-shaped engine the day the UI
   backend asks for a query Variant A can't answer in <10 ms. Until
   then, A is enough.

5. **Snapshots (B) are the dominated option.** The verifier that makes
   snapshots safe re-folds the whole log on restart anyway, dissolving
   the headline benefit. Without the verifier, a tampered snapshot can
   silently violate INV-NO-CORRUPT-FORWARD. Either you pay the cost
   that B was supposed to avoid, or you weaken the invariant. Reject.

**Concrete v0 commitment:**
- Ship Variant A.
- Ship the lock-file sidecar from "what we couldn't put in the log".
- Ship the fsync-watermark sidecar.
- Add a contract test: for every new event kind, a fixture exists that
  proves the kind survives a restart. (Cheap; mechanical.)
- Add a benchmark: replay 10k events; assert <250 ms on Daniel's
  machine. Tripwire for "we need C earlier than expected".

---

## Open questions for Daniel

1. **Cross-run state.** This seam is per-run by construction. Are there
   any cross-run reads the engine actually needs? (e.g. "has root N
   ever produced a successful plan-generation > 1?") If yes, the
   answer is *probably* a separate cross-run journal — not a shared
   manifest — but it deserves an ADR.

2. **Run-lock policy on stale PID.** When the lock-file points at a
   PID that's no longer alive, do we (a) auto-break the lock and emit
   a `lock_broken` event, (b) refuse to start and require operator
   intervention (most conservative; aligns with `INV-NO-CORRUPT-FORWARD`),
   or (c) require operator intervention only if the dead engine's last
   log line wasn't a `run_ended`?

3. **Replay determinism for re-derived projections.** Variant A says
   "the projection is a pure function of the log." That's true today.
   But if a future event kind's projection logic *evolves* (we fix a
   bug in how `plan_generation_bumped` updates state), then re-deriving
   an old run from disk gives a *different* projection from what the
   run actually used. Is that acceptable (we accept the corrected
   projection as the new truth) or do we need projection-logic
   versioning (e.g., projector-version is recorded in `run_started`
   and old runs use old projectors)?

4. **fsync cost vs. throughput.** Every prototype `fsync`s every
   append. For an engine that emits ~1 event per second this is free;
   for one that bursts hundreds of events during a worktree scan it
   is not. Acceptable to batch-fsync (e.g., per-tick) at the cost of
   losing the last <50 ms on power-cut?

5. **Variant C lazily-materialised.** Should we make the SQLite view
   *optional and on-demand* — built only when the UI backend opens a
   run, never written by the engine? That gives us A's invariant story
   and C's query story without the dual-write hazard. (My recommendation
   above implicitly assumes this is on the table.)

---

## What's in this directory

```
prototypes/persistence-event-log/
├── README.md                              ← you are here
├── requirements.txt
├── variant-a-pure-log/
│   ├── README.md
│   ├── events.py                          ← pydantic v2 discriminated union
│   ├── store.py                           ← EventStore + Engine + Projection
│   └── demo.py                            ← runs all 6 scenarios
├── variant-b-log-plus-snapshots/
│   ├── README.md
│   ├── events.py
│   ├── store.py                           ← + snapshot writer + verifier
│   └── demo.py
└── variant-c-sqlite-view-jsonl-truth/
    ├── README.md
    ├── events.py
    ├── store.py                           ← + SQLite projector + reconcile
    └── demo.py
```

Each variant's `demo.py` is self-contained and idempotent (it wipes its
own `.demo-runs/` on every run).

## Running

```bash
python -m venv .venv && .venv/Scripts/activate
pip install -r requirements.txt
cd variant-a-pure-log && python demo.py
cd ../variant-b-log-plus-snapshots && python demo.py
cd ../variant-c-sqlite-view-jsonl-truth && python demo.py
```

All three demos exercise the six required scenarios end-to-end:
1. Write the canonical event sequence
2. Query current state
3. Engine restart
4. Corruption (truncated log / tampered snapshot / view-ahead-of-log)
5. Schema evolution (unknown event kind round-trips and is counted)
6. Two concurrent runs in the same process, no cross-talk

[polyphony-manifest]: https://github.com/PolyphonyRequiem/polyphony/blob/main/src/Polyphony/Manifest/RunManifest.cs
