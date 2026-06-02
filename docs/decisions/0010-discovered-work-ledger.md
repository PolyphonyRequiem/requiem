# ADR 0010 — Discovered-Work Ledger

**Status:** DRAFT (Wave 7 / overnight prototyping pass — design exploration; NOT YET RATIFIED)
**Date:** 2026-06
**Author:** Squad synthesis (Ravel · Satie · Fauré), recorded by the design seat.
**Supersedes:** none
**Superseded by:** —
**Cross-cuts:** ADR-0006 (merge-group topology — Q5 roll-forward, Q6 replan gate, Q7 `review_group`), ADR-0008 (curated artifacts across the run lifecycle, forthcoming — the curator pattern this ADR instantiates), ADR-0009 (spec-kit alignment — speckit `tasks.md` is the projection surface).

---

## TL;DR recommendation

Adopt a narrow, **append-only discovered-work ledger** as the operational
companion to ADR-0006 Q6's "HIGH-and-below findings roll forward within the
existing plan structure." When an implementer notices out-of-scope or
follow-up work mid-leaf, it has a legitimate, durable place to put it —
instead of silently scope-creeping into the current PR, dropping it, burying
it in a PR description, or over-escalating into a CRITICAL replan.

The reshaped idea is **not** a freely-mutable `tasks.md` the implementer
edits at will. It is:

1. **Capture (always-on):** the implementer emits `discovered_work[]`
   records; these are durable **events**, idempotent under crash+resume.
2. **Classify (curator pattern):** an agent evaluates each discovery → a
   deterministic disposition is taken against user policy → a human gate is
   the guaranteed fallback.
3. **Project (read-only):** the ledger folds into a discovered-work
   `tasks.md` view *beside* the frozen plan, plus an optional tracker item.
4. **Surface (curated):** completed discovered work is delivered in
   **curated batches at leaf boundaries**, not as a flood of per-task PRs.

For the single-operator audience the value is **attention preservation** —
Requiem remembers what the agent noticed so the operator need not
reconstruct it from logs, diffs, and vibes — not team coordination.

This decision is **DRAFT**: the recommended shape below is evidence-backed
by three throwaway prototypes (see "Evidence"), but four judgment calls and
one resolved-but-unconfirmed conflict are left for Daniel (see "Open
questions").

---

## Context

ADR-0006 Q6 narrowly gated mid-flight **replan** to a speckit-CRITICAL-class
invalidity (constitution/invariant violation, falsified foundational
assumption, impossible dependency structure, baseline-blocking coverage
gap). Everything **HIGH-and-below** was told to "roll forward within the
existing plan structure, NOT replan." Q5 chose curator-decided **roll-forward**
recovery. Together these decided *that* non-structural discovered work rolls
forward — but never specified *where it goes*. That gap is this ADR.

The seed (Daniel, 2026-06-02):

> "I want to discuss an idea around keeping some types of work items
> (process.yaml configured) such as tasks to be used BY THE implementation
> lifecycle as a working, live todo list. Discovered work can be put on that
> task list as an implementer works on something for incremental delivery and
> review. … This might be a bad and derailing idea, but I could see it
> holding value."

speckit (ADR-0009 L1) already produces a `tasks.md` checklist during
planning. So the genuinely new property is not "a task list" — it is **a
list the implementer keeps appending to as it discovers work, surfaced for
incremental review.** The squad's job was to isolate the marginal value of
the *live + discovered-work + incremental-review* properties and pressure-test
the "derailing" risk Daniel named.

Prior art for "process.yaml configured" work-item types is polyphony's
process-config: types carry **facets** (`plannable` / `implementable` /
`actionable`); "Task" is `[implementable, actionable]`. The config also
carries a `review_policies` block (per-stage `agent_review` / `human_review`
/ `auto_merge`).

---

## Decision (recommended shape — DRAFT)

### 1. Capture is always-on; the sink/surfacing is the optional part

There is **no clean `live_tasks_enabled` master toggle.** A prototype
(Fauré) empirically confirmed that even with the feature "off," an always-on
contract remains: the implementer still emits discoveries, the classifier
still must decide ignore/tracker/gate. A master toggle would fork every
executor, resume path, and completion rule into two mental models.

**Minimal always-on contract:** the implementer MAY emit `discovered_work[]`;
the curator classifies each; policy decides the disposition. What is
*optional* (config-gated) is whether discoveries become an in-run ledger +
`Task` sink and incremental delivery, versus being filed/deferred.

### 2. The ledger is event-log-first; `tasks.md` is a projection

Appends are durable events (`task_discovered` / `task_appended`) carrying a
stable Requiem id, a discovered-work-local id (`D###`, **not** speckit's
`T###`), parent placement, rationale, dependency hints, and a content-hash
**fingerprint**. The discovered-work `tasks.md` is a **deterministic,
read-only projection** of the log. This preserves
`INV-EVENT-LOG-AUTHORITATIVE` and `INV-RESTART`.

**Idempotency:** the fingerprint input is
`parent_leaf_id + work_item_type + normalized(title) + normalized(description)`.
A prototype (Ravel) proved across three crash points that this dedupes a
genuine re-discovery while keeping two distinct same-title discoveries
separate. (Raw-text fingerprints duplicate on whitespace drift; title-only
fingerprints lose distinct work — both rejected by the prototype.)

**Projection surface:** discovered work lives in a `tasks.md` **beside** the
frozen plan `tasks.md`, with its own `D###` id namespace. A single
regenerated `tasks.md` was prototyped and rejected: it dropped manual /
reviewer context and collided ids with planned `T###` tasks.

### 3. Classification follows the curator pattern (agent → deterministic → gate)

Disposition menu (validated complete by the Fauré prototype; `patch_now`
wins ties with `append_to_ledger`):

| Disposition | When | Effect |
|---|---|---|
| `patch_now` | tiny, same-scope, low-risk | folded into the current leaf's work with a receipt; no new Task |
| `append_to_ledger` | real follow-up work, in-plan-shape | new `D###` ledger entry / `Task` sink under policy |
| `file_tracker` | out-of-scope / non-blocking future work | deferred tracker item (twig/ADO) |
| `critical_replan_candidate` | speckit-CRITICAL structural invalidity | **nominate only** — cannot self-trigger replan; human confirms per Q5 |
| `discard` | duplicate / not actionable | no-op, with a receipt |
| `needs_human` | guaranteed fallback | human gate |

The Q6 boundary held in the prototype: only CRITICAL items nominate replan;
HIGH-and-below route with `replan=none`.

### 4. Placement: flat task, sibling of the nearest decomposable parent

A discovered task is **non-decomposable** (`can_decompose: false`) **but
attaches as a sibling leaf under the nearest decomposable ancestor** — NOT as
a child of the implementable leaf that discovered it. The Ravel prototype
showed child-of-current-leaf placement turns a declared *leaf* into a
*parent*, reopening the recursive↔flat impedance ADR-0009 fights;
sibling-of-nearest-decomposable-parent keeps `impl/{root}-{item}` branch
naming coherent. (This reconciles a squad conflict — see "Open questions
→ Q-1".)

### 5. Surfacing: curated batching at leaf boundaries

A simulation (Satie) over many synthetic runs found curated batching beats
incremental-per-task by ~25% fewer discovered PRs, ~52% fewer trivial
clicks, and ~19% lower weighted review surface. Root-only close-out was
rejected (produces 571–1219 LoC lumps). **Default:** curated batching at
leaf boundaries, batched by `review_group` (ADR-0006 Q7), with a **~120 LoC
early-surface floor** (never open a discovered-work PR below it mid-leaf;
roll into the batch unless risk-marked) and a **~300 LoC hard cap**.
Discovered-task PRs **reuse the existing `review_policies`** — a discovered
Task is just another impl slice, not a new review regime. The simulation
also gave independent evidence that `review_group` earns its keep: ~0.9
extra PRs/run buys the elimination of mixed-context review surfaces.

### 6. Anti-creep guardrails (unanimous)

Current-altitude only (a discovery cannot append above its altitude) ·
flat discovered tasks in v0 (`can_decompose: false`) · bounded count
(`max_items_per_leaf`) · curator gate on escalation · default to batched
review, not micro-PRs.

### Proposed `process.yaml` shape (DRAFT)

```yaml
types:
  Task:
    facets: [implementable, actionable]
    live_todo: { accepts_discovered_work: true, can_decompose: false }

curation:
  discovered_work:
    enabled: true            # gates the sink + incremental delivery, NOT capture
    sink_type: Task
    max_items_per_leaf: 5
    auto_append: low_risk     # auto-append low-risk; gate the rest
    patch_now: true           # allow tiny same-scope fold-in with a receipt
    gate_when: [uncertain_scope, touches_contracts, exceeds_budget]
    q6_replan_on: critical_only
    off_default: file_tracker # when disabled / declined, file don't page
    fallback: human_gate
    surface:
      batch_by: review_group
      early_surface_floor_loc: 120
      hard_cap_loc: 300
```

---

## Evidence (three throwaway prototypes, 2026-06-02)

All three ran in the gitignored `.requiem/prototypes/` scratch area; no
commits; nothing in `src/` or `docs/` was touched. Prototype code is
discardable; the `FINDINGS.md` artifacts are preserved in the session record.

- **Ravel — ledger + idempotency + placement.** Confirmed event-log-first
  idempotency across 3 crash points; derived the canonical fingerprint;
  **broke** "single regenerated `tasks.md`" (→ beside + `D###` namespace);
  **broke** "child of current leaf" (→ sibling of nearest decomposable
  parent).
- **Fauré — disposition router.** Confirmed the 6-way menu is complete after
  a `patch_now`-wins priority fix; empirically confirmed "optional is not
  byte-for-byte clean"; set OFF-default to `file_tracker`; confirmed the Q6
  nominate-only boundary.
- **Satie — surfacing simulation.** Quantified curated-batching-at-leaf as
  the default; rejected root-only close-out; gave independent evidence for
  `review_group`; derived the ~120 LoC anti-fatigue floor.

---

## Open questions for Daniel

These resolve before this ADR can move DRAFT → ACCEPTED. Recommend walking
them one at a time in the established cadence.

1. **Placement conflict (resolved-but-unconfirmed).** The squad split: Fauré
   assumed discovered tasks are children of the current leaf; Ravel's
   prototype showed that reopens recursive↔flat impedance. Proposed
   reconciliation (Decision §4): **flat / non-decomposable task, attached as
   a sibling leaf under the nearest decomposable ancestor.** Confirm or
   override.
2. **Visible-during-run vs leaf-closeout.** Satie's numbers favour surfacing
   PRs at leaf boundaries. Do you also want discovered work **visible live**
   (read-only, in the projected `tasks.md`) during the run, even though PRs
   surface at leaf close?
3. **`file_tracker` OFF-default.** Acceptable as the disabled/declined
   default? It presumes a tracker sink (twig/ADO) exists in v0. If not, the
   OFF-default must fall back to `needs_human` or `discard-with-receipt`.
4. **`patch_now` without a gate.** Comfortable letting an implementer fold
   tiny, same-scope, low-risk work into the current leaf's PR with only a
   receipt (no human gate)? Or should every new obligation become a `Task`?

---

## References

- **ADR-0006** Decision log — Q1 (reviewable-set reframe), Q5 (roll-forward
  recovery), Q6 (CRITICAL-gated replan; HIGH-and-below rolls forward), Q7
  (optional `review_group`).
- **ADR-0008** (forthcoming) — curated artifacts across the run lifecycle;
  this ledger is one instance of its agent-evaluate → deterministic-action →
  human-gate pattern.
- **ADR-0009** — spec-kit alignment; speckit `tasks.md` is the planning-side
  projection beside which the discovered-work `tasks.md` lives.
- **north-star** — `INV-EVENT-LOG-AUTHORITATIVE`, `INV-RESTART`,
  `INV-SINGLE-PROCESS`; single-operator audience (§5); Q1 phrasing "surface
  reviews the user is likely to care about, at a size that is reasonable."
- Prior art — polyphony `process-config.yaml` (facets; `review_policies`).
