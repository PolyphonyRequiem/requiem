# ADR 0009 — Spec Kit Alignment

**Status:** DRAFT (Wave 7 / Debussy seat — design exploration; NOT YET RATIFIED)  
**Date:** 2026-06  
**Author:** Debussy  
**Supersedes:** none  
**Superseded by:** —  
**Cross-cuts:** ADR-0006 (merge-group topology), ADR-0007 (PR lifecycle), ADR-0008 (curated artifacts across the run lifecycle, forthcoming)

---

## TL;DR recommendation

No, it is not too late to say **"let's build around speckit?"** The timing is unusually good because Requiem has not locked the plan artifact format yet: ADR-0006 Q2 decided to build the plan-PR capability, but `plan_pr` is unbuilt; ADR-0008 will decide plan cut/gate/persist policy, but is not yet ratified; the current planner still emits a bespoke `<run_id>.plan.tree.json` sidecar.

Adopt a layered alignment:

1. **L0 — Vocabulary:** already done. Requiem has adopted speckit's `constitution` concept and CRITICAL/HIGH/MEDIUM/LOW severity ladder for replan eligibility.
2. **L1 — Artifacts:** make the plan PR a speckit-shaped feature directory (`spec.md`, `plan.md`, `research.md`, `data-model.md`, `contracts/`, `quickstart.md`, `tasks.md`). **This is the recommended v0 target.**
3. **L2 — Phases / analyze gate:** map Requiem's planning boundary onto specify → plan → tasks → analyze, and use speckit's `analyze` semantics as the literal detector for ADR-0006 Q6 CRITICAL invalidity. **Fast-follow after L1.**
4. **L3 — Build on speckit implementation:** shell out to the real speckit CLI/scripts/agents as Requiem's planning substrate. **Explicit non-goal for v0.**

The seam is not engine-vs-engine. Speckit owns the front half — *what to build*. Requiem owns the back half — *execute the build across agents, branches, reviews, merge gates, and recovery*. They compose at the planner-output → execution-graph boundary.

---

## Context

Requiem is a greenfield, single-process Python replacement for the polyphony + conductor SDLC orchestration stack. ADR-0001 commits to collapsing the old process split into one Python process. ADR-0005 gives recursive planning a safe sub-workflow primitive with `INV-SUBWORKFLOW-LOG-ISOLATION`. ADR-0006 accepts Option D for v0 branch topology: one `feature/{root}` integration trunk, one optional `plan/{root}` branch, and `impl/{root}-{item}` branches for implementable leaves. ADR-0007 keeps PR lifecycle platform-agnostic through one `PrPlatform` Protocol.

Speckit is a spec-driven-development toolkit: prompts, scripts, markdown templates, and a `.specify/` directory convention. Its phase model is:

```text
constitution → specify → clarify → plan → tasks → analyze → implement
```

The core artifacts are markdown files under a feature directory:

```text
specs/[###-feature]/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
└── tasks.md
```

The important observation: speckit is not an autonomous recursive SDLC engine. Its templates and prompts define a disciplined front-end for feature intent, technical design, task slicing, consistency analysis, and sequential/parallel task execution. Requiem's missing surface is not another engine; it is a durable, reviewable, human-legible plan artifact that can be consumed into Requiem's execution graph.

Current Requiem state is still malleable:

- `planning.py` emits `PlanResult`, `ChildPlan`, and `PlannerOutput`, then writes either `<run_id>.plan.md` or `<run_id>.plan.tree.json` for human-facing sidecar use.
- `full_sdlc.py` is still a five-stage linear pipeline: `dispatch → plan → implement → pr_lifecycle → close_out`.
- Multi-leaf execution is deliberately out of scope in the current demo; the plan tree exists, but execution collapses to one leaf.
- ADR-0006 Q2 says the plan-PR capability must be built, but its payload format is not yet implemented.

That makes this the right moment to choose whether Requiem's reviewable plan artifacts should stay bespoke or become speckit-shaped.

---

## The seam: planner output → execution graph

The natural integration seam is the boundary Requiem already has but has not formalized:

```text
planning artifact(s) → leaf graph / EdgeGraph → implementation sub-workflows
```

Speckit should own the left side:

- `constitution.md` — non-negotiable project invariants and governance.
- `spec.md` — user value, scenarios, requirements, assumptions, success criteria.
- `plan.md` — technical context, constitution checks, chosen structure.
- `research.md` / `data-model.md` / `contracts/` / `quickstart.md` — planning support artifacts.
- `tasks.md` — ordered, dependency-aware, `[P]`-marked implementation task list.
- `analyze` report — read-only consistency findings with CRITICAL/HIGH/MEDIUM/LOW severity.

Requiem should own the right side:

- recursive fan-out via `SubWorkflowNode`;
- stable run and item identity;
- `feature/{root}` trunk, `plan/{root}` branch, and `impl/{root}-{item}` branches;
- PR lifecycle and remediation;
- curator decisions over cut/gate/persist and review surfaces;
- event-log-authoritative resume and recovery;
- merge gates and close-out.

This lets Requiem adopt speckit's artifact language without importing speckit's runtime as a hard dependency.

---

## Alignment spectrum

| Level | Alignment depth | What Requiem adopts | Recommendation |
|---|---|---|---|
| **L0 — Vocabulary** | Concepts only | `constitution` as the home for invariants; speckit CRITICAL/HIGH/MEDIUM/LOW severity ladder for plan invalidity | **Already done** via ADR-0006 Q6. Keep. |
| **L1 — Artifacts** | Markdown shapes | Plan PR payload becomes a speckit feature directory: `spec.md`, `plan.md`, `research.md`, `data-model.md`, `contracts/`, `quickstart.md`, `tasks.md` | **Recommended v0 target.** Build before `plan_pr` implementation locks a bespoke format. |
| **L2 — Phases / analyze gate** | Workflow semantics | Requiem maps planner phases onto specify → plan → tasks → analyze; `analyze` becomes the literal leaf-boundary CRITICAL detector | **Fast-follow.** Valuable once L1 artifacts exist. |
| **L3 — Build on speckit** | Runtime dependency | Requiem shells out to speckit scripts/agents/CLI as its planning substrate | **Non-goal for v0.** Defer until their contracts stabilize and we have churn tolerance. |

The lower-regret move is **L0 + L1 now, L2 analyze-gate next, L3 deferred**.

---

## Recursive ↔ flat projection

This is the hard part. Speckit's `tasks.md` is a flat-ish task list for one feature. It supports ordering, phases, user-story grouping, dependencies, and `[P]` parallel markers. It does **not** have a first-class concept of "this task is itself a sub-spec with its own child task graph."

Requiem, by contrast, decomposes recursively: a root may produce children; a child may be decomposable; leaves become implementation work; ADR-0006 Option D still uses one root trunk and per-leaf impl branches. The projection cannot pretend these models are identical.

### Option A — Root as a tree of speckit feature directories (recommended)

**Projection from Requiem plan tree to speckit artifacts:**

```text
specs/{root}/
├── spec.md
├── plan.md
├── tasks.md                    # index/coordination tasks for root-level leaves
├── features/
│   ├── {item_id_a}/             # child is decomposable → its own feature directory
│   │   ├── spec.md
│   │   ├── plan.md
│   │   └── tasks.md
│   └── {item_id_b}/
│       ├── spec.md
│       ├── plan.md
│       └── tasks.md
└── .requiem-plan.json           # optional machine projection/index, not the review surface
```

Rules:

1. Every Requiem plan node has a **stable planner-declared item id**. That id is the bridge between speckit task IDs and Requiem branch IDs.
2. A **decomposable** node gets its own speckit feature directory.
3. An **implementable** child maps to tasks inside its nearest decomposable parent's `tasks.md`.
4. Root `tasks.md` acts as an index when all children are decomposable: it names child feature directories as plan units, but does not pretend a child feature's internal task graph is one flat checklist item.
5. Each feature directory may use speckit's normal artifact set. Requiem may add a machine-readable index only as a projection cache; markdown remains the review surface.

**Projection back from speckit artifacts to Requiem execution:**

- `spec.md` and `plan.md` supply context to implementation agents.
- `tasks.md` is the source of execution-order structure at that feature-directory level.
- Task IDs (`T001`, `T002`) are **local display IDs only**; they are not branch IDs and must not be treated as stable across replans.
- Requiem requires a stable item id per executable leaf. That id must live outside speckit's sequential task number. Candidates:
  - front matter or a small `requiem` metadata block in `tasks.md`;
  - a sibling `.requiem-plan.json` generated from the markdown;
  - a naming convention in task descriptions such as `[item: data-model]`.
- `[P]` markers become hints for parallel scheduling only when file-path and dependency analysis agree. They are not sufficient proof of independence.
- Sequential task order becomes a conservative dependency chain inside one leaf or one feature directory.

**Branch mapping under ADR-0006 Option D:**

| Speckit artifact | Requiem branch / surface |
|---|---|
| `specs/{root}/...` plan directory | diff on `plan/{root}` PR into `feature/{root}` |
| root feature directory after plan PR merge | lives on `feature/{root}` trunk if curator chooses persist |
| implementable leaf with stable item id `item` | `impl/{root}-{item}` branch from `feature/{root}` |
| decomposable child directory `features/{item}/` | no separate branch by default; it is planning state and context. Its implementable descendants get `impl/{root}-{descendant}` branches |
| final feature PR | `feature/{root}` → `main`, gated on leaf PRs and requirement disposition |

**Why this option fits Requiem:** it preserves recursion without forcing speckit's flat task list to carry recursive semantics it does not own. It also aligns with ADR-0006's decision that branch topology should not encode recursive state; the event log and plan artifacts carry state, while branches carry diffs under review.

**Failure modes:**

- **Artifact sprawl.** A deep decomposition can produce many feature directories. ADR-0008's curator must decide which directories to cut into the plan PR, gate, persist, summarize, or strip.
- **Review overload.** A root plan PR containing many nested `spec.md` / `plan.md` / `tasks.md` files can become as unreviewable as a giant code PR. Curator policy needs thresholds and summaries.
- **Stable-id leakage.** If Requiem relies on speckit's `T001` numbering, replan will rename work accidentally. The stable item id must be Requiem-owned and planner-declared.
- **Cross-directory dependency ambiguity.** Speckit `[P]` markers are local; cross-child code dependencies need Requiem's EdgeGraph layer, not plain markdown inference.

### Option B — One root speckit directory with one giant `tasks.md`

**Projection:** flatten the entire Requiem plan tree into the root `tasks.md`, using phases or labels to represent child/sub-child boundaries.

**Advantages:**

- One PR-visible artifact set.
- No nested directory convention beyond normal speckit.
- Easy to point humans at "the plan."

**Costs / failure modes:**

- It destroys the semantic distinction between "task" and "sub-spec." A decomposable child becomes a checklist item that secretly expands into another planning universe.
- Replan is hazardous. Reordering or inserting tasks shifts `T###` IDs and creates false diffs.
- Large roots become unreadable and violate the reason ADR-0006 cared about reviewable sets.
- Executor parsing becomes brittle because hierarchy is encoded in headings and labels instead of an explicit tree.

This is attractive for trivial roots but wrong as the general model.

### Option C — Speckit per implementable leaf only

**Projection:** keep Requiem's recursive plan tree bespoke; emit speckit artifacts only after a leaf is known implementable.

**Advantages:**

- Speckit remains exactly within its one-feature comfort zone.
- Leaf agents get familiar `spec.md` / `plan.md` / `tasks.md` context.
- Avoids nested feature directories.

**Costs / failure modes:**

- The plan PR cannot review the recursive plan as a speckit plan diff; it reviews a bespoke tree plus leaf-local speckit files.
- Speckit's `analyze` can catch leaf-local inconsistency but not root-level omissions or cross-leaf dependency impossibility.
- This punts the hard seam rather than solving it; Requiem still maintains two planning languages.

Useful as a migration fallback, but not the v0 target if we want plan PRs to be speckit-shaped.

### Recommendation for the projection

Adopt **Option A**: root as a tree of speckit feature directories, with implementable leaves mapped to tasks inside their nearest decomposable parent's directory and stable Requiem item IDs carried explicitly.

The key design rule: **speckit task IDs are local checklist IDs; Requiem item IDs are stable execution IDs.** Do not collapse them. Let speckit express human-readable work slicing; let Requiem maintain the EdgeGraph and branch identity.

---

## Cross-ADR wiring

### ADR-0006 Q2 — plan PR

If L1 lands, the plan-PR payload **is** the speckit feature directory. The operator reviews a speckit plan diff: `spec.md`, `plan.md`, `data-model.md`, `tasks.md`, and supporting artifacts, not a Requiem-private JSON rendering. This must be decided **before** implementing the `plan_pr` verb; otherwise Requiem will create a bespoke plan-PR format and immediately have migration debt.

### ADR-0006 Q6 — replan mid-flight

ADR-0006 Q6 already adopted speckit's severity ladder. Daniel's framing is the contract:

> "replanning midflight is only in scope if something about the plan is found to be fundamentally invalid."

Q6 defines "fundamentally invalid" as speckit-CRITICAL invalidity and records the four CRITICAL trigger classes in its decision-log table: constitution/invariant violation, falsified foundational assumption, impossible dependency structure, and baseline-blocking coverage gap. This ADR should not restate those triggers divergently. L2 simply makes speckit's `analyze` phase the literal detector that produces those findings at leaf boundaries and recovery gates.

### ADR-0008 — curator (forthcoming)

ADR-0008 and ADR-0009 pair tightly. ADR-0008 decides **cut/gate/persist** for plan artifacts; ADR-0009 recommends that the artifacts under that policy are speckit-shaped.

The constitution is also the natural home for Requiem's invariants:

- `INV-SINGLE-PROCESS`
- `INV-EVENT-LOG-AUTHORITATIVE`
- `INV-SUBWORKFLOW-LOG-ISOLATION`
- `INV-RESTART`
- `INV-NO-CORRUPT-FORWARD`
- other north-star invariants as they graduate

Speckit says constitution conflicts are non-negotiable and automatically CRITICAL. That matches Requiem's replan gate, but it means the curator must decide whether a run has one repo-level constitution, one root-level constitution snapshot, or both.

### ADR-0007 — PR lifecycle

Speckit artifacts are platform-agnostic markdown. Nothing in this ADR conflicts with ADR-0007's ADO-primary, GitHub-supported `PrPlatform` decision. Plan and impl PRs can be hosted wherever `PrPlatform` points; the artifact shape is independent of PR host.

---

## Options considered

### 1. Full-adopt speckit as the planning substrate (L3 now)

Requiem shells out to the real speckit scripts and agents for constitution/specify/clarify/plan/tasks/analyze, then consumes their output.

**Pros:** maximum reuse; lowest invention risk; Requiem stays close to upstream speckit UX.

**Cons:** high coupling to their current prompt and script structure; `.specify/scripts` are operational conventions, not yet Requiem contracts; recursive projection still remains ours; failures now cross a process/tool seam ADR-0001 deliberately avoided for internal logic.

**Verdict:** attractive future, wrong v0 bet.

### 2. Layered alignment: L0 + L1, fast-follow L2, defer L3

Requiem adopts speckit vocabulary and artifact shapes, adds a projection layer, and keeps the runtime inside Requiem.

**Pros:** captures the human and review value now; preserves Requiem's single-process architecture; gives ADR-0008 concrete artifacts to curate; leaves room to adopt upstream speckit later if it stabilizes.

**Cons:** Requiem owns the projection layer and must track speckit artifact churn; some UX becomes tied to speckit's conventions.

**Verdict:** recommended.

### 3. Status quo: bespoke Requiem plan artifacts

Keep `<run_id>.plan.tree.json` and invent a Requiem-native markdown rendering for plan PRs.

**Pros:** maximum control; no upstream churn risk; easiest executor parse story.

**Cons:** creates a private planning language exactly when a better public-ish artifact language is available; forfeits speckit's analyze/constitution leverage; makes "plan PR" less legible to operators already learning spec-driven development.

**Verdict:** only choose this if Daniel rejects the speckit mental model outright.

---

## Recommendation

Commit to **L1 for v0**: the plan PR should carry speckit-shaped artifacts, not a bespoke Requiem tree rendering. Keep L0 as already accepted. Treat L2 as the immediate follow-up once L1 artifacts exist: `analyze` becomes the literal CRITICAL detector feeding ADR-0006 Q6 replan eligibility. Defer L3 until upstream speckit runtime contracts are stable enough that Requiem can depend on them without re-importing the process-boundary churn ADR-0001 escaped.

For recursive projection, adopt **root as a tree of speckit feature directories** and require explicit stable Requiem item IDs for executable leaves. The plan PR can then be a real speckit plan diff, while the executor still consumes a Requiem-owned EdgeGraph built from stable IDs, task ordering, `[P]` hints, and explicit dependency metadata.

---

## Consequences

### Positive

- The plan PR becomes reviewable in a known, disciplined artifact language.
- Speckit's constitution model gives Requiem a natural home for invariants instead of scattering them across ADR prose and prompts.
- Speckit's CRITICAL severity ladder already matches ADR-0006 Q6's replan gate; L2 turns that conceptual match into an executable detector.
- The current unbuilt `plan_pr` surface can be built once around the right payload shape.
- Requiem keeps its architectural bet: single-process engine, event-log-authoritative state, sub-workflow isolation, and platform-agnostic PR lifecycle.

### Costs / risks

- **Projection layer debt.** L1 is not free. Requiem must map recursive plans to speckit artifacts and map speckit artifacts back to executable leaves.
- **Artifact churn risk.** Speckit templates and prompts can change. If Requiem ties operator UX to their conventions, we absorb some of that churn.
- **Stable-id discipline becomes non-negotiable.** Speckit's `T001` task IDs are not enough for branch identity or replan reconciliation. Requiem must carry its own item IDs.
- **Curator pressure increases.** More artifacts mean more cut/gate/persist decisions. ADR-0008 must prevent plan PRs from becoming documentation dumps.
- **L1 can look like L3 if we are sloppy.** Adopting artifact shapes must not silently become shelling out to upstream scripts in the middle of Requiem's deterministic planning path.

This is design debt now for leverage later. The recommendation is still lower-regret than bespoke artifacts because the plan-PR format is a one-way-ish door: once operators start reviewing Requiem-native plan diffs, migrating them to speckit-shaped diffs becomes needless churn.

---

## Open questions for Daniel

1. Does the **root-as-tree-of-feature-dirs** projection match your mental model, or do you want the root plan PR to stay one flat speckit feature directory for v0?
2. Is L2's `analyze` gate part of the v0 acceptance bar, or strictly a fast-follow after L1 plan artifacts land?
3. Constitution scope: one repo-level constitution, one root-level snapshot committed in the plan PR, or both?
4. Where should stable Requiem item IDs live in the speckit artifact set: front matter, a `requiem` metadata block, sidecar `.requiem-plan.json`, or task labels?
5. Should ADR-0008's curator be allowed to strip speckit planning artifacts before `feature/{root}` merges to `main`, or should some as-built subset always persist?
6. For tiny leaf-only roots, do we still emit a full speckit directory, or let the curator collapse to a shorter plan summary?

---

## References

Requiem:

- `docs/decisions/0001-single-process-architecture.md`
- `docs/decisions/0005-subworkflow-invocation-primitive.md`
- `docs/decisions/0006-merge-group-topology.md`
- `docs/decisions/0007-pr-lifecycle-architecture.md`
- `docs/briefings/open-questions-wave7.md`
- `docs/references/v0-parity-readiness.md`
- `src/requiem/workflows/planning.py`
- `src/requiem/workflows/full_sdlc.py`

Speckit local prompts read from `C:\Users\dangreen\projects\cloudvault-service-api\main\.github\agents\`:

- `speckit.constitution.agent.md`
- `speckit.specify.agent.md`
- `speckit.clarify.agent.md`
- `speckit.plan.agent.md`
- `speckit.tasks.agent.md`
- `speckit.analyze.agent.md`
- `speckit.implement.agent.md`
- `speckit.checklist.agent.md`

Speckit upstream templates fetched from `github/spec-kit`:

- `templates/spec-template.md`
- `templates/plan-template.md`
- `templates/tasks-template.md`
- `templates/checklist-template.md`
- `templates/constitution-template.md`

Note: `memory/constitution.md` was attempted at `https://raw.githubusercontent.com/github/spec-kit/main/memory/constitution.md` and returned 404; the constitution shape above is based on the local constitution agent prompt and upstream constitution template.
