# Phase A: User Acceptance Demo Plan

> **For Daniel.** What to run, in what order, to make Phase A seam decisions
> with the highest possible signal per minute of your time.

## 0. TL;DR

Phase A shipped 9 PRs and 1 perspectives doc. Each ships runnable Python
demos. **You don't need to read 27 variant demos.** Here's a curated path:

| Tier | What | Time | Decision it unlocks |
|---|---|---|---|
| **0** | Integration demo (walking-skeleton α) | 5 min | "Does the integrated design hold together?" |
| **1** | Wagner DSL feel test (A vs C) | 10 min | Author surface for v0+ |
| **1** | Mahler agent boundary feel test (A vs B) | 10 min | Whether we eat ~50 transitive deps for pydantic-ai sugar |
| **1** | Beethoven cancel-resume test | 5 min | Does INV-RESTART actually work? |
| **2** | Brahms-events emit feel test | 5 min | Typed-per-kind vs envelope-loose at the emit site |
| **3** | The other 4 seam READMEs (skim) | 15 min | Confirm or flag |

Total budget: ~50 minutes. After that you have enough to greenlight Phase B
or send any seam back for another lap.

If you have **5 minutes**: just run the integration demo (Tier 0). If
it produces a complete `.events.jsonl` and a sane synthesized output without
any exception, the design composes; you can take the remaining seam decisions
asynchronously over the next few days while Phase B starts.

---

## Tier 0 — Integration Demo (Walking Skeleton α)

**Status:** **In flight.** Dispatched to `verdi-integration` agent in
parallel with this plan being written. PR will land at #10 or #11. Refresh
the repo's PR list in ~30-60 minutes.

**What it composes (the recommended variant from each seam):**

| Seam | Variant used | PR ref |
|---|---|---|
| Verb outcome | Stravinsky B (PEP 604 sealed unions + match) | #4 |
| Event stream | Brahms-events B (envelope-loose + typed emit helper) | #5 |
| Kernel | Beethoven C (data-driven interpreter) | #7 |
| Persistence | Bach A (pure log) | #2 |
| Agent boundary | Mahler A (Protocol-based AgentProvider) + FakeProvider | #8 |
| DSL | Wagner A (fluent builder) lowered to pydantic data | #1 |
| External-process | Liszt B+C hybrid (per-tool clients in a frozen Toolbelt) | #3 |
| Harness | Brahms-harness B (pytest fixtures) | #6 |
| Agent teams | The pattern from PR #9 (`.team(...)` sugar over `parallel_fork`) | #9 |

**The workflow itself:** a `code-review` team-step. 3 fake "reviewer" agents
review a snippet in parallel, a synthesizer agent produces a verdict, a human
gate asks Daniel to approve, the run completes. Total run time: < 5 seconds
with FakeProvider, no API keys needed.

**What you'll see when you run it:**
- Terminal output showing each phase
- A real `.events.jsonl` file you can `cat` (or `jq`)
- A typed `RunOutcome` printed at the end
- Source code that's small enough to read in one sitting (~400 LOC across
  the engine + the demo)

**The decision it informs:** does the recommended composition hold? If yes,
Phase A is done; ADRs 0002-0010 codify the choices and Phase B
(real walking skeleton with a real workflow) begins. If anything feels off
— e.g., the agent-team pattern is awkward, the JSONL is hard to read, the
kernel hides too much — that's a signal to revisit one seam before
committing.

**Run command (once Verdi's PR lands and is checked out):**
```powershell
cd C:\Users\dangreen\projects\requiem  # or wherever the demo lives
pip install -r demos/walking-skeleton-alpha/requirements.txt
python -m demos.walking_skeleton_alpha
# OR
python demos/walking-skeleton-alpha/demo.py
```

---

## Tier 1 — The Three Decisions That Most Shape Daily Use

These are the seams where your aesthetic + ergonomic preferences matter
most, because YOU will write workflows, declare agents, and read failures
the most.

### 1a. Wagner DSL feel test — Variant A vs Variant C

**Why this one matters:** you'll author every workflow in this language.
Picking the wrong surface is a tax on every workflow file you touch from
here forward.

**Run:**
```powershell
cd C:\Users\dangreen\projects\requiem-wagner
pip install -r prototypes/dsl-shape/requirements.txt
python prototypes/dsl-shape/variant-a-fluent-builder/demo.py
python prototypes/dsl-shape/variant-c-declarative-pydantic/demo.py
```

**Look at:**
- `prototypes/dsl-shape/variant-a-fluent-builder/close_out.py` (the workflow file)
- `prototypes/dsl-shape/variant-c-declarative-pydantic/close_out.py`
- Then read `prototypes/dsl-shape/README.md` for Wagner's tradeoff table

**What you're feeling for:**
- Which one reads more like the workflow you imagine in your head?
- Which one's typos break first (lint vs construct vs run)?
- Which one would a new contributor pattern-match faster?

**Wagner's rec:** A on top of C (builder is sugar, data is canon).
**Default if you don't pick:** go with Wagner's rec.

---

### 1b. Mahler agent boundary feel test — Variant A vs Variant B

**Why this one matters:** the dependency surface. Variant B brings
pydantic-ai (~50 transitive deps). Variant A is a Protocol you can read
in 5 minutes. Once you ship Variant B, removing pydantic-ai is a major
refactor.

**Run:**
```powershell
cd C:\Users\dangreen\projects\requiem-mahler
pip install -r prototypes/agent-boundary/requirements.txt
python prototypes/agent-boundary/run_all.py
```

**Look at:**
- `prototypes/agent-boundary/variant-a-protocol-provider/agent_provider.py`
- `prototypes/agent-boundary/variant-b-pydantic-ai/agent.py`
- The dependency lists in each `requirements.txt`

**What you're feeling for:**
- Authoring an agent: how much ceremony does each variant ask of you?
- Reading the contract: can you hold the whole boundary in your head with A?
- Is the dependency tax of B worth the ergonomic gain?

**Mahler's rec:** A — Protocol-based. Refuse B's deps + double retry loop.
**Default if you don't pick:** go with Mahler's rec.

---

### 1c. Beethoven cancel-resume test — INV-RESTART proof

**Why this one matters:** every other invariant assumes restart works.
If INV-RESTART is theoretical you'll discover it the first time the engine
crashes mid-workflow. Best to feel it now.

**Run:**
```powershell
cd C:\Users\dangreen\projects\requiem-beethoven
pip install -r prototypes/state-machine-kernel/requirements.txt
# pick variant C (the runtime kernel rec)
python prototypes/state-machine-kernel/variant-c-data-driven/demo.py
# In the demo Beethoven explicitly KILLS the process mid-run and restarts.
# You should see the second run resume from where the first stopped, replaying nothing the user saw.
```

**Look at:**
- The terminal output — does the restart phase make obvious what was skipped?
- The `.events.jsonl` — can you read what happened?
- The `_reconstruct()` function in `variant-c-data-driven/engine.py` —
  this is the load-bearing code for the whole invariant

**What you're feeling for:**
- After restart, is it OBVIOUS what was already done?
- Did the engine silently skip something it shouldn't have?
- Could YOU debug a stuck run with just the JSONL?

**Beethoven's rec:** C (interpreter) as runtime + A (fluent builder via
Wagner) as authoring sugar.
**Default if you don't pick:** go with Beethoven's rec.

---

## Tier 2 — Schema Choice You Want to Look At

### 2a. Brahms-events emit feel test

**Why:** how easy is it to emit a new event kind? You'll add event kinds
through the life of the project. Cost-per-kind matters.

**Run:**
```powershell
cd C:\Users\dangreen\projects\requiem-brahms-events
pip install -r prototypes/run-event-stream/requirements.txt
python prototypes/run-event-stream/variant-a-typed-discriminated/demo.py
python prototypes/run-event-stream/variant-b-envelope-loose/demo.py
# Compare the line size + the emit-site code in both
```

**Look at:**
- The actual `.events.jsonl` output of each — can you read it as a human?
- `grep` for emit sites in each variant — which has less ceremony?
- The "schema evolution" section of each demo — adding a new kind in v2

**What you're feeling for:**
- The first time you add a new event kind, how much work is it?
- Could you read the JSONL in production to debug a stuck run?

**Brahms-events rec:** B (envelope-loose + typed emit helper).
**Default if you don't pick:** go with Brahms's rec.

---

## Tier 3 — The Other 4 (Skim the READMEs)

These are seams where the recommendation is clear and the variants don't
have big aesthetic differences. Read the PR README for each — that's
~3 minutes per seam.

| Seam | PR | Recommendation |
|---|---|---|
| Stravinsky — outcomes | #4 | B (PEP 604 sealed + match) |
| Bach — persistence | #2 | A (pure log); C (SQLite view) as additive later |
| Liszt — external-process | #3 | B+C hybrid (per-tool clients in frozen Toolbelt) |
| Brahms-harness — scenarios | #6 | B (pytest) primary, A (YAML) migration veneer |

If anything in the PR READMEs raises a red flag, ping me to dispatch a
deeper look. Otherwise these decisions are "ACK rec, move on."

---

## After demos — what happens next

1. **You decide.** For each seam: take the rec, take a different variant,
   or send back for another lap.
2. **I capture the decisions** as ADRs 0002–0010 (one per seam) plus
   ADR 0011 covering the agent-team pattern integration.
3. **Phase B opens.** Walking skeleton against a real workflow
   (close-out is the smallest; an agent-team-shaped workflow is the
   highest-novelty). Per roadmap.
4. **Debussy re-engages** as seam PRs merge — she'll update
   `perspectives/ui-sdlc/06-perspectives-on-seams.md` with UX implications
   of each chosen variant.

---

## Open questions across all 9 PRs (the load-bearing ones)

These are the questions surfaced by seam agents that affect Phase B
sequencing. None block your demo run; all should be answered before Phase B
lands the walking skeleton.

1. **Verb-by-reference vs verb-by-name** (Wagner Q-1 + Beethoven Q-K1).
   Recommendation: by-reference for in-process; by-name only at the harness
   seam. Cheap default; revisit if the registry pattern becomes painful.
2. **`error_kind` open string vs closed enum tied to 21-signal catalogue**
   (Stravinsky Q-3). Recommendation: closed enum at v0, with an explicit
   `extensibility_escape: dict` for cases the enum doesn't cover.
3. **Single `.events.jsonl` vs sibling `.notifications.jsonl`**
   (Brahms-events Q-1 + Debussy Q-1). Recommendation: single stream, two
   lenses; `kind=domain_signal` is just another envelope.
4. **`receipts` placement — on `Success` only or peer field on envelope?**
   (Debussy Q-2). Recommendation: peer on envelope — failure forensics matter
   more than success ones.
5. **`parallel_fork` primitive** (Beethoven Q-K7). Recommendation: **adopt
   for v0**, see PR #9 for full rationale (agent-team support).
6. **Pause-as-distinct-from-cancel** (Debussy Q-3). Recommendation: skip
   for v0. Restart-from-log is a sufficient substitute.
7. **Workflow versioning recorded in `workflow_started` event**
   (Beethoven Q-K8). Recommendation: yes, cheap to add now, expensive to
   retrofit.

If you want me to opinionate harder on any of these before you read PRs,
say the word — I'll write a one-page brief per question.
