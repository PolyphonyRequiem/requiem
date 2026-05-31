# Boulez — Antagonistic Architecture Review: Error-Handling Deep Dive

**Reviewer:** Boulez (antagonistic architecture)
**Date:** 2026-05-31
**Target:** `error-handling-deep-dive-consolidated-20260531.md` (Scribe synthesis) plus the 7 underlying seat handoffs
**Co-reviewer (code craft):** Ravel — runs in parallel; this verdict covers architecture/seams/vocabulary only
**Charter:** `.squad/agents/boulez/charter.md` — Reviewer Rejection Protocol applies

---

## § 1. Verdict summary

**ACCEPT-WITH-EDITS.**

The load-bearing claim of the consolidated report — *error handling is a layered contract (HTTP transport → verb exit-class → workflow `on_error:` → agent receipts → journal-mediated diagnose), with no engine-initiated abandonment and the journal as the single source of truth for replay safety* — holds against hostile reading and is internally consistent across six of the seven seats. Four corrections are non-optional before the synthesis can be treated as canonical; none of them require redesign — they require the synthesis to be re-issued with the sharper post-grilling vocabulary. None of the underlying seat designs are rejected outright. Two authors conceded substantive errors on contact (Beethoven-2 on the closed-enum signal vocabulary; Bach-2 on the durable-state placement of retry counters). One author (Mahler-2) correctly identified a synthesis-level downgrade of a load-bearing safety primitive (`retry_key:` validator strictness). One author (Wagner-2) withdrew a primitive (`type: fatal`) that violated locked north-star §5.2 surface taxonomy.

Authority for the rewrite per Reviewer Rejection Protocol: Scribe, not the original seat authors. Scope of the rewrite: the five corrections in § 5 below.

---

## § 2. Findings against the consolidated report

Each finding is numbered and cites the rejection criterion from charter § "Rejection Criteria (the bar)". Findings are stated as defects in the synthesis; underlying seat work is addressed in § 3.

### F-CONSOL-1 — Closed-enum signal vocabulary contradicts an accepted ADR. (Criteria #9, #4)

The synthesis §2.3 cites "Beethoven-2's 21-signal closed enum" as a HIGH-confidence locked vocabulary, with "Adding a new signal requires an explicit grilling round, not a one-off PR." The ADR at `docs/decisions/domain-signal-envelope.md` (Status: Accepted, 2026-05-28, Bach as author, **Daniel as vocabulary authority**) explicitly inverts that posture. ADR Evolution Policy item 2: *"Adding new `kind` or `cta_kind` values: permitted without version bump. Consumers that switch on `kind` MUST have a default/fallback case."* The synthesis adopts a proposal that is structurally incompatible with a locked ADR by the same architect seat (Bach) for which Daniel is the named vocabulary authority. The synthesis fails to surface the conflict.

**Author concession (Beethoven-2, grilling F1):** *"CONCEDE. I did not know about `docs/decisions/domain-signal-envelope.md`… My §3 is unsound as written. Correct posture: the 21 signals are a **seed catalogue**, not a closed enum; new kinds remain additive-by-default per ADR; the §5.6 grilling round is for *invariant changes to the envelope*, not for additions to the kind vocabulary."*

### F-CONSOL-2 — Three competing durable-state placements for one logical surface. (Criteria #3, #2)

Synthesis §2.2 ("Retry-counter durability boundary") names three options — Mahler-2's stateless engine, Bach-2's `run-budget.json`, Beethoven-2's `manifest.RetryLedger` — then synthesises with: *"Mahler-2's engine stays stateless. Bach-2's journal-derived counter is the source of truth for diagnostics. Beethoven-2's `RetryLedger` field is a manifest convenience view into the same journal data — not a separate writer."* That sentence is unsound: a filesystem JSON file is not the same physical surface as a manifest field, neither is a journal-derived view. The synthesis conflates *logical reads* with *physical writes* and leaves the seam contract undeclared. Two of the three sub-proposals are stores; one is a query; the synthesis treats them as compatible.

**Author concession (Bach-2, grilling F1):** *"You're right. By my own §1 decision tree, retry-counter state is 'per-verb-invocation evidence' → journal. Both my `run-budget.json` AND Beethoven-2's `manifest.RetryLedger` violate it… Canonical placement: SQLite journal. Single writer (`JournaledActionDecorator`). Read via `polyphony journal query`… No `RetryLedger` field… Withdrawn… `WorkflowContext`: if conductor needs a materialized count for `retry:` decisioning, it queries the journal at node-entry and caches in-memory per-run only."*

### F-CONSOL-3 — Synthesis silently downgrades the load-bearing safety primitive for `retry:`. (Criteria #2, #5)

Mahler-2 §2.3 names the `retry_key:` + `node.idempotent: true` validator hard-error as *"the structural enforcement of Daniel's invariant"* ("don't let corrupt state move forward"). Synthesis R10 lists it as MEDIUM confidence and "Phase 2 add-on." That conversion is unsupported by any seat. If `retry:` ships in Phase 2 without the hard-error validator, there is a known unsafe window in which a workflow author can attach `retry:` to a non-idempotent node (e.g. `pr post-comment-ado`, which the synthesis itself flags as Bucket D in Bach's §2 audit) and conductor will silently dispatch duplicates. The synthesis offloads invariant enforcement to a deferred concern; the seat did not.

**Author position (Mahler-2, grilling F1):** *"Synthesis error. Position holds; R10 needs MEDIUM→HIGH and 'add-on'→'ship-with.' I did not concede… either retry ships with the hard-error lint, or retry doesn't ship in Phase 2. Phase 2.5 sequencing only works if Daniel explicitly accepts a known-unsafe window — which contradicts the invariant he named verbatim."*

### F-CONSOL-4 — Terminal-kind vocabulary drift across three seats; synthesis ships none of them. (Criterion #4)

The "manifest corruption / inconsistent state detected" terminal concept appears under four names across three seats:
- Bach-2 §3 Type 1: `corruption_detected` (envelope)
- Beethoven-2 §3 signal #21: `manifest_corruption_suspected`
- Beethoven-2 §3 signal #6 payload: `halted_by_drift` as a `terminal_kind` value
- Wagner-2 §1.3 Example D: `type: fatal` as a generic primitive

The synthesis does not surface this drift, does not pick a canonical name, and adopts Wagner-2's `type: fatal` without acknowledging that two other seats define distinct (and named) terminals for the same condition. **Bach-2 conceded (grilling F2)** and resolved with **`polyphony.corruption_detected` (envelope kind)** + **`corruption_halted` (workflow terminal node)**, with no manifest field. **Beethoven-2 conceded (grilling F3)** and aligned on **`manifest_corruption_suspected` (signal kind)** + **`halted_by_corruption` (run-level `terminal_kind`)**, dropping `halted_by_drift` ("drift is a surrender, not a terminal"). Bach and Beethoven still disagree on the signal noun (`corruption_detected` vs `manifest_corruption_suspected`); the synthesis must pick one and propagate.

### F-CONSOL-5 — `type: fatal` adopted despite violating locked north-star §5.2 surface taxonomy. (Criteria #9, #4)

Synthesis §2.4 endorses Wagner-2's `cleanup:`, Mahler-2's `terminate.kind:`, **and Beethoven-2's `workflow_superseded`** as "composing." It does not address that Wagner-2's `type: fatal` (§1.3 Example D) has no operator-engagement option — directly conflicting with north-star §7 invariant *"Every operator-facing surface is dual-mode. A 2-second rubber-stamp must be possible and a deeper-engagement entry point must be present."* and locked §5.2 surface categorization ("Surfaces are inflection, surrender, or attestation moments — never every state transition. The three categories cover every justified surface; no other category exists.")

**Author concession (Wagner-2, grilling F2):** *"You're right… 'unconditionally terminal with no operator engagement' is neither inflection, surrender, nor attestation… Withdrawing D-CONDUCTOR-1's `type: fatal` clause. Replacing with: lint that flags single-option `human_gate` nodes routing to `abort_run` as P6 violations requiring a second option."*

### F-CONSOL-6 — `terminate.kind:` bundling: annotated-exit vs typed-re-raise are conflated. (Criterion #5)

Synthesis §2.4 treats Mahler-2 §3.3's `terminate.kind:` as a single primitive. **Mahler-2 conceded (grilling F2)** that his §3.3 collapsed two distinct surfaces: (a) annotated terminal (parent sees `subworkflow.unhandled`, kind in `details.terminal_kind`, *not routable* on the kind), vs (b) typed re-raise (parent's `on_error: "external.git.*"` route fires *on the child's declared kind directly*). His proposal needs (b); his sizing was for (a). The synthesis adopted the bundle without disambiguating, leaving the seam contract between sub-workflow terminal and parent `on_error:` dispatch undefined.

### F-CONSOL-7 — "Engine-initiated abandonment does not exist" is presented as a new invariant; it is a corollary of two locked north-star clauses. (Criterion #7)

Beethoven-2 §9 ("the single most important thing in this report") and synthesis R13 propose locking this as a §7 invariant addition. North-star §5.4 (locked) already says *"Run termination is via operator action or a §5.2 surrender surface, never via a timer."* North-star §5.5 (locked) already says *"Reset is always available, always operator-mediated, never auto-invoked."* The proposed addition is a corollary of these two clauses applied to the abandonment terminal specifically. Not a defect — but the synthesis should name it as a *clarification of existing invariants* rather than a new locked invariant, so the north-star doesn't accrete redundant rows. (Beethoven-2's typology — three abandonment buckets + supersession-is-distinct — IS a new contribution worth locking; the invariant is not.)

### F-CONSOL-8 — `verify_or_quarantine` (R15) named without failure mode. (Criterion #10)

R15 lists "verify_or_quarantine node type + required_predecessors: lint (Wagner §2.5, §5.4)" with effort "Phase 3" and confidence "MEDIUM." Wagner-2's §5 describes the *trichotomy* (consistent / terminal / inconsistent) but does not specify what happens when verification itself fails (network failure during the verify probe? false-positive inconsistency report?). The synthesis lists it as deferred without naming the gap. This is salvageable — defer the primitive, but require the synthesis to add the missing failure-mode question to § 6 decision asks. Currently it's an architectural promise with no contract.

### F-CONSOL-9 — Exit-code collision between `polyphony run diagnose` and the script exit-code contract. (Criterion #4, internal inconsistency)

Beethoven-2 §5.4 specifies `polyphony run diagnose` returns **exit code 0/1/2** (safe / review / do-not-resume). Liszt-2 §1.2 (R3, synthesis-endorsed) specifies the canonical script exit-code contract as **0/2/3/4/5** where **2 = usage error**. If `Lint-ConductorScripts.ps1` (R3) is going to lint *every* polyphony-written script invocation including those that wrap `polyphony run diagnose`, the diagnose verb's exit-2 ("review with concerns") will be indistinguishable from a script-fleet usage error. The synthesis adopts both proposals without flagging the conflict. Either `diagnose` must use a different code (suggest 0/10/20) or it must be exempted from the lint with documented rationale.

### F-CONSOL-10 — Sequencing plan promises Sprint-1 work that depends on un-locked vocabulary. (Criterion #6)

Sprint 1 (S1.4) ships receipts on routing agents; S1.8 locks the 21-signal enum (now to be re-issued as a seed catalogue per F-CONSOL-1); S1.9 ships `polyphony run diagnose`. The diagnose verdict-tier table (Beethoven-2 §5.3) references signal kinds (`manifest_corruption_suspected`, `state_drift_detected`, `auth_lapse_detected`) whose names changed under grilling. A Sprint 1 that ships `diagnose` before the post-grilling envelope ADR amendment lands hard-codes names that will be renamed. Sequence S1.8 *before* S1.9.

---

## § 3. Findings against individual seat handoffs

Code-craft findings (envelope schemas, prompt wording, lint regex) are Ravel's; this section covers architectural seams only.

### Mahler-2 — `mahler-2-error-deep-20260531.md`

- **MAHLER-1** §2.3 `retry_key:` proposal is sound; concede no architectural defect. Synthesis-level downgrade is F-CONSOL-3.
- **MAHLER-2** §3.3 `terminate.kind:` conflates two surfaces (see F-CONSOL-6). Author concedes; the seat handoff text should be marked with a disambiguation note before the synthesis re-issues.
- **MAHLER-3** §4.2 ("bach-2 conflict point") correctly anticipates that retry counters must be ephemeral on resume. Bach-2's grilling response confirms the journal as the source of truth — Mahler-2's position is upheld and stronger than his original framing (he expected to fight; Bach yielded).

### Wagner-2 — `wagner-2-error-deep-20260531.md`

- **WAGNER-1** §4.3 `cleanup:` block. Originally underspecified; author supplied the missing seam contract under grilling (composes-not-masks; sees `$conductor.primary_error`; idempotency is the verb's job). Spec is now defensible. Synthesis must incorporate the spec into R14.
- **WAGNER-2** §1.3 Example D `type: fatal` — withdrawn under grilling. Synthesis must remove `type: fatal` from the Sprint 3 (S3.x) backlog (it is not in current sequencing, but lint replacement IS: see proposed edit in § 5).
- **WAGNER-3** §2.5 `required_predecessors:` primitive is sound and well-bounded. Five named candidate sites; existing trust-chain lint as gold standard. No defect.
- **WAGNER-4** §5 `verify_or_quarantine` — incomplete (see F-CONSOL-8).

### Bach-2 — `bach-2-error-deep-20260531.md`

- **BACH-1** §4 `run-budget.json` — withdrawn under grilling. Synthesis must remove from R20 entirely (not "defer" — *withdraw*; the journal already holds the data).
- **BACH-2** §6.2 retry counter — author committed to drafting a new ADR at `docs/decisions/retry-counter-placement.md` declaring SQLite journal canonical, forbidding manifest/filesystem mirrors. Synthesis must reference this future ADR in R10 and remove the implication that `RetryLedger` is a "convenience view."
- **BACH-3** §3 Type 1–4 corruption typology is sound. Terminal naming defect resolved under grilling (F-CONSOL-4).
- **BACH-4** §5 conductor patches P1–P3 (`checkpoint_key`, `CONDUCTOR_CHECKPOINTS_DIR`, `retry_attempt` template) are architecturally sound; no defect.

### Beethoven-2 — `beethoven-2-error-deep-20260531.md`

- **BEETHOVEN-1** §3.1 "21-signal closed enum" — see F-CONSOL-1. Post-grilling: 20 signals (delete `mission_drift_observed`), open enum, additive per ADR. Synthesis re-issue must reflect.
- **BEETHOVEN-2** §3 #20 `mission_drift_observed` — withdrawn (covered by lint; redundant runtime signal).
- **BEETHOVEN-3** §2 abandonment typology (3 buckets + 1 non-bucket) — sound; the *typology* is the genuine contribution, not the invariant addition (see F-CONSOL-7).
- **BEETHOVEN-4** §5 `polyphony run diagnose` verb — sound architecturally; exit-code conflict is F-CONSOL-9.
- **BEETHOVEN-5** §6.2 `manifest.RetryLedger` — withdrawn implicitly by Bach-2's concession that the journal is canonical. Synthesis must remove.

### Liszt-2, Stravinsky-2, Brahms-2

Out of architecture scope (code craft / contract details — Ravel's territory). Architectural seams used by these seats (`CONDUCTOR_ERROR_OUT` envelope, `CONDUCTOR_CANCEL_TOKEN` sentinel, harness fault injection) are sound and consistent with the layered architecture.

---

## § 4. Grilling transcripts (compact)

Full transcripts available via the seat agents' turn history; the sharpest exchanges quoted.

### Beethoven-2 — F1 (closed-enum vs ADR)

> **Boulez:** "The existing ADR at `docs/decisions/domain-signal-envelope.md`… EXPLICITLY says: 'New kinds may be added without a version bump as long as existing consumers ignore unknown values.'… Your proposal inverts the ADR's extensibility model."
>
> **Beethoven-2:** "CONCEDE. I did not know about the ADR… My §3 is unsound as written. Correct posture: the 21 signals are a **seed catalogue**, not a closed enum; new kinds remain additive-by-default per ADR; the §5.6 grilling round is for *invariant changes to the envelope*, not for additions to the kind vocabulary."

### Bach-2 — F1 (retry-counter placement)

> **Boulez:** "Your §1 decision tree explicitly forbids stores that aren't one of {manifest, journal, .polyphony/state JSON, ADO tag, in-memory}. Which of these is the canonical home for retry-counter state…?"
>
> **Bach-2:** "Concede with sharpening… Both my `run-budget.json` AND Beethoven-2's `manifest.RetryLedger` violate [my own §1]. They're denormalized views over data the journal already records… Canonical placement: SQLite journal. Single writer (`JournaledActionDecorator`)… No `RetryLedger` field… ADR slug: `docs/decisions/retry-counter-placement.md` — declares journal canonical, forbids manifest/filesystem mirrors."

### Mahler-2 — F1 (synthesis downgrade)

> **Boulez:** "Is this a synthesis error (your hard-error position survives, R10 needs MEDIUM→HIGH and 'add-on'→'ship-with') or did you concede the downgrade silently? Pick one."
>
> **Mahler-2:** "Synthesis error. Position holds… The lint is ~40 lines of validator code against the same RouteDef the retry RFC already touches, so coupling cost is zero. Decoupling is a synthesis judgment, not a design one. Push R10 back: either retry ships with the hard-error lint, or retry doesn't ship in Phase 2. Phase 2.5 sequencing only works if Daniel explicitly accepts a known-unsafe window — which contradicts the invariant he named verbatim."

### Wagner-2 — F2 (`type: fatal` vs §5.2)

> **Boulez:** "[`type: fatal`] is none of [inflection / surrender / attestation]… either (a) it IS a surface, in which case it's a one-option human_gate and the abstraction adds nothing, or (b) it is NOT a surface, in which case it conflicts with §5.5 'every surfaced moment is dual-mode'. Which?"
>
> **Wagner-2:** "Conceded. You're right… 'unconditionally terminal with no operator engagement' is neither inflection, surrender, nor attestation… The honest read of my own §1.3 Example D: the 8 single-option gates `type: fatal` was meant to replace are themselves §5.5 violations *today*… The real fix is **upgrade them to genuine dual-mode** — `{retry-after-fix-inputs, abort}` — which is a regular `human_gate` with two options. No new primitive needed. Withdrawing D-CONDUCTOR-1's `type: fatal` clause."

---

## § 5. Proposed edits to the consolidated report

Scribe applies these; the original seat authors do not, per Reviewer Rejection Protocol. Edits are listed in priority order. **None of the edits change the load-bearing thesis; they reconcile vocabulary, sequencing, and primitive scope with the post-grilling reality.**

### E1 — Re-frame the signal vocabulary (§2.3, R12, S1.8, decision ask D-SIGNAL-ENUM)

- Replace every occurrence of "21-signal closed enum" with **"20-signal seed catalogue (open-by-default per `docs/decisions/domain-signal-envelope.md` Evolution Policy)"**.
- Delete signal #20 `mission_drift_observed` from the enum; reduce count to 20.
- Strike the synthesis sentence "Adding a new signal requires an explicit grilling round, not a one-off PR." Replace with: *"New signal `kind` values are additive without version bump per the domain-signal-envelope ADR; the §5.6 grilling round is reserved for changes to the envelope contract itself (schema fields, severity vocabulary, lifecycle disposition values)."*
- Update D-SIGNAL-ENUM default text to: *"Lock the 20-signal seed catalogue as the initial polyphony domain-signal vocabulary; lock the abandonment typology + the corollary 'engine-initiated abandonment does not exist'."*

### E2 — Re-frame retry-counter durability (§2.2, §3.4, R10, R20)

- Strike the §2.2 sentence "Bach-2's journal-derived counter is the source of truth for diagnostics. Beethoven-2's `RetryLedger` field is a *manifest convenience view* into the same journal data — not a separate writer." Replace with: *"The SQLite journal is the single physical writer and source of truth for retry-counter state (per Bach-2 grilling concession). The `run-budget.json` and `manifest.RetryLedger` proposals are withdrawn. Conductor's `retry:` decisioning queries `JournalQueryRunner.CountAsync` at node-entry and caches in-memory per-run only. A new ADR at `docs/decisions/retry-counter-placement.md` (Bach-2 to draft) will declare the journal canonical and forbid manifest/filesystem mirrors."*
- Strike R20 entirely (`run-budget.json` is withdrawn, not deferred).
- Remove Beethoven-2 §6.2 `RetryLedger` references in §3.4 conflict resolution; replace with: *"Single writer: SQLite journal. Bach-2 ADR pending."*

### E3 — Promote `retry_key:` validator to ship-with-retry (R10)

- R10 confidence: MEDIUM → **HIGH**.
- R10 effort label: "Phase 2 add-on" → **"Phase 2 (ship-with retry, not optional)"**.
- Sprint 2 must add an explicit S2.x item: *"Adopt `retry_key:` + `node.idempotent: true` validator hard-error simultaneously with R7 (retry:). Validator rejects `retry:` lacking either field. Refusing to ship the validator with retry leaves a known-unsafe window for non-idempotent retries (per Mahler-2 §2.3)."*
- Decision ask **D-RETRY-KEY** default text: change "Yes — ship together. Otherwise retry is an attractive nuisance." → **"Yes — ship-with retry. The validator is the structural enforcement of Daniel's invariant; deferring it is a silent concession to a known-unsafe window."**

### E4 — Canonicalize the corruption-terminal vocabulary (§2.4, R13, §7, decision asks)

- Add to §2.4 a new bullet: *"Corruption-terminal vocabulary (post-grilling reconciliation): the verb-emitted envelope kind is `polyphony.corruption_detected` (Bach-2). The signal kind is `manifest_corruption_suspected` (Beethoven-2). The run-level `terminal_kind` is `corruption_halted` (Bach-2) — Beethoven-2's `halted_by_drift` is dropped; drift is a surrender, not a terminal. Two seats still disagree on the verb noun (`corruption_detected` vs `manifest_corruption_suspected`); Daniel-arbitration required (see § 7 Q4)."*

### E5 — Withdraw `type: fatal`; replace with lint (§2.4, §7 "What we should NOT do", R-row for cleanup)

- Strike "Mahler's `terminate.kind:` (smallest)" framing from §2.4 (see F-CONSOL-6 — this needs disambiguation, not a one-liner). Replace §2.4 with the two-line disambiguation: *"(a) Annotated terminal: `type: terminate` + `kind:` field (~5 lines, diagnostic only — parent receives `subworkflow.unhandled` with kind in `details.terminal_kind`). (b) Typed re-raise: distinct primitive (`type: raise` or `terminate: { propagate: true, kind: ... }`) — parent's `on_error:` route fires on the child's declared kind directly. Mahler-2 §3.3 needs (b); engine cost is ~30 lines plus parent-side dispatch."*
- Add `type: fatal` to §7 "What we should NOT do" table: *"`type: fatal` primitive — withdrawn by Wagner-2 under review. Violates north-star §5.2 surface taxonomy (no inflection/surrender/attestation category for unconditional terminal). The eight single-option `human_gate→abort_run` instances must be upgraded to dual-mode {fix-and-retry, abort}, not replaced by a new primitive."*

### E6 — Resolve diagnose-vs-script exit-code collision (Beethoven-2 §5.4, R3, R4)

- Add to R4 (`polyphony run diagnose`) a footnote: *"Exit codes 0/10/20 (not 0/1/2) to avoid collision with the Liszt-2 5-class script contract (`2 = usage error`). The 0/10/20 spacing leaves room for future severity tiers without straying into the script-contract range."*
- Add to R3 (script exit-code contract) a footnote: *"`polyphony run diagnose` is exempt from this contract — it is operator-facing and uses 0/10/20 for verdict tiers. The lint must whitelist `polyphony run diagnose` and any future operator-verdict verbs."*

### E7 — Re-sequence S1.8 before S1.9

- Sprint 1 ordering: S1.8 (lock 20-signal seed catalogue + abandonment typology + corollary invariant) must complete before S1.9 (`polyphony run diagnose`), because the diagnose verb's verdict-tier table references signal kinds whose names were re-canonicalized under grilling.

### E8 — Frame the "engine-initiated abandonment" claim as a clarification (R13, §2 of Beethoven-2 §9)

- Re-label R13 from "Lock 'engine-initiated abandonment does not exist' as §7 invariant" to: *"Document the 'engine-initiated abandonment does not exist' rule as a §5.4/§5.5 corollary in north-star §7, with reference back to the locked invariants 'Run termination is via operator action or a §5.2 surrender surface' (§5.4) and 'Reset is always available, always operator-mediated, never auto-invoked' (§5.5). The genuine new contribution is Beethoven-2 §2 abandonment typology (3 buckets + 1 cascade non-bucket)."*

### E9 — Add `verify_or_quarantine` failure-mode to § 6 decision asks (R15)

- Add to § 6.F or § 6.E: *"D-VERIFY-FAILURE — Wagner-2 §5's `verify_or_quarantine` proposal does not specify what happens when the verification probe itself fails (network failure during the probe; false-positive inconsistency report). Default: defer the primitive until the failure mode is specified. Naming the gap is § 5 E9; closing it is a future grilling round."*

### E10 — Surgical inline edits applied directly to consolidated report

Edits below this paragraph are 1–3 sentence corrections within the surgical-correction class permitted by the charter; applied inline. All larger edits remain in § 5 above for Scribe to execute.

*(See git diff on `error-handling-deep-dive-consolidated-20260531.md`. Applied: vocabulary tag at top of §2.3 flagging the ADR conflict; strike-through note at R20.)*

---

## § 6. Proposed doc updates

### D1 — `docs/decisions/retry-counter-placement.md` (new ADR)

**Owner:** Bach-2 (committed under grilling F1).
**Content:** SQLite journal is the single physical writer and source of truth for retry-counter state. Manifest and filesystem JSON mirrors are forbidden. Conductor's `retry:` decisioning queries `JournalQueryRunner.CountAsync` at node entry and caches in-memory per run only. The seam between conductor's in-memory cache and the journal is read-only-on-entry, write-via-`JournaledActionDecorator`-only.

### D2 — `docs/decisions/domain-signal-envelope.md` (amendment)

**Owner:** Bach (vocabulary authority) + Daniel sign-off.
**Content:** Add a new section ("Signal kind seed catalogue, v1") referencing Beethoven-2's 20-signal proposal as the initial population. Reaffirm Evolution Policy item 2 — additive without version bump. Add a clarifying note that signal kinds are workflow-emitted and additive by default; tightening to a closed enum would require a separate ADR.

### D3 — `docs/north-star.md` §7

**Owner:** Daniel.
**Content:** Add the Beethoven-2 §2 abandonment typology as a new invariant cluster: "Engine-initiated abandonment does not exist (corollary of §5.4 + §5.5). Abandonment has three operator-mediated kinds (operator_explicit / operator_after_surrender / superseded); supersession has a distinct terminal (`workflow_superseded`); cascading from a parent's abandonment is operator-decided per child, never automatic." Do NOT add a new top-level invariant ("engine-initiated abandonment does not exist") because it is already entailed by two locked invariants.

### D4 — `docs/glossary.md`

**Owner:** Bach (Daniel-arbitration on the corruption-noun question).
**Content:** Add `corruption_halted` (workflow terminal node), `polyphony.corruption_detected` *or* `manifest_corruption_suspected` (verb-emitted envelope kind — Daniel picks one), `workflow_superseded` (workflow terminal node distinct from `workflow_abandoned`).

### D5 — `docs/polyphony-architecture.md`

**Owner:** Bach.
**Content:** Add a verb-error catalogue section listing the canonical kinds emitted to `CONDUCTOR_ERROR_OUT` and the 5-class exit-code contract (Liszt-2 §1.2), including the `polyphony run diagnose` exemption (E6 above).

---

## § 7. Architectural questions still open for Daniel

Numbered, with stakes.

1. **Q1 — Corruption envelope noun: `polyphony.corruption_detected` (Bach-2) or `manifest_corruption_suspected` (Beethoven-2)?** Stakes: vocabulary single-source-of-truth. Bach-2's verb noun ("detected") is active and asserts certainty; Beethoven-2's ("suspected") is hedged and leaves room for false positives. Default: **`manifest_corruption_suspected`** — Daniel's hard rule is *"don't let corrupt state move forward"*, which is symmetric to refusing to assert corruption until the evidence is overwhelming. Hedged noun matches operator-mediated recovery posture.

2. **Q2 — Does `retry:` ship in Phase 2 without the `retry_key:` validator?** Stakes: existence of a known-unsafe window during which a workflow author can attach `retry:` to a non-idempotent verb. Mahler-2's position (synthesis-disputed): ship-with-retry. Default: **ship-with-retry**, per Mahler-2's argument.

3. **Q3 — Does the 5-class script exit-code contract apply to operator-facing verbs (`polyphony run diagnose`)?** Stakes: F-CONSOL-9 — exit code 2 means "usage error" in the script contract and "review with concerns" in the diagnose verb. Default: **exempt operator-facing verbs**, use 0/10/20 for diagnose verdicts.

4. **Q4 — Is the 20-signal seed catalogue the right shape, or should some be promoted to engine-level (conductor-emitted) vs verb-emitted?** Stakes: signals 1/2/6/7/8 in Beethoven-2's enum (`run_started`, `run_resumed`, `run_ended`, `surface_opened`, `surface_closed`) are workflow-lifecycle signals that conductor naturally observes. If polyphony emits them, conductor's `type: emit` step has to fire from a workflow YAML location, which is verbose and error-prone. Default: **defer to §5.6 observability grilling round** (Beethoven-2 §4.4 already names this as a "starved" grilling agenda item).

5. **Q5 — Does Beethoven-2's `polyphony run diagnose` 3-tier verdict gate the `--intent resume` flow at the launcher (`Invoke-PolyphonySdlc.ps1`), or is it purely advisory?** Stakes: if it gates, the operator cannot resume past a 🛑 verdict without an explicit override; if advisory, the corrupt-state clause is operator-discipline rather than engine-enforced. Default: **gates at the launcher with `--force` operator override** (consistent with §5.5 "operator-mediated recovery").

6. **Q6 — Does `polyphony reconcile --root N` (Bach-2 #4) ship as Sprint 3 follow-on, or is it deferred to next quarter pending dogfood evidence?** Stakes: Bach-2 calls reconcile "the biggest single completeness win for restart-friendliness." The synthesis defers; the seat author considers it Sprint-2. Default: **Sprint 3** (synthesis), but flag for re-evaluation if Sprint 1's `diagnose` verb surfaces incomplete-state cases at higher-than-expected frequency.

---

## § 8. Reviewer-rejection housekeeping

- Per charter "Boundaries / If I review others' work": ACCEPT-WITH-EDITS means the load-bearing claim survives but the document must be re-issued with the corrections in § 5 before being treated as canonical. **Scribe owns the rewrite, not the original seat authors.**
- The 10 findings against the synthesis (§ 2) are the rewrite scope. The 4 findings against individual seats (§ 3) are the inputs Scribe consumes. Neither set authorizes a redesign.
- Authors are not blocked on Boulez sign-off; they are blocked on Scribe re-issue. After re-issue, this verdict marks the round complete.
- Ravel's parallel review (code craft / schemas / receipts wording) is independent; if Ravel raises an architectural concern, escalate via Bach mediation per charter.

— Boulez, antagonistic review
*Filed 2026-05-31. Verdict: ACCEPT-WITH-EDITS. Load-bearing claim survives; ten synthesis-level corrections required before canonization.*
