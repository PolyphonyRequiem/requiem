# Ravel — Antagonistic Code-Craft Review: Error-Handling Deep Dive

**Reviewer:** Ravel (antagonistic code craft)
**Date:** 2026-05-31
**Targets:** the seven seat handoffs — `mahler-2`, `wagner-2`, `bach-2`, `liszt-2`, `stravinsky-2`, `beethoven-2`, `brahms-2`
**Sister review (architecture):** `boulez-review-error-deep-20260531.md` — same hostile reading at the seams/vocabulary layer; this review covers code, contracts at the syntactic surface, and per-language craft
**Charter:** `.squad/agents/ravel/charter.md` — Reviewer Rejection Protocol applies; I name defects and stop

---

## § 1. Verdict summary

| Seat | Verdict | Net | One-line basis |
|---|---|---|---|
| Mahler-2 | **ACCEPT-WITH-EDITS** | 1 conceded scope, 3 defended cleanly | `_MISSING` sentinel is JSON-safe by *key presence*, not identity (defended); patch-readiness framing overstated for `workflow.py` (conceded); `retry_key:` render contract specified; `store_error` pre-condition for #236 confirmed in writing |
| Liszt-2 | **ACCEPT-WITH-EDITS** | 5/5 conceded — material | Exit-1=transient was the bug at the centre of the contract (now corruption); lint ran live with 71 violations, claim "two of four burns, not three" downgraded; `-warn` suffix was decorative bug; cancel-sentinel needs shared module + L9 lint; PSDrive prelude crash confirmed empirically |
| Brahms-2 | **ACCEPT-WITH-EDITS** | 5/5 conceded — sized | Speculative imports replaced with extant `ProviderError`/`ValidationError` + stub; three undefined helpers inlined into Patch C; Chaos-1's faux entry now real with proper `nodes_failed` synthesis; PROP-C wired to new `per_item_execution_counts`; Patch B grown by `--sequenced-exit-codes` shim mode; **honest LOC: 240 → 370** |
| Stravinsky-2 | **ACCEPT-WITH-EDITS** | 4/4 conceded — patched live | Records rewritten to `required init-only` per `Models/ValidateResult.cs`; `verify_receipts.ps1` gained the full Liszt prelude + `try/catch` around exit-code-as-signal `git` calls; retry-stack bound sharpened from 18 → 6 via `retry:` / `reroll:` mutual exclusion; receipts scope narrowed from "hallucination detection" to "tool-call-grounding floor" |
| Bach-2 | **ACCEPT-WITH-EDITS** | 4/4 with nuance | `AdoAuthenticationException` catch is dead code in the subprocess seam — replaced with `ExternalToolException` exit-code/stderr classification, ~120 LOC owned by Mozart (not 40); preflight is fail-fast (matches `JournaledActionDecorator.RunWithAsync:24`); `run-budget.json` already withdrawn per Boulez F-CONSOL-2; `checkpoint_key:` render must happen at validator load-time, fall back to timestamp at save-time |
| Wagner-2 | **ACCEPT-WITH-EDITS** | 3/3 — one new bug self-surfaced | Output-block `>-` semantics defended (a, b); but `workflow_error_gate.output.error` doesn't exist (self-caught new bug; corrected cascade form supplied); `cleanup:` × primary-failure cascade contract specified ("cleanup-failure-during-error-handling wins, single surfaced gate"); `when_choice` overload resolved by renaming to `via_route:` (gate option `value:` = script route `label:`) |
| Beethoven-2 | **ACCEPT-WITH-EDITS** | 3/3 conceded | `diagnose` verb exit codes pulled back to `0` always (verdict in JSON); ledger vs budget split (different question, different scope, both ship); `auth` removed from `retry_exhausted.operation_kind` enum — `auth_lapse_detected` supersedes, no duplicate emission |

**Net verdict (code-craft layer):** **No outright REJECT.** All seven seats patched on contact. No fabricated APIs that survive grilling. No silent state-corruption defaults that survive grilling. The two highest-severity defects — Liszt-2's `exit 1 = transient` (Daniel-invariant violation) and Stravinsky-2's `verify_receipts.ps1` missing prelude (false-positive corrupt-state escalation) — were named, conceded, and patched in the same turn.

**Authority for the rewrite per Reviewer Rejection Protocol:** none invoked — every author addressed their own defects. Scribe re-issue per Boulez applies; the per-seat code-shaped corrections below should be folded into the same re-issue, not landed separately.

---

## § 2. Per-seat findings

Each finding is numbered (M/L/B/S/Bk/W/Be) for traceability and cites a charter rejection criterion. Author replies are summarised; full text in § 3.

### Mahler-2

**M-1 — Sentinel survival across JSON checkpoint round-trip.** Charter criterion: untyped seam-crossing data + clever-with-cost code.

The `_MISSING: object = object()` sentinel uses identity comparison (`is _MISSING`). If conductor checkpoints `WorkflowContext`, JSON cannot round-trip object identity. The patch is broken if the discriminator is identity. **Defended cleanly:** `context.py:597-604` (`to_dict`) deep-copies `agent_outputs` to a plain dict; `from_dict` rebuilds from `data.get("agent_outputs", {})`. The sentinel never enters `agent_outputs`; it appears only as the default in `.get(name, _MISSING)` inside `_add_agent_input`. The discriminator the patch relies on is **key presence**, not sentinel identity — and `{"flag": null}` JSON-round-trips as `{"flag": None}` with key intact. Sentinel survives because it doesn't have to. Hold.

**M-2 — Patch-readiness framing overstated.** Charter criterion: misleading names + lying comments (extended: misleading scope claims).

§1.3 said "ready to commit." §1.5 admitted `workflow.py`'s 4 hunks were "deliberately not resolved." A patch is not commit-ready when its highest-risk file is unread. **Conceded:** retracting "ready to commit"; §1.3 should read "ready to commit for `context.py` only." Patch is correct for its scope; scope was overstated.

**M-3 — `retry_key:` Jinja render-failure semantics undefined.** Charter criterion: missing docs + untyped seam-crossing data.

`retry_key: "{{ workflow.run_id }}:{{ node.name }}:{{ inputs.branch }}"` under `StrictUndefined` raises `UndefinedError` if `inputs.branch` is absent. Validator can catch only static absence, not runtime context-shape mismatch. **Specified contract:** render at first attempt, cache as string, pass via `CONDUCTOR_RETRY_KEY` to every retry. If render fails, attempt 1 fails with new `internal.retry_key_render_failed` kind; retry route precondition unmet; falls through to next matching `on_error:` route. Validator lints references statically via existing `_JINJA_ENV` at `validator.py:62`. Hold with the contract documented.

**M-4 — `store_error` linear history inflation deferred via TODO.** Charter criterion: no follow-up PR defense (clarified: applies to defects in *this* PR).

PR #229 doesn't add retry; the bug doesn't manifest until #236 lands. TODO acceptable IF the retry implementer (sonnet-mahler) is on-the-record blocked from #236 without addressing it. **Committed in writing:** the retry implementer cannot merge #236 without either (a) fixing `store_error` to overwrite/tag-by-attempt, or (b) preventing linear `agent_history` inflation under retry. Recorded for the squad ledger.

---

### Liszt-2

**L-1 — `exit 1 = transient` violates Daniel's "don't let corrupt state move forward" invariant.** Charter criterion: convention violations without justification + clever-with-cost code. **Severity: highest in the entire seven-seat review.**

§1.2 reserves code `1` for "un-classified throws" and treats it as `transient` for routing. An unclassified throw means we have no evidence the script's side effects are bounded — half-pushed refs, half-written watermarks, half-mutated PR state are all live possibilities. "Transient" means engine auto-retries. Auto-retry against an unknown world = re-execution of partial state = corrupt state moving forward, by definition. **Conceded:** code `1` → **class: corruption**, kind: `unclassified_throw`. Routes to human gate. Never auto-retried. Codes 1 and 5 collapse to the same class; the difference is provenance (declared vs accidental), preserved in the `kind`. Contract table updated.

**L-2 — `Lint-ConductorScripts.ps1` claimed working without execution.** Charter criterion: missing tests for new code.

§0 bullet 4 made the testable claim "StrictMode + ErrorAction would catch three of four burn cases at first keystroke." Code was shipped without execution-evidence. **Author ran it live:** 71 violations across 22 scripts in `polyphony\scripts\` + `.conductor\registry\scripts\`. Probe at `.squad/handoffs/liszt-2-lint-probe.ps1`. L4-warn alone fires on 22/22. Material correction: the "three of four burns" claim **downgrades to "two of four — the structural ones."** Burn case 4 (`resolve-pr-policy.ps1` exit-code swallow) is a semantic logic bug the lint cannot detect.

**L-3 — `-warn` suffix decorative; warnings trip non-zero exit.** Charter criterion: misleading names.

`Lint-ConductorScripts.ps1` ends with `if ($violations.Count -gt 0) { exit 1 }`. L4 violations marked `'L4-warn'` count toward `$violations` and trip exit 1. The suffix lies. **Conceded:** split counters (`$errors`, `$warns`); exit 1 only on `$errors.Count`. Day-one CI fails on L1/L2/L3/L5/L6 (hard); L4/L8 informational until baseline is clean.

**L-4 — Cancel-sentinel contract requires `Test-CancelRequested`; function is never defined and lint never enforces.** Charter criterion: missing docs + convention violations without justification.

§3 Rule 6 mandates `Test-CancelRequested` between long-running operations. Helper never defined; lint script never enforces. Contract enforced by convention only. **Conceded with fix:** shared `scripts/ConductorScript.psm1` module exports `Test-CancelRequested`, `Write-Stderr`, `Write-ErrorAndEnvelope`, `Write-AtomicJson`, `Read-WatermarkWithFence`. Lint L9 (heuristic: `Start-Sleep` ∨ `WaitForExit` ∨ `while ($true)` ⇒ MUST contain `Test-CancelRequested` call). **Honest residual gap:** L9 catches edits, harness fuzz-cancel catches runtime; no IDE enforcement.

**L-5 — Prelude `[Environment]::CurrentDirectory = $PWD.ProviderPath` throws under non-FileSystem PSDrive.** Charter criterion: locale/encoding fragility (extended: provider/cwd fragility).

`$PWD.ProviderPath` is empty string on `Env:\`, `HKLM:\`, `Cert:\`; `.NET` setter rejects empty same as null; throw fires before script body executes; operator sees a prelude crash with no script context. **Confirmed empirically** under `Set-Location Env:`. **Conceded:** guard with `if ($PWD.Provider.Name -eq 'FileSystem' -and -not [string]::IsNullOrEmpty($PWD.ProviderPath))`.

---

### Brahms-2

**B-1 — Patch A imports nonexistent exception classes.** Charter criterion: code that doesn't compile + untyped seam-crossing data.

`from conductor.exceptions import AgentOutputDecodeError, ProviderError` — neither resolves on `main` today as the patch is written. **Verified against `conductor/exceptions.py`:** 13 classes present; `AgentOutputDecodeError` absent; `ProviderError` at line 9 confirmed. **Conceded:** Patch A now imports `ProviderError`, `ValidationError`, `AgentTimeoutError`; new `HarnessInjectedMalformedJson(ValidationError)` stub for the malformed-JSON case, swap-pointed for the real class when #236 lands.

**B-2 — Patch C calls three undefined helpers.** Charter criterion: code that doesn't compile.

`_first_failure_time`, `_parse_timestamp`, `_first_call_index` — none defined in patch, none in `tests/harness/driver/trace.py`. **Conceded:** all three now defined inline in Patch C. Patch C size revised 80 → 140 LOC.

**B-3 — Chaos-1 scenario silently passes without an unspecified 4-LOC `TraceRecorder` extension.** Charter criterion: missing tests for new code + clever-with-cost code.

`no_cli_calls_after: { architect_agent: [...] }` keyed on an agent name; `script_failed` events don't synthesise across agent failures; absent constraint = green scenario regardless of injected fault. **Verified against `engine/workflow.py:2186`:** `script_failed` exists; no `agent_failed`. **Conceded:** Patch C now ships `TraceRecorder.nodes_failed` synthesising across `script_failed` + `agent_timeout` + `parallel_agent_failed` + `for_each_item_failed` + `subworkflow_failed` + `workflow_failed`. Chaos-1's faux entry replaced with real one + `xfail-until-Patch-A+C` banner.

**B-4 — PROP-C calls `per_item_execution_counts` not implemented.** Charter criterion: code that doesn't compile.

**Conceded:** `TraceRecorder.per_item_execution_counts` added to Patch C; PROP-C wired to it.

**B-5 — `harness-failing-script` is a "tiny helper" not in Patch B.** Charter criterion: missing tests for new code (test scaffold itself absent).

PROP-A through PROP-E cannot run without it. **Conceded:** new **Patch B-extended** documents `--sequenced-exit-codes` argv mode (~60 LOC, C# + Python helper). §2 now has explicit dependency banner ranking all five PROPs as gated on B-extended + C.

**Net for Brahms-2:** advertised cost was honest about gaps once named; **honest total: ~370 LOC across two PRs (was ~240).**

---

### Stravinsky-2

**S-1 — Discriminated-union records use primary-constructor positional syntax; polyphony convention is `required init-only`.** Charter criterion: convention violations without justification.

`Models/ValidateResult.cs:3-18` is the canonical shape: `public sealed record FooResult { public required int X { get; init; } }`. Stravinsky's proposed `public sealed record EvidenceReviewApproved(string Comment, ...) : EvidenceReviewOutcome(Comment, ...)` is incompatible — different construction syntax, different inheritance pattern, no `[JsonDerivedType]` precedent in `src/Polyphony/Models/`. **Conceded:** §5a/b/c rewritten to init-only-with-`required`. New idiom flagged explicitly as a polyphony-first introduction of `[JsonPolymorphic]` + `[JsonDerivedType]`; defended against the established skill convention.

**S-2 — `verify_receipts.ps1` example missing Liszt-2 §4.1 prelude; native commands run unfenced.** Charter criterion: convention violations without justification + locale/encoding fragility. **Severity: high — would generate false-positive corrupt-state escalations.**

`git rev-parse "origin/$($env:IMPL_BRANCH)"` and `git diff --name-only` run without `$PSNativeCommandUseErrorActionPreference = $true`. A `git` failure (missing `IMPL_BRANCH`, fetch failure) silently continues; `$diffFiles` becomes empty; every `inspected_files` entry then "not in the diff" → violations spam → workflow routes to `hallucinated_success_gate` for a **tooling failure**, not a reviewer hallucination. **Conceded:** example rewritten with full prelude (lines 502-506), `try/catch` around `git rev-parse` distinguishing tooling failure (`error_code: git_ref_unresolvable` → `workflow_error_gate`) from receipts violation (→ `hallucinated_success_gate`), plus `try/catch` around `git merge-base --is-ancestor` (uses non-zero exit as boolean signal, which `$PSNativeCommandUseErrorActionPreference` would otherwise throw on). Routes table grew a third arm.

**S-3 — Retry-stack bound: `RetryPolicy.max_attempts × retry.max × reroll.max` = 18 LLM calls per failing node.** Charter criterion: missing docs + clever-with-cost code (cost: real $).

§3 introduces `reroll:` alongside Mahler's `retry:`; provider already retries 3×. Stack as written: `3 × (1 + 1) × (1 + 2) = 18`. **Conceded with sharpened bound:** `retry:` (Mahler) and `reroll:` (Stravinsky) are **mutually exclusive on `type: agent` nodes**; agents only get `reroll:`. Bound: `(1+1) × 3 = 6` LLM calls max per node. Validator-rule ask added for Mahler-2's RFC.

**S-4 — Receipts pattern detects tool-call failure, not hallucination.** Charter criterion: misleading names.

The patch claimed hallucination detection. If the agent has the diff in context, it can fabricate `inspected_files` AND `reviewed_sha` trivially — both receipts. The pattern detects **agents that don't have the diff** (truncated context, tool-call failure). **Conceded with scope correction:** §2 title and top paragraph changed — "tool-call-grounding floor, not hallucination detector." Explicit list of what receipts catch (1–3: didn't-see-artifact class) vs what they don't (4–6: competent-rubber-stamp class). The rubber-stamp class needs P11 rubric scoring with verifier-checkable sub-dimensions, flagged to whoever owns PR #8.

---

### Bach-2

**Bk-1 — `AdoAuthenticationException` catch is dead code in the subprocess seam.** Charter criterion: dead code + misleading names. **Verified in the polyphony source:**
- `ExternalToolTimeoutException` exists at `Infrastructure/Processes/ExternalToolTimeoutException.cs` ✓
- `AdoAuthenticationException` exists at `Infrastructure/AzureDevOps/Auth/AdoAuthenticationException.cs` ✓ (also thrown at `AdoAccessTokenProvider.cs:112`, caught at `AdoClient.cs:69` and in 7 `Pr*Ado` verbs).

But — and this is the defect — **`AdoAuthenticationException` never reaches `SeedChildren`.** twig is a subprocess; ADO auth failures via twig surface as `ExternalToolException` (non-zero exit + stderr), not as `AdoAuthenticationException`. The catch block as written is dead. **Conceded:** replace with classification on `ExternalToolException` exit code / stderr (`"401"|"403"|"TF400813"` → auth; else unknown). Owner: Mozart, in `Infrastructure/Processes/`. **Honest cost: ~120 LOC, not 40.**

**Bk-2 — S3-fix journal preflight failure-mode unspecified.** Charter criterion: missing docs + swallowed exceptions risk.

If `journal.CountAsync` throws (SQLite locked, journal path inaccessible), what happens? **Conceded with explicit posture:** **fail-fast.** Matches existing pattern at `JournaledActionDecorator.RunWithAsync:24` — `RecordStartAsync` awaited before action body; SQLite throw aborts verb before mutation. S3-fix preflight adopts the same posture: throw propagates, verb exits non-zero with `kind: "journal_unavailable"`. Never catch-and-default-to-zero (would make the circuit breaker bypassable by inducing a journal failure).

**Bk-3 — `run-budget.json`: unbounded growth + no atomic-write + no concurrent-verb lock.** Charter criterion: resource leaks + races + convention violations.

Already addressed at the architecture layer by Boulez F-CONSOL-2; `run-budget.json` is **withdrawn** by Bach in favour of journal-derived counters. Confirmed by Bach-2 in grilling response: "Withdrawn per Boulez F1." For the record, all three Ravel-flagged defects were real: (a) unbounded `events` list, (b) no pruning at write under `window_seconds`, (c) two concurrent verbs would race the write with no atomic-rename or lock. Moot.

**Bk-4 — `checkpoint_key:` Jinja render-failure contract undefined.** Charter criterion: missing docs.

Cited contract elsewhere: `CheckpointManager.save_checkpoint` "never raises." So an `UndefinedError` at template render either silently drops the save or falls back. **Conceded with specified contract:** template render must happen at **workflow load time** in `config/validator.py`, not at save time. Undefined input → workflow refuses to load (loud, early). Renders-to-empty at save time → `CheckpointManager.save_checkpoint` logs `WARN checkpoint_key rendered empty, falling back to timestamp` and uses timestamp key. Never silently drops save (preserves "never raises"); operator sees the warning; resume still works (degraded to timestamp lookup).

---

### Wagner-2

**W-1 — Silent-loss fix's Jinja output block: three latent semantics issues.** Charter criterion: untyped seam-crossing data + missing tests.

(a) `>-` folds newlines into spaces — leading-space risk for `abandoned: " true"`. **Defended:** single-line block produces no fold; renders to `"true"` / `"false"`; per M7 convention (`actionable.yaml:157`), parent compares string-true.
(b) `workflow_error` empty case becomes `""` with possible newline. **Defended:** `{%- else %}` strips fold-space; `{% endif %}` terminates with no content; `>-` strips trailing newlines. Parent's `output.workflow_error != ''` correctly evaluates false.
(c) **NEW bug self-surfaced by author under grilling:** the proposed fix referenced `workflow_error_gate.output.error`, but `workflow_error_gate` is a `human_gate` whose output shape is `{ choice: ... }`. **There is no `.error` field on it.** The error originates upstream and must be cascaded from `executor_router.output.error`, `ensure_evidence_branch.output.error`, `compose_addendum.output.error`. Corrected cascade form supplied; §3.0 updated. Author caught his own bug before I caught it; charter credit.

**W-2 — `cleanup:` × primary-failure cascade undefined.** Charter criterion: missing docs.

Conductor was already routing to `workflow_error_gate` (primary error route); cleanup itself fails → wants to route to `cleanup_failed_gate`. Two competing targets. **Specified contract:**

| Primary | Cleanup | Surfaced |
|---|---|---|
| ok | ok | primary route |
| ok | fail | `on_cleanup_failure` route |
| fail | ok | primary error route |
| fail | fail | **`on_cleanup_failure` route only** — prompt MUST render both `$conductor.primary_error` and `$conductor.cleanup_error` |

Single surfaced gate, never two. Author writes one cleanup-failed gate prompt with both error contexts. Adding to §4.3 as the contract paragraph.

**W-3 — `when_choice` is source-type-overloaded; matcher would smell.** Charter criterion: convention violations without justification + misleading names.

Same key meaning different things for script vs gate predecessors. **Conceded with clean rename:** `via_route:` matches `route.label` on scripts OR `option.value` on gates — same field name, statically resolvable from the source node's `routes:` block. No source-type-dependent matcher. Updating §2.5.

---

### Beethoven-2

**Be-1 — `polyphony run diagnose` exit-code 0/1/2 collides with extant `ExitCodes.cs` 0/1/2/3/4 contract.** Charter criterion: convention violations without justification. **Verified in source:** `ExitCodes.cs` defines `Success=0, RoutingFailure=1, ConfigError=2, CacheError=3, HealthCheckFailed=4`. Beethoven's 0/1/2 verdict mapping silently corrupts existing wrappers. Boulez F-CONSOL-9 flagged the same conflict at the architectural layer. **Conceded with option (b):** `diagnose` returns **0 always**; verdict lives in JSON output (`{ "verdict": "safe" | "review" | "do_not_resume", ... }`). Wrapper script (`Invoke-PolyphonySdlc.ps1`) parses the JSON field. Rationale: `diagnose` is a *read* command — its exit code should reflect "did the diagnostic itself succeed," not "what did it find." §5.4 exit-code line struck; verdict is JSON-only.

**Be-2 — `RetryLedger` field in manifest vs Bach-2's `run-budget.json`.** Charter criterion: convention violations (cross-seat duplication).

Both addressed at architecture layer by Boulez F-CONSOL-2. Beethoven's grilling reply distinguishes the two as different scopes (ledger = per-(node, item, error_kind, window) attempt counter; budget = root-scoped overall envelope cap). Bach has since canonicalised both as **journal-derived queries, not separate stores** — Bach owns durability; Beethoven's logical materialisation queries journal at node-entry and caches in-memory per-run only. Coordinated post-grilling.

**Be-3 — Vocabulary collision: `retry_exhausted(operation_kind: auth)` vs separate signal `auth_lapse_detected`.** Charter criterion: misleading names.

Either an exhausted auth retry emits both (channels receive duplicates), or one (which? by what rule?). **Conceded with rule:** auth removed from #11's `operation_kind` enum. Auth exhaustion emits **only `auth_lapse_detected`** (signal #14). Rationale: auth failures aren't retryable in the same sense — N identical-failure retries are not N attempts at a self-healing condition. Operation-kind enum trimmed to 6 (`network, provider, rate_limit, git_push, ado_write, gh_call`). Channels never receive duplicates.

---

## § 3. Grilling transcripts

Full author replies preserved in each agent's session history. Key concession quotes:

- **Mahler-2 on M-1:** *"You're wrong, but the question is sharp. Citation: `context.py:597–604` (`to_dict`) and `:620` (`from_dict`). … The sentinel is **never stored** in `agent_outputs`; it appears only as the default in `.get(agent_name, _MISSING)`. … Sentinel survives because it doesn't have to."* — defended.
- **Liszt-2 on L-1:** *"Conceded. Exit 1 = transient IS the bug. Daniel's invariant: 'don't let corrupt state just move forward.' … Correction: Exit 1 → `class: corruption`, kind: `unclassified_throw`. Routes to human gate. Never auto-retried."* — material correction.
- **Liszt-2 on L-2:** *"Ran it. 71 violations across 22 scripts. Probe script at `.squad/handoffs/liszt-2-lint-probe.ps1`. … 'StrictMode + ErrorAction catches three of four burns' downgrades to two — the structural ones; the policy-swallow is a semantic bug a lint can't catch."* — material downgrade.
- **Brahms-2 on B-1/2/3/4/5:** patched live; honest LOC count moved from 240 to 370. *"Reflects the real cost honestly."*
- **Stravinsky-2 on S-3:** *"Your 18 is correct if all three layers stack. Asserted bound: `retry:` (Mahler) and `reroll:` (mine) are mutually exclusive on `type: agent` nodes. With that: `(1+1)×3 = 6` LLM calls max per node."*
- **Stravinsky-2 on S-4:** *"Scope correction. 'Tool-call-grounding floor, not hallucination detector.' Section title changed."*
- **Bach-2 on Bk-1:** *"My catch block is dead code. … Real ~120 lines, not 40."*
- **Wagner-2 on W-1(c):** *"Real bug you didn't name but I owe you: my proposed fix references `workflow_error_gate.output.error` — but `workflow_error_gate` is a `human_gate` whose output is `{ choice: ... }`. There is no `.error` field on it."* — author found bug Ravel missed; net code-quality outcome positive.
- **Beethoven-2 on Be-1:** *"CONCEDE the conflict, choose (b). … `polyphony run diagnose` returns 0 always; verdict lives in JSON output. … verdict is data, not a failure mode of the verb."*

---

## § 4. Proposed surgical inline edits

Per charter: typo-class / pure-craft only; no semantic changes. The substantive corrections above belong in the Scribe re-issue, not in inline edits.

**None applied.** Every defect named here is large enough that an inline patch would muddle the audit trail. Authors have updated their own handoffs (Stravinsky-2 and Brahms-2 already edited the originals in place under grilling; Liszt-2 announced a `liszt-2-ravel-grilling-addendum-20260531.md` rather than a rewrite to preserve "what I shipped vs what Ravel caught"). The transparency is more valuable than uniformity.

---

## § 5. Convention updates required (skill-doc deltas)

Findings that imply existing skill docs need amendment:

1. **`polyphony-cli-developer/SKILL.md`** — `ExitCodes` section (line 158-167) is stale: `HealthCheckFailed = 4` exists in `ExitCodes.cs` but is missing from the skill doc. Boulez F-CONSOL-9 + Ravel Be-1 both depend on the canonical list being correct. Skill doc owner should add the row and reaffirm "these are the **only** exit codes" alongside.

2. **`polyphony-cli-developer/SKILL.md`** — `Result records and JSON serialization` (line 104-147) currently prescribes `required init-only` records as the only pattern. Stravinsky-2's discriminated-union proposal (now corrected to init-only syntax) introduces `[JsonPolymorphic]` + `[JsonDerivedType]` for the first time in polyphony. Skill doc should add a paragraph on polymorphic results with the established AOT-safe pattern, since this will recur (every verb output that needs an open verdict set will hit the same shape).

3. **`watermark-poll-pattern/SKILL.md`** — currently does not enumerate that the pattern's `Write-WatermarkAtomic` (lines 60-68) is the **canonical** atomic-write idiom for *any* per-run JSON file polyphony scripts produce. Bach-2's `run-budget.json` (now withdrawn) and any future per-root state file should be governed by it. Skill doc should add a "Beyond watermarks" section naming the pattern as the load-bearing single-writer atomic-write convention for the script fleet.

4. **New skill doc: `conductor-script-prelude`** (proposed) — covers Liszt-2's §4.1 prelude (StrictMode + ErrorActionPreference + PSNativeCommandUseErrorActionPreference + FileSystem-guarded CurrentDirectory), the cancel-sentinel `Test-CancelRequested` helper, and the L1–L9 lint rules. Liszt-2 §4.3 + L-1 to L-5 grilling corrections form the content. Owner: Liszt for first cut.

---

## § 6. Cross-language seam audits

The defects below cross language boundaries and are not "owned" by any single seat. They surfaced during grilling but were not part of any one author's brief.

**X-1 — C# verb → PowerShell shellout → JSON envelope → conductor `step.error.kind`.** Mahler-2 owns the conductor side; Liszt-2 owns the script side; Bach-2 and Mozart own the verb side. The Mahler-convention envelope (`step.error.{kind, message, details}`) is documented in the Liszt-2 §1.2 contract and the Mahler-2 §3 sketch. Stravinsky's `verify_receipts.ps1` was the only seat-level proposal to actually wire this end-to-end (and it conceded a `try/catch` gap around native commands under grilling). **No remaining defect**; the convergence around Mahler's envelope is real, but the **test** of that convergence is Brahms-2's Patch C — which depends on the `nodes_failed` synthesis (Brahms B-3 concession) shipping. Sequencing: Brahms B-3 fix must land before any cross-seam regression test on envelope shape can run.

**X-2 — `CONDUCTOR_CANCEL_TOKEN` sentinel-file path lifecycle.** Liszt-2 introduces the env-var sentinel; the env-var carries a filesystem path. Who creates the parent directory? Who deletes the token file after cancel propagation? Race on first-use: script-A reads `CONDUCTOR_CANCEL_TOKEN` env var, calls `Test-Path`, file does not exist, returns `false`, continues; engine writes the token mid-poll; script-A misses the cancel. This is mostly mitigated by the §3 Rule 6 "check between every blocking operation," but only mostly. Add to Liszt-2's contract: the engine writes the sentinel file *atomically* (temp + rename, same volume) before signalling any waiting nodes, so `Test-Path` cannot observe a half-written file. Add to the shared module: `Test-CancelRequested` should `Test-Path -LiteralPath` (Liszt's helper does — confirmed in L-4 grilling response).

**X-3 — `CONDUCTOR_RETRY_KEY` env var: Liszt-2's script contract does not tell scripts to consume it.** Mahler-2 introduces `CONDUCTOR_RETRY_KEY` in §2.3 of his handoff; scripts that opt into `retry_key:` should be able to read it as an env var and emit it into their per-attempt artifacts (e.g., for journal correlation). Liszt-2 §4.1 prelude does not mention it; §3 Rules do not require scripts to surface it. **Defect:** the contract is one-sided. Either (a) the env var is set but never consumed by scripts (waste); or (b) scripts are expected to consume it but have no specification of what to do with it. Coordinate Mahler-2 + Liszt-2 on a one-line clause: "Long-running scripts that write to `$env:POLYPHONY_RUN_ID`-scoped artifacts SHOULD include `$env:CONDUCTOR_RETRY_KEY` (if set) in the artifact's `meta:` block for journal correlation."

---

## § 7. Open questions for Daniel

These are decisions the author replies did not resolve. None blocks any single seat from shipping; all need to be answered before the post-grilling re-issue is canonical.

1. **Unified retry-counter store ADR (`docs/decisions/retry-counter-placement.md`).** Bach-2 committed to drafting this after Boulez F-CONSOL-2. It defines: journal as canonical store; in-memory per-run cache for hot-path reads; query verb (`polyphony journal query`) for diagnostics. **Need:** Daniel's lock on "the journal is the only retry-counter persistence; no manifest field, no separate JSON file, no in-memory-only counter that survives crash." Default = yes.

2. **`retry_key:` validator strictness sequencing.** Boulez F-CONSOL-3 + Ravel M-3 both surface this. Mahler-2 holds firm: `retry:` cannot ship in Phase 2 without the hard-error validator on `retry_key:` + `node.idempotent: true`, else there's a known-unsafe window for non-idempotent nodes carrying `retry:`. **Need:** Daniel's accept of "ship-with, not add-on." Default = yes (matches the invariant).

3. **Exit-code 5 for `polyphony run diagnose` review verdict?** Be-1 conceded to "verdict lives in JSON, exit 0 always." Alternative: add `ReviewRecommended = 5` to `ExitCodes.cs` so wrapper scripts can branch without parsing JSON. Architecturally cleaner per Daniel's general posture (data in stdout, status in exit code). **Need:** Daniel's call on whether the verdict is data (exit 0, JSON-only) or status (new exit code). Default = exit 0 + JSON (simpler; matches Beethoven's concession; matches that `diagnose` is read-only).

4. **`ConductorScript.psm1` shared module location and ownership.** Liszt-2 L-4 concession creates a new shared PowerShell module. Where does it live in the polyphony repo (`scripts/ConductorScript.psm1`? `scripts/lib/`?), and who owns its tests? Default = `scripts/ConductorScript.psm1`, Liszt owns first cut, follows existing `scripts/*.Tests.ps1` Pester convention.

5. **`HarnessInjectedMalformedJson` lifecycle.** Brahms-2 B-1 concession introduces a stub exception in the harness's own exceptions module, "swap-pointed for the real class when #236 lands." The swap is a follow-up edit that must not be forgotten. Default = grep-able TODO + a fail-loud test once #236 lands; PR #236 author owns the cleanup.

---

## § 8. Reviewer note on process

Charter prescribed "name the defects, never propose the fix." Every author bar one proposed their own fix on contact. The exception is Mahler-2 on M-1, where I was wrong and he was right — the sentinel pattern is in fact JSON-safe because the discriminator is key-presence not identity. That is the cleanest possible outcome for an antagonistic review: the bar holds, the authors meet the bar, the cases where I miscalled are caught immediately. No agent in this set produced a fix-needs-fix loop.

**Recommendation:** Scribe folds the per-seat code-shaped corrections from §2 into the same re-issue Boulez requested. Ravel's review does not need a separate re-issue cycle.

*Filed: 2026-05-31. Ravel (Opus-4.7-high). Code-craft seat. Sister review: `boulez-review-error-deep-20260531.md`.*
