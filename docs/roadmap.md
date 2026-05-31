# Requiem Roadmap

> v0 means: full polyphony+conductor feature parity, with no meaningful regression, on Requiem's single-process architecture and UI-from-day-one foundation. **v0 is a delivery target, not a phase.** The phases below are the sequence by which v0 is reached.

The customer is Daniel; demos are scheduled at every phase boundary for hands-on direction-setting.

---

## Phase A — Seam shaping

**Goal:** for each load-bearing seam, produce 2-3 runnable prototype variants and demo them so the seam's shape is decided before any production code is written. This is the highest-leverage agile move available on a "no meaningful regression" rewrite: cheap to redo, expensive to retrofit.

**Duration target:** ~2-3 days (saturating the agent fleet)

**Output:**
- `prototypes/<seam>/<variant>/` directories, each with a runnable demo and a README
- A demo gallery README at `prototypes/README.md` summarizing the matrix
- One ADR per seam recording the decision and the variants considered

**Seams (provisional — refined as Phase A starts):**

1. **Verb outcome contract** — discriminated union shape; how it's defined, returned, serialized to events
2. **Run-event stream** — `.events.jsonl` schema; what's an event vs what's a state; the contract every consumer reads
3. **State machine kernel** — node, transition, route, gate, subworkflow primitives in Requiem's shape
4. **Persistence / manifest as event log** — durable vs reconstructible split; manifest as projection
5. **Agent boundary** — LLM invocation, output binding, prompt template location, FakeProvider seam
6. **UI ↔ event-stream binding** — trace-view vs graph-view vs split-primary; SSE/WS protocol details
7. **DSL / workflow definition shape** — Python fluent builder vs decorators vs typed dicts vs YAML-with-typed-schema
8. **External-process abstraction** — git/gh/twig/LLM injection seam for harness use
9. **Harness scenario contract** — what a scenario file looks like; the assertion vocabulary; chaos primitives

**Exit criterion:** Daniel has hands-on-reviewed prototypes and made decisions on each seam. An ADR is committed for each decision.

---

## Phase B — Walking skeleton

**Goal:** the thinnest possible end-to-end vertical slice. One real work item runs through one workflow with the UI showing live traversal, the harness asserting the run, and the verbs touching real external state.

**Duration target:** ~3-7 days

**Slice composition:**
- Workflow: `close-out` (smallest workflow in the polyphony catalogue: load guidance → run close-out analyst → emit observations)
- Platform: GitHub (smaller surface than ADO)
- Work item: a real ADO item (close-out reads it; doesn't transition state)
- Verbs: the minimum subset close-out needs (`plan load-guidance`, agent invocation, output emission)
- UI: shows the workflow's nodes, the live trace, the agent output, the final state
- Harness: one scenario that asserts the workflow reaches `completed`

**Exit criterion:** Daniel runs the skeleton on a real ADO item from the terminal, watches it in the UI, and the harness scenario passes in CI.

---

## Phase C — Vertical slices toward parity

**Goal:** each slice adds one polyphony non-negotiable feature. Each slice is shipped, demoed, and locked before the next one begins.

**Slice ordering (provisional — re-sequenced if dependencies surface):**

1. **`plan-level` workflow** — recursive planning, child seeding, plan PR open/merge
2. **`feature-pr` + `github-pr` workflow** — feature PR creation, review, remediation, merge on GitHub
3. **`implement-merge-group` workflow** — MG branching (`mg/`, `impl/`), coder/reviewer loop, MG PR
4. **Worktree isolation** — per-item worktrees for parallel dispatch
5. **`polyphony` (root) workflow** — full tree-walking root orchestrator with batch dispatch
6. **`ado-pr` workflow** — ADO PR lifecycle (second platform)
7. **Twig integration** — write-side ADO bridge
8. **Renegotiation / restart / restack remedies** — the support workflows
9. **Reset + reconcile verbs** — full restart-friendliness validated against the invariants

Each slice ends with:
- A demo to Daniel
- A regression check against the equivalent old-polyphony behaviour
- ADRs for any new decisions surfaced during the slice
- An updated parity-tracker line

**Exit criterion:** the parity tracker shows 10/10 non-negotiables complete and Daniel has run a real root work item end-to-end through Requiem.

---

## Phase D — Parity cutover

**Goal:** real dogfood. Requiem runs a real SDLC root for a real repository (e.g., cloudvault or journal), with old polyphony staying available only as a rollback option. After 1-2 weeks of clean dogfood, old polyphony is sunsetted.

**Exit criterion:** Daniel declares Requiem v0 shipped. Old polyphony freezes (no new feature work).

---

## What lives outside the v0 line

These are explicitly post-v0 and out of scope for the phases above:

- Multi-operator collaboration
- A workflow-authoring GUI
- Cross-run analytics (Airflow-Grid-style)
- An "inbox" UI for cross-run human gates (single-operator audience does not need it)
- Plugin marketplaces / third-party verb registries
- Polyphony-compatible workflow format (Requiem authors workflows in its own DSL; bridge is one-way migration tooling, not bidirectional compat)

---

## How decisions are made

- Daniel is the customer. Demo gates are direction-setting moments, not approval bureaucracy.
- The squad (Mahler, Wagner, Bach, Beethoven, Stravinsky, Brahms, Liszt, Mozart, Reich, Sibelius, and the antagonistic reviewers Boulez + Ravel) is the proposing body.
- Boulez and Ravel are mandatory reviewers on every Phase A seam decision and every Phase C slice.
- ADRs in `docs/decisions/` are immutable once accepted — disagreement is captured in superseding ADRs that link back, not by edits.
- Squad output (handoffs, deep dives, reviews) lives in `polyphony-squad-spike/.squad/handoffs/`. Anything load-bearing is summarized into Requiem's own `docs/references/` so the engine repo is self-contained.
