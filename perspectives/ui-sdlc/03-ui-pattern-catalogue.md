# 03 — UI pattern catalogue, mapped to rough edges

> Every concrete UI pattern from `platespinner-survey.md` (`PS:`) and `workflow-viz-research.md` (`WV:`), with a recommendation: borrow / adapt / reject for v0. Patterns are tagged with the rough edge (`02-polyphony-rough-edges.md`) they address.

Recommendation labels:
- **`[BORROW]`** — take it largely as-is, well-suited to Requiem
- **`[ADAPT]`** — the shape is right but Requiem's context (AI-native, single-operator, single-process) requires modification
- **`[REJECT]`** — doesn't fit, would actively hurt, or is misaligned with an invariant
- **`[DEFER]`** — good idea but post-v0 (see `05-forward-looking-deferred.md`)

---

## From the platespinner survey

### PS-1 — Trace-first, path-based concurrency rendering (`PS:§4`)

A linearized trace with collapsible scopes, **not** a node-edge graph as the primary view. Path-based traversal so concurrent branches interleave correctly.

**Verdict: `[BORROW]`** [BET]

Addresses: I1 (MG branch comprehension), I2 (loop iterations), R1 (subworkflow opacity).

Why this is the right primary view for Requiem:
- The polyphony workload is **deeply nested**: root → plan-level → actionable → implement-merge-group → coder-reviewer-loop. A node-edge graph at that depth produces unreadable spaghetti. Trace-with-scopes naturally collapses depth.
- The viz research (`WV:Temporal §1`) confirms log-first is the right choice for long-running workflows where the topology is less interesting than the timeline.
- Platespinner has already validated this works on real polyphony runs (`PS:§4 path-based concurrency handling`).

What needs to change for Requiem:
- The trace must bind to the **in-memory event stream**, not the on-disk `.events.jsonl` (`ADR-0001` consequences). No tailing.
- Scope rows must show the **discriminated outcome variant** as a pill (Success/RetryableFailure/PermanentFailure/NeedsHuman/Cancelled) — that pill is the *only* state vocabulary the trace uses (`NS:INV-DISCRIMINATED-OUTCOMES`).

### PS-2 — Scope headers with focus-as-navigation (`PS:§3`)

Click a scope to focus/expand it; the focus trail behaves like browser history.

**Verdict: `[BORROW]`** [BET]

Addresses: R1 (drilling into subworkflows without losing context).

Why: focus-as-navigation is a learned-once-applies-everywhere pattern. Daniel uses browser back/forward constantly; reusing the mental model is free.

### PS-3 — Gate chips with jump-to-next on scope headers (`PS:§3`)

A scope header shows pending gate counts; clicking cycles through gates.

**Verdict: `[ADAPT]`** [BET]

Addresses: G1 (gate discovery), G3 (signals lost to parent scope).

What to keep: the "gate chip is a peer of the scope header, not buried in the row body" idea.

What to change: in Requiem the **inbox** is the primary gate-discovery surface, not the per-scope chip. The chip remains as a *secondary* discovery affordance for when the operator is already focused on a specific run. Don't make the chip the only way — that puts the burden on the operator to be looking.

### PS-4 — Optimistic gate resolution (`PS:§3`)

UI collapses the gate immediately on submit, before server confirmation.

**Verdict: `[BORROW]`** with a caveat.

Why: latency hiding is correct UX in 99% of cases.

The caveat: gates that mutate durable state (e.g., "approve plan PR merge") must NOT be optimistic in the dangerous sense — the UI should show "submitting..." for the half-second the verb takes, and if the verb fails, the gate must reappear with the failure reason. Optimistic ≠ assumed-successful. (This is the UI corollary of `INV-NO-CORRUPT-FORWARD`.)

### PS-5 — Live pulse / DurationTicker (`PS:§4`)

Subtle "streaming live" cue at the trace tail.

**Verdict: `[BORROW]`** [BET]

Addresses: K1 (the "is it actually running?" anxiety).

Almost free to implement; high feel-of-life payoff. (`WV:Step Functions §2` "real-time state-colour overlay" is the spatial equivalent on a graph canvas; the live pulse is the trace equivalent.)

### PS-6 — Suppress agent start/end for `human_gate` (`PS:§8`)

The gate card is the single visible surface for human gates; redundant agent rows are hidden.

**Verdict: `[BORROW]`** [BET]

Addresses: noise reduction across the trace.

Apply the same de-duplication principle to every node category where there's a higher-order surface: `script` nodes that exist purely to emit a domain signal could be collapsed into the signal row, for example.

### PS-7 — List-detail shell (run list left, run detail right) (`PS:§3`)

**Verdict: `[ADAPT]`** [OPTION]

The Requiem split (covered in `01-feel-of-the-loop.md`) is three-zone, not two. The "run list" lane is replaced by the **inbox**, which is action-oriented rather than topology-oriented. A list of all runs (active + recent) belongs in a secondary tab or a collapsible sidebar, not as a permanent peer of the run-detail view, because the inbox already collapses to the "runs that need you right now" subset.

### PS-8 — Sticky ancestor headers (`PS:§3`)

When drilled deep, ancestor scopes remain visible as navigation spine.

**Verdict: `[BORROW]`**

Standard "breadcrumb that doesn't go away" pattern; cheap and high-value at polyphony's nesting depth.

### PS-9 — Tray supervisor / PWA / in-app update (`PS:§5 (c), §8`)

**Verdict: `[DEFER]`** post-v0.

Justification: nice for distribution but not necessary for v0. Daniel runs everything locally; a regular browser tab is fine. Tray supervisor + PWA is a "make this a product" feature, not a "make this a tool" feature.

### PS-10 — Enrichers (`PS:§5 (a)`)

Plugin loader for ADO/git metadata enrichment.

**Verdict: `[BORROW]`** [BET]

The enricher pattern is the right shape for keeping UI rendering decoupled from ADO/git specifics (the seam research already shows this concern, `PS:§8 "enrichment is opt-in and read-only"`). Lift the pattern; the actual enrichers will be Requiem-specific.

---

## From the workflow-viz 10-system survey

### WV-1 — Temporal Compact view: retry grouping with attempt expansion (`WV:Temporal §5`)

One row per logical activity with an `Attempt N of M` badge; expand to see each attempt as a sub-row.

**Verdict: `[BORROW]`** [BET]

Addresses: I2 (loop iterations), and retry visibility generally.

Why: this is the **best density-efficient retry rendering in the field**. The viz research explicitly recommends it for Requiem (`WV: § Recommendation`). It composes naturally with the platespinner trace model — retry attempts become collapsible sub-scopes.

### WV-2 — Step Functions amber `.waitForTaskToken` colour (`WV:Step Functions §2`)

A distinct amber state for "paused, waiting for an external callback."

**Verdict: `[BORROW]`** [BET]

Addresses: G2, G3, the entire human-gate visual treatment.

This is the canonical color for `NeedsHuman` outcomes. Paired with a pulsing animation (so it's distinguishable from a static amber failure-state, important for colour-blind users — `WV: § Recommendation`).

### WV-3 — Step Functions real-time state-colour overlay on a static graph canvas (`WV:Step Functions §2`)

The graph topology is static; execution state is painted on as colour.

**Verdict: `[ADAPT]`** — graph canvas is `[DEFER]` to post-v0 as a *primary* view; the *principle* (static topology, dynamic state overlay) is `[BORROW]`.

We don't need a graph canvas in v0 — the trace view (PS-1) is the primary. But the principle that **topology never reshuffles during execution** is load-bearing for any future graph view. Bake into the event schema: scope structure is fixed at scope-entered time and doesn't mutate.

### WV-4 — Airflow Grid View (task × run heatmap) (`WV:Airflow §3-4`)

A 2D matrix: rows = tasks, columns = runs, cells coloured by terminal state.

**Verdict: `[DEFER]`** post-v0, but **bake support into the event-log schema now**.

This is the cross-run pattern-recognition surface (X6) and the viz research's #1 density recommendation for an 8-hour/day operator. It's not v0 because v0 needs the inbox + trace; cross-run analytics is a refinement. But the event log must carry enough metadata (workflow name, root_id, task identity, terminal outcome) that a future Grid View can derive itself without persistence changes. **Action for the engine seats**: keep this in mind when finalizing the event schema (Brahms-events seat in particular).

### WV-5 — Airflow Gantt view (`WV:Airflow §4`)

Per-run horizontal bars showing parallel execution and wall-clock spans.

**Verdict: `[DEFER]`** post-v0.

Useful but not first-day. The trace view already shows ordering and duration inline; Gantt is the densification for advanced debugging.

### WV-6 — Airflow log side panel with structured streaming logs (`WV:Airflow §7`)

Click task → side panel with paginated, ANSI-coloured log stream.

**Verdict: `[ADAPT]`**

Borrow the side-panel pattern; the *content* of the side panel for Requiem is **not** raw ANSI logs — it's the structured event/prompt/response/receipts triple (covered in `01-feel-of-the-loop.md` § 15:30). Polyphony today has very few "raw log streams" per agent; what it has is structured agent I/O. Treat that as the log.

### WV-7 — Dagster staleness indicator (`WV:Dagster §2`)

Yellow/amber "asset upstream changed, needs re-run" indicator.

**Verdict: `[BLUE-SKY] / [DEFER]`** post-v0.

Asset-graph semantics aren't a natural fit for polyphony's process model (we're not asset-centric, we're work-item-centric). But the **concept** of "this thing is stale because something it depends on changed" could be transferred to **work items**: "AB#3287's plan is stale because AB#3401 (parent) just changed scope." Renegotiation today is implicit and runs detect drift; a staleness indicator would make it explicit. Worth revisiting in Phase D.

### WV-8 — Dagster structured queryable logs (`WV:Dagster §7`)

Logs as data (timestamp + level + step_key + message), filterable.

**Verdict: `[BORROW]`** [BET]

Addresses: C3 (post-run forensics), X3 (manifest-as-JSON).

This aligns with `NS:INV-EVENT-LOG-AUTHORITATIVE`: the event log **is** the structured log. Filter by node/scope/outcome variant/domain-signal kind. The Dagster pattern + the polyphony event vocabulary compose directly.

### WV-9 — Argo per-attempt child nodes (`WV:Argo §5`)

Each retry rendered as a distinct child node in the DAG.

**Verdict: `[REJECT]`** in favour of WV-1 (Temporal Compact).

Argo's approach clutters at high retry counts. The viz research explicitly prefers Temporal Compact for the single-operator case (`WV: § Controversial #2`).

### WV-10 — Prefect typed-input gate forms from Pydantic schema (`WV:Prefect §8`)

`pause_flow_run(wait_for_input=MyModel)` auto-generates a form in the UI from the type annotation.

**Verdict: `[BORROW]`** [BET]

Addresses: G2 (decision vs acknowledgement gates), and the entire human-gate "what am I being asked?" surface.

Why this is **the** killer pattern for Requiem:
- Single-process Python (`ADR-0001`) means the workflow author can declare a Pydantic model and the UI backend can introspect it without any extra protocol.
- The viz research explicitly identifies this as the best human-input UX in the entire field (`WV:Prefect §8`).
- It composes with `INV-NO-ENGINE-ABANDONMENT`: surrender gates are typed forms asking the operator to choose ("retry / supersede / cancel / mark-failed"), each with its own required fields.

Implementation note for the wagner/dsl-shape seat: the DSL should make typed-gate-declaration a one-line affordance. Don't make this hard to author.

### WV-11 — n8n tabular data inspection panel (`WV:n8n §7`)

Side panel with input/output as a sortable table; each item in node output is a row.

**Verdict: `[ADAPT] / [BLUE-SKY]`**

For Requiem, the analogue is "show me every artifact this agent inspected" (i.e., receipts as a table). Same shape, different content. Worth prototyping when the receipts pattern is widespread enough that a row count matters.

### WV-12 — GitHub Actions notification + review-button approval (`WV:GitHub Actions §8`)

Reviewer gets a notification with approve/reject buttons.

**Verdict: `[BORROW]`** for the OS-notification side; the in-UI approval surface comes from Prefect (WV-10).

Addresses: G1 (gate discovery from outside the tool).

### WV-13 — Conductor HUMAN task type with separate "Human Tasks Inbox" (`WV:Conductor §8`)

A separate dedicated UI for human tasks across workflows.

**Verdict: `[BORROW]` the concept, but it's not separate** — it's *the inbox*, integrated into the main UI (covered in `01-feel-of-the-loop.md`). Conductor splits it into a separate app because they have a team-of-approvers audience; we have a single-operator audience and integration is correct.

### WV-14 — VS Code Notebook output-below-cell + named-terminal-tabs (`WV:VS Code §1-2`)

Output spatially adjacent to the producing code; named tabs per worker.

**Verdict: `[ADAPT]`** [BET]

The "output below the producing step" mental model maps directly to the trace view: a node row, and immediately below it as collapsible content, the node's output / receipts / prompt. No detail-panel context-switch for the common case.

This is a hybrid of platespinner's collapsible scopes and VS Code's notebook locality. Worth prototyping in the v0 walking skeleton.

---

## Patterns NOT in either source that Requiem needs

Briefly, patterns the surveys didn't surface but that the deep dive implies we need:

### N1 — The Diagnose Verdict Card

`DD:§2.6` (the `polyphony run diagnose` verb) has no canonical UI in any of the 10 systems surveyed (none of them have a comparable concept). For Requiem:

[BET] — when a run terminates non-cleanly, the run page auto-renders a **Diagnose Verdict Card** at the top:

```
🛑  Run AB#3287 — DO NOT RESUME
    state_drift_detected at 17:42
    manifest hash mismatch on .polyphony/state/3287/seed-manifest.json
    recommended: polyphony reconcile --root 3287 to converge state
    [run diagnose]   [run reconcile]   [open manifest diff]
```

This is the UI form of `INV-NO-CORRUPT-FORWARD`. Reads from the journal; offers the only safe next actions; no `[resume]` button until verdict ✅.

### N2 — Prompt diff against previous run

Covered in `01-feel-of-the-loop.md` § 15:30. No system in the survey does this because none are AI-native. **[BET]** — this is the single biggest UI bet I'd make for AI debugging.

### N3 — The Receipts Badge

Covered in `01`. A `📎 N` badge on every reviewer agent row showing how many artifacts were inspected; absent badge = potential hallucination. No analogue in the survey.

---

## Strongest signals

If Daniel reads only one section of this file, it should be this one.

1. **The trace view (PS-1) is the primary view, full stop.** Not a graph canvas. The viz research and the platespinner production deployment converge on this for long-running, deeply-nested workflows. Don't relitigate.

2. **The inbox is the second primary view, peer to the trace.** Everything else (run list, metrics, history) is secondary or post-v0. The inbox-always-visible decision is what makes multi-run-in-flight feel manageable.

3. **The five discriminated-outcome variants (`INV-DISCRIMINATED-OUTCOMES`) are the entire visual vocabulary.** Green/blue/amber/red/grey, mapped 1:1 to Success/RetryableFailure/NeedsHuman/PermanentFailure/Cancelled. No other color allocations. This makes UI engineering nearly free and color-blind support natural.

4. **Typed-schema gate forms (WV-10 Prefect pattern) are the killer human-gate UX.** This single decision puts Requiem ahead of every system in the survey for AI-agent-approval workflows.

5. **Receipts must be visible on the canvas, not buried.** Hallucinated success (I3) is the highest-blast-radius failure mode and only a visible-by-default receipts surface defends against it operationally.
