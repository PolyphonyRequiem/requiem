# ADR 0003 — Agent Teams as a First-Class Workflow Primitive

**Status:** Accepted
**Date:** 2026-05-31
**Supersedes:** none

## Context

Daniel is currently using the squad pattern (~10 specialised agents in
parallel, each with a charter, with adversarial reviewers and a synthesiser)
to BUILD Requiem. The pattern is load-bearing for his SDLC and emerged
without explicit tooling. The question raised on 2026-05-31:

> we should also make sure this solution supports "agent teams" like we are
> using here with squad natively for some work steps. Is that possible
> without a complete overhaul?

PR #9 (`docs/patterns/agent-teams.md`) answered: yes, by composing Phase A
primitives + one open question (Beethoven Q-K7 — `parallel_fork`). The
walking skeleton (PR #11) implemented the pattern in ~40 LOC of kernel
addition and proved the `.team(...)` builder method works end-to-end.

## Decision

Agent teams are a first-class supported workflow primitive in Requiem v0.
The implementation requires:

1. **`parallel_fork` primitive** in the kernel — adopts Beethoven Q-K7 from
   PR #7. Cost: ~50 LOC kernel addition (Verdi-1 measured ~40 LOC in
   walking skeleton).
2. **`AgentDef.charter`** field — Mahler's `AgentProvider` Protocol gains
   an optional `charter: str | Path` field carrying the agent's persona /
   system-prompt source. Harness can mock per-charter.
3. **`.team(...)` sugar** in Wagner's fluent builder. ~30 LOC. Lowers to a
   `TeamNode` in Beethoven's data model. Shape:
   ```python
   .team(
       name="seam-prototypes",
       agents=[
           Agent("stravinsky", charter=".squad/agents/stravinsky/charter.md"),
           Agent("brahms", charter=".squad/agents/brahms/charter.md"),
       ],
       synthesizer=Agent("scribe", charter=".squad/agents/scribe/charter.md"),
   )
   ```
4. **`team_id` + `agent_id` envelope fields** on Brahms-events events.
   Free to add since the envelope is loose. Lets the future UI render per-agent
   trace rows within a team-step fan-out.

## Rationale

The squad pattern is not a special case Daniel chose; it is how AI-augmented
work composes naturally when there's more than one role per stage. Building
the primitive once gives Requiem the same expressive power for any future
workflow that needs parallel agents with distinct charters — recursive
planning, adversarial review, multi-perspective synthesis, code review with
domain specialists, etc.

The cost is small (~120 LOC across kernel + DSL + envelope) and isolated to
seams that Phase A already shaped. Deferring would force every team-shaped
workflow into N parallel sub-workflow invocations — functional but
ergonomically taxed, as PR #9 §3 documents.

## What composes without new primitives

ADR 0003 codifies the primitive. The following patterns work today on the
Phase A baseline + this primitive — no further engine changes needed:

- **Long-running team members** (10-90 min). Mahler's `invoke()` is async;
  the kernel awaits. Suspension/resume from event log means crash mid-team
  is recoverable.
- **Per-agent worktree isolation.** Toolbelt's `git(cwd=...)`.
- **Antagonistic peer review chain.** Sequential nodes: produce → review →
  revise. No primitive.
- **Dynamic team composition.** Wagner's data-driven DSL — team membership
  can be built at runtime from upstream step output.
- **Human gate over team output.** Beethoven's gate primitive.

## What does NOT compose easily — deferred

- **Real-time inter-agent messaging mid-run** (the live `write_agent`
  pattern). Would need a `Mailbox` primitive on the Toolbelt
  (`agents.send(target, msg)`, `agents.recv(timeout)`). Deferred until a
  real workflow requires live conversation, not fire-and-synthesise. The few
  Phase A cases where peer messaging happened (Boulez consulting Mahler-2
  during adversarial review) are expressible as sub-workflows.
- **Persistent agent identity across runs.** Each Requiem run instantiates
  agents from charter; identity is per-run. Cross-run reputation/history
  would need an `agent_history` store. Deferred — not needed for v0.

## Consequences

### Positive

- Squad pattern becomes Requiem-native. Daniel's existing way of working
  carries forward without a meta-tool.
- Recursive planning (Phase C vertical slice) becomes a team-pattern
  application, not ad-hoc orchestration: planner is synthesiser, child
  plans are team members.
- The pattern self-documents via PR #9's pattern doc and PR #11's
  walking-skeleton example.

### Negative

- `parallel_fork` introduces concurrent verb execution into the kernel.
  Race conditions in shared state (event log appends, Toolbelt-mediated
  filesystem writes) become real. Mitigation: append-only log with
  per-event monotonic IDs; Toolbelt clients are per-call (no shared mutable
  state); verbs receive isolated `cwd`.
- The `.team(...)` sugar layer is a place where the abstraction can leak
  if added carelessly. Constraint: the lowered `TeamNode` MUST be inspectable
  in the same pydantic data model as any other node; introspection (`requiem
  describe`) must show team membership as data, not as a runtime closure.

## References

- PR #9 — `docs/patterns/agent-teams.md`
- PR #7 — Beethoven seam PR (open question Q-K7)
- PR #11 — walking skeleton (40 LOC kernel implementation reference)
- `polyphony-squad-spike/.squad/` — the live squad as canonical example
