# 07 — The Demo Contract

> **Trigger:** Walking Skeleton α (PR #11). Daniel ran it and said *"i don't get what I'm looking at, but keep going I guess!"* — the loudest possible UX signal we'll get this phase. The demo succeeded as an integration test (it proves Phase A's variants compose) and failed at its customer-facing purpose.
>
> **Charter of this file:** make sure no Phase B+ deliverable shown to Daniel fails the same way. Every future demo must pass a checklist before it gets shown.

---

## 1. The "I don't get what I'm looking at" failure, dissected

I ran the demo myself. I read the README. I read the `.events.jsonl`. I read the `.summary.md`. Here is what was on screen when Daniel hit `python demo.py`, and what was missing.

### What was on screen

```
========================================================================
Walking Skeleton α — run_id=demo-1780287814
========================================================================
workflow      : code-review  (9 nodes, 11 edges)
recommended   : Stravinsky B + Brahms B + Beethoven C + Bach A
              + Mahler A + Wagner A + Liszt B+C + Pattern #9
log_dir       : C:\Users\...\.runs
------------------------------------------------------------------------
  [gate human_gate] Reviewer team finished. Approve verdict?
  [gate human_gate] options: ('approve', 'reject') → auto-picking 'approve'
------------------------------------------------------------------------
wall-clock    : 91.0 ms
result.kind   : Completed
disposition   : completed  (final_node=end)
projection    : {
  "nodes_entered": ["start","read_snippet","flaky_lint","flaky_lint", ...],
  "verbs_completed": 9, "retries": 1, "team_branches_completed": 3,
  "terminal": "completed", "total_events": 34
}
```

### What's wrong with it, line by line

| What's there | What it says to Daniel | What he needed |
|---|---|---|
| `recommended : Stravinsky B + Brahms B + Beethoven C + Bach A + ...` | "this is a label sticker for an engineering bake-off you weren't in" | The names of seam variants are engineering bookkeeping. Daniel doesn't care which letter Stravinsky picked. |
| `workflow : code-review (9 nodes, 11 edges)` | "this is a graph you can't see" | "I'm running a 3-reviewer code review on `sample_snippet.py`." Nouns from his world, not the kernel's. |
| `[gate human_gate] options: ('approve', 'reject') → auto-picking 'approve'` | "this is a python tuple, automatically dismissed without showing you what's being approved" | The verdict being approved should be **on screen** at the gate: "1 blocking issue, 1 warning, 1 info. Recommend NOT to merge. Approve? [a/r]" |
| `projection : { nodes_entered: ..., verbs_completed: 9, retries: 1, team_branches_completed: 3, terminal: completed, total_events: 34 }` | "this is engine telemetry" | The *verdict*. `Recommend merge: false. Top issue: unhandled ValueError on int(x).` That data exists — it's hidden in `.runs/<run_id>.summary.md`. |
| `nodes visited : start → read_snippet → flaky_lint → flaky_lint → review_team → ...` | "...this is the runtime trace" | The narrative: "Read snippet → lint (retried once, transient OOM) → 3 reviewers in parallel → synthesized verdict → you approved → archived." |
| `event log : ...events.jsonl (10494 bytes)` + `inspect with: jq -c .` | "your homework is to jq the log" | A pre-rendered human-readable summary right here in the terminal, with `--raw` for the curious. |

### What was *not* on screen (but should have been)

1. **The artifact under review.** Daniel never sees `sample_snippet.py`. The whole demo is *about* that snippet — but it's invisible. Show it. It's 7 lines.
2. **The reviewer findings.** They exist in `.summary.md` but only after the run. They should be streamed as each reviewer reports.
3. **The verdict, in his actual terms.** "🚫 Don't merge. Blocking: unhandled ValueError. Warning: mutable default. Info: dict lookup pattern."
4. **The retry as a story, not a sequence.** "🔁 Lint failed (transient OOM); retrying... ✓ passed on attempt 2." Not `flaky_lint, flaky_lint` in a list.
5. **What this would mean on a real workday.** "If this were a real SDLC day: you'd have just blocked a bad merge before your morning coffee."

### The diagnosis

**The demo told the story of how the engine works. Daniel needed the story of what the engine did.** Those are different stories told from different vantage points. The first is for engineers building the engine; the second is for the customer of the engine. The walking-skeleton-α demo is the first story. Phase B+ demos must be the second.

This is a **default vocabulary failure**. The default terminal output speaks in seam names, node IDs, envelope kinds, and metrics. The customer-facing vocabulary is artifacts, findings, verdicts, decisions. **The customer-facing vocabulary should be the default; the engineering vocabulary is `--debug`.**

---

## 2. Five demo patterns that DO speak to Daniel-as-customer

These are the shapes I'd build Phase B+ demos around. Each is a candidate, not the only answer. Cite Daniel's workday from `01-feel-of-the-loop.md`.

### Pattern A — Workday vignette (the headline pattern) **[BET]**

**Shape:** the demo opens with a one-paragraph scenario in plain English ("It's Monday morning. A PR arrived overnight. Let's see what Requiem does with it.") and ends with a one-paragraph outcome ("Verdict in 91ms: don't merge. Daniel saves the morning that PR would've cost."). In between, the engine narrates what it's doing in **workday nouns**.

**Why:** Daniel evaluates Requiem against the question "is this the thing that makes my SDLC day feel right?" Vignettes answer that question directly. Engine-mechanics demos answer "does this compose?" — which is what Daniel pays the squad to know, not what he wants to be told.

**Reference:** `01-feel-of-the-loop.md` § 09:00, 17:00 — the entire file is workday vignettes by hour.

### Pattern B — Live narration in customer English **[BET]**

**Shape:** every event the kernel emits has a render function that produces one **human-readable line** describing what just happened in *the workflow's domain*, not the engine's. The terminal streams those lines as the run progresses. `--raw` gives you the JSON.

Concrete: `node_entered{node=read_snippet}` renders as `▶ Reading sample_snippet.py`. `verb_completed{outcome=Success, value={loc:7}}` renders as `✓ Read 7 lines`. `retry_attempted{reason="OOM"}` renders as `🔁 Lint flaked (linter OOM) — retrying`.

**Why:** the trace IS the demo. If the trace narrates in customer terms, the demo communicates by default — no separate "demo script" needed. This is the **single highest-leverage thing the Verdi-2 CLI can do** (see § 4 below).

**Reference:** `PS:§4` (platespinner trace lines are already human-shaped per category — `scope-start`, `agent-start`, `gate`, `route`); `WV:Step Functions §2` (real-time state overlay tells the story without a sidebar); `WV:VS Code §1` (output below the cell, locality of reference).

### Pattern C — The verdict card, front and center **[BET]**

**Shape:** when a run produces an outcome the operator cares about (a code review verdict, a plan reviewer decision, a PR merge result), the demo ends with a **verdict card** — a small block of formatted text showing the outcome in the operator's terms. Not the engine's `result.kind` and `disposition`; the verdict's actual content.

Concrete for the α demo:
```
─── Verdict ──────────────────────────────────────────────────────────
  🚫  Don't merge
      Blocking:  unhandled ValueError on int(x)
      Warning:   mutable default cache={} leaks state across calls
      Info:      cache.keys() lookup could be O(1)
  → recommendation: send back to coder for blocking fix
──────────────────────────────────────────────────────────────────────
```

**Why:** the answer to "what did this demo prove" should be visible in the last screenful, in nouns Daniel recognises. Today that information exists in `.runs/<run_id>.summary.md` but the terminal never tells you to look there until after the run ends.

**Reference:** `WV:Prefect §8` (typed-input gate forms — the verdict card is the *output* equivalent of the typed input). `WV:Dagster §2` (assets show their last materialization inline; same principle: surface the outcome where the eye lands).

### Pattern D — Counterfactual side-by-side **[OPTION]**

**Shape:** the demo shows two columns. Left: "what you'd do today in polyphony." Right: "what Requiem just did." Same task, side by side. Sometimes the columns highlight a specific improvement (e.g., "polyphony: gate sat unanswered for 47 minutes because no notification. Requiem: gate prompt appeared in OS toast within 2 seconds").

**Why:** Daniel's mental model is "Requiem must be no worse than polyphony, ideally better." Side-by-side makes the comparison explicit, which is exactly what the customer-pitch is about.

**Caveat:** expensive to produce per-demo. Use for **milestone demos** (Phase B exit, Phase C exit), not every PR.

**Reference:** none in the surveys directly — this is a customer-pitch idiom from product demos, not orchestration UIs.

### Pattern E — "What's at stake" framing **[BET]**

**Shape:** every demo opens with one sentence saying *what would go wrong if this didn't work*. "If retry doesn't survive an OOM, you'd have to rerun the entire PR review by hand every time the linter flakes." Then the demo proves the bad outcome didn't happen.

**Why:** Phase A demos so far have been "look, it works." Phase B+ demos must be "look, the bad thing didn't happen *because* this works." The first is engineering; the second is product. The α demo proved INV-RESTART by truncating a log mid-run — genuinely cool — but the stakes ("you'd have to redo $X minutes of LLM calls if restart didn't work") were never named, so the proof landed flat.

**Reference:** `NS:§2` (every invariant has a *why* paragraph — that paragraph is the "what's at stake." Demos should reuse those paragraphs verbatim where they apply).

---

## 3. The Demo Contract Checklist

> **Hard contract:** every Phase B+ deliverable shown to Daniel MUST pass this checklist before being shown. Any agent dispatching a demo to Daniel cites this file and confirms each box.

### Pre-demo (the dispatcher's job)

- [ ] **§3.1 — Workday framing.** The demo starts with one sentence naming the workday scenario being simulated (Pattern A). NOT "this demo proves seam X composes with seam Y."
- [ ] **§3.2 — Stakes named.** One sentence saying what would go wrong if this didn't work (Pattern E). The stakes must be in workday terms, not engineering terms.
- [ ] **§3.3 — Artifacts visible.** Any artifact the workflow operates on (a code snippet, a PR, a work item, a manifest) is shown to the operator at least once. Not just referenced by path.
- [ ] **§3.4 — Verdict card.** The demo ends with a verdict card (Pattern C) in customer English. If there's no verdict-shaped outcome, the demo ends with a `Status:` line in customer English describing what was achieved.
- [ ] **§3.5 — Engineering chrome is `--debug` only.** Seam variant labels (`Stravinsky B + ...`), graph metrics (`9 nodes, 11 edges`), runtime telemetry (`projection: { ... }`), and raw event-log paths are HIDDEN by default. They appear behind a `--debug` or `--verbose` flag.

### During the demo (the engine's job)

- [ ] **§3.6 — Live narration in customer English.** Every event renders as one human-readable line describing what happened in workflow terms (Pattern B). NOT a JSON dump. NOT a category-name string. If a renderer doesn't exist for an event kind, the demo MUST add it before shipping. **No event kind appears in customer output without a renderer.**
- [ ] **§3.7 — Retries told as a story, not a sequence.** When a node retries, the narration says so: `🔁 <node>: <reason> — retrying (attempt 2)` ... `✓ <node>: succeeded on attempt 2`. NOT `node_entered flaky_lint, node_entered flaky_lint` on two lines.
- [ ] **§3.8 — Gates show what they're asking about.** A human gate displays the **content of the decision** (the verdict text, the diff, the plan), not just the prompt + options. NOT `[gate] options: ('approve', 'reject')` with no context.
- [ ] **§3.9 — Auto-resolved gates are flagged.** If the demo auto-handles a gate, it must say so (`auto-approving for demo`). Daniel must never wonder whether *he* approved or whether the demo did.
- [ ] **§3.10 — Time-to-meaningful-output ≤ 5 seconds.** Daniel should see the first customer-facing line within 5 seconds. Engine startup, dependency loading, etc. happen behind a single "Starting..." line. Long pre-amble = lost attention.

### Post-demo (artifact quality)

- [ ] **§3.11 — README opens with the vignette.** The README's first paragraph is the same workday vignette the terminal output starts with. NOT "this demo proves Phase A composes."
- [ ] **§3.12 — README's TOC includes a "What does this mean for my workday?" section.** That section answers Daniel's question, not the squad's.
- [ ] **§3.13 — One-screen success path.** The README's "what you'll see" section fits in one terminal screen (~40 lines) and matches what actually appears verbatim. If it doesn't match, the README is stale.

### Hard fails (immediate "do not ship")

- ❌ Output contains a JSON dump as the *primary* communication of a result.
- ❌ The terminal closes without showing a verdict or status line in customer English.
- ❌ A gate is auto-resolved without showing what was being decided.
- ❌ Seam names / variant letters appear in default output without a `--debug` flag.
- ❌ Daniel has to `cat` or `jq` a file to find out what the demo did.
- ❌ The README spends more space on "what this proves about the architecture" than "what this does for the operator."

---

## 4. Implications for Verdi-2's CLI

Verdi-2 is promoting the engine to `src/requiem/` with a real CLI: `requiem run <workflow>`, `requiem events <run_id>`, etc. **The CLI must be the first surface that obeys the Demo Contract.** Specifically:

### §4.1 — `requiem events <run_id>` is the demo-rendering primitive **[BET]**

Every demo's live narration (§3.6) is *literally the output of* `requiem events <run_id> --follow`. The CLI and the demo share one renderer. This means:

- One place to fix bad rendering instead of N demo scripts.
- The renderer is testable (same input → same output).
- When the UI ships, it consumes the same render-function output as text-or-JSX.

### §4.2 — Default rendering is customer English; `--raw` is JSON **[BET]**

```
$ requiem events demo-1780287814
▶ run_started — code-review on sample_snippet.py
▶ Read sample_snippet.py (7 lines)
🔁 Lint failed: linter OOM — retrying (attempt 2)
✓ Lint passed on attempt 2
▶ Started 3 reviewers in parallel
  ✓ style_reviewer: warn — mutable default cache={} leaks state
  ✓ correctness_reviewer: blocking — unhandled ValueError on int(x)
  ✓ performance_reviewer: info — cache.keys() could be O(1)
✓ Synthesized verdict — don't merge (1 blocking, 1 warn, 1 info)
🚦 Gate: approve verdict? (auto-approved for demo)
✓ Wrote summary to .runs/demo-1780287814.summary.md
■ Completed — code-review on sample_snippet.py
```

```
$ requiem events demo-1780287814 --raw
{"run_id":"demo-1780287814","ts":"...","kind":"run_started", ...
{"run_id":"demo-1780287814","ts":"...","kind":"node_entered","node_id":"start", ...
... (the existing .events.jsonl, line for line)
```

### §4.3 — Renderer registry: one renderer per event kind **[BET]**

The renderer is a `dict[EventKind, Callable[[Event], str]]`. Adding a new event kind without a renderer is a **lint error** (the test suite refuses to ship a kind without a render function). This forces customer-facing thinking on every engine PR that adds an event.

Suggested API for Verdi-2:

```python
@render.register("node_entered")
def _(event: NodeEntered) -> str:
    return f"▶ {humanize(event.node_id)}"

@render.register("retry_attempted")
def _(event: RetryAttempted) -> str:
    return f"🔁 {humanize(event.node_id)}: {event.reason} — retrying (attempt {event.next_attempt})"
```

The renderer also receives an enrichment context (the workflow's `humanize()` map, the artifact under review, etc.) so it can produce workflow-aware output, not just kind-name-aware output.

### §4.4 — `requiem run` streams renderer output by default

The walking-skeleton-α demo's `demo.py` would become (roughly):

```python
result = await requiem.run("code-review", input=sample_snippet)
# ... and during the run, the CLI prints the renderer output live
# at the end, prints the verdict card
```

The customer demo is `requiem run code-review sample_snippet.py`. **No demo script needed.** That fact alone is the strongest signal Verdi-2's CLI can send Daniel.

### §4.5 — Glyphs and colours: one alphabet for the whole field

Map to the 5 discriminated-outcome variants (`NS:INV-DISCRIMINATED-OUTCOMES`):

| Glyph | Meaning | Outcome variant | Color (when terminal supports it) |
|---|---|---|---|
| `▶` | starting | (action, not outcome) | dim |
| `✓` | success | `Success` | green |
| `🔁` | retry attempted | `RetryableFailure` → retry | amber |
| `🚦` | needs human | `NeedsHuman` | amber, pulsing if live |
| `✕` | failed permanently | `PermanentFailure` | red |
| `■` | terminated cleanly | `Cancelled` or `Completed` | dim |

This is the **CLI alphabet** that maps 1:1 to the **UI color vocabulary** (`03-ui-pattern-catalogue.md` § Strongest signals #3). One alphabet, two surfaces.

### §4.6 — `requiem run` exit code maps to outcome

- `Completed`/`Success` terminal → exit 0
- `NeedsHuman` (suspended, no handler) → exit 2
- `Cancelled` → exit 130 (POSIX convention)
- `PermanentFailure` → exit 1

This lets `requiem run` compose into scripts (CI, batch dispatch, etc.) without parsing output.

---

## 5. What I'd ship next, concretely

If I were dispatching Phase B's walking skeleton, I'd ask the dispatcher to:

1. **Pick a workflow from the parity inventory** (`PI:§2`) — `close-out` is the smallest (`docs/roadmap.md` Phase B already picks it). Real ADO read; observation generation.
2. **Build the renderer** (§4.3) for the events `close-out` emits, BEFORE writing any demo script.
3. **Write the demo as one command:** `requiem run close-out --work-item AB#3287`.
4. **Verdict card at the end:** the observations written to ADO (or the diff that would be written), in customer English.
5. **Pre-flight the Demo Contract checklist (§3) with whoever reviews the demo before showing Daniel.** If any box fails, the demo doesn't ship.

The α demo proved Phase A composes. The β demo must prove Daniel can drive Requiem through one real task and tell, by *looking*, that it was Requiem doing the work — without anyone explaining what they're looking at.

---

## 6. Strongest signals (one-screen)

1. **Default vocabulary is the customer's, not the engineer's.** Seam names and envelope kinds are `--debug` only.
2. **The CLI's event renderer is the demo's narration engine** (§4.1) — one place to get right, one place to test.
3. **Every demo opens with a workday vignette and ends with a verdict card.** Engineering chrome lives in the middle, hidden.
4. **No event kind ships without a renderer** (§4.3). Add to lint.
5. **The glyph+color alphabet (§4.5) is shared with the future UI.** Five outcome variants → five glyphs → five colors. One alphabet for the whole project.
