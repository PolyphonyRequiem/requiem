# 05 — Forward-looking patterns to defer

> Patterns and ideas I find compelling but that I think should explicitly NOT be designed for v0. Each entry has rationale + a "revisit when" trigger so we don't lose them.

The discipline of *not building* matters (`DD:§7` makes this point on the engine side; same principle applies on the UI side).

---

## D1 — Workflow-authoring GUI

**The pattern:** drag-and-drop workflow editor (Step Functions Workflow Studio, n8n editor canvas). Operator builds workflows visually.

**Why defer:** explicitly out of scope per `NS:§5`. Workflows are authored in code. The UI shows execution, not authoring. Building a GUI authoring tool is a 6-month side quest that doesn't move parity forward.

**Revisit when:** never, probably. Or only after Requiem has shipped v1 and there's evidence that non-engineer users would benefit. Single-power-user audience does not need it.

---

## D2 — Chat-as-UI / "talk to your workflow"

**The pattern:** a chat panel where the operator types "show me the failed runs from yesterday" and an LLM translates to engine queries. ChatOps for orchestration.

**Why defer:** the inbox + trace + diagnose verdict cover the 95% case without LLM latency or hallucination risk in the *operator* surface. Adding LLM mediation between operator and engine **violates `INV-NO-CORRUPT-FORWARD` in spirit** — the engine becomes ambiguous about what the operator asked for.

**[BLUE-SKY] when this might come back:** as an **adjacent**, never-load-bearing surface. e.g., a "summarize what happened in AB#3287" affordance that produces prose from the event log, with the structured trace still being the source of truth. Strictly augmentation, never substitution.

**Revisit when:** post-v0, and only if dogfood shows operator time spent in cross-run summarization exceeds 30 min/week.

---

## D3 — Mobile / responsive UI

**The pattern:** the dashboard works on a phone.

**Why defer:** the target audience is one operator on a 4K monitor (`NS:§5`, `WV:` audience constraint). Mobile is the wrong shape for the trace view; mobile is the wrong shape for the inbox at scale. The cost of responsive is 2x design + 2x test surface for zero target-audience benefit.

**Revisit when:** post-v1, if Daniel ever asks for "approve gates from my phone." That use case alone could justify a *gate-only* mobile surface (read inbox, respond to gates, never see the trace).

---

## D4 — Multi-operator collaboration

**The pattern:** two operators viewing the same run; presence indicators; collaborative gate approval.

**Why defer:** explicitly out of scope per `NS:§5`. Single-operator target.

**Revisit when:** never for v0. If post-v0 the project gets adopted by a team, revisit at that point with full re-design.

---

## D5 — Cross-run analytics dashboard (Airflow Grid / Dagster Insights equivalent)

**The pattern:** time-series charts of success rates, retry frequencies, gate dwell times across weeks of runs.

**Why defer:** valuable for post-v0 pattern recognition (rough edge X6) but not v0. v0 needs the inbox + trace + per-run detail. Analytics is a refinement that requires the data to *exist* first.

**Revisit when:** Phase D / post-parity. **Critical:** make sure the event log schema (Q1 in `04-sdlc-open-questions.md`) is rich enough that analytics can be retrofitted without persistence changes. If we drop event metadata to "save space," we lock ourselves out of this entirely.

---

## D6 — AI-generated summaries of runs

**The pattern:** at run completion, an LLM reads the event log and generates a prose summary. Same for failures: "this run failed because the architect agent decided to seed 7 children, which exceeded the depth-guard threshold."

**Why defer:** the "today" view in `01-feel-of-the-loop.md` § 17:00 is the *structured* equivalent (derived from event log). It's deterministic and free. AI summaries are an enhancement that introduces hallucination risk in a surface where `INV-NO-CORRUPT-FORWARD` matters less (post-hoc summary is low-stakes) but where Daniel might come to over-rely on the summary and miss anomalies.

**Revisit when:** post-v0, **only** if (a) the structured "today" view proves insufficient for end-of-day comprehension and (b) the AI summary can be sandboxed (e.g., it never appears as the *only* source of information about a run; the structured log is always one click away).

---

## D7 — Cross-repo / cross-project dashboard

**The pattern:** a single Requiem instance manages SDLC for multiple repositories; the UI has a repo selector.

**Why defer:** v0 is single-repo + single-operator. Multi-repo introduces auth, RBAC, namespace, and routing concerns that explode scope.

**Revisit when:** post-v1, **if** Daniel ever wants to run Requiem against both `polyphony` and `cloudvault` (or whatever the second target is) simultaneously. Probably easier to spin up two Requiem instances on different ports.

---

## D8 — Plugin / extension marketplace

**The pattern:** third-party verb registries, community workflows, etc.

**Why defer:** `NS:§5` lists this as explicitly out of v0. Premature.

**Revisit when:** never for v0. Only meaningful after v0 stabilizes and there's a user base larger than 1.

---

## D9 — Time-travel scrubber

**The pattern:** drag a slider on the trace and the detail panel shows the world as it was at that event. (Mentioned in `01-feel-of-the-loop.md` § 15:30 as a `[BLUE-SKY]`.)

**Why defer:** the killer feature for AI debugging, but **expensive** to implement correctly. Requires every projection (manifest, branch state, worktree state) to be reconstructible at any event ID. The infrastructure (event log) supports it; the projections need to be designed for it.

**Revisit when:** Phase D or post-v0. **Critical:** make sure projections in the meantime are written as **pure functions of the event log up to event N**, not as imperative mutations. If projections are imperative, scrubbing is impossible to add later. This is a constraint to put on the **Bach** persistence seat now, even though scrubbing is deferred.

---

## D10 — Live editing of agent prompts

**The pattern:** during a run, the operator edits the prompt that's about to be sent to the next agent.

**Why defer:** violates `INV-RESTART` (you can't replay a run if prompts were mutated). Also probably violates `INV-EVENT-LOG-AUTHORITATIVE` because the prompt that ran wouldn't match the prompt in any saved file.

**Revisit when:** never as live-edit. **Maybe** as a "fork this run with modified prompts" affordance post-v0 — fork creates a new root with the prompt change recorded as part of the fork event, and the original run is untouched. This composes with the invariants instead of fighting them.

---

## D11 — Notification escalation

**The pattern:** if a gate is unanswered for >N minutes, send a louder notification (SMS, page).

**Why defer:** the single-operator target means there's no escalation tier. If Daniel didn't see the toast, sending an SMS to Daniel doesn't help.

**Revisit when:** post-v0, only in conjunction with D4 (multi-operator).

---

## D12 — Inline visual diff of code changes in the trace

**The pattern:** when the coder agent produces a diff, show the syntax-highlighted diff inline in the trace, not as a link to GitHub.

**Why defer:** lovely affordance but materially expensive (need a diff renderer, language detection, syntax highlighting). v0 can punt to "open in GitHub" buttons; v1 can do inline.

**Revisit when:** Phase D, after parity. Genuinely high-value once parity is stable.

---

## Meta: how to keep this list honest

These deferrals are easy to violate piecemeal because each one is individually small. The discipline:

- Every deferred item that gets added back must explicitly cite *which v0 capability is now stable enough that the deferral can be lifted*.
- Adding a deferred capability "because Daniel asked for it during a demo" is a strong signal — but it should still trigger an ADR-style decision, not a quiet PR.
- The list above is meant to be *prescriptive*. If a seat is tempted to add something on this list mid-stream, that's a signal to stop and have a conversation, not a green light.

[BLUE-SKY] one last thought: the most dangerous deferral above is **D9 (time-travel scrubber)** because deciding to defer it correctly *now* requires foreknowledge that projections must be pure functions. If we defer without that constraint, we make scrubbing impossible later. So the deferral isn't "do nothing now"; it's "do the cheap constraint now so the expensive feature is buildable later." This pattern — *cheap-now-constraint to keep an expensive-later-feature on the table* — is worth applying to every item on this list. For each deferral, ask: what's the cheap thing today that keeps the option open?
