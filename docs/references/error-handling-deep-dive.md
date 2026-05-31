# Error-Handling Deep Dive — Consolidated Report

**Date:** 2026-05-31
**Squad:** Mahler-2, Wagner-2, Bach-2, Liszt-2, Stravinsky-2, Beethoven-2, Brahms-2 (all opus-4.7-high)
**Companion:** Holographic-viability assessment (separate question, Appendix A)
**Scribe:** consolidating across 7 error-handling seats + 1 side investigation
**Daniel's north-star constraints (verbatim, preserved):**

> 1. "BE RESTART FRIENDLY, but don't let corrupt state just move forward."
> 2. "We should stop on network/auth after 3 retries." *(Bach-2 confirms this is already implemented at `AdoClientPolicy.cs:115` + `GhClientPolicy.cs:63` for HTTP transport.)*
> 3. "GitHub Issues is OUT OF SCOPE for polyphony."
> 4. "yes, I agree domain signal vs notification" (Bach's framing).

---

## 0. TL;DR

Seven Opus seats converged with surprising tightness on a single architecture: **error handling is a layered contract** in which (a) HTTP transport already retries 3× and stops on auth, (b) verbs classify failures and exit with a discriminated severity class, (c) workflows route on `on_error: <kind>` with bounded retry that is provably idempotent or refuses to start, (d) agents emit *receipts* so verification can be mechanical, and (e) the journal lets a "reconcile/diagnose" verb tell the operator whether resuming is safe. The system is ~75 % there; the missing 25 % is **classification, schema, and the discipline of failing honestly** — not net-new engine machinery.

### Top 5 highest-leverage actions (ranked by impact × confidence / effort)

| # | Action | Owner | Effort |
|---|--------|-------|--------|
| 1 | **Fix `actionable.yaml` silent-loss bug** (Wagner-2 §3.0): `workflow_abandoned` / `workflow_error_gate` aren't surfaced in the `output:` block. 5-line change; zero conductor dependency. | Wagner-2 / Mozart | <1 day |
| 2 | **Resolve PR #229's 7 conflicts** using Mahler-2's `_MISSING` sentinel patch (§1.3) — context.py substantive, 6 mechanical. Unblocks every downstream Phase 2 design. | Mahler-2 | 2–4 hr (+2 hr for `workflow.py` review) |
| 3 | **Adopt the 5-class script exit-code contract** (Liszt-2 §1.2): `0/2/3/4/5 = success/usage/permanent/transient/corruption`. Migrate the 12 production scripts; codify in lint. This is the one decision that makes `on_error:` routing safe across the script fleet. | Liszt-2 | ~2 hr migration + lint PR |
| 4 | **Ship `polyphony run diagnose`** (Beethoven-2 §5; Bach-2 #4): three-tier verdict (✅/⚠️/🛑) that operationalizes "don't resume into corrupt state." Smallest mission-recentering wedge. | Mozart + Bach-2 | ~1 day |
| 5 | **Adopt the receipts pattern + `verify_receipts` script** for routing agents (Stravinsky-2 §2): `inspected_artifacts` field is the only mechanical defence against hallucinated success. ~150 tokens per output; close to free. | Stravinsky-2 + Wagner-2 | 1 sprint |

### Top 3 architectural insights

1. **Domain signal ≠ channel notification.** Bach's framing is now load-bearing across all seats. The 21-signal closed enum (Beethoven-2 §3) is the polyphony vocabulary; channels (platespinner, Teams, Hermes — never GH Issues) just deliver. Every error-handling proposal in this round is either a *producer* of a signal or a *consumer* of one.
2. **Retry has a layering everyone respects:** HTTP transport (already done, 3×) → script in-call (e.g., `git push --rebase`) → workflow `on_error: + retry:` (Mahler-2's RFC #236) → operator. The cross-cutting requirement is that the *idempotency contract* between layers is explicit (`retry_key:` + `node.idempotent: true`) so a retry can never silently double-mutate. Five seats independently identified this as Daniel's "corrupt state" clause expressed in code.
3. **Receipts > LLM-as-judge.** The seats unanimously rejected adding "a verifier agent that watches the reviewer." Mechanical receipts grounded in observable git/PR state are non-hallucinatable and cost ~150 tokens. The `evidence_floor_check` already proves the pattern works; we just generalize it.

### Open decision asks for Daniel (consolidated — ~12 of the original 50+)

See §6 for the full list with defaults. The four that **block other work** are:

- **D1** Approve the 5-class exit-code contract (Liszt-2 L2-1)? *Default: yes.* Blocks `on_error:` route adoption across script fleet.
- **D2** Push posture on PR #229: do we resolve & push, or wait? *Default: Mahler-2 fixes context.py + 5 mechanical hunks, defers `workflow.py` to a dedicated pass.*
- **D3** Lock the abandonment typology — "engine-initiated abandonment does not exist" as a §7 invariant addition (Beethoven-2 §2.3)? *Default: yes.*
- **D4** Greenlight `polyphony run diagnose` as P1 polyphony-side work (Beethoven-2 §5; Bach-2 #4)? *Default: yes — ~1 day.*

---

## 1. The error-handling story today (synthesis)

A horizontal scan across the seven seats produces a single picture:

| Layer | What works | What fails |
|-------|-----------|------------|
| **(a) Conductor engine** | `on_error:` Phase 1 designed (PR #229, 7 conflicts — fix in Mahler-2 §1); `RetryPolicy` on agent provider transients works (`config/schema.py:377`); `type: terminate` (v0.1.18) gives clean exits | No `on_error:` on `type: workflow` nodes (validator hard-errors); no retry on `script:` nodes (Mahler-2 RFC #236 in flight); subworkflow errors arrive as opaque exception strings; checkpoint keyed by timestamp not root_id; no `CONDUCTOR_CANCEL_TOKEN` for Windows kill semantics |
| **(b) Workflow YAMLs** | `failure_mode: continue_on_error` on `for_each` works mechanically; `polyphony.yaml:98–100` "always exit 0, surface via envelope" convention catches anticipated failures; squash-coverage trust chain (`implement-merge-group.yaml:1102–1148`) prevents AB#3175-class corruption | Single-option human gates routing to `abort_run` (8 of them) pretend to be decisions but are acknowledgements; `actionable.yaml:152–170` silently loses `workflow_abandoned`/`workflow_error_gate` to the parent; 60-line PowerShell aggregator in `root-batch-dispatch.yaml` is a workaround for the missing `subworkflow.*` kind |
| **(c) Scripts** | Watermark-poll discipline in `Poll-PrStateDelta.ps1` is the gold-standard "observe → decide → mutate → record" pattern (Liszt-2 §3); `Sync-BareRepo.ps1` classifies `noop`/`fast_forward`/`squash_reset` before acting | **Three exit-code dialects** in production (`0/2/3/4/5` vs `0/1/2/3/4` vs always-0); 4 real burn cases where masking failures looked like UX decisions; prelude (`Set-StrictMode`, `$PSNativeCommandUseErrorActionPreference`) is set in 6/13 scripts; `Write-Host` (banned — invisible to conductor) is in `abort-run.ps1` |
| **(d) Agent prompts** | `RetryPolicy(max_attempts=3, retry_on=[provider_error, timeout])` handles daily provider 5xx well | Schema drift (verdict: "APPROVED" capital A) and hallucinated success have **zero detection** for reviewer cases; agents have no way to say "I couldn't actually review this"; compaction at 200K context turns occasional hallucination into systematic hallucination (Stravinsky-2 §6) |
| **(e) Durable state** | `ManifestPlanLedger.Apply` (`ManifestCommands.cs:716–885`) is the gold standard for idempotency: records `(prNumber, itemKey, prev_gen, mergeCommit)`, returns `Recorded=false` on replay, detects same-PR-different-commit as a conflict; same-root run lock is OS-atomic (`RunLockStore.cs:90`); journal records start+end+outcome for every verb (`JournalStore.cs:78–91`); `AdoClientPolicy.cs:115` + `GhClientPolicy.cs:63` **already stop after 3 retries on auth** | Checkpoints land in `$TMPDIR` and are keyed by timestamp + random suffix; no `(workflow_name, root_id)` addressability; `pr post-comment-ado` has no marker → would post duplicates if `on_error: retry:` ever fires; `record-rebase`/`record-approval`/`mark-impl-merged` double-append on replay (currently benign) |
| **(f) External remediation** | Human gates exist for most surrender surfaces; `workflow_abandoned` terminal works for operator-chosen abandon | No `workflow_superseded` terminal (conflated with abandon); engine-initiated abandonment is implicitly possible; no `polyphony run diagnose` so resume vs reset is a guess |
| **(g) Testability** | 13 harness scenarios pass; `gates: {gate_name: option}` block proves scripted-gate handler works (AB#3212); `script_failed`, `route_taken`, `subworkflow_started/completed/failed` events all exist already | **None** of the 13 scenarios use `exit_code: 1` even though it's supported (Brahms-1's claim was wrong); no agent-level fault injection (blocks RFC #236 validation); no `no_cli_calls_after` invariant assertion — exactly the test that encodes Daniel's constraint |

---

## 2. Cross-cutting themes

These are the threads the individual seats couldn't see in isolation. Each names the seats involved, the proposal, and a Scribe-level synthesis (judgment, not consensus).

### 2.1 Receipts-as-anti-hallucination *(Stravinsky-2 §2, Brahms-2 INV-NO-CORRUPT-FORWARD, Bach-2 idempotency seam audit, Liszt-2 §3 watermark rule 5)*

Four seats converged on the same shape from different angles: a node that mutates state (or claims to have inspected state) must emit a **receipt** — a structured artifact reference — that a *mechanical* downstream check verifies. Stravinsky calls it `inspected_artifacts`; Bach calls it the ledger pattern; Liszt calls it "observe → decide → mutate → record"; Brahms calls it `no_cli_calls_after`.

**Synthesis (HIGH confidence):** receipts are the single most important pattern in this round. Adopt for routing agents (Stravinsky §5), generalize from `evidence_floor_check` to all reviewer-class agents, and make the harness assertion (`no_cli_calls_after`) part of the scenario contract. Never auto-recover a receipts violation — always hand off to a human gate. Cost: ~150 tokens per agent output, ~80 LOC harness change, ~40 LOC per receipt verifier script.

### 2.2 Retry-counter durability boundary *(Mahler-2 §4.2, Bach-2 §4, Beethoven-2 §6)*

All three seats agreed: **retry counters are ephemeral on resume; envelopes of past failures ARE persisted.** A resumed run re-attempts the failing node fresh; it sees that the previous run logged `kind: external.ado.transient` (useful diagnosis) but doesn't carry forward "attempt 2 of 3" (would deadlock the retry forever after one process death).

Where they differ: Bach-2 wants a `.polyphony/state/{rootId}/run-budget.json` *sliding-window* circuit breaker (§4) so a chronically-flaky upstream can't burn 100 retries in a row. Beethoven-2 wants a `manifest.RetryLedger` keyed by `(workflow, node, work_item_id, error_kind, window)`. Mahler-2 wants nothing persistent — engine stays stateless.

**Synthesis (MEDIUM confidence):** ship Mahler's stateless engine retry first. Add Bach's run-budget *only if* dogfood shows per-verb caps insufficient. The journal already records the data; promote when there's evidence we need it.

### 2.3 Domain-signal vocabulary *(Beethoven-2 §3, with consumers from every seat)*

> **Boulez review (2026-05-31) — vocabulary flag:** Beethoven-2 conceded under grilling that the "closed enum" framing contradicts `docs/decisions/domain-signal-envelope.md` (Accepted 2026-05-28), whose Evolution Policy item 2 is explicitly open-by-default. Re-frame as a **20-signal seed catalogue** (signal #20 `mission_drift_observed` withdrawn — covered by lint). Scribe to apply edits per `boulez-review-error-deep-20260531.md` § 5 E1.

Beethoven-2 owns the 21-signal closed enum, but every seat produces or consumes signals. The producer/consumer map:

| Signal | Producer | Consumer |
|---|---|---|
| `retry_exhausted` (#11) | Mahler-2's `RetryDef` exhaustion; today Liszt-2's scripts via verb | Wagner-2's routes; channel registry |
| `state_drift_detected` (#12) | Bach-2's `manifest validate`; Wagner-2's `verify_or_quarantine` | `polyphony run diagnose` (Beethoven-2 §5) |
| `manifest_corruption_suspected` (#21) | Bach-2's manifest hash check | `run diagnose` 🛑 verdict |
| `surface_opened` (#7) | Wagner-2's `human_gate` nodes | Channel; trust-ramp telemetry |
| `auto_decision_taken` (#18) | Stravinsky-2's routing agents under `auto` posture | Trust-ramp telemetry |
| `mission_drift_observed` (#20) | Brahms-2's lint rules | dev channel + `run diagnose` |

**Synthesis (HIGH confidence):** Lock the 21-signal enum as Beethoven-2 §3 proposes; every cross-seat surface either emits one of these or registers as a channel. Adding a new signal requires an explicit grilling round, not a one-off PR.

### 2.4 The `terminate.kind:` debate — synthesis of a 3-way conflict

- **Mahler-2** wants `type: terminate` to accept a `kind:` field (~5 lines schema + 10 lines engine) so workflows can re-raise typed errors at a chosen level (§3.3).
- **Wagner-2** wants a `cleanup:` block with `finally` semantics (§4.3) and walked back saga compensation (§4.1).
- **Beethoven-2** wants a `workflow_superseded` terminal distinct from `workflow_abandoned` (§2.1 bucket 3).

**Synthesis (HIGH confidence):** these are not competing — they compose. Ship all three: (a) Mahler's `terminate.kind:` (smallest), (b) Beethoven's `workflow_superseded` (smallest, just a terminal name), (c) Wagner's `cleanup:` block (slightly larger; needs `on_cleanup_failure:` to honour Daniel's constraint). Reject saga compensation explicitly.

### 2.5 The `script:` node contract *(Liszt-2 §1 + §5, Brahms-2 §1 Patch B)*

Both seats propose the same contract: exit-code dialect, `CONDUCTOR_ERROR_OUT` envelope, `CONDUCTOR_RETRY_ATTEMPT` env var, `CONDUCTOR_CANCEL_TOKEN` sentinel for Windows cooperative cancel. The overlap is intentional: Liszt-2 specifies what scripts must emit, Brahms-2 specifies what the harness must observe.

**Synthesis (HIGH confidence):** treat Liszt-2 §1.2 + §2.1 + §5.1 as the canonical "script ⇄ conductor contract" document. Brahms-2 Patch B implements the harness side. Mahler-2 wires the env vars and watchdog (~100 LOC Python in conductor). Bundle with Mahler's RFC #236.

### 2.6 `polyphony reconcile` / `polyphony run diagnose` — same verb or different? *(Bach-2 #4, Beethoven-2 §5)*

Two seats proposed verbs that look adjacent:

- **Bach-2 `polyphony reconcile --root N`** — re-runs the relevant `ensure-*` verbs in topology order to converge incomplete state (Type 2 corruption in Bach's typology).
- **Beethoven-2 `polyphony run diagnose`** — produces a 3-tier verdict (✅/⚠️/🛑) for whether a run is safe to resume; the launcher gates on it before `--intent resume`.

**Synthesis (MEDIUM confidence):** they are **complementary, not the same**. `diagnose` answers "should I resume?" and is purely read-only. `reconcile` answers "make the state consistent" and is write-side (re-invokes idempotent verbs). The two compose: `diagnose` returns ⚠️ "incomplete state at AB#3401" → operator runs `reconcile --root 3401` → `diagnose` returns ✅ → operator resumes. Ship `diagnose` first (smaller, gates Daniel's invariant directly); `reconcile` is a P2 follow-on.

### 2.7 PR #229 unblock path *(Mahler-2 §1, Wagner-2 §3.3)*

Mahler-2's catalogue: 7 conflicts (1 substantive in `context.py`, 6 mechanical), with full replacement text in §1.3. Wagner-2 has one specific ask: **lift the Phase-1 validator block that hard-errors on `on_error:` for `type: workflow` nodes**, even if the runtime support is Phase 2. That unblocks ~30 callsites of authoring-now-shipping-later.

**Synthesis (HIGH confidence):** combine the two asks into a single push. Mahler-2 resolves context.py + 5 mechanical hunks, defers `workflow.py` (4 hunks) to a dedicated 2-hour review session, and adds the validator unblock as a Phase 1.5 commit. Daniel posts the §1.6 PR comment if push isn't granted.

---

## 3. Conflicts requiring resolution

### 3.1 Default route `retry.max`: 2 vs 3

- **Mahler-2** says **2** (§2.4, §F decision): combined with `provider.max_attempts=3` that's 9 LLM calls per stuck node.
- **RFC #236** said **3** (= 12 calls).

**Resolution (HIGH confidence):** **2**. Mahler-2's argument is correct — provider × route multiplication caps at 9, which is generous. Add a validator warning at `provider × (1+route.max) > 6`.

### 3.2 Compensation / `cleanup:` / saga

- **Wagner-1** proposed saga-pattern compensation.
- **Wagner-2** walked it back (§4.1, 4 reasons): wrong abstraction; our verbs are already idempotent; reverse-execution-order is ill-defined for our parallel for_each; only 3 sites would use it.
- **Wagner-2** proposes `cleanup:` block instead (§4.3) with `on_cleanup_failure:` routing — finally-semantics, NOT compensation.

**Resolution (HIGH confidence):** Adopt Wagner-2's walk-back. Saga is rejected; `cleanup:` block is adopted. The pattern that survives is **idempotent verbs + state-discovery on re-entry + finally-style cleanup terminals**. This is consistent with `teardown_worktree` already being wired four times manually in `root-item-dispatch.yaml`.

### 3.3 `type: store` *(Liszt-1)* vs cross-run artifact API *(Bach-2)*

Liszt-1 had asked for a `type: store` primitive for cross-run persistence. **Liszt-2 explicitly retracts** this in §6 Bach-2 coordination: "I retract the `store: primitive` specific shape from Liszt-1 Gap 2 in favour of Bach-2's artifact API — same problem, his framing is cleaner."

**Resolution (HIGH confidence):** retraction stands. Use Bach-2's filesystem approach (`.polyphony/state/{rootId}/{kind}.json`) today; conductor-native artifact API is a P3 deferred item (Bach-2 patch P4).

### 3.4 Where retry-counter persistence lives

- **Mahler-2**: stateless engine, no persistence.
- **Bach-2**: journal-derived, optionally promoted to `run-budget.json`.
- **Beethoven-2**: `manifest.RetryLedger` with `(workflow, node, work_item_id, error_kind, window)` key.

**Resolution (MEDIUM confidence):** Mahler-2's engine stays stateless. Bach-2's journal-derived counter is the source of truth for diagnostics. Beethoven-2's `RetryLedger` field is a *manifest convenience view* into the same journal data — not a separate writer. The sliding-window circuit breaker is **deferred until dogfood evidence demands it**.

### 3.5 Convergent (no conflict, worth noting)

- Receipts pattern: 4 seats converged.
- `subworkflow.*` error kind taxonomy: Mahler-2 + Wagner-2 + Brahms-2 agree.
- Cancel-short-circuits-retry: Liszt-2 §5.4 — Mahler-2 should adopt as a special case.
- Receipt enforcement is human-gate-only on violation (no auto-reroll): Stravinsky-2 §3 + Brahms-2 INV-NO-CORRUPT-FORWARD.

---

## 4. Ranked recommendations

Ranked roughly by (impact × confidence) / effort.

| # | Action | Owner | Effort | Impact | Confidence | Depends on |
|---|--------|-------|--------|--------|------------|------------|
| R1 | Fix `actionable.yaml` silent-loss (Wagner-2 §3.0) | Wagner-2 | <1 day | High | HIGH | nothing |
| R2 | Resolve PR #229 (Mahler-2 §1.3) | Mahler-2 | 4 hr | High (unblocks all Phase 2) | HIGH | nothing |
| R3 | Adopt 5-class script exit-code contract + lint (Liszt-2 §1.2, §4.3) | Liszt-2 | ~2 hr migration | High | HIGH | nothing |
| R4 | Ship `polyphony run diagnose` (Beethoven §5) | Mozart + Bach | 1 day | High (Daniel's constraint operationalized) | HIGH | journal queries |
| R5 | Adopt receipts on routing agents + `verify_receipts.ps1` (Stravinsky §2) | Stravinsky + Wagner | 1 sprint | High (closes hallucinated-success) | HIGH | output schema PR |
| R6 | Backfill `[VerbResult]` + discriminated-union outcomes (Stravinsky §5) | Mozart | 3 days | High | HIGH | nothing (already on roadmap) |
| R7 | `on_error:` validator unblock on `type: workflow` (Wagner-2 D-CONDUCTOR-1; Mahler §3.3) | Mahler-2 | small | High (unblocks ~30 callsites of authoring-now) | HIGH | PR #229 merge |
| R8 | `workflow.on_error:` top-level catch-all (Mahler §3.2 Rank 1) | Mahler-2 | ~80 LOC + tests | Medium | HIGH | PR #229 merge |
| R9 | Subworkflow `subworkflow.<name>.<kind>` envelope propagation (Mahler §3.2 Rank 2) | Mahler-2 | Phase 2 | High (kills 60-line aggregator) | HIGH | PR #229 merge |
| R10 | `retry_key:` + idempotency lint (Mahler §2.3) | Mahler-2 | Phase 2 add-on | High (makes retry safe by construction) | MEDIUM | RFC #236 |
| R11 | Harness Patches A+B+C+D (Brahms §1) — fault injection, `no_cli_calls_after`, two-phase | Brahms-2 | ~240 LOC | High (testability for everything above) | HIGH | nothing |
| R12 | Lock 21-signal closed enum (Beethoven §3) | Beethoven + Bach | docs + scaffolding | High (channel discipline) | HIGH | grilling round |
| R13 | Lock "engine-initiated abandonment does not exist" as §7 invariant (Beethoven §2) | Beethoven | docs | High (closes Daniel's open question) | HIGH | nothing |
| R14 | `cleanup:` block + `on_cleanup_failure:` (Wagner §4.3) | Wagner + Mahler | Phase 2 | Medium | MEDIUM | none |
| R15 | `verify_or_quarantine` node type + `required_predecessors:` lint (Wagner §2.5, §5.4) | Wagner + Mahler | Phase 3 | Medium | MEDIUM | Bach-2 sticky quarantine |
| R16 | `CONDUCTOR_CANCEL_TOKEN` sentinel + 30s cooperative-then-kill (Liszt §5) | Liszt + Mahler | ~100 LOC Python | Medium (Windows correctness) | HIGH | bundle with RFC #236 |
| R17 | Compaction event + agent `limits:` (Stravinsky §4–6) | Stravinsky + Mahler | conductor RFC | Medium | MEDIUM | provider SDK hooks |
| R18 | `PrCommentMarker`-keyed dedup on `post-comment-ado` (Bach #9) | Mozart | ~30 LOC | Low (today); HIGH (after `on_error: retry:`) | HIGH | nothing |
| R19 | `polyphony reconcile --root N` umbrella verb (Bach #4) | Mozart + Bach | ~150 LOC | High (Type 2 corruption recovery) | MEDIUM | after diagnose |
| R20 | ~~`run-budget.json` sliding-window circuit breaker (Bach §4)~~ **WITHDRAWN** under Boulez review — Bach-2 conceded the journal already holds the data; no new durable surface. See `boulez-review-error-deep-20260531.md` § 5 E2. | — | — | — | — | — |

---

## 5. Sequencing plan (3 sprints)

### Sprint 1 — ZERO conductor changes (ship today against current conductor)

| # | Item | Back-ref |
|---|------|----------|
| S1.1 | Fix `actionable.yaml` silent-loss bug | R1 |
| S1.2 | Adopt 5-class script exit-code contract; ship `Lint-ConductorScripts.ps1`; migrate 12 scripts | R3 |
| S1.3 | Fix `resolve-pr-policy.ps1` / `resolve-unattended-cap-mode.ps1` / `resolve-research-policy.ps1` swallow-exit-code anti-pattern (Liszt §2.2 burn case 3) | (in R3) |
| S1.4 | Add receipts (`inspected_artifacts`) to architect, evidence_reviewer, root_reviewer prompts; ship `scripts/verify-receipts.ps1`; wire `hallucinated_success_gate` | R5 |
| S1.5 | Ship harness Patches A+B+C+D (agent faults, `delay_ms`, route enrichment, `no_cli_calls_after`, two-phase) | R11 |
| S1.6 | Author chaos scenarios Chaos-1 through Chaos-8 (Brahms §3) | (in R11) |
| S1.7 | Verify the 3-retries-on-network/auth rule is actually wired at `AdoClientPolicy.cs:115` + `GhClientPolicy.cs:63`; document in skill | Bach §0 finding |
| S1.8 | Lock the 21-signal closed enum and the "engine-initiated abandonment does not exist" invariant | R12, R13 |
| S1.9 | Ship `polyphony run diagnose` (3-tier verdict) | R4 |
| S1.10 | Add `PrCommentMarker` to `post-comment-ado` before any `retry:` ever fires on it | R18 |

### Sprint 2 — with PR #229 merged

| # | Item | Back-ref |
|---|------|----------|
| S2.1 | Resolve PR #229 conflicts + push (Mahler §1.3) | R2 |
| S2.2 | Lift Phase-1 validator block on `on_error:` for `type: workflow` nodes | R7 |
| S2.3 | Ship `workflow.on_error:` top-level catch-all PR (Phase 1.5) | R8 |
| S2.4 | Adopt discriminated-union outcomes (`outcome: approved/changes_requested/could_not_review`) for the 4 routing agents; backfill `[VerbResult(typeof(X))]` | R6 |
| S2.5 | Wire `subworkflow.<workflow>.<kind>` propagation (Phase 2 of #229's parent) | R9 |
| S2.6 | Author paired-failure-success harness scenarios per `failure_tier:` annotation (Brahms §4) | R11 |

### Sprint 3 — with RFC #236 / `retry:` shipped

| # | Item | Back-ref |
|---|------|----------|
| S3.1 | Adopt `retry_key:` + idempotency validator lint | R10 |
| S3.2 | Migrate the ~30 callsites to `on_error: + retry:` | post-R7 |
| S3.3 | Bundle `CONDUCTOR_CANCEL_TOKEN` + `timeout_seconds:` script-node fields into the same conductor PR | R16 |
| S3.4 | Ship `cleanup:` block + `on_cleanup_failure:` | R14 |
| S3.5 | Author the chaos suite property tests (PROP-A through PROP-E) in conductor itself | Brahms §2 |
| S3.6 | `polyphony reconcile --root N` umbrella verb | R19 |
| S3.7 | `workflow_superseded` terminal alongside `workflow_abandoned` | Beethoven §2.1 |
| S3.8 | Compaction event + agent `limits:` RFC | R17 |

**Deferred / re-evaluate after Sprint 3:** R20 (sliding-window circuit breaker), `verify_or_quarantine` primitive (R15), structured `EngineFaultHook`, dynamic `workflow:` path templating.

---

## 6. Open decision asks for Daniel

Grouped by theme. Defaults provided for every one — absent input we proceed on default.

### A. PR #229 + conductor mechanics

- **D-PR229-PUSH** — Who applies the 7-conflict fix?
  - **Default:** Mahler-2 fixes `context.py` + 5 mechanical hunks (~4 hr) and defers `workflow.py` (4 hunks) to a dedicated 2-hr review. **Blocks:** Phase 2 of the entire error-handling roadmap.
- **D-VALIDATOR-UNBLOCK** — Lift Phase-1 validator block on `on_error:` for `type: workflow` nodes in PR #229 itself? *(Wagner §3.3)*
  - **Default:** Yes. **Blocks:** ~30 callsites authoring-now-shipping-later.

### B. Retry semantics

- **D-RETRY-MAX** — Default `retry.max` is 2 or 3? *(Mahler §F vs RFC #236)*
  - **Default:** **2** (Mahler-2's recommendation). Bounds at 9 LLM calls combined with `provider.max_attempts=3`.
- **D-RETRY-KEY** — Adopt `retry_key:` + `node.idempotent: true` validator in Phase 2 alongside `retry:`? *(Mahler §2.3, §B)*
  - **Default:** Yes — ship together. Otherwise retry is an attractive nuisance.
- **D-CANCEL-SHORT-CIRCUIT** — Confirm `kind: 'cancelled'` short-circuits the retry loop (no fresh attempt)? *(Liszt §5.4)*
  - **Default:** Yes. Otherwise 24 h deadline burns another 24 h.

### C. Script contract

- **D-EXIT-CODE-CLASS** — Adopt 5-class script exit-code contract (`0/2/3/4/5 = success/usage/permanent/transient/corruption`)? *(Liszt §1.2)*
  - **Default:** Yes. **Blocks:** safe `on_error:` adoption across script fleet.
- **D-CANCEL-TOKEN** — Bundle `CONDUCTOR_CANCEL_TOKEN` sentinel + `timeout_seconds:` script-node field into Mahler's RFC #236 PR? *(Liszt §5.1, §5.3)*
  - **Default:** Yes (~100 LOC Python in conductor).

### D. Agents

- **D-RECEIPTS** — Mandate `inspected_artifacts` receipts on every reviewer-class agent (root_reviewer, evidence_reviewer, plan_reviewer)? *(Stravinsky §2, §8.2)*
  - **Default:** Yes. ~150 tokens/output; closes the highest-risk failure mode.
- **D-RECEIPTS-VIOLATION** — Always HANDOFF to human gate on receipts violation (never auto-reroll)? *(Stravinsky §3, §8.7)*
  - **Default:** Yes. Auto-rerolling a suspected hallucination = "letting corrupt state move forward."
- **D-DISCRIMINATED-OUTCOMES** — Adopt `outcome: approved/changes_requested/could_not_review` discriminated unions for the 4 routing agents in this quarter? *(Stravinsky §5)*
  - **Default:** Yes — start with `evidence_reviewer` in PR #8 (rubric work is the insertion point).

### E. Vocabulary + structural

- **D-SIGNAL-ENUM** — Lock the 21-signal closed enum + "engine-initiated abandonment does not exist" §7 invariant? *(Beethoven §3, §2.3)*
  - **Default:** Yes (both).
- **D-DIAGNOSE** — Greenlight `polyphony run diagnose` as P1 polyphony-side work this quarter? *(Beethoven §5, Bach #4)*
  - **Default:** Yes — ~1 day, smallest mission-recentering wedge.
- **D-RECONCILE** — Ship `polyphony reconcile --root N` as Sprint 3 follow-on to `diagnose`? *(Bach #4–5)*
  - **Default:** Yes, but Sprint 3 (after `diagnose` proves the verdict-driven flow).

### F. Compensation / cleanup / superseded

- **D-NO-SAGA** — Accept Wagner-2's walk-back: saga compensation is **rejected**; adopt `cleanup:` block instead? *(Wagner §4.1, §4.3)*
  - **Default:** Yes. Three sites, all idempotent — engine compensation is over-engineering.
- **D-SUPERSEDED-TERMINAL** — Add `workflow_superseded` terminal distinct from `workflow_abandoned`? *(Beethoven §2.1)*
  - **Default:** Yes. Loses audit information otherwise.

---

## 7. What we should NOT do

These are explicitly rejected proposals. The discipline of *not building* matters.

| Rejected | Why | Cited from |
|---|---|---|
| **Saga-pattern compensation primitive** | Wrong abstraction for our workload. Three sites; all idempotent. Engine-coordinated unwinder over-engineers a problem reverse-by-restart already solves. | Wagner-2 §4.1 (walks back wagner-1) |
| **New `node_error_routed` / `node_retry_attempt` events** | Withdrawn by Brahms-2 (§6.2). Enrich existing `route_taken` with `matched_on_error`, `retry_attempt` — fewer events, less coupling. | Brahms-2 Appendix B |
| **Liszt-1's `type: store` primitive** | Retracted by Liszt-2 (§6 Bach-2 coordination) in favour of Bach-2's cross-run artifact API framing (same problem; cleaner shape). | Liszt-2 §6 ask 2 |
| **GitHub Issues integration / `channel_kind: github_issue`** | Daniel locked: out of scope. Channel registry rejects it. | Beethoven-2 §0 constraint 3 |
| **Transient-fault auto-classification in the engine** | Cross-platform fragility; author has better signal; auto-classification is silent when wrong. Use script-author-emitted `kind:` system. | Mahler-2 §3.4 |
| **Bach-2's run-budget circuit breaker — NOT now** | Smaller per-verb caps via journal preflight (Bach S3-fix) are sufficient; promote only on evidence. | Bach-2 §6 final paragraph |
| **LLM-as-judge "verifier agent that verifies the reviewer"** | Just pushes hallucination one level up. Mechanical receipts grounded in repo state are non-hallucinatable. | Stravinsky-2 §2 (Why receipts and not LLM-as-judge) |
| **`workflow.py` hasty mechanical merge in PR #229** | 4 hunks, 4800+ line file; warrants dedicated 2-hr review session. Mechanical "keep both" introduces latent dispatch-order bugs. | Mahler-2 §1.5 |
| **Engine-initiated abandonment** | Four invariants converge: §5.4 no-kill-timers, §5.5 operator-mediated recovery, §5.2 human-in-the-loop attestation, Daniel's corrupt-state rule. | Beethoven-2 §2.2 (the single most important thing in the report) |
| **Two-driver harness for resume scenarios** | Withdrawn by Brahms-2. Single driver + `phases:` block + shared `bin/` is ~50 LOC, not a new concept. | Brahms-2 Appendix B |
| **`Write-Host` in scripts** | Banned by Liszt-2's lint (L5). `abort-run.ps1` currently uses it — real bug. | Liszt-2 §1.2, §4.3 |

---

## Appendix A — Holographic-as-MCP (separate question)

**Verdict (high confidence):** **BUILD A SMALLER VERSION** (~80 lines FastMCP wrapper).

**Surface:** 4 tools — `recall`, `probe`, `reason`, `retain`. Skip `forget` (destructive, no audit), `trust` (corrupts convergence signal), `contradict` (O(n²)), `related` (niche).

**Why viable:** the `HolographicMemoryProvider` is loop-independent — it's a `MemoryProvider` plugin (`agent/memory_provider.py:42`) whose `handle_tool_call` does nothing but call `self._store` / `self._retriever`. The "MCP can't drive `_AGENT_LOOP_TOOLS`" warning applies to the *built-in* `memory` tool in `model_tools.py:493`, not to the holographic provider. Confirmed by reading `__init__.py:259–344`.

**State coupling:** both processes open the same `$HERMES_HOME/memory_store.db`; SQLite WAL is the lock (`store.py:115–119`, 10s timeout). Realistic concurrency profile: brief writes (~ms), rare overlap. One defensive measure — wrap `retain` in retry-once on `sqlite3.OperationalError: database is locked`.

**Utility scenarios (weekly+):** `probe("polyphony")` at session start → 5–10 high-trust facts; cross-project `reason(["polyphony", "conductor"])`; `recall` over Daniel's curated mental model (vs. raw conversation in `session_store_sql`). Complements (does not duplicate) `store_memory` and `session_store_sql`.

**Build plan:** 60–90 min total. Phase 0 verify, Phase 1 wrapper (20 min), Phase 2 smoke test (15 min), Phase 3 register with Copilot CLI (15 min), Phase 4 soak (20 min), Phase 5 stop.

**State-coupling concern (the one real caveat):** `_rebuild_bank` (`store.py:498–528`) deletes+inserts the bank row on every `add_fact`. Concurrent writers may produce briefly stale banks — not corrupt, self-healing on next add. Acceptable.

**This is not part of the error-handling work.** It is a side investigation Daniel asked for.

---

## Appendix B — Seat-by-seat handoff index

| Seat | File | Focus |
|---|---|---|
| **Mahler-2** | `mahler-2-error-deep-20260531.md` | PR #229 7-conflict catalogue with replacement text; retry semantics (RFC #236 open Qs); `retry_key:` + idempotency lint; `workflow.on_error:` top-level catch-all; for_each retry placement (option iii) |
| **Wagner-2** | `wagner-2-error-deep-20260531.md` | 4-tier failure vocabulary (partial/soft/hard-branch/catastrophic); skip-gate-don't-corrupt pattern with `required_predecessors:`; walks back saga in favour of `cleanup:`; `verify_or_quarantine` node type; flags `actionable.yaml` silent-loss bug |
| **Bach-2** | `bach-2-error-deep-20260531.md` | Resume-by-design; 5-place durable-state partition; verb idempotency audit (30 verbs, 4 buckets); 4-flavor corrupt-state typology; seed-children gap analysis; conductor patches P1–P3 (checkpoint_key, CONDUCTOR_CHECKPOINTS_DIR, retry_attempt); confirms HTTP 3-retry-no-auth already implemented |
| **Liszt-2** | `liszt-2-error-deep-20260531.md` | 5-class script exit-code contract; `CONDUCTOR_ERROR_OUT` envelope schema; transient/permanent/corruption kind taxonomy; 4 real burn cases; 6-rule restartable-script pattern; PowerShell prelude + `Lint-ConductorScripts.ps1`; `CONDUCTOR_CANCEL_TOKEN` sentinel design |
| **Stravinsky-2** | `stravinsky-2-error-deep-20260531.md` | 7-mode agent failure catalogue with frequencies; cite-and-verify receipts pattern; retry/reroll/handoff/abort decision tree; `reroll:` policy (parallel to `retry:`); discriminated-union output schemas for 4 routing agents; compaction-survival design |
| **Beethoven-2** | `beethoven-2-error-deep-20260531.md` | 7-option external remediation decision tree; abandonment typology (3 buckets + 1 cascade non-bucket); **"engine-initiated abandonment does not exist"** invariant; 21-signal closed enum; mission-drift analysis; `polyphony run diagnose` 3-tier verdict |
| **Brahms-2** | `brahms-2-error-deep-20260531.md` | INV-RESTART + INV-NO-CORRUPT-FORWARD; harness Patches A (FaultMode), B (delay_ms + retry-attempt), C (no_cli_calls_after), D (two-phase); 5 Hypothesis property tests; 8 chaos scenarios (full YAML); paired-scenario rule for `failure_tier:`; skip-gate-don't-corrupt lint |
| **Holographic-viability** | `holographic-viability-20260531.md` | Separate question; verdict BUILD SMALLER VERSION; ~80-line FastMCP wrapper; loop-independence verified; state-coupling analysis |

---

*End — Scribe consolidation, 2026-05-31. Eight handoffs synthesized. Top 5 actions + 4 blocking decisions surfaced. Full decision provenance and rejection rationales preserved.*
