# Pattern: Agent Teams as a Workflow Composition

> **Status:** Pattern proposal. Reads against Phase A seam recommendations
> (PRs #1–#8). Asks for one Phase A decision (Beethoven Q-K7) to land cleanly.
> Not an ADR; ADRs follow once Daniel picks seam decisions.

## 1. What this is

A pattern for orchestrating multiple AI agents in parallel within a Requiem
workflow, where each agent has a distinct charter (persona, system prompt,
tools, worktree) and contributes to a shared outcome.

**Canonical real-world example: this work right now.** Daniel is currently
using Copilot CLI to drive a "squad" of ~10 agents producing Requiem's own
Phase A design:

- 7 deep-dive composer seats (Mahler, Wagner, Bach, Liszt, Stravinsky, Beethoven, Brahms)
- 2 antagonistic reviewers (Boulez, Ravel)
- 1 perspectives brainstormer (Debussy)
- Periodic Scribe synthesis runs

Each agent has a charter file (`.squad/agents/<seat>/charter.md`), a distinct
system prompt + personality, a worktree of its own, access to peer messaging
via `write_agent`, and a shared decisions ledger
(`.squad/decisions.md`). The squad pattern is what shipped Phase A in an
afternoon. We want Requiem to support it as a first-class workflow shape, not
as an external meta-tool driving Requiem.

## 2. Does Requiem support this natively?

**Yes — without a complete overhaul.** Agent teams are an *emergent*
composition over Phase A primitives. The table below maps each squad concept
to the Phase A primitive that already serves it.

| Squad concept | Phase A primitive (rec'd) |
|---|---|
| Agent with charter | `AgentProvider.invoke(agent_def, ...)` — Mahler A (PR #8). `AgentDef` carries `system_prompt`, `tools`, `response_model`. Add one field: `charter: str \| Path`. |
| Parallel dispatch of N agents | `parallel_fork` step — Beethoven Q-K7 (PR #7, open question). Without it, N parallel sub-workflow invocations — possible but awkward. With it, one node. |
| Per-agent worktree isolation | Toolbelt's `git` carries `cwd` per call — Liszt B (PR #3). Each agent's verb runs in its own worktree. Same pattern Daniel is using manually right now. |
| Shared decisions ledger | Toolbelt's `events.emit("decision", ...)` — Brahms-events B (PR #5). OR a verb appending to a Markdown file via Toolbelt. |
| Antagonistic reviewer chain | Sequential nodes: `dispatch_team → synthesize → adversarial_review → revise`. No new primitive. |
| Synthesizer (Scribe role) | One more `AgentProvider.invoke()` with a synthesis prompt that takes prior outputs as input. |
| Per-agent event scoping (UI traces) | Brahms-events scoping carries `run_id`; add `team_id` + `agent_id` envelope fields. |

**Verdict:** the engine, persistence, agent boundary, event stream, toolbelt,
DSL, and harness all compose. One open Phase A question (Beethoven Q-K7)
makes the pattern ergonomic; the rest are sugar or convention.

## 3. The one Phase A decision

**Beethoven Q-K7: adopt `parallel_fork` as a v0 primitive.** Cost: ~50 LOC
in the kernel. Payoff: agent teams become a one-liner.

With Wagner's fluent builder (PR #1) + this primitive:

```python
workflow = (
    Workflow("phase-a-fleet")
    .team(
        name="seam-prototypes",
        agents=[
            Agent("stravinsky", charter=".squad/agents/stravinsky/charter.md"),
            Agent("brahms-events", charter=".squad/agents/brahms/charter.md"),
            # ...
        ],
        synthesizer=Agent("scribe", charter=".squad/agents/scribe/charter.md"),
    )
    .route(when="success", to="adversarial_review")
    .team(
        name="adversarial-review",
        agents=[
            Agent("boulez", charter=".squad/agents/boulez/charter.md"),
            Agent("ravel", charter=".squad/agents/ravel/charter.md"),
        ],
    )
    .gate("daniel_decision", prompt="Pick seam decisions")
    .build()
)
```

Without Q-K7 the same shape is reachable through N sub-workflow invocations
in a loop — uglier, but functional. Recommendation: **adopt Q-K7**.

## 4. What also composes — without new primitives

These work today against the Phase A recs as proposed:

- **Long-running agents (10-90 min each).** Mahler's `AgentProvider.invoke`
  is async; Beethoven's kernel awaits async verbs. A team-step that runs for
  an hour is just an `await`. Suspension/resume from the event log
  (INV-RESTART) means crash-during-team-step is recoverable: the engine
  replays the log, sees `team_started` without `team_completed`, re-invokes
  the team. (Idempotency assumed for re-invocation — Stravinsky's `retry_key`
  semantics apply.)
- **Per-agent worktree isolation.** Already a pattern in Liszt's Toolbelt;
  each team-member verb passes its worktree as `cwd`.
- **Antagonistic peer review.** A chain of nodes, not a real-time
  conversation. Produces → reviewers gate → synthesizer revises.
- **Dynamic team composition.** Wagner's data-driven DSL means workflows are
  pydantic models; an upstream step's output can construct the `agents=[...]`
  list at runtime. "Spawn one reviewer per concern raised" works because the
  team-step body isn't decided until the previous step completes.
- **Human gate over team output.** Beethoven's gate primitive already serves
  this. Daniel picks across team artifacts in the UI when UI ships.
- **Per-agent traces in the (future) UI.** Brahms-events scoping +
  Debussy's outcome-kind-as-color recommendation give the UI everything it
  needs to render a team-step as a fan-out chart of per-agent rows.

## 5. What does NOT compose easily

- **Real-time inter-agent messaging mid-run** (the `write_agent` pattern).
  Mahler's AgentProvider is request/response. To support live agent
  conversations, we'd need a `Mailbox` primitive on the Toolbelt
  (`agents.send(target, message)`, `agents.recv(timeout)`). Doable; not in
  any current seam.
  - **Mitigation:** most squad work is fire-and-synthesize. The few cases
    where peer messaging happened during Phase A (Boulez consulting
    Mahler-2 during adversarial review) are expressible as a sub-workflow:
    "Boulez identifies a question → spawn a sub-workflow that invokes
    Mahler-2 with the question → Boulez reads the answer from sub-workflow
    output." No mid-run messaging needed.
- **Persistent agent identity across runs.** Right now Boulez is a named
  thing in `.squad/agents/boulez/`. Each Requiem run would instantiate
  Boulez from charter; identity is per-run. To accumulate cross-run
  reputation/history, we'd need a separate `agent_history` store (not in any
  current seam, and not needed for v0).

## 6. What this means for Phase A → Phase B → Phase C

### Phase A — open questions newly *highlighted* by this pattern

- **Beethoven Q-K7** (`parallel_fork` primitive): **strongly recommend
  adopting**. Cheap, unlocks team-step ergonomics.
- **Mahler open question** (model targets / scripting key): add `charter`
  field to `AgentDef`; harness scripts by `agent.name` (which can equal seat
  name).
- **Wagner open question** (DSL surface): add `.team(name, agents,
  synthesizer)` sugar. ~30 LOC.
- **Brahms-events open question** (envelope fields): add `team_id` and
  `agent_id` to the envelope alongside `run_id`. Free if the envelope is
  loose (which Brahms recommended).

### Phase B — walking skeleton candidate change

Daniel's current Phase B target is the close-out workflow (smallest). Close-out
does NOT exercise the team pattern. Two options:

- **Option α:** Keep close-out as Phase B. Add a tiny team-step workflow as
  "walking-skeleton-2" — e.g., a `seed-review` workflow that dispatches 2
  reviewers + 1 synthesizer over a seed manifest. Proves team pattern
  without expanding the close-out scope.
- **Option β:** Replace Phase B target with a "PR-review with antagonistic
  reviewers" workflow. Bigger, but exercises team pattern + PR lifecycle in
  one slice. Higher confidence that the integrated design holds.

Recommendation: α. Keeps the walking skeleton tight; team-step exercise is
small and additive.

### Phase C — vertical slices

The recursive planning workflow (one planner spawns N child plan-level runs)
IS a team pattern. Implement it as `parallel_fork` over child sub-workflows,
not as ad-hoc orchestration. The squad-style charter+synthesizer pattern
applies directly: the planner is the synthesizer, child plans are the team
members, the team-step completes when all children land.

## 7. Open questions for Daniel

1. **Does the squad pattern itself become a Requiem-shipped workflow
   library?** A `requiem.patterns.squad` module that provides charter
   conventions, the team-step shape, and an adversarial-review wrapper —
   or do those stay as conventions in user repos?
2. **Charter format.** Markdown today (`.squad/agents/<seat>/charter.md`).
   Does Requiem standardise on this so `.team(charter_dir=".squad/")`
   works out of the box, or stay agnostic?
3. **Antagonistic review as a primitive vs as workflow sugar.**
   `Workflow.with_adversarial_review(reviewers=[boulez, ravel])` as a
   one-liner that wraps the prior step — or always-explicit
   sequential nodes?
4. **Recursive planning composition.** Each child plan executed by its own
   team (a planner + N implementers + 2 reviewers), or single-agent per
   child? The former is what Daniel did manually for Phase A. Cheap to
   support if `parallel_fork` lands.
5. **`write_agent` mid-run.** Defer the Mailbox primitive (recommend) or
   build it now? Recommend defer; revisit when a real workflow needs live
   peer conversation, not fire-and-synthesize.

## 8. Recommendation

**Adopt agent teams as a first-class supported pattern in Requiem v0.** No
engine overhaul required. The deltas needed against Phase A recs:

1. Adopt **Beethoven Q-K7** (`parallel_fork` primitive — ~50 LOC).
2. Add `charter: str | Path` to Mahler's `AgentDef`.
3. Add `.team(...)` sugar to Wagner's fluent builder (~30 LOC).
4. Extend Brahms-events envelope with `team_id` + `agent_id`.
5. Document the squad-style chain (dispatch → synthesize → adversarial
   review → revise) as a recommended pattern in
   `docs/patterns/agent-teams.md` (this file).

If `parallel_fork` is deferred, agent teams still work as N parallel
sub-workflow invocations — functional but ergonomically taxed.

## 9. References

- Phase A seam recommendations: PRs #1 (Wagner — DSL), #2 (Bach —
  persistence), #3 (Liszt — external-process), #4 (Stravinsky — outcomes),
  #5 (Brahms-events — event stream), #7 (Beethoven — kernel),
  #8 (Mahler — agent boundary).
- `perspectives/ui-sdlc/` branch (Debussy) for UX implications of team-step
  rendering.
- The squad itself: `polyphony-squad-spike/.squad/` contains the charters
  and decisions ledger we're using to prototype Requiem in real time.
