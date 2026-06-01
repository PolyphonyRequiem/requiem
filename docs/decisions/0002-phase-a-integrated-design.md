# ADR 0002 — Phase A Integrated Design

**Status:** Accepted (Daniel via integration-test validation, PR #11)
**Date:** 2026-05-31
**Supersedes:** none
**Superseded by:** —

## Context

Phase A produced 9 PRs (#1-#9), each shipping 2-3 runnable Python prototype
variants for one architectural seam plus a seat-author recommendation. The
integration demo (PR #11, Verdi-1) composed every recommended variant into
a single end-to-end workflow that runs in 90 ms, exercises the agent-team
pattern (PR #9), and proves INV-RESTART end-to-end via `demo_resume.py`.

Daniel ratified the integrated composition via integration test rather than
seam-by-seam variant selection. The composition becomes the v0 baseline; any
single variant can be reconsidered later by superseding this ADR.

## Decision

The Phase A seam recommendations are adopted as the v0 architecture:

| Seam | Variant adopted | Source PR | Files (post-promote) |
|---|---|---|---|
| Verb outcomes | Stravinsky B — PEP 604 sealed unions + `match`-case dispatch | #4 | `src/requiem/outcomes.py` |
| Event stream | Brahms-events B — envelope-loose JSONL + typed emit helpers | #5 | `src/requiem/events.py` |
| State machine kernel | Beethoven C — data-driven pydantic interpreter | #7 | `src/requiem/kernel.py` |
| Persistence | Bach A — pure event log; manifest dies as a concept | #2 | `src/requiem/persistence.py` |
| Agent boundary | Mahler A — Protocol-based `AgentProvider` + `FakeProvider` | #8 | `src/requiem/agent.py` |
| DSL (authoring) | Wagner A — fluent Python builder lowered to pydantic data | #1 | `src/requiem/dsl.py` |
| External-process | Liszt B+C hybrid — per-tool typed clients in a frozen `Toolbelt` | #3 | `src/requiem/toolbelt.py` |
| Harness | Brahms-harness B — pytest fixtures over the real engine | #6 | `tests/` |
| Agent teams | PR #9 pattern (see ADR 0003) | #9 + #11 | `src/requiem/teams.py` |

The promoted source layout lands as v0.0.1 via the `promote/engine-v0`
branch (Verdi-2 in flight at time of writing).

## Rationale

The strongest evidence is the integrated composition's behaviour: 9 seats
working independently against the same north-star invariants triangulated on
patterns that compose without integration friction. Specific evidence per
seam:

- **Stravinsky B + Beethoven C** — the `match` site on `outcome` inside the
  kernel reads as one arm per outcome variant; INV-CANCEL-SHORT-CIRCUITS-RETRY
  is a single explicit case. Verdi-1: "reads beautifully."
- **Bach A + Brahms-events B** — the loose envelope is exactly what the pure
  log wants. Replay → projection is ~30 LOC. The two seams compose without
  either adapting to the other.
- **Mahler A FakeProvider** — scripting by `agent.name` works for
  `parallel_fork` team-steps without modification; ~80 LOC, no library
  coupling. (Variant B's pydantic-ai was rejected for ~50 transitive deps
  and a competing retry loop inside ours.)
- **Wagner A + Beethoven C** — Wagner and Beethoven independently arrived at
  the same pattern: ergonomic surface (fluent builder) on top of canonical
  data (pydantic interpreter). One seat said "A on top of C", the other said
  "C as runtime, A as authoring sugar" — isomorphic recommendations.
- **Liszt B+C hybrid** — confining tool-version coupling to one client per
  tool (B) while preserving the "verb takes one injected object" testability
  of effect injection (C). Cross-platform bite caught at the runner
  (`NotADirectoryError` vs `FileNotFoundError`).

## Variants rejected (with reasoning)

Each rejection is from the corresponding PR; this ADR records them for
future challenge. Detail in the PR READMEs.

- **Stravinsky A** (pydantic discriminated union, `kind: Literal[...]`):
  pydantic's `discriminator` does NOT make mypy narrow on `outcome.kind ==
  "x"` — runtime contract and type-checker contract diverge. Strong at the
  JSON seam, weak at the dispatch site.
- **Stravinsky C** (ABC sealed hierarchy + visitor `dispatch(handler)`):
  more boilerplate for the same exhaustiveness story; class-wide enforcement
  not worth the ceremony when match-case + `assert_never` cover function-local.
- **Brahms-events A** (typed-per-kind discriminated union): readers must
  catch `union_tag_invalid` for every unknown kind in mixed-version cohorts;
  schema evolution is expensive vs B's loose envelope.
- **Brahms-events C** (CloudEvents 1.0 envelope): extension attributes must
  be flat primitives, so Stravinsky's `Outcome` can't live in the envelope
  anyway — kills the envelope-as-lens pitch. Defer until a real
  out-of-process consumer materialises.
- **Beethoven A** (class-based nodes + transition table): more boilerplate
  per workflow than C; topology isn't first-class data.
- **Beethoven B** (async-coroutine kernel): async-ness was mostly cosmetic
  at the kernel layer; async belongs IN verbs, wrapped through the registry.
- **Bach B** (event log + periodic snapshots): the snapshot verifier
  dissolves the snapshot's own benefit (must re-fold to validate).
- **Bach C** (SQLite view + JSONL truth): a clean additive upgrade for when
  the UI backend asks queries A can't serve in <10 ms — but premature for v0.
- **Mahler B** (pydantic-ai): ~50 transitive deps, FakeProvider couples to
  library-internal message-part taxonomy, library has its own retry loop
  running inside ours.
- **Mahler C** (LiteLLM direct): most code per agent, no cross-agent reuse.
  Right shape if we later decide agents are "just functions."
- **Wagner B** (decorator DSL): routes-list two-spotting is the same
  readability tax as `actionable.yaml` today; parameterised workflows get
  awkward.
- **Wagner C as authoring surface** (pure pydantic declarative): retained as
  the canonical engine input, but the author surface is A.
- **Liszt A** (single ProcessRunner protocol): doesn't confine tool-version
  coupling; verbs must hand-classify every tool's exit codes.
- **Liszt C as primary** (effect injection with signature introspection):
  too much magic at the call site; the Toolbelt borrows C's "one injected
  object" idea without the introspection.
- **Brahms-harness A as primary** (YAML scenarios): assertion ceiling is the
  YAML schema; INV-RESTART needs Python scaffold anyway. Retained as a
  migration veneer.
- **Brahms-harness C as primary** (record/replay): INV-RESTART can't be
  recorded; sub-workflow scoping needs special-case `agent_scope` per call.

## Consequences

### Positive

- Engine + verbs + UI backend in one Python process (INV-SINGLE-PROCESS) is
  proven viable; integrated demo runs in 90 ms.
- All 7 north-star invariants are exercised by the walking skeleton + its
  resume test.
- The pattern across seats (typed at the call site, validated at the JSON
  boundary, loose at the wire) gives uniform ergonomics regardless of which
  layer you're working in.
- The agent-team pattern (ADR 0003) composes from these primitives without
  engine overhaul — the squad pattern Daniel is using to build Requiem
  becomes a first-class Requiem workflow shape.

### Negative / load-bearing follow-ups

- `BadOutput` placement: Mahler treats it as its own outcome variant
  (distinct from `PermanentFailure` because it must NOT be network-retried).
  Verdi-1 collapsed it for the demo. Verdi-2 promotes it to a 6th outcome
  variant — see ADR 0003 / 0004 for cross-cutting policy.
- Resume logic: Verdi-1's `_reconstruct` is 70 LOC with 4 cases.
  Verdi-2 attempts a state-struct-based simplification. If the simplification
  doesn't reduce LOC and complexity, the original stands.
- 27 prototype directories under `prototypes/*/variant-*` will be retained
  as reference (not deleted) but explicitly NOT promoted. They serve as
  artifacts for the rejected-variant reasoning above.

### Open invariant candidates (surfaced during Phase A; pending ADR)

- "Sub-workflows must filter `last_completed_node()` by `run_id`" (Brahms-harness PR #6)
- "Gate resume reads the *resolved choice*, not the recorded outcome key" (Beethoven PR #7)

These become candidates for north-star §2 amendment in a follow-up ADR.

## Alternatives considered (whole-design level)

- **Fork conductor + polyphony as separate processes.** Rejected before
  Phase A as the inverse of ADR 0001.
- **Defer Phase A; build walking skeleton first; let composition pressure
  shape the seams.** Rejected because the walking skeleton is itself a
  composition test; you need rough seam shapes to compose. Phase A produced
  those rough shapes in one evening of parallel agent work.
- **Per-seam ADR (0002-0010).** Rejected in favour of this single
  consolidated ADR. Per-seam tradeoff analyses live in their PR READMEs;
  duplicating into ADRs adds maintenance burden without informational
  value. If a seam decision is later challenged, the challenge becomes its
  own ADR superseding the relevant table row.

## References

- PRs #1-#9 (Phase A seam prototypes)
- PR #11 (Verdi-1 walking skeleton)
- `docs/phase-a-demos.md` (curated demo plan; PR #10)
- `docs/patterns/agent-teams.md` (PR #9)
- `docs/references/error-handling-deep-dive.md` (inheritance from prior session)
