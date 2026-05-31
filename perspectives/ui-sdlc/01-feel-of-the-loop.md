# 01 — What driving Requiem should feel like

> A texture document. Written before any prototype exists, deliberately. The goal is to fix the *target sensation* so seam decisions can be evaluated against it.

---

## The headline feeling

**It should feel like driving a car, not flying a helicopter.**

Polyphony today feels like a helicopter: many simultaneous inputs (TTY launcher, conductor dashboard tab, twig CLI, raw `.events.jsonl` tail, git log, gh CLI, the manifest JSON I keep `cat`'ing). Each input is necessary because no single surface is authoritative. The cognitive cost of integrating them is what makes an 8-hour day feel like a 12-hour day.

A car has: one wheel, one accelerator, one brake, one dashboard. The dashboard has many gauges but they're all *on the dashboard*. The driver's eyes stay forward.

Requiem's analogue:
- **One wheel** — one place to kick off, pause, cancel, resume runs.
- **One dashboard** — one surface showing every active run and the next thing each needs.
- **Many gauges** — the surface contains drilldowns (logs, receipts, retries, diffs), but they're inside the dashboard, not on a different tab in a different process.
- **Eyes forward** — the operator should rarely have to leave Requiem to do Requiem-mediated work. (Leaving for `git`, `gh`, code editing — fine. Leaving to *understand what Requiem is doing* — broken.)

That image is the north star for this entire perspectives doc.

---

## A workday, hour by hour

### 09:00 — open the laptop

Polyphony today: `Invoke-PolyphonySdlc.ps1 -Intent dispatch -RootId 3401`, wait, two terminal windows + a browser tab open. Conductor dashboard takes ~6s to show anything because it has to parse `.events.jsonl`.

Requiem [BET]: one URL bookmarked. The page loads instantly with the **inbox at the top** (`PS:§3` gate chips concept, generalized). Inbox rows:

```
🚦  AB#3401  Plan reviewer requesting clarification on RFC scope        [resolve]
⚠️  AB#3287  Retry budget exhausted on github-pr.merge — needs decision  [surrender]
🛑  AB#3199  state_drift_detected last night; safe to resume?             [diagnose]
✅  AB#3175  closed-out cleanly at 23:14                                  [archive]
```

The inbox is the *only* thing on screen that requires action. Everything else is below the fold. This collapses Daniel's `git log -p .polyphony/state/`-style morning scan to one glance.

Source: `NS:INV-EVENT-LOG-AUTHORITATIVE` (the inbox is a projection); `DD:§5 S1.9` (`polyphony run diagnose` produces the verdict that drives the 🛑 row); `WV:GitHub Actions` (notification + review-button pattern for gates).

### 09:15 — kick off a new root

Today: `twig set 3450; twig sync; Invoke-PolyphonySdlc.ps1 -Intent dispatch -RootId 3450`. Wait 30s for preflight. Get a "started" line. Switch to the browser. Refresh until the dashboard catches up.

Requiem [BET]: a "+ start root" button in the inbox header. Form is **one field** (work-item ID) and a "preflight" checkbox (default on). Pressing submit pushes me into the run page **immediately** — preflight events stream in as they happen because the UI is reading from the same in-memory event stream the engine is writing to (`ADR-0001` collapses the polling lag).

The feel I want: **the cursor never blinks at a black terminal waiting for a verb to finish.** If the verb takes 30s, the verb takes 30s — but the UI is alive the whole time, showing me `preflight_started`, `manifest_loaded`, `worklist_built`, etc. as discrete steps.

Source: `ADR-0001` consequence (engine + UI backend in one process, no file-tail lag); `PS:§4` (live pulse / DurationTicker is already a proven pattern).

### 10:30 — three runs in flight

Mid-morning: AB#3450 has spawned three child plans, each at the implement stage. The dashboard needs to show me **three concurrent legs without making me click between them**.

This is where I have a strong opinion and a wild one.

**[BET]** — primary view is **split-primary**, three zones:
1. **Left 50%**: per-run trace-with-collapsible-scopes (the platespinner LabsTracePlayground model, `PS:§4`).
2. **Right top 30%**: the inbox (always visible, badges live-update as gates open/close).
3. **Right bottom 20%**: a detail/output panel pinned to whatever I last clicked.

The inbox-always-visible pattern is the single decision that makes "three runs in flight" feel manageable. The viz research (`WV:`) cross-cutting pattern #4 is "multi-view switching for different time horizons" — the inbox is implicitly a Grid View collapsed to one column (the column is "next thing each run needs").

**[BLUE-SKY]** — a fourth "ambient" surface: a 1-pixel-wide vertical strip on the far left of the screen showing the live event stream as colored ticks (green/blue/amber/red). At normal density it looks like a stripe of teletext. The point isn't to read it — it's so peripheral vision detects an unusual color (sudden red, sustained amber). This is borrowed from monitoring dashboards (think Datadog's heartbeat strip), not orchestration UIs.

### 12:00 — a gate pops up while I'm in another window

This is the **failure mode I most want to design out**. Today: the conductor dashboard's badge updates, but if I'm in VS Code I don't see it. The gate sits unanswered for 47 minutes. The run effectively stalled because *Daniel was the limit*.

[BET]: Requiem must have an OS-level notification surface (Windows toast, macOS banner). The notification text must be the *gate prompt itself*, not "a gate is pending." If the prompt is "approve plan PR #3450 for AB#3401?" the toast says exactly that with `[approve] [reject] [open]` buttons. Clicking `[open]` lands me in the run page focused on that gate (`PS:§3` "gate chips with jump-to-next" generalized to OS notifications).

[OPTION A]: bundle the notifications via Hermes if it's available (the seat-context references Hermes as a channel).
[OPTION B]: just use the browser's Notification API + a system-tray helper, no separate Hermes dependency.

I lean B for v0 because B is one less moving part and Daniel runs everything on one machine. Hermes integration is a post-v0 nice-to-have. (Note: `PS:§5` lists a `supervisor.py` tray pattern from platespinner that's directly liftable.)

### 14:00 — a retry exhausted on the network

The GH PR creation hit transient 5xx three times and surrendered. Today: I find out by reading the `.events.jsonl` after the fact. Or worse, I don't, and AB#3287 just sits in "implementing" forever.

Requiem [BET]: the 5-variant outcome surface (`NS:INV-DISCRIMINATED-OUTCOMES`) **becomes the entire color vocabulary of the UI**. There are no other states.

- `Success` → green
- `RetryableFailure` → amber, with a `↻ 2/3` counter badge (Temporal Compact view, `WV:Temporal §5`)
- `PermanentFailure` → red, sticky, requires explicit dismissal (operator must acknowledge before the row leaves the inbox)
- `NeedsHuman` → amber pulsing border (Step Functions `.waitForTaskToken` treatment, `WV:Step Functions §2`)
- `Cancelled` → grey, with a small `✕` glyph

This means a UI engineer never has to invent a new color for a new error condition. The color **is** the variant tag.

Source: `NS:INV-DISCRIMINATED-OUTCOMES`; `WV:cross-cutting #2` (universal 4-color palette).

### 15:30 — debugging a failed agent

Now I need to know **why the architect agent decided to seed 7 children instead of 3**. Today: I open `.events.jsonl`, find the right `agent_invoked` row, copy the prompt out, find the response, manually diff against expectation.

Requiem [BET]: every agent row in the trace has an `[inspect]` button. Clicking opens the detail panel (right bottom 20%) showing the **canonical prompt** that was sent (post-template-rendering, post-context-assembly), the **response**, the **receipts** (`DD:§2.1` receipts pattern), and a **diff against the previous run's prompt** if this is a re-execution. Diffing prompts is the single highest-leverage debugging affordance I can imagine for AI-heavy workflows and **nothing in the 10-system survey does it** because none of those systems are AI-native.

[BLUE-SKY]: a "scrubber" on the trace. Drag back to event #4823 and the detail panel shows you the world *as it looked at that event*: which files existed, which branches existed, which manifest entries existed. This is feasible because `INV-EVENT-LOG-AUTHORITATIVE` makes the event log the source of truth — you can project the world at any event ID without rerunning. **This is the killer feature** if Requiem can pull it off. It is also probably a Phase D project, not v0.

### 17:00 — end of day

Today: no closeout summary. I rely on memory and git log to know what shipped.

Requiem [BET]: a "today" view at the inbox header. One paragraph: "3 roots completed (AB#3287, #3450, #3175). 1 root surrendered to gate (AB#3199 — manifest corruption suspected, 18:42 ago). Time spent in human gates: 14 minutes. Time spent in agent execution: 6h 22m." This is **derived entirely from the event log**, so it's free once the log is the source of truth. (`NS:INV-EVENT-LOG-AUTHORITATIVE`.)

---

## The smaller textures (each is a section I could expand)

### Cancellation must feel instant

Today: `CONDUCTOR_CANCEL_TOKEN` is a sentinel file with up-to-30s pickup latency (`DD:§2.5`). On Windows that's the *good* path; the bad path is that the verb just keeps going.

Requiem [BET]: cancel is a button. Click. The UI shows "cancelling..." for at most 1 second (the asyncio.CancelledError dispatch loop). Then every running node renders with the grey `✕` glyph. **There must be no path where cancel is queued and the user has to wait for it.** This is `INV-CANCEL-SHORT-CIRCUITS-RETRY` made tactile.

### Receipts should be visible, not buried

`DD:§2.1` is unanimous that mechanical receipts are the anti-hallucination defense. But receipts are useless if they're an internal field nobody sees.

Requiem [BET]: every reviewer-class agent row in the trace shows a tiny `📎 N` badge where N is the count of inspected artifacts. Hover → tooltip lists them ("PR #3450 head commit a1b2c3d, .polyphony/state/3401/seed-manifest.json sha 4f5e..."). Click → detail panel with the full inspection trace. If receipts are missing (`hallucinated_success_gate` triggered), the badge goes red and the gate appears in the inbox with the missing-receipt explanation pre-rendered.

### "Resume vs reset" must never be a guess

`DD:§2.6` (`polyphony run diagnose`) gives a ✅/⚠️/🛑 verdict. Today this is a CLI verb you run before deciding.

Requiem [BET]: in the inbox, a 🛑 row never shows a `[resume]` action. It shows `[diagnose]`. After `diagnose` runs and the verdict is ✅, the row mutates to offer `[resume]`. You cannot click resume on a corrupted run because the affordance literally doesn't exist. This is `INV-NO-CORRUPT-FORWARD` made into a UI invariant.

### Three vocabularies — `INV` says two; the UI is the third audience

`NS:§3` defines three vocabularies (execution events, domain signals, verb outcomes). The UI is the consumer of all three, so the UI also has a *fourth* implicit vocabulary: the visual one (color, icon, position, sound). It must be **strictly derived** from the other three. No UI-only state names. No UI-only error categories. If the UI displays "this run is stuck," there must be a corresponding domain signal (`retry_exhausted`, `state_drift_detected`, etc.) that produced it. This rule is what keeps the UI from drifting into a fourth vocabulary the way polyphony's launcher banner sometimes did.

---

## What "feel" means, made falsifiable

If the UI is right, I expect Daniel to:

1. **Glance at the inbox first thing in the morning and know within 30 seconds what he needs to touch.** Falsifiable: time Daniel from page-load to first-meaningful-decision on three consecutive mornings.
2. **Cancel a run with no anxiety.** Falsifiable: ask Daniel "did you trust that cancel actually worked?" after each cancel for a week.
3. **Resume a run only when it's safe, never by accident.** Falsifiable: count the number of `resume`-then-`reset` whiplashes in a 2-week dogfood window. Target: 0.
4. **Discover a gate within 60 seconds of it opening even when in another window.** Falsifiable: instrument gate-opened-to-operator-noticed times via the OS notification dismissal.
5. **Debug a failed agent run in under 5 minutes** without leaving the Requiem UI. Falsifiable: time the next three "why did the architect do that?" investigations.

These are the success metrics for the *feel* of v0. They are not formal SLOs — they are the things I'd ask Daniel about at the Phase D demo.
