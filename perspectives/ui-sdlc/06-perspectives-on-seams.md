# 06 — Per-seam UX implications

> This file lives. As each engine seat lands a PR on its seam, this file gets a section reading the PR and writing the UX/SDLC implications I see.
>
> **Status (initial):** scaffold only. No seam PRs landed at time of writing — all sibling worktrees are still on the scaffold commit `7204393`. Each section below is a placeholder with the questions I'll be looking to answer when the PR lands.

The map of seats → seams (from the squad orientation):

| Worktree | Branch | Seam |
|---|---|---|
| `requiem-stravinsky` | `seam/verb-outcome-contract` | Verb outcome contract |
| `requiem-brahms-events` | `seam/run-event-stream` | Run-event stream |
| `requiem-beethoven` | `seam/state-machine-kernel` | State machine kernel |
| `requiem-bach` | `seam/persistence-event-log` | Persistence / event log |
| `requiem-mahler` | `seam/agent-boundary` | Agent boundary / FakeProvider |
| `requiem-wagner` | `seam/dsl-shape` | DSL / workflow definition |
| `requiem-liszt` | `seam/external-process` | External-process abstraction |
| `requiem-brahms-harness` | `seam/harness-contract` | Harness scenario contract |

---

## Stravinsky — verb-outcome-contract

**Status:** _awaiting PR_

**Questions I'll be checking against when the PR lands:**
- Is `receipts` a peer field on the outcome envelope, or buried inside `Success` only? (See `04-sdlc-open-questions.md` Q5.)
- How does the union serialize into the event log? Is the variant tag the JSON discriminator field? (Determines whether the UI can pattern-match cleanly.)
- Are the 5 variants exactly `Success | RetryableFailure | PermanentFailure | NeedsHuman | Cancelled`, or did the prototype find a 6th needed?
- Is there a `kind:` taxonomy *inside* `RetryableFailure` and `PermanentFailure` (the `DD:§2.5` script-kind taxonomy: `external.ado.transient`, `internal.bug`, etc.)? If yes, how does the UI render the second-level kind?

**Likely UX implications:**
- The 5 variants become the entire color vocabulary (`03-ui-pattern-catalogue.md` § Strongest signals #3). If a 6th sneaks in, that decision needs to surface to the UI design.

---

## Brahms-events — run-event-stream

**Status:** _awaiting PR_

**Questions I'll be checking against:**
- Is there an in-memory event bus separate from the durable log, or a single primary? (Q2.)
- What fields are on a base `Event`? (timestamp, scope_path, parent_event_id, run_id, variant tag, payload — at minimum.)
- Are domain signals (`NS:§3.2`) in the same stream or a sibling stream? (Q11.)
- How is `scope_path` represented? (Determines whether path-based concurrency rendering works — `PS:§4`.)
- Is there a sequence number (`event_id`) for tie-breaking when timestamps collide?

**Likely UX implications:**
- This seam *is* the SSE binding contract for the future UI. Get the event shape right and the UI design is mostly downstream.

---

## Beethoven — state-machine-kernel

**Status:** _awaiting PR_

**Questions I'll be checking against:**
- Are subworkflows first-class scope events, or opaque agent-style nodes? (Q4.)
- Is there a `pause` primitive distinct from `cancel`? (Q12 — I lean no for v0.)
- How does the kernel express `for_each` and parallel scopes? (Determines whether the trace view can render concurrency correctly.)
- Is "current scope" a piece of engine state, or always derivable from the event stream? (For restart semantics.)

---

## Bach — persistence-event-log

**Status:** _awaiting PR_

**Questions I'll be checking against:**
- Is the manifest a projection of the event log, or an independently-mutated file? (Q6.)
- Are projections written as **pure functions of events up to event N**? (Critical for the time-travel scrubber post-v0 — see `05-forward-looking-deferred.md` D9.)
- What's the storage shape? JSONL? SQLite? (Affects retrieval cost for cross-run analytics deferred to post-v0.)
- How is "the run is finished" represented? A terminal event, or a flag on the run record?

---

## Mahler — agent-boundary

**Status:** _awaiting PR_

**Questions I'll be checking against:**
- Can FakeProvider produce any of the 5 outcome variants, including `NeedsHuman` with a Pydantic-schema payload? (Q7.)
- How are prompts assembled (templating engine, context injection)? Is the *final assembled prompt* recoverable from the event log? (Critical for the prompt-diff feature — `01-feel-of-the-loop.md` § 15:30.)
- How is LLM cancellation propagated? (Q8.)
- What does the FakeProvider's scenario file look like? (Should compose with the harness scenario file — Q10.)

---

## Wagner — dsl-shape

**Status:** _awaiting PR_

**Questions I'll be checking against:**
- Does the DSL distinguish mutating from pure verbs? (Q9.)
- Is gate declaration a one-line typed-Pydantic-model affordance? (Q3.)
- How are routing decisions expressed? (Determines whether the trace can render "route taken: X because Y" without inventing new metadata.)
- How readable is a complete workflow definition in this DSL? (Critical for the operator who needs to understand "what's going to happen" before kicking off.)

**Likely UX implications:**
- A clunky DSL means workflow authors hand-code awkward verb chains, which produce awkward event streams, which produce awkward traces. DSL ergonomics propagate to the UI.

---

## Liszt — external-process

**Status:** _awaiting PR_

**Questions I'll be checking against:**
- How is `git` / `gh` / `twig` invocation wrapped? (Subprocess primitives, async, timeout-aware?)
- Is the 5-class exit-code contract (`DD:§4 R3`) enforced at the wrapper, or is it the script's responsibility?
- How is cancellation propagated to in-flight subprocesses? (Q8.)
- Is there a "dry-run" affordance (subprocess invocation but no actual execution)? Useful for harness, useful for UI preview.

---

## Brahms-harness — harness-contract

**Status:** _awaiting PR_

**Questions I'll be checking against:**
- Can scenarios declare expected gates + canned responses with typed schemas? (Q10.)
- Do scenarios produce event logs the UI can replay against? (If yes, harness scenarios become free UI snapshot tests.)
- Can scenarios assert on receipts (`no_cli_calls_after` from `DD:§2.1`)?
- Is there a "record this real run, replay it later" affordance? (Lets dogfood incidents become regression scenarios automatically.)

---

## Synthesis after Walking Skeleton α (PR #11, commit `3d4cda6`)

**Read at:** `C:\Users\dangreen\projects\requiem-verdi\demos\walking-skeleton-alpha` on 2026-06-01. I ran `python demo.py`, read the `.events.jsonl` (34 events), read the `.summary.md`, and read the full README. Daniel's reaction was *"i don't get what I'm looking at, but keep going I guess!"* — full demo-failure-analysis is in `07-demo-contract.md`. This section is about what the α composition tells us about the **seams themselves**, not the demo packaging.

### Cross-seam UX implications

**SY1 — The seam composition is right; the operator-facing layer is missing.** Phase A succeeded as integration. Every variant fired; the kernel routed correctly; the event log is restartable. The thing that's missing isn't another seam — it's a **rendering layer** that sits between the event log and the operator. Verdi-2's `requiem events <run_id>` is that layer; see `07-demo-contract.md` § 4.

**SY2 — The default `verb_completed{outcome:{kind:success, value:{...}}}` envelope is correct for storage and wrong for display.** The α `.events.jsonl` is dense, machine-shaped, and rich enough to derive a beautiful UI from — *if* a renderer translates. Without a renderer, the operator sees raw envelope chrome and bounces. **Implication for Brahms-events:** the event schema should NOT add display-oriented fields ("human_message" etc.) — that would push presentation into the engine. The split is right; the missing piece is downstream.

**SY3 — The fluent DSL (Wagner A) reads beautifully bottom-up but doesn't surface "what does this workflow DO" to a non-author.** Reading `workflow.py` requires understanding the engine. There's no `workflow.describe()` or similar that produces a customer-shaped synopsis. **Recommendation for Wagner:** add a `.describe(audience="operator") -> str` to the built Workflow that produces a one-paragraph customer-English summary. The vignette in `07-demo-contract.md` Pattern A wants this as a primitive.

**SY4 — The retry path `flaky_lint, flaky_lint` in `nodes_entered` is the trace at its rawest.** The same retry rendered as `🔁 Lint failed (OOM): retrying… ✓ succeeded on attempt 2` is the trace at its best. The difference is a renderer (~5 lines of Python keyed on `retry_attempted` + the bracketing `node_entered` events). The platespinner `PS:§4` scope grouping does the same logical thing at the UI layer. **Implication:** the renderer + the UI scope grouping share a model — the **retry block** is one logical unit. Engine emits 3 events; renderer collapses them into one display unit; UI does likewise.

**SY5 — The auto-resolved gate is the canonical "what you're looking at" failure in miniature.** `[gate human_gate] options: ('approve', 'reject') → auto-picking 'approve'` shows the prompt but not the verdict being approved. The verdict exists (`.summary.md` proves it); it just isn't piped into the gate prompt. **Implication for Mahler/Wagner gate primitive:** human gate definitions must accept a `context_renderer` callable that produces "what's being decided" content at the moment of gate presentation. The Prefect `wait_for_input` pattern (`WV:§Prefect §8`) handles this implicitly through the typed model; the α demo's free-text gate doesn't.

**SY6 — INV-RESTART works and the demo proved it, but the proof landed flat because the stakes weren't named.** The `demo_resume.py` truncates the log mid-run and resumes; the FakeProvider's one-entry-per-reviewer script proves no re-execution. This is technically beautiful. But Daniel's "I don't get what I'm looking at" applies here too: he doesn't think in INV-RESTART terms, he thinks in "what would happen if my laptop died during a 40-minute LLM-heavy run." **Implication for future restart demos:** open with the stakes ("if restart didn't work, you'd burn 40 minutes of LLM calls every time the process died") and close with the saved cost ("0 reviewer LLM calls re-run on resume").

### Updated per-seam sections

#### Stravinsky — verb-outcome-contract ✅ answered (variant B adopted)

PR #4 — Stravinsky B (PEP 604 sealed unions + `match`). Adopted in walking-skeleton α.

- Q5 (receipts position): partially answered — `inspected_artifacts` is a field on `Success` only in the α composition (`engine/outcomes.py`); a `RetryableFailure` from `flaky_lint` has no receipts surface. ⚠️ I still think peer-of-variant is right; this is a thing to revisit when reviewer-class agents go through the boundary in Phase B.
- The 5-variant union held. `BadOutput` collapse into `PermanentFailure(error_kind="bad_output")` (README "Deviations" §) is the right call — keeps the alphabet tight (`§4.5 demo contract`).

#### Brahms-events — run-event-stream ✅ answered (variant B adopted)

PR #5 — envelope-loose + typed `emit_*` helpers. Adopted in α.

- Q2 (in-memory bus): not yet exercised; α just writes to disk. The bus question reappears when the UI ships.
- Q11 (signals real-time): not exercised; α has no domain signals.
- **New finding (SY2):** the envelope is right for storage. Don't add display fields. The renderer in `requiem events` is the right layer.
- `scope_path` is not in the α schema. The `team_id` and `agent_id` fields fake the nesting for the one team in this demo. For deeper nesting (subworkflows in Phase B), a proper `scope_path` becomes load-bearing.

#### Beethoven — state-machine-kernel ✅ answered (variant C adopted)

PR #7 — data-driven interpreter. Adopted in α.

- Q4 (subworkflows): not exercised in α; README explicitly notes subworkflows are elided. ⚠️ Phase B's `close-out` workflow is simple enough that this remains deferred; the `polyphony.yaml` root in Phase C will force it.
- Q12 (pause vs cancel): not exercised. Still recommend [no pause for v0].
- `TeamNode` as first-class node kind (Beethoven Q-K7 / Pattern #9): this is the right call. Without it, agent teams compile to N awkward subworkflows. ✓

#### Bach — persistence-event-log ✅ answered (variant A adopted)

PR #2 — pure log, no separate manifest. Adopted in α.

- Q6 (manifest as projection): the α demo has no manifest — everything derives from the log. This is the most aggressive version of the "manifest as projection" answer. For Phase B+ with real ADO state, the manifest may need to come back as a *cache* of derivable state — but its authority is the log. ✓
- D9 (time-travel scrubber) constraint preserved: the α projection is a pure function of events (`engine/kernel.py` rebuilds state from the log on resume). ✓ Scrubber-feasibility is preserved.

#### Mahler — agent-boundary ✅ answered (variant A adopted)

PR #8 — Protocol `AgentProvider` + FakeProvider. Adopted in α.

- Q7 (FakeProvider variants): α's FakeProvider scripts by `agent_name` and returns canned `parsed` dicts. It does NOT produce `NeedsHuman` outcomes — only `Success`. ⚠️ Phase B must extend FakeProvider to produce all 5 variants; the demo contract (`07` § 4.4) depends on it being possible to script "what if the reviewer escalated?"
- Prompt recovery from event log: the `agent_invoked` payload includes the prompt. ✓ The "prompt diff against previous run" feature (`01-feel-of-the-loop.md` § 15:30) is enabled.

#### Wagner — dsl-shape ✅ answered (variant A adopted)

PR #1 — fluent builder lowered to pydantic data. Adopted in α.

- Q3 (typed gate schemas): NOT exercised. The α human_gate uses free-text prompt + string-list options. ⚠️ Phase B+ should adopt typed Pydantic gates (`WV:Prefect §8`) when a real workflow needs richer input than approve/reject. This is unblocked, just not exercised yet.
- Q9 (mutate-vs-pure declaration): NOT exercised. Verbs in α are all-or-nothing; no `mutates:` flag. ⚠️ Becomes load-bearing in Phase C when retry+idempotency lint matters.
- **New finding (SY3):** add a `.describe(audience="operator")` affordance on the built Workflow. Cheap; pays for itself the first demo.

#### Liszt — external-process ✅ answered (variant B+C hybrid adopted)

PR #3 — per-tool clients in a frozen `Toolbelt`. Adopted in α.

- Q8 (cancellation): NOT exercised; α has no cancel path. Phase B+ should add at least one "kill mid-run, expect Cancelled" harness scenario.
- The Toolbelt fakes (used in harness fixtures) are the right shape for the demo contract (`07` § 4.4) — they let the demo run with zero external dependencies, which keeps time-to-first-output under 1s.

#### Brahms-harness — harness-contract ✅ answered (variant B adopted)

PR #6 — pytest fixtures (`make_engine`, `auto_approve`, `log_dir`). Adopted in α.

- Q10 (typed gate scenarios): α uses `auto_approve` which is a yes-to-everything handler. No typed-schema gate scenario yet. ⚠️ Becomes relevant when a Phase B+ workflow has a typed gate.
- The harness's 4 tests are well-targeted (full run, retry, suspended-without-handler, INV-EVENT-LOG-AUTHORITATIVE). The fourth is particularly valuable — it's the kind of *invariant* test the deep dive's `DD:§S1.6` chaos scenarios should evolve into.
- **Recommendation:** add a harness scenario that asserts the **renderer output** matches a golden file. Once the demo contract's renderer ships, golden-file testing for customer-English output is the cheapest way to catch regressions in demo quality.

---

## How I'll update this file going forward

Cadence: when Daniel pings me that a seam PR has landed, OR when a `git fetch` shows a sibling worktree has new commits beyond `7204393`, OR when a demo lands.

Per landing:
1. Read the PR (or the worktree's diff) in full. **Run the demo if there is one.**
2. Update the relevant seam's section and add a Synthesis entry if cross-seam.
3. If a finding should update `01`-`05` or `07`, do so in the same commit and call it out in the commit message.
