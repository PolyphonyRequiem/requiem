# ADR 0027 — Reviewer escalation handling: failure modes, policies, and sidecar

**Status:** Amended (2026-07-11)
**Date:** 2026-06-17
**Relates to:**
ADR-0010 (planning tier model — defines decomposable/implementable
verdict cycle the reviewer participates in),
ADR-0025 (dogfood delivery path — Gap A introduced the implementable
short-circuit; this ADR addresses the symmetric case where the
PLANNER produces output but the REVIEWER blocks),
ADR-0026 (per-type process config — Deliverables surfaced richer
reviewer feedback than the prior flat-tier model did, which is why
escalation handling is now the gating issue).

## Context

Dogfood runs 4-14 (CVAPI Scenario `#62759077`) repeatedly hit
`escalation_gate` and died — even when the planner had produced
materially good plans. The escalation policy is the single largest
source of "the planner did real work and we threw it away."

Six failure modes flow into one undifferentiated `escalation_gate`
today, all blocking. They are NOT all the same kind of failure:

### Failure mode taxonomy (from observed dogfood evidence)

| Mode | Description | Real example | Plan ship? |
|---|---|---|---|
| **M1 — Reviewer-prompt bug** | Reviewer escalates because it thinks it's missing context that's actually there | Run 5: "cannot evaluate without seeing children" — fix was prompt-side (commit 6d8bbb5) | NO. System is broken; fix the prompt. |
| **M2 — Human-input-required** | Reviewer correctly identifies a scope/policy/PM decision the planner can't make autonomously | Run 14: "customer visibility of fallback SKU in portal/API — needs PM decision" | YES (plan is fine), but the question needs follow-through. |
| **M3 — Convergence failure** | Planner keeps producing different plans, reviewer keeps finding new (legitimate) concerns; classic moving-target | Run 12 Feature 2: 5 iterations, each addressed prior round's concerns, reviewer found new ones; ITER_CAP exhausted | MAYBE. Plan is *usable*; reviewer's incremental polish is endless. |
| **M4 — Quality floor** | Reviewer thinks plan is fundamentally wrong (wrong shape, missing entire concerns), not iterating toward acceptable | Hypothetical; haven't seen it cleanly | NO. Shipping a fundamentally-wrong plan creates bad ADO state. |

(M5-M8 — bad output, recursion gates, type-policy gates, twig/ADO
failures — are *not* escalation policy questions. They route to
their own dedicated gates with their own correct semantics.)

### The undifferentiated-gate problem

A global `--on-escalate=accept-last` would silently accept M4
escalations (dangerous: bad ADO state). A global `escalate` blocks
legitimate M2/M3 cases (today's pain, blocking the dogfood). The
right primitive is **always preserve the reviewer's escalation
reasoning as a durable artifact**, then let the operator's risk
tolerance decide whether to ship.

## Decision

### Two-part design (Shape C from the design conversation):

**Now (this ADR):**

1. **Always write a `escalation-feedback-{run_id}.md` sidecar** when
   `escalation_gate` fires, regardless of policy. The sidecar
   includes:
   - the run id and root item id
   - the iteration count + ITER_CAP at which escalation fired
   - the last reviewer's verdict + full feedback text
   - the last planner's output (children titles + types)
   - links/paths to the events.jsonl and plan tree for full forensic
   - explicit "Open questions" section (the reviewer's blocking
     concerns, surfaced for human follow-up)

   This is **always written** — even when policy=escalate (default,
   today's behavior). The sidecar IS the answer to M2: an operator
   can read it, resolve the open questions, and rerun planning.

2. **`--on-escalate=escalate|accept-last|abort` CLI flag** on
   `requiem-end-to-end`. Default `escalate` preserves today's
   behavior (no breaking change). The flag selects an auto-responder
   for `escalation_gate` specifically:
   - `escalate` (default): interactive prompt as today; sidecar
     always written
   - `accept-last`: auto-answer `proceed` to record the last planner
     output as `needs_human` for audit; the end-to-end driver then
     pauses before plan commit or fanout
   - `abort`: auto-answer `abort` (terminate the run); sidecar still
     written so the operator knows what was lost

   The flag affects ONLY `escalation_gate`. Other gates
   (bad_output_gate, type_policy_gate, recursion_depth_gate, etc.)
   still get the operator's interactive handler — they're correctness
   gates, not policy gates.

3. **No changes to the reviewer's prompt or output schema.** The
   reviewer continues to emit `{approve, revise, escalate}`. Per-
   reason classification is the Shape A follow-up (see below).

**Later (ADR-0028, planned):**

4. **Reviewer self-classifies the escalation reason** in its output
   (`escalation_reason: human_input_required | convergence_stalled
   | quality_inadequate | system_bug`). The policy becomes a map
   (`--on-escalate human_input_required:accept-last,quality_inadequate:escalate`).
   This is the M4-safe version. Deferred until we have empirical
   distribution data from N runs to know if M4 is real or
   hypothetical in practice — Shape A is significant reviewer-
   prompt churn and we shouldn't ship it speculatively.

### Mutation safety amendment (2026-07-11)

- A plan whose final verdict is `needs_human` is never an execution input,
  regardless of `--on-escalate`. `commit_plan` accepts only `approved`, and
  committed-leaf resolution/fanout enforce the same boundary.
- `accept-last` is retained as an audit convenience, not an authorization to
  mutate ADO or dispatch implementation. Operators resolve the recorded
  questions and rerun until planning produces an approved, aligned artifact.
- Planner overlap pins are validated from authoritative ADO parentage plus
  Requiem lineage markers. Fixable invalid pins receive bounded revision;
  ambiguous ownership or conflicting lineage remains genuinely HITL.
- Tests that exercise `gate_handler=_proceed_handler` continue to
  work (the new flag doesn't affect tests that supply their own
  handler; the handler chain composes — flag wraps default, tests
  override the whole thing).

### Recursive runs

When a child subworkflow's `escalation_gate` fires, the child's
sidecar is written to the child's log path
(`<log-dir>/<sub-run-id>.escalation-feedback.md`), and the parent
sees a `needs_human` outcome that bubbles per existing semantics
(ADR-0017). The policy is inherited by child runs via the
`gate_handler` propagation already in place
(`_active_gate_handler_cv` ContextVar).

## Scope of this ADR

**In scope:**

- `escalation-feedback-{run_id}.md` sidecar always written on
  `escalation_gate` entry
- `--on-escalate` CLI flag on `requiem-end-to-end` (default
  `escalate` = current behavior)
- Gate handler factory that translates the flag into an auto-
  responder for `escalation_gate` and proxies other gates to the
  inherited handler
- Tests pinning all three policies + sidecar content
- Re-run dogfood `#62759077`; any needs-human result must pause with a
  sidecar, and only a subsequent approved plan may seed or fan out

**Out of scope (Shape A / ADR-0028):**

- Reviewer schema change to include `escalation_reason`
- Per-reason policy map (`--on-escalate human_input_required:accept-last,...`)
- Routing the sidecar's "Open questions" automatically as ADO
  follow-up work items (would require more ADO surface than the
  current twig client exposes)

## STATUS log

- **2026-06-17 PROPOSED.** Plan written; no code committed against
  this ADR yet. Implementation order:
  1. `planning.py`: capture escalation context in
     `escalation_gate`'s prompt + write the sidecar; thread an
     `escalation_policy` field through `build_engine`
  2. CLI flag on `end_to_end.py` + gate-handler factory
  3. Tests: 3 policies × sidecar content × child-inheritance
  4. Re-run dogfood and confirm shipped Deliverables + sidecar
- **2026-07-11 AMENDED.** Generic `accept-last` continuation is no longer
  allowed to cross the mutation boundary. It records the unresolved plan and
  sidecar for audit, while `commit_plan`, leaf resolution, and fanout require an
  approved, proposal/children-aligned artifact. Overlap reuse is evidence-first:
  exact direct-child title/type, same-Scenario Requiem lineage, and unique
  ownership are required before a planner pin can survive review.
