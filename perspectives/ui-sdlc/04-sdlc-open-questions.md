# 04 — SDLC open questions raised by seam decisions

> The engine seats (Stravinsky, Brahms-events, Beethoven, Bach, Mahler, Wagner, Liszt, Brahms-harness) are about to make decisions that will permanently shape the SDLC operator's daily experience. This file surfaces the questions whose UX/SDLC implications I think Daniel needs explicit awareness of *before* the decisions land.

These are not blocking — engine seats own their seams. These are the **second-order effects** I'd want Daniel to push back on if the seats answer them differently than the perspectives doc would predict.

Each question is tagged with the **owning seat(s)**, the **SDLC consequence**, and my **preferred answer** with caveats.

---

## Q1 — Does every verb invocation appear in the event log?

**Seats:** Stravinsky (verb outcome contract), Brahms-events (run event stream), Bach (persistence)

**SDLC consequence:** if some verbs (e.g., pure-read verbs like `state next-ready`) don't write to the event log, then C3 (post-run forensics) has gaps. The operator asking "why did the engine think there was nothing to do?" can't see the pure-read verb's reasoning.

**Preferred answer [BET]:** **yes, every verb call writes a `verb_invoked` + `verb_returned` event pair**, including reads, including the receipts (`inspected_artifacts`). Cost is one journal append per call; benefit is full operator-time-travel.

**Caveat:** there's a tension with `NS:INV-EVENT-LOG-AUTHORITATIVE` if verbs are called *during UI rendering* (e.g., the operator clicks "expand" and the backend lazily calls a read-verb to fetch data). Those calls shouldn't pollute the run's event log. Resolution: separate **UI inspection log** from the **run execution log**. The run log is operator-state; the inspection log is operator-curiosity.

---

## Q2 — How does the UI subscribe to "what changed" for a single run?

**Seats:** Brahms-events (run event stream)

**SDLC consequence:** this determines whether the UI feels live or laggy (rough edge K1, X1). Polyphony today tails `.events.jsonl`; the SSE bridge in platespinner introduces ~1s lag. Requiem can do better because `ADR-0001` puts the engine and UI backend in one process.

**Preferred answer [BET]:** **an in-memory event bus** that the engine publishes to *synchronously with* the durable journal write. The UI backend subscribes to the bus and pushes via SSE. **Failure semantics**: if the journal write fails, the bus emit must also fail (atomic — bus is a side effect of journal write, not a peer). This preserves `INV-EVENT-LOG-AUTHORITATIVE` (the journal is still authoritative; the bus is a cache).

**Caveat:** the bus is process-local. If the UI backend is restarted, it must replay the journal from the last seen event ID to rebuild its in-memory state. This is fine — restart cost is bounded by event count, and the inbox view is derivable on the fly.

**Risk if answered differently:** if the bus emits *before* journal write, a crash window can leak phantom events the UI showed but the run never recorded. The operator's "I saw it happen" memory disagrees with the journal. This is exactly the kind of inconsistency `INV-NO-CORRUPT-FORWARD` exists to prevent.

---

## Q3 — Is the Pydantic gate schema introspectable from the frontend?

**Seats:** Wagner (DSL shape), Mahler (agent boundary), Brahms-events (event stream)

**SDLC consequence:** this determines whether WV-10 (Prefect typed-input gate forms) is buildable. If yes, the killer human-gate UX is on the table. If no, gates are free-form text or hand-coded forms per gate.

**Preferred answer [BET]:** **yes** — the DSL declares gates with a Pydantic model, the engine serializes the JSON Schema into the `human_gate_presented` event, and the UI renders the form from the schema. No hand-coded UI per gate type.

**Open sub-question:** how rich can the schema be? Strings + enums + numbers cover 90% of gates today. But "select N PRs from a list" or "edit this diff" are richer interactions. **Recommendation:** v0 supports the 90% — strings, enums, numbers, multiline text. Rich pickers are post-v0 (added on demand).

---

## Q4 — What does "subworkflow" look like in the event stream?

**Seats:** Beethoven (state machine kernel), Brahms-events

**SDLC consequence:** addresses rough edge R1 (subworkflow errors as opaque strings). If the event stream treats subworkflows as first-class scope events with their own nested children, the UI gets PS-1 trace-view nesting for free. If subworkflows look like opaque agent calls with a single result string, the UI has to invent a way to drill in.

**Preferred answer [BET]:** **first-class scope events** — `subworkflow_started` with a `parent_run_id` and `child_run_id`, then the child's events nest under it via `scope_path`. Same nesting machinery as `for_each` and parallel scopes.

This is what `DD:§2 R9 (subworkflow.<workflow>.<kind> envelope propagation)` is already pointing at on the polyphony side; Requiem starts there.

---

## Q5 — How are receipts represented in the verb-outcome union?

**Seats:** Stravinsky (verb outcome contract)

**SDLC consequence:** receipts (`DD:§2.1`) are the anti-hallucination defense. If they're inside the `Success` variant only, then a `RetryableFailure` or `NeedsHuman` outcome from a reviewer agent can't carry receipts — and the operator loses the "what did the agent actually look at?" forensics for non-success cases.

**Preferred answer [BET]:** **receipts are a peer field on the outcome envelope, not inside any one variant.** Every outcome variant can carry receipts. This means:
- `Success(receipts=[...])` — the reviewer approved and looked at these things
- `NeedsHuman(receipts=[...], reason="...")` — the reviewer escalated, here's what they looked at first
- `PermanentFailure(receipts=[], reason="...")` — the reviewer failed; the empty list is itself diagnostic

**Caveat:** this slightly muddies the discriminated-union purity (`INV-DISCRIMINATED-OUTCOMES`). The envelope shape is something like `Outcome(variant: Success | RetryableFailure | …, receipts: list[Receipt])`. I'd argue receipts are cross-cutting metadata, not part of the variant.

---

## Q6 — Where does the manifest live in Requiem's mental model?

**Seats:** Bach (persistence + event log)

**SDLC consequence:** today operators inspect `.polyphony/state/{rootId}/seed-manifest.json` directly (`PI:§3`, rough edge X3). If the manifest stays as a separate JSON blob in Requiem, the same JSON-blob debugging pattern persists. If it's a projection of the event log, the UI can render it as a derived table with diff-against-previous-state.

**Preferred answer [BET]:** **manifest is a projection.** The engine maintains it in-memory (`ManifestPlanLedger.Apply` from `DD:§1 layer e` is the polyphony equivalent — port the pattern to Python); the on-disk JSON is a cache for restart. The UI never reads the JSON; it asks the engine for the current manifest state and the engine derives it.

**Sub-question:** does the manifest schema survive verbatim, or get redesigned? **I'd argue:** keep the schema for parity, redesign the *access pattern*.

---

## Q7 — What's the FakeProvider's contract?

**Seats:** Mahler (agent boundary), Brahms-harness (harness contract)

**SDLC consequence:** the harness story (`DD:§S1.5-S1.6` Patches A-D) determines how much of "I ran a real workday" we can simulate. If the FakeProvider can fake `NeedsHuman` outcomes, the UI's gate UX is testable in CI. If not, gate UX is dogfood-only.

**Preferred answer [BET]:** **FakeProvider can produce any of the 5 outcome variants, including `NeedsHuman` with arbitrary Pydantic-schema payloads.** This makes WV-10 typed-input forms harness-testable.

**Implication for the UI seat (future):** harness scenarios should drive UI snapshot tests. A scenario that produces a `NeedsHuman(schema=ApprovePrSchema)` outcome should be assertable as "the UI rendered an ApprovePr form."

---

## Q8 — How is cancellation propagated to in-flight verb calls?

**Seats:** Liszt (external process), Mahler (agent boundary)

**SDLC consequence:** addresses rough edge X2. Determines whether the UI's "cancel" button feels instant or laggy. `ADR-0001` says asyncio.CancelledError is the in-process answer for Python verbs. The question is what happens to **external-process verbs** mid-call.

**Preferred answer [BET]:**
- Pure-Python verbs: `asyncio.CancelledError`, sub-100ms.
- External-process verbs (`git`, `gh`, `twig`, LLM HTTP calls): the verb's async wrapper catches `CancelledError`, sends `SIGTERM` (or Windows equivalent) to the subprocess, waits up to N seconds (default 5?), then `SIGKILL`. Verb returns `Cancelled` variant.
- LLM HTTP calls: the LLM client must support cancellation via the httpx client's request cancellation (most do).

**UI implication:** the cancel button shows "cancelling..." for the wait-window. If the wait-window exceeds N seconds, the button changes to "force-kill" — operator-mediated escalation, not silent extension.

---

## Q9 — Does the DSL distinguish "this node mutates state" from "this node is pure"?

**Seats:** Wagner (DSL shape)

**SDLC consequence:** the `INV-RESTART` invariant says every state-mutating verb must be idempotent or refuse to start. If the DSL doesn't surface mutate-ness, the runtime can't enforce idempotency checks, and the receipts pattern has nothing to attach to.

**Preferred answer [BET]:** **yes, explicitly.** Borrow from `DD:§4 R10 (retry_key + node.idempotent: true validator)` — a node declaration includes `mutates: bool` and `retry_key: str | None`. The DSL refuses to accept `retry: ...` on a node that doesn't declare `mutates=True` + `retry_key=...`. This makes retry safe by construction.

**UI implication:** the trace view can render a tiny `✏️` glyph on every mutating row. The operator instantly distinguishes reads from writes when scanning a long trace.

---

## Q10 — What does the harness scenario file look like for a gate?

**Seats:** Brahms-harness (harness contract)

**SDLC consequence:** if scenarios can express "the engine reaches gate G, the harness submits the typed input X, the engine continues," then the entire gate path is testable. If not, gates are tested by mocking only the engine side and skipping the UI's role.

**Preferred answer [BET]:** scenarios declare expected gates + canned responses:

```yaml
gates:
  - id: approve_plan_pr
    expect_schema: ApprovePrSchema
    respond_with:
      decision: approve
      comment: "looks good"
```

The harness asserts the gate fires with the expected schema and feeds the response back. This is the harness equivalent of WV-10.

---

## Q11 — Are domain signals delivered to channels in real-time or batched?

**Seats:** Brahms-events (run event stream)

**SDLC consequence:** rough edge G3 (silent loss of signals). The 20-signal seed catalogue (`DD:§2.3`) is the operator-facing alphabet. If signals are batched, the inbox lags. If real-time, the inbox feels instant.

**Preferred answer [BET]:** **real-time**, same publish path as execution events (Q2). Channel delivery (Teams, Hermes) can be async; UI delivery is synchronous-with-publish.

**Caveat:** "real-time" means within one event loop tick. If a signal triggers a downstream verb (e.g., `retry_exhausted` triggers a `human_gate`), the gate event must follow the signal in the log order — not a race.

---

## Q12 — Does the engine support "pause" as distinct from "cancel"?

**Seats:** Beethoven (state machine kernel), Mahler (agent boundary)

**SDLC consequence:** this is a feature polyphony lacks (`PI:§6` — gates exist but mid-run pause does not). Some operator scenarios want "I need to step away; freeze this run" without killing in-flight verbs.

**Preferred answer [OPTION]:**
- [OPTION A] No pause primitive. Operator cancels and restarts. Maps to `INV-RESTART`.
- [OPTION B] Pause primitive. Engine suspends after the current verb returns (does not interrupt in-flight). UI shows "paused" pill; resume continues from the next routing decision.

I lean **A for v0** because `INV-RESTART` makes A's UX acceptable (resume is cheap), and B introduces a new state with subtle semantics (what's the difference between "paused" and "waiting at a gate"?). Revisit post-v0 if dogfood shows demand.

---

## Q13 — Does the UI run in-process with the engine or as a separate process?

**Seats:** "UI backend" is implicit in `ADR-0001`. Probably touched by Beethoven or whoever owns the engine entrypoint.

**SDLC consequence:** the answer determines crash blast radius. If the UI backend is in the engine process, a UI bug can crash the engine mid-run. If it's a separate process talking via socket, you pay an extra IPC seam.

**Preferred answer [BET]:** **in-process, in a separate asyncio task with a hard exception boundary.** Per `ADR-0001` Consequences: "every verb call is wrapped in an exception boundary that converts uncaught exceptions to `PermanentFailure`" — apply the same discipline to UI request handlers. UI handlers catch their own exceptions and never propagate to the engine's task tree.

**Implication for restart semantics:** if the UI handler panics, the engine continues; the UI tab shows a "backend error, refresh" banner. The frontend reconnects to SSE and replays from the last seen event. The run keeps running.

---

## Where these questions overlap

The five questions whose answers most tightly couple to each other:

- **Q1 + Q5 + Q11** — the *shape of an event* is determined together. Receipts as outcome envelope (Q5) + every verb in the log (Q1) + signals as peer to events (Q11) is one coherent schema. If any of these answers diverge, the event log gets weird shapes.

- **Q2 + Q13** — in-memory bus (Q2) + in-process UI backend (Q13) compose naturally. If Q13 answers "separate process," Q2 needs a serialization layer.

- **Q3 + Q9 + Q10** — the typed-input forms (Q3) and the mutate-declaration (Q9) and the harness gate scenarios (Q10) are the same surface from three angles. The DSL shape decision needs to address all three at once.

Daniel: if you only have time to push on one batch, **push on the Q1/Q5/Q11 event-shape cluster.** The other questions are recoverable; an event-shape mistake is the most expensive to undo because every consumer depends on it.
