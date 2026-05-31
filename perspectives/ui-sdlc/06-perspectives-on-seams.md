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

## Synthesis (to be written as PRs land)

Once 3+ seam PRs have landed, this section will synthesize cross-seam UX implications I couldn't see from any single seam in isolation. Currently empty.

---

## How I'll update this file

Cadence: when Daniel pings me that a seam PR has landed, OR when a `git fetch` shows a sibling worktree has new commits beyond `7204393`.

Per seam PR:
1. Read the PR (or the worktree's diff if not yet PR'd) in full.
2. Update that seam's section here: replace "_awaiting PR_" with "Read at commit `<sha>`, dated `<date>`."
3. Mark each question as ✅ answered / ⚠️ partially / ❌ answered differently than expected / 🚫 not addressed.
4. Add 1-3 paragraphs on the UX consequences I now see.
5. Commit + push as `perspectives: update after <seam> PR landed`.

If I notice a *cross-seam* implication, add it to the Synthesis section.

If I notice an implication that should change `01`-`05`, edit those files in the same commit and call it out in the commit message.
