# ADR 0016 — Legible Accumulation over Opaque Memory; Hermetic Execution

**Status:** Accepted
**Date:** 2026-06-05
**Relates to:** ADR-0003 (agent teams), ADR-0014 (Hermes fan-out executor)
**Supersedes:** none

## Context

Requiem mounts a Hermes worker fleet as its delivery substrate (ADR-0014).
This creates an architectural tension worth naming explicitly:

- **Requiem is a hermeticist.** Its kernel premise is *event-log-is-truth*:
  resume-fidelity, config snapshotted into the log (ADR-0015), fail-closed,
  type-routing-as-data. Its reason to exist in enterprise ADO-land is that a
  run is **reproducible, auditable, and fully on the record**.
- **Hermes is an accumulator.** Its pitch is "the self-improving agent with a
  built-in learning loop" — persistent per-profile memory that gets better the
  longer it runs.

The strategic question (2026-06-05):

> How much of this is "drop in a preconfigured container, point at a repo with
> a policy + process config, hit go" (a hermetic appliance) vs "use whatever
> systems you already have and accumulate knowledge for your repos over time"
> (an accumulative ecosystem integration)? The AI community is leaning toward
> the latter. What does that mean for us?

The naive accumulative path (opaque agent memory) would break the auditability
that justifies Requiem existing. The naive hermetic path (a frozen appliance
that never learns) would be rigid, high-friction, and lose to tools that
improve themselves. Neither pole is acceptable.

## Decision

Adopt a single cross-cutting principle:

> **Execution is hermetic. Learning is legitimate only when it is legible.**

Concretely:

1. **Per-run delivery is hermetic.** The worker fleet is pinned, isolated, and
   disposable. Task/item state lives only in Requiem's event log and the run's
   kanban board, and is discarded with the run. A run is replayable and
   explainable from the log alone.

2. **Knowledge accumulates only as repo-resident, version-controlled
   artifacts** — never as opaque agent memory. The repo *is* the memory. The
   knowledge substrate is three reviewable surfaces:
   - `process.yaml` — policy + routing (tier model, role→profile).
   - a **doctrine** artifact — house-style: conventions, test/branch/commit
     rules, the things a returning contributor "just knows".
   - **skills** — reusable worker how-to.

3. **The fleet is hydrated from those artifacts at run time**, then pinned and
   hash-recorded into the event log for that run. This collapses the two poles:
   "drop in the container" and "uses what's in your repo and accumulates" become
   the *same* mechanism — the container is built from the repo's accumulated,
   reviewed doctrine.

4. **Learning flows back as pull requests.** When a run discovers something
   durable (a recurring fix, a sharper routing rule), Requiem proposes an edit
   to `process.yaml` / doctrine — a normal PR a human reviews. Self-improving,
   fully on the record. No off-log behavioural drift.

5. **External memory providers (Holographic, Honcho, …) are an optional cache
   or index over repo-resident truth, never the authority.** Provenance always
   traces to a committed artifact + the event log.

## Distinction: house-style vs task-state

The line that makes this enforceable:

| House-style (may persist, as repo artifacts) | Task-state (must NOT persist in agent memory) |
| --- | --- |
| "tests run via targeted pytest, never full suite" | "item #88's PR is at `feature/88`" |
| "async clients use `*_async`; keep fakes in lockstep" | "last run I chose bcrypt for the schema" |
| branch/commit conventions, lint rules, layout | "the API leaf is blocked on the schema leaf" |
| type-routing is data in process.yaml | per-run ADO state |

House-style is stable, safe to persist, and *helps* a worker. Task-state is
per-item/per-run; if it bleeds across runs via profile memory, event-log-is-
truth breaks. The line is enforced by **isolation** (disposable per-run fleet),
not by a polite rule asking profiles to forget.

## Relationship to prior ADRs

- **ADR-0003** made agent teams a first-class *in-process* kernel primitive
  (`parallel_fork`, `.team()`, per-run charters) and explicitly **deferred**
  "persistent agent identity across runs" (no `agent_history` store for v0).
  This ADR does not revive `agent_history`. Cross-run identity is realised
  *externally* via Hermes profiles whose durable knowledge is repo-resident
  doctrine — legible, not an opaque history store. ADR-0003's in-process teams
  remain valid for Requiem's *own* reasoning (recursive planning, adversarial
  review); this ADR governs the *delivery* fleet.
- **ADR-0014** established Hermes kanban as the external delivery executor.
  This ADR supplies the governing philosophy; ADR-0017 supplies the applied
  architecture.

## Consequences

### Positive

- Reproducibility/auditability (Requiem's differentiator) is preserved while
  the system still improves over time.
- No bespoke memory subsystem to build: git + review *is* the learning loop.
- The tool self-documents — its learnings are first-class reviewable repo state.

### Negative / costs

- Every durable learning is a PR a human merges. In most domains this is
  friction; in enterprise SDLC it is usually a *requirement*, so we pay a cost
  the customer already wants paid.
- Requires discipline: any feature that introduces off-log behavioural state
  (hidden memory, ambient context the log doesn't capture) violates the
  principle and must be rejected or made legible.

## References

- ADR-0003 — Agent Teams as a First-Class Workflow Primitive
- ADR-0014 — Hermes fan-out executor
- ADR-0015 — Process-config routing (config snapshotted into the event log)
- ADR-0017 — Hermes Delivery Fleet (applied design)
