# ADR 0004 — Cross-Cutting Defaults

**Status:** Accepted (defaults applied; supersede individually as needed)
**Date:** 2026-05-31
**Supersedes:** none

## Context

Phase A surfaced 7 cross-seam open questions that affect multiple
implementations and would be ambiguous if left unanswered. This ADR records
the v0 defaults so Phase B agents have a stable foundation. Any individual
default may be superseded by a follow-up ADR once we have production
evidence; until then, these are the answers.

## Decisions

### 4.1 Verb-by-reference vs verb-by-name

**Decision:** by-reference for in-process; by-name only at the harness seam.

The kernel holds direct callable references in the workflow data model
(`Verb` is a wrapper around `Callable[..., Outcome]`). The harness layer
takes a name-keyed registry so YAML scenarios and CLI invocations can refer
to verbs by string. Conversion happens at the registry boundary.

**Rationale:** by-reference preserves IDE jump-to-definition, type
narrowing, and refactoring safety. The harness still gets a string seam
for declarative scenario authoring without forcing the engine to thread
names through every dispatch.

**Affected:** Wagner Q-1, Beethoven Q-K1.

### 4.2 `error_kind` taxonomy — closed enum with escape hatch

**Decision:** closed enum at v0 tied to the 21-signal seed catalogue, with
an explicit `extensibility_escape: dict[str, Any] | None` field on
`PermanentFailure` and `RetryableFailure` for cases the enum doesn't cover.

**Rationale:** closed enum gives the UI a finite color/icon vocabulary
(Debussy's "outcome-kind as color" pattern), enables exhaustive
forensic analysis, and forces deliberation when a new error kind is needed
(amend the enum + ADR). The escape hatch prevents the closed taxonomy from
blocking emergency work.

**Affected:** Stravinsky Q-3.

### 4.3 Event stream topology — single `.events.jsonl`, two lenses

**Decision:** one `.events.jsonl` per run carrying both engine events and
domain signals; domain signals have `kind="domain_signal"` as a distinguishing
envelope value. The "two lenses" are filter expressions on the single
stream, not separate files.

**Rationale:** Brahms-events recommended this in PR #5; Debussy seconded it
in `perspectives/ui-sdlc/03-ui-pattern-catalogue.md`. One file means: one
fsync watermark, one offset per consumer, one tail. Two files would have
forced ordering reconciliation across streams.

**Affected:** Brahms-events Q-1, Debussy Q-1.

### 4.4 `receipts` placement — peer field on the outcome envelope

**Decision:** `receipts: list[Receipt] = []` is a peer field on every
outcome variant (`Success`, `RetryableFailure`, `PermanentFailure`,
`NeedsHuman`, `Cancelled`, `BadOutput`), not nested inside `Success` only.

**Rationale:** failure forensics matter more than success forensics.
"What did the verb actually do before it failed?" is the question that
debug sessions live or die by. Putting receipts on every variant adds
trivial cost and pays for itself the first time you triage a
`PermanentFailure` and want to know what changed before the failure.

**Affected:** Debussy Q-2.

### 4.5 `parallel_fork` primitive — adopt for v0

**Decision:** adopt. See ADR 0003.

**Rationale:** ADR 0003 §2.1. ~50 LOC kernel addition; unlocks agent-team
pattern; deferring forces every team-shaped workflow into clunky
sub-workflow invocations.

**Affected:** Beethoven Q-K7.

### 4.6 Pause-as-distinct-from-cancel — skip for v0

**Decision:** no `Paused` outcome; no `pause` operation distinct from
cancel. To "pause" a run, cancel it and restart-from-log later.

**Rationale:** Debussy lean (Q-3) — INV-RESTART makes restart-from-log
substantively equivalent to pause-resume. Adding a real pause primitive
requires the kernel to retain in-memory continuations distinct from event
log state, which complicates INV-EVENT-LOG-AUTHORITATIVE.

**Re-open trigger:** if a Phase C workflow surfaces a case where
restart-from-log is materially worse than pause (e.g., expensive verb that
just completed but state isn't yet checkpoint-safe), revisit.

**Affected:** Debussy Q-3.

### 4.7 Workflow versioning — recorded in `workflow_started`

**Decision:** every `workflow_started` event includes a
`workflow_version: str` field. Workflows declare their version via the
fluent builder (`Workflow("close-out", version="0.0.1")` or default `"0"`).
Replay against a different workflow version emits a
`workflow_version_mismatch` event and refuses to continue unless
`--force-replay` is passed.

**Rationale:** Beethoven Q-K8 — cheap to add now (one field), expensive to
retrofit (every replay would need a version-recovery heuristic). Version
mismatch on replay is exactly the case where INV-NO-CORRUPT-FORWARD demands
we stop rather than guess.

**Affected:** Beethoven Q-K8.

## Consequences

### Positive

- Phase B agents (and Verdi-2 promoting the engine) have unambiguous
  answers to every cross-cutting question. No design re-litigation in
  Phase B PRs.
- The escape hatches (4.2's `extensibility_escape`, 4.7's `--force-replay`,
  4.6's "re-open trigger") prevent the closed-decision defaults from
  blocking emergency work.

### Negative

- Closed enums in 4.2 and explicit version checks in 4.7 add small ongoing
  ADR maintenance — every new `error_kind` or workflow-shape change touches
  an ADR. Deliberate friction; the alternative is silent contract drift.
- 4.6 means we'll discover the pause-vs-restart tradeoff in production
  rather than design-time. Acceptable cost for staying on the
  restart-from-log invariant.

## References

- PRs #1-#9 (Phase A seam PRs, each containing the source open questions)
- ADR 0002 (Phase A Integrated Design)
- ADR 0003 (Agent Teams as First-Class Primitive)
- `docs/phase-a-demos.md` §"Open questions" — the original list as posed to
  Daniel
