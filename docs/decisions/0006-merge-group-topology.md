# ADR 0006 — Merge-Group Topology for Implementation

**Status:** ACCEPTED (2026-06-01 — Q1 resolved during Wave 7 Bruckner walkthrough).
**Date:** 2026-06 (Wave 6; Q1 closed Wave 7).
**Author:** Stravinsky (design-research seat).
**Supersedes:** none (first MG-topology decision; replaces the implicit
"single `feature/<item_id>` branch" baked into `implementation.py`).
**Superseded by:** —
**Cross-cuts:** ADR-0008 (review-surface curation) — separates
"branch topology" from "reviewable-set policy". See "Decision log" below.

---

## Context

Wave 5 Mahler-3 audit
([`docs/references/v0-parity-readiness.md` §2.3, §9 row #7](../references/v0-parity-readiness.md))
verdicted **NO-GO for full §9 v0** with merge-group topology absent as one
of five blocking gaps. The audit's exact words:

> **Merge-group topology absent (non-negotiable #7).** The single-leaf
> `implementation.py` cannot represent polyphony's `mg/` + `impl/` branch
> structure. Backfilling this is multi-day work and touches branch
> naming, PR ordering, scope-close logic.

Daniel opened this for redesign on entering Wave 6:

> "I'm open to rethinking the merge-group topology design by the way.
> Not convinced we had it right before."

This ADR is the rethink. It is a **decision document**, not an
implementation. The deliverable is one recommended topology with explicit
trade-offs and a list of open questions Daniel must close before any
code lands.

### What we have today

`src/requiem/workflows/implementation.py:385` hard-codes:

```python
branch_name = f"feature/{inputs.item_id}"
```

One workflow run = one leaf item = one branch `feature/<item_id>` =
one GitHub PR merged to `main` independently. There is no notion of
sibling items, no integration surface, no plan-vs-impl PR split, no
worktree, and no atomic-co-merge guarantee. `full_sdlc.py:55-59`
explicitly says:

> Multi-leaf plans: `plan` may produce decomposable trees, but the demo
> collapses them to one leaf in the implementation stage. Real fan-out
> is Berlioz-Phase-D's job.

Planning today (`src/requiem/workflows/planning.py`) recurses freely
and produces a fully recursive `PlanResult` tree (one `PlanResult` per
node, `children: list[PlanResult]`,
[`planning.py:160-176`](../../src/requiem/workflows/planning.py))
but execution flattens to a single leaf. **The tree-of-plan-output
already exists; what's missing is a tree-of-execution that consumes it.**

### What polyphony does

Polyphony's branch model (Rev 4) is documented in the
[`polyphony-branch-model` skill](../../.github/skills/polyphony-branch-model/SKILL.md)
([mirror in the squad-spike repo](file:///C:/Users/dangreen/projects/polyphony-squad-spike/.github/skills/polyphony-branch-model/SKILL.md))
and is summarised here only enough to make the trade-offs explicit:

```
main
 └── feature/{root_id}                            ← integration trunk
      ├── plan/{root_id}                          ← root plan branch
      │    └── plan/{root_id}-{item_id}           ← descendant plan
      ├── mg/{root_id}_{mg_id}                       ← top MG
      │    ├── impl/{root_id}-{item_id}              ← impl branch (flat)
      │    └── mg/{root_id}_{mg_id}_{nested_mg_id}   ← nested MG
      │         ├── impl/{root_id}-{owner_item_id}
      │         └── impl/{root_id}-{descendant_id}
      └── evidence/{root_id}-{item_id}            ← evidence branch
```

Plus: driver-enforced promotion gates per layer; stable planner-declared
MG ids (`^[a-z][a-z0-9-]{0,30}$`); a default-nest trigger
(decomposable AND implementable → nested MG); mandatory merge commits
at all promote-chain layers; a topology hash committed to a run
manifest on the feature branch; same-root run lock; renegotiation flow
with parent-plan-generation serialisation; cross-sibling code-dependency
rebase with materialisation gate; an `_` vs `-` vs `/` three-delimiter
discipline forced by git's ref namespace; depth-3 warning / depth-5
hard stop.

It is *a lot*. The skill file alone is 628 lines.

---

## The problem MGs actually solve

Before evaluating options we have to name what work the MG layer does.
Each candidate problem gets a one-line statement, a citation if I have
one, and a marker:

| # | Problem | What it means | Status in v0 Requiem |
|---|---------|---------------|----------------------|
| **P1** | **Per-item review attribution** | Reviewer sees one PR per work item, even when items co-merge. Reviewer comments and ADO PR-links are 1:1 with items. | Trivially solved when there's one leaf. Not solved for fan-out. ([skill §"Isolation scope ↔ branch contract"](file:///C:/Users/dangreen/projects/polyphony-squad-spike/.github/skills/polyphony-branch-model/SKILL.md), line ~500). |
| **P2** | **Atomic co-merge of related items** | When sibling leaves are designed to land together (e.g. "data layer + migration"), either both land or neither lands. No "A merged, B failed, A is now orphaned in `main`" failure mode. | **Not solved today.** With one leaf there is no question. Fan-out without an integration surface would expose this. |
| **P3** | **Cross-sibling code dependency** | Leaf B compiles/tests against leaf A's code. B's branch needs A's commits in its base. | Not solved today (no fan-out). Polyphony solves this with `(scope, cross_mg_code_dep)` policy + materialisation-gated auto-rebase ([skill §"Cross-sibling code dependencies"](file:///C:/Users/dangreen/projects/polyphony-squad-spike/.github/skills/polyphony-branch-model/SKILL.md) lines 329-381). |
| **P4** | **Test / CI containment** | A failing test on item B shouldn't block item A from progressing. | Today both share `main`; tests on A's PR are independent of B's. With an integration surface this becomes a real question (whose tests run on the integration branch?). |
| **P5** | **Reviewer cognitive load** | A 50-file feature PR is unreviewable; 50 micro-PRs are review-fatiguing. The MG layer is a "natural read unit" between leaf and feature. | Today there is only the leaf level; no aggregator above it. |
| **P6** | **Plan-PR vs impl-PR separation** | The operator can review the *plan* (what we propose to do) before any code is written, then the *implementation* on its own PR. Two distinct review surfaces with two distinct mental modes. | Not solved today. `planning.py` writes a plan artefact (`<run_id>.plan.tree.json`) but does not open a PR. ([Mahler-3 §9 row #6](../references/v0-parity-readiness.md) — "Plan PR open/merge ❌"). |
| **P7** | **Topology stability under replanning** | Re-running planning on a subtree doesn't rename branches that already have open PRs. | Not relevant today (no fan-out). Polyphony solves this with planner-declared stable MG ids + a topology hash gate. |
| **P8** | **Same-root concurrent-run prevention** | Two operators (or one operator twice) can't simultaneously drive the same root and stomp each other's branches. | Partially solved by `root_dispatch`'s deterministic `root_run_id` ([`root_dispatch.py:1-44`](../../src/requiem/workflows/root_dispatch.py)) — second dispatch reuses the manifest. No lock today, but INV-SINGLE-PROCESS + same-process re-entry covers the common case. |
| **P9** | **Parallel work on independent items** | When items have no code dependency, agents can work on them concurrently. | Not solved today (no fan-out, no worktree). Polyphony solves this with per-item worktrees. |
| **P10** | **Driver-enforced merge ordering** | Parent merge gates wait for children. Git ancestry alone is not the canonical signal — PR-state-plus-requirement-disposition is. | Not relevant today (no children to wait on). |

### Wheat-vs-chaff classification of polyphony's MG model

For each piece of polyphony's MG machinery, my call:

| Polyphony piece | Classification | Reasoning |
|-----------------|----------------|-----------|
| `feature/{root}` integration trunk | **LOAD-BEARING** | Solves P2 (atomic co-merge) and gives P3 a natural answer (siblings branch off the same trunk). Required for any fan-out story. |
| `impl/{root}-{item}` per-leaf branch | **LOAD-BEARING** | Solves P1 (per-item review attribution). |
| `plan/{root}` plan branch + plan PR | **LOAD-BEARING** | Solves P6 (plan-vs-impl review surfaces). Polyphony's most operator-loved feature in dogfood per the inventory. |
| `mg/{root}_{mg_id}` top-MG branch | **LOAD-BEARING for large fan-outs, OPTIMISATION for small ones** | Solves P5 (cognitive load) when there are many leaves. For ≤5 leaves the feature trunk *is* the natural aggregator and an MG layer is overhead. |
| Nested `mg/{root}_{mg_path}_{nested}` | **CONDUCTOR-INHERITED** | Justified historically by conductor's lack of an authoritative event log: branch topology *encoded* recursive plan state because there was no other durable substrate. Requiem has INV-EVENT-LOG-AUTHORITATIVE; the topology no longer needs to encode state. |
| Stable planner-declared MG ids (`^[a-z][a-z0-9-]{0,30}$`) | **LOAD-BEARING IF P7 is in scope; OPTIMISATION otherwise** | Solves P7 (replan stability). v0 may not have replan-mid-flight as a use case. |
| Topology hash in run manifest | **LOAD-BEARING for resume across replan; CONDUCTOR-INHERITED for the rest** | Solves "did the topology change since last run?" In Requiem the equivalent question is answered by the event log + manifest sidecar pair already in `root_dispatch.py`. |
| Default-nest trigger (decomposable AND implementable → nested MG) | **CONDUCTOR-INHERITED** | A *rule* that derives topology from plan facets. Required only because nested MGs exist; if you don't nest, you don't need a trigger. |
| Three-delimiter discipline (`/`, `-`, `_`) | **BUG-COMPATIBLE** | Forced by git's ref namespace + polyphony's choice to encode hierarchy *in the ref name*. We need the discipline only if we adopt nested `mg/` paths. |
| Driver-enforced merge gates per layer | **LOAD-BEARING** | Solves P10. Real even without `mg/` layer: parent (feature) PR must wait on children (leaves). |
| Cross-MG code-dep rebase with materialisation gate | **OPTIMISATION (advanced)** | Solves P3 *across MG boundaries*. If there are no MG boundaries (option D below) the problem reduces to standard intra-trunk stacking. |
| Parent-plan-generation serialisation for renegotiation | **OPTIMISATION (advanced)** | Solves a multi-driver replan race condition. The v0 audience is one operator, one root, one driver (north-star §5 — "single power-user audience"); this race is hypothetical. |
| Same-root run lock | **LOAD-BEARING** | Solves P8 even in v0. Cheap to add. |
| Per-item worktrees | **OPTIMISATION** | Solves P9 (parallelism). Worth it eventually, but **agent-throughput is the bottleneck before git is** in the single-operator audience; sequential execution is acceptable for v0. (Mahler-3 §9 row #5 lists this as a separate non-negotiable; treating it as orthogonal to MG topology is intentional — see "Out of scope" below.) |
| `evidence/{root}-{item}` branches | **DEFERRED** | Polyphony's actionable-facet machinery. Requiem does not have the actionable facet today ([Mahler-3 §2.2 — actionable.yaml "❌ missing"](../references/v0-parity-readiness.md)). Out of scope for this ADR. |

### One more observation — why polyphony needed `mg/` and Requiem may not

The single most architecturally important reason polyphony has nested
`mg/` branches is that **conductor's checkpoint state is ephemeral**
([parity inventory §3](../references/polyphony-parity-inventory.md)
— "*Conductor checkpointing is explicitly **not** durable cross-run
state*"). The branch topology had to carry recursive plan state because
there was nowhere else durable to put it. The driver inspecting
`mg/1234_data-layer_item-4567` *is* reading workflow state from git.

Requiem has **INV-EVENT-LOG-AUTHORITATIVE** (north-star §2) and
**INV-SUBWORKFLOW-LOG-ISOLATION** (north-star §2, [ADR-0005](0005-subworkflow-invocation-primitive.md)).
The event log carries the state. Recursion lives in
`{sub_run_id}.events.jsonl` sidecars. Branch topology no longer has to
*encode* plan structure — it only has to *serve* git's needs (one ref
per concurrent diff under review).

**This is the load-bearing argument for a simpler topology than
polyphony's.** When you remove the "branches as state substrate"
requirement, the case for `mg/{root}_{parent}_{nested}` weakens
substantially.

---

## Options considered

### A. Polyphony-compatible MG (port the full Rev 4 model)

**Elevator pitch:** Implement everything in the polyphony-branch-model
skill. Recursive `plan/`, `mg/`, `impl/`, `evidence/` branches.
Driver-enforced promotion gates. Stable planner-declared MG ids.
Topology hash. Same-root lock. Cross-sibling rebase. Per-item worktree.
Total feature parity.

**Branch sketch:** identical to the polyphony skill diagram, above.

**New invariants required:**
- INV-MG-ID-IMMUTABLE — once a branch exists under an `mg_id`, the id
  cannot change for the life of the run.
- INV-TOPOLOGY-HASH-IS-RESUME-KEY — same-root resume keys off topology
  hash; mismatched hash → human gate, never silent rename.
- INV-DRIVER-GATES-MERGE — git ancestry is observed, never enforced;
  PR-state + requirement-disposition is the canonical signal.
- INV-NO-DIRECT-MG-COMMITS — impl PRs are the only way commits reach
  an MG branch.

**Solves:** P1, P2, P3, P4, P5, P6, P7, P8, P9, P10. The full set.

**Cost:**
- Net-new code: branch-management module (~1500 LOC by my eye-on-skill
  estimate), driver gate state machine, topology-hash computation,
  manifest schema bump, lock service, renegotiation handler, cross-MG
  rebase orchestrator, worktree allocator.
- Net-new YAMLs in Requiem terms: a tree of sub-workflows
  (`impl-merge-group`, `feature-pr`, plan-PR machinery) per the
  parity inventory §2.
- Net-new operator surface: depth gates, topology gates, MG id naming
  rules to teach.
- Risk: **importing complexity that v0 doesn't need.** The CONDUCTOR-INHERITED
  pieces in the wheat-vs-chaff table are not free; each one shows up
  as routing edges, error kinds, gate variants, and operator-facing
  prompts.

**Trade-offs vs. status quo:**
- Pro: full §9 parity. Path to multi-tenant later if v0 grows up.
- Con: Requiem stops being "the simpler thing that replaces polyphony"
  and starts being "polyphony reimplemented in Python". The
  architectural win (INV-SINGLE-PROCESS, INV-EVENT-LOG-AUTHORITATIVE,
  ADR-0005) gets buried under topology machinery.

---

### B. Flat children (no integration surface)

**Elevator pitch:** Every implementable leaf is its own
`feature/<item_id>` branch (as today). Each leaf opens its own PR
direct to `main`. Atomicity, if needed, happens by *aggregation at
root level* — the root workflow tracks "all children merged?" via the
event log, not via branch topology.

**Branch sketch:**

```
main
 ├── feature/100                ← leaf 1 PR → main
 ├── feature/101                ← leaf 2 PR → main
 └── feature/102                ← leaf 3 PR → main
```

**New invariants required:**
- INV-LEAF-MERGES-INDEPENDENT — every implementable item merges to
  `main` on its own. There is no co-merge guarantee at the branch
  level.

**Solves:** P1 (per-item review trivially), P4 (test containment is
just normal PR-level CI), P9 (parallel work — leaves are independent
branches on `main`).

**Does not solve:**
- **P2** — no atomic co-merge. If leaf B fails to merge after leaf A
  succeeded, A is in `main` orphaned.
- **P3** — cross-sibling code-dep is impossible without surgery. B's
  branch off `main` cannot see A's unmerged code.
- **P5** — for a 20-leaf feature the operator gets 20 independent PRs
  with no aggregator.
- **P6** — no plan PR. The plan exists as a JSON sidecar; the operator
  cannot review it in the GitHub UI before code lands.
- **P10** — there is no parent merge to gate.

**Trade-offs:**
- Pro: zero net-new code (`implementation.py` is already this).
  Easiest path to "the demo handles fan-out".
- Pro: cleanest INV-RESTART story (each leaf is its own independent
  sub-workflow run; the existing crash-point matrix already covers
  it).
- Con: this is **not actually a fan-out story** — it's a "fan-out of
  unrelated items". The whole point of MGs is to handle *related*
  items, and B drops the relatedness.
- Con: the "what if a leaf fails after siblings merged?" question has
  no good answer.

**Verdict:** acceptable for the **re-scoped v0** Mahler-3 named (single
power-user, single root, treats all leaves as independent). Not
acceptable for the full §9 v0 because non-negotiable #7 explicitly
requires "merge-group implementation … with idempotent re-entry" —
flat-children does not produce merge groups at all.

---

### C. Single merge-train per root

**Elevator pitch:** One `train/<root>` branch onto which every child
rebases and lands sequentially in topological order. When all children
have landed on the train, root opens `train/<root>` → `main` as one
PR. Each child still gets its own *review* PR (squashed onto the
train).

**Branch sketch:**

```
main
 └── train/{root_id}            ← integration trunk
      ← impl-1 rebased + squashed on
      ← impl-2 rebased + squashed on
      ← impl-3 rebased + squashed on
```

**New invariants required:**
- INV-TRAIN-IS-LINEAR — the train branch's commit history is linear
  (each child lands as one squash commit). No merge commits on the
  train.
- INV-CHILD-PR-REBASES-ON-TRAIN-HEAD — every child PR's HEAD is
  `git merge-base train/<root>` ancestry at merge time.

**Solves:** P1 (per-item review via child PRs), P2 (atomic co-merge —
train → main is one merge), P3 (children rebase on train head, so a
child can see prior children's code), P5 (the train PR is the aggregator),
P6 (could be added: train opens *first* as a plan PR), P10 (driver
gates train→main merge on "all children landed").

**Does not solve:**
- **P9** — the train is a serial integration surface; children land
  one at a time. Parallel agent work is possible (multiple agents
  produce branches concurrently), but the *land step* is serial.
- **P7** — no nested MG concept; replan that adds children mid-flight
  just appends to the train, which is fine, but replan that *removes*
  a landed child requires `git revert` on the train (auditable but
  awkward).

**Trade-offs:**
- Pro: simpler than option A. Half the new code (no nested-MG
  recursion, no topology hash, no cross-MG rebase machinery — just
  rebase-on-head).
- Pro: matches how humans naturally stage related work ("queue up the
  diffs, ship as one feature").
- Con: serialisation of the land step. For 20 leaves that's 20
  sequential rebases.
- Con: rebase-on-head means every child branch's history rewrites as
  earlier children land. Reviewer comments on old SHAs go stale.
- Con: still doesn't address P6 (plan PR) without adding a separate
  `plan/<root>` branch on top.

**Verdict:** clean and shippable, but the rebase-rewriting-SHAs problem
is real and operator-visible. Option D below avoids it.

---

### D. Plan-PR-as-aggregator (RECOMMENDED — see below)

**Elevator pitch:** One `feature/<root>` integration trunk per run.
Every implementable leaf is an `impl/<root>-<item>` branch off the
trunk; leaf merges to the trunk via individual PR (squash or merge,
operator's choice). The plan itself is a `plan/<root>` branch with its
own PR (against the trunk, opened before any impl branch is cut). The
trunk merges to `main` as one feature PR. **No nested `mg/` layer.**
For large fan-outs that benefit from a middle aggregation level
("data layer" vs "UI layer"), the planner declares a stable
**review group label** — purely a UI/dashboard grouping concept, **not
a branch**. This is the polyphony Rev 4 model with the recursive
`mg/{root}_{path}_…` layer collapsed.

**Branch sketch:**

```
main
 └── feature/{root_id}                          ← integration trunk (run scope)
      ├── plan/{root_id}                        ← plan PR → feature trunk
      ├── impl/{root_id}-{item_id_A}            ← leaf A PR → feature trunk
      ├── impl/{root_id}-{item_id_B}            ← leaf B PR → feature trunk
      └── impl/{root_id}-{item_id_C}            ← leaf C PR → feature trunk
```

Three branch prefixes: `feature/`, `plan/`, `impl/`. Two delimiters:
`/` for ref-class, `-` for `{root}-{item}` payload. **No `_`. No
recursive `mg_path`.**

**New invariants required:**
- INV-FEATURE-TRUNK-PER-RUN — every root run owns exactly one
  `feature/<root>` branch; it is the only thing that ever merges to
  `main` for that run.
- INV-IMPL-BRANCH-PER-LEAF — every implementable leaf gets exactly one
  `impl/<root>-<item>` branch off the trunk.
- INV-PLAN-PR-PRECEDES-IMPL — the plan PR opens before any impl
  branch is cut; impl branches branch from the trunk *after* the plan
  PR merges (or from a `--skip-plan-pr` operator override).
- INV-DRIVER-GATES-FEATURE-MERGE — the feature → main merge waits on
  *all* leaf-impl PRs to land on the trunk and *all* in-scope items'
  requirement dispositions to be satisfied.
- INV-NO-DIRECT-TRUNK-COMMITS — the feature trunk receives commits
  only via merged impl PRs and the merged plan PR.

**Solves:**
- **P1** — one impl PR per leaf, full review attribution.
- **P2** — atomic co-merge: nothing reaches `main` until the feature
  PR merges; if a leaf fails partway, the trunk is just abandoned (or
  resumed) without polluting `main`.
- **P3** — cross-sibling code-dep: B branches from the trunk, which
  already contains A's merged code. Standard intra-trunk stacking, no
  cross-MG rebase machinery.
- **P4** — test containment: trunk-level CI runs on every impl PR's
  merge into the trunk; failing tests on one impl PR block that PR
  only.
- **P5** — partially. The feature trunk is the natural aggregator.
  For large fan-outs, planner-declared review-group labels (a UI
  grouping, *not* a branch) cluster impl PRs in the dashboard.
- **P6** — plan PR is a real `plan/<root>` branch with a real PR
  reviewable in the GitHub UI before any impl branch exists.
- **P8** — same-root run lock + manifest sidecar as today
  (`root_dispatch.write_manifest`), extended with a feature-branch
  refusal: cannot start a fresh run if `feature/<root>` exists with
  open impl PRs.
- **P10** — driver gates trunk → main on leaf-impl-PR aggregate state.

**Partially solves:**
- **P9** — parallel work is *possible* (agents produce branches
  concurrently) but the *trunk-merge step* serialises. For v0 (single
  operator, agent-throughput is the bottleneck) this is acceptable.

**Does not solve:**
- **P7** — replan-mid-flight with stable ids. v0 question: do we have
  this use case? The audit doesn't list it. Defer until we do.

**Trade-offs vs. polyphony status quo:**
- Drop: nested `mg/{root}_{path}` recursion, topology hash, default-nest
  trigger, `_` vs `-` delimiter discipline (we have only `/` and `-`),
  cross-MG code-dep rebase machinery, parent-plan-generation
  serialisation, depth-3 warning / depth-5 hard stop.
- Keep: per-item review (P1), atomic integration (P2), plan-PR
  separation (P6), driver gates (P10), same-root lock (P8).
- Move from "branch encodes state" to "event log encodes state, branch
  encodes diff under review" — aligns with INV-EVENT-LOG-AUTHORITATIVE
  by construction.

**Trade-offs vs. each other option:**
- vs. **A (polyphony-compat):** loses nested-MG aggregation for
  20+-leaf features; gains a ~5-10× smaller surface area. v0 audience
  is one operator on one root — 20-leaf features are post-v0.
- vs. **B (flat children):** gains P2, P3, P6, P10. Trades 1 extra
  branch type (`feature/`) and 1 extra PR per run (the feature PR).
- vs. **C (merge-train):** wins on SHA stability for in-flight impl
  PRs (no rewriting earlier children's history). Loses linear-history
  property on the trunk (merge commits in the trunk are fine — they're
  the integration record), which is a feature for some teams and a
  cost for others.

---

### E. Hybrid — option D as v0, optional MG nesting in v1

**Elevator pitch:** Ship option D for v0. Add a `--nest-mg=<id>`
planner override in v1 that lights up *one level* of nested `mg/`
branches *only for subtrees the planner explicitly groups*. No
default-nest trigger; nesting is opt-in.

**This is not really a separate topology — it's option D plus a
forward-compatibility note.** Listed here only to make explicit that
"recommend D for v0" does not foreclose porting the nested-MG layer
later if real workload demands it.

---

## Recommendation

**Adopt option D (plan-PR-as-aggregator) for v0.**

Rationale:

1. **It solves all the load-bearing problems** (P1, P2, P3, P4, P5
   trunk-level, P6, P8, P10) and defers the
   CONDUCTOR-INHERITED / OPTIMISATION ones (P7, P9, P5 multi-MG-level).

2. **It is the smallest topology consistent with non-negotiable #7.**
   The audit's verbatim requirement is "Merge-group implementation
   (`mg/`, `impl/`) with idempotent re-entry". Strict reading requires
   the `mg/` prefix; the load-bearing reading requires "a per-PR
   review surface plus an integration surface". Option D satisfies the
   load-bearing reading. **This ADR is the place to either renegotiate
   the strict reading or commit to A — Daniel's call (open question
   1).**

3. **It maps to Requiem's existing primitives without ceremony.**
   - `feature/<root>` replaces `feature/<item_id>` in
     `implementation.py:385` — one-line change conceptually, larger
     ripple in idempotency keys.
   - `impl/<root>-<item>` is a renaming of the existing per-leaf
     `feature/<item_id>` model.
   - The plan PR is a new workflow node in `planning.py` that opens a
     PR on the plan sidecar artefact already written.
   - Each leaf-impl runs as a sub-workflow per ADR-0005; INV-RESTART,
     INV-SUBWORKFLOW-LOG-ISOLATION already covered.
   - The driver gate on feature→main is the existing `close_out`
     workflow plus an aggregate-children check.

4. **It honours the architectural bet of Requiem.** The reason Requiem
   exists at all (per ADR-0001 and the north-star §2 invariants) is to
   *replace* polyphony's complexity, not to reimplement it. Adopting
   option A would buy parity at the cost of the architectural payoff.
   Adopting D buys 80% of the parity for 30% of the complexity and
   leaves the door open for nested-MG opt-in (option E) when a real
   workload demands it.

5. **It does not foreclose option A.** The migration from D to A is
   additive — a new `mg/` ref-class plus a default-nest trigger. The
   migration from A to anything simpler is not — branches are one-way
   doors (polyphony skill, line 628).

### What it costs to build (rough)

| Component | Estimated effort |
|-----------|------------------|
| Rename `feature/<item_id>` → `feature/<root>` + `impl/<root>-<item>` in `implementation.py`, with idempotency-key migration and a per-leaf sub-workflow shim. | 1-2 days |
| New `plan_pr` verb in `planning.py` (opens `plan/<root>` PR against the trunk on plan-write). | 1 day |
| Same-root lock — extend `root_dispatch.write_manifest` to refuse if `feature/<root>` exists with open impl PRs and the operator did not pass `--resume`. | 0.5 day |
| Feature-PR workflow (`feature_pr.py`) — opens trunk → main PR when all children's impl PRs are merged; gates on requirement disposition. | 2 days |
| Fan-out executor (the missing "Berlioz-Phase-D" piece) — given a recursive `PlanResult` tree, dispatch each implementable leaf as a sub-workflow via existing ADR-0005 primitive. Aggregate via the existing crash-point-13/14 patterns. | 2-3 days |
| Tests: per-leaf INV-RESTART, atomic-co-merge happy path, atomic-co-merge mid-flight failure (one leaf fails, no merge to main), plan-PR-precedes-impl invariant, same-root lock, fan-out resume after worker death. | 2 days |
| Operator-facing render hints + verdict cards for the new flow. | 1 day |
| **Total** | **~10 working days** for a single seat, comparable to one Phase-C-class workflow. |

Option A's analogous estimate is, eyeballing the polyphony skill,
3-4× larger plus operator-doc work.

---

## Consequences

### Positive
- Closes non-negotiable #7 with a load-bearing answer, not a strict-prefix
  cargo-cult.
- Unlocks fan-out for `full_sdlc.py` (the Berlioz-Phase-D work the
  current demo punts on).
- Plan-PR-vs-impl-PR separation (P6) is operator-visible value that
  polyphony's dogfood loved.
- Keeps the topology vocabulary small (three prefixes, two delimiters)
  — easy to teach, easy to render in the UI, hard to corrupt.
- Aligns branch-as-diff with INV-EVENT-LOG-AUTHORITATIVE — branches
  carry diffs, the log carries state.

### Negative / load-bearing follow-ups
- **Drop:** nested `mg/` aggregation, default-nest trigger, topology
  hash, cross-MG rebase machinery, parent-plan-generation
  serialisation, depth gates, `evidence/` branches (the actionable
  facet does not exist in Requiem today; see [Mahler-3 §2.2 row
  "actionable.yaml"](../references/v0-parity-readiness.md)).
- **Defer:** P9 (parallel item execution). v0 fan-out is serial;
  per-item worktrees are a separate non-negotiable (#5) and a separate
  ADR. Sequential execution on a single workspace is acceptable for
  the single-operator audience (north-star §5).
- **Risk: option D does not satisfy a literal reading of non-negotiable
  #7.** The audit text says "`mg/`, `impl/`". Option D has `impl/` but
  no `mg/`. Either the audit's reading relaxes to "per-leaf review +
  integration surface" (which option D satisfies) or option A is the
  only acceptable answer. **This is open question 1.**
- **Idempotency-key migration:** today `feature/<item_id>` is the
  resume key for an implementation sub-workflow. Under option D, the
  key becomes `(root_id, item_id)` and the impl branch becomes
  `impl/<root>-<item>`. INV-RESTART requires every state-mutating
  verb to be idempotent under the new key — needs a careful pass.
- **Test surface:** every implementation-workflow test has to handle
  the `feature/` → `impl/` rename. ~29 tests in
  `test_implementation_workflow.py` plus ~12 in `test_full_sdlc.py`.
- **Operator mental model shift:** "where did my code land?" answer
  changes from "the `feature/12345` branch and PR" to "the
  `impl/100-12345` branch, then the `feature/100` trunk PR, then
  `main`". The dashboard / verdict cards must explain this clearly or
  the operator will be confused.

### Out of scope for this ADR (filed as separate concerns)
- **Per-item worktree isolation (Mahler-3 §9 #5).** Orthogonal to
  topology. A future ADR (`0007-worktree-isolation.md`?) decides
  whether worktrees are per-leaf or per-MG or none.
- **ADO PR lifecycle (Mahler-3 §9 #10).** Orthogonal — both options
  apply equally to GitHub and ADO PRs.
- **Reset / reconcile verbs.** Orthogonal — recovery surfaces work the
  same for any topology.
- **Web dashboard (Mahler-3 §9 #8).** The dashboard renders the
  topology, but the topology choice does not depend on the dashboard.

---

## Open questions for Daniel

These were genuine open questions when the ADR shipped; resolutions land
under "Decision log" below as they close.

1. **Does the literal reading of non-negotiable #7 stand?** — **CLOSED 2026-06-01.**
   Resolution: load-bearing reading. Daniel amends the audit row's wording
   from `"mg/, impl/"` to **"per-item review + integration surface"**.
   Option D is accepted. See "Decision log → Q1" below.

2. **Plan-PR realisation: real PR or virtual artefact?** — **CLOSED 2026-06-01.**
   Resolution: build the plan-PR capability (`plan/<root>` branch + PR-cut
   verb against the feature trunk). The cut/gate/persist policy is
   agent-evaluated × user-policy and lives in ADR-0008. See
   "Decision log → Q2" below.

3. **Atomic co-merge guarantee — how strict?** — **CLOSED 2026-06-01.**
   Resolution: curator-decided recovery (per-slice, agent-evaluated ×
   user-policy), with human-gate roll-forward a *required* option in the
   policy menu. Abandon-trunk becomes one selectable (terminal) recovery
   action, not the default. See "Decision log → Q5" below.

4. **Replan mid-flight: in scope for v0?** — **CLOSED 2026-06-01.**
   Resolution: in scope, but **narrowly gated** — replan fires only on a
   CRITICAL-class invalidity (per speckit's severity ladder) discovered
   after impl began. Requires stable planner-declared item ids (now
   in-scope for v0). See "Decision log → Q6" below.

5. **Should the planner declare review-group labels?** — OPEN; **scope
   shifted** by the Q1 reframe. The reviewable-set decision is no longer
   purely a "labels in YAML" question — it is the seed of an agent-driven
   curation policy. Tracked as Q7; the deeper concept lives in **ADR-0008
   (review-surface curation)**.

6. **Same-root run lock semantics.** — OPEN. Tracked as Q8.

7. **Should `feature/<item_id>` survive for the single-leaf case?** —
   OPEN. Tracked as Q10.

---

## Decision log

### Q1 (closed 2026-06-01) — non-negotiable #7 reading

**Decision:** Load-bearing reading. Option D is adopted. The audit row
wording is amended from `"Merge-group implementation (mg/, impl/) with
idempotent re-entry"` to `"Per-item review + integration surface with
idempotent re-entry"` — this ADR is the canonical place that amendment
lives; a follow-up edit will flow into `docs/references/v0-parity-readiness.md`
when the audit row's status is updated.

**Reframe captured during the decision (Daniel's framing):**

> "MGs were really about reviewable sets. That was the logical value.
> Users may not want to review every PR, especially small ones we can
> auto-merge. We should probably not even create a PR if the user doesn't
> want to review it. The north star really is about *surfacing reviews to
> the user that they're likely to want to care about, at a size that is
> reasonable*. Might want that to be a tunable decision made by agents
> rather than purely deterministic."

This separates two concerns that polyphony's MG model conflated:

- **Branch topology** (this ADR) — how branches encode an in-flight
  implementation on disk. Option D: `feature/<root>` + `plan/<root>` +
  `impl/<root>-<item>`. Three prefixes, two delimiters, no nesting.

- **Reviewable-set policy** (forthcoming ADR-0008) — per-slice, which
  diffs we surface for human review, at what size, via what surface
  (GH PR, in-process / "local" review, auto-merge). The decision is
  **agent-tunable** (a curator role decides per-slice based on diff
  shape, risk score, item type, operator preference), not a static
  config knob.

The branch topology decided here is the chassis; ADR-0008 will decide
how many reviewable slices ride on the chassis and how each one is
surfaced. The two ADRs compose: a slice marked `auto_merge` by the
curator does not need an `impl/` PR at all; a slice marked `pr_review`
gets the full Option-D `impl/<root>-<item>` PR; a slice marked
`local_review` is reviewed in-process and either auto-merges to the
trunk or escalates to a PR if the local reviewer flags concerns.

**Cascade effects on other Q's** (now reflected in the briefing):

| Q | How Q1's resolution reshapes it |
|---|---|
| Q5 | Auto-merged slices that already landed in `main` change the abandon-trunk option's blast radius. |
| Q7 | Promoted from "UI grouping labels" to a first-class concept — the planner emits *review intent* per slice, not just labels. Folds into ADR-0008. |
| Q10 | If the curator says "tiny + safe + auto-merge" for a leaf-only root, the `feature/<root>` trunk may not be needed at all. |
| Q2 | The plan-PR itself is subject to the same curator decision — sometimes a real `plan/<root>` PR, sometimes a markdown summary, occasionally auto-confirmed for trivial roots. |

**Follow-up actions:**

1. **Dispatch ADR-0008 (curated artifacts across the run lifecycle)** as a
   Wave 7 design seat after the remaining Q's are resolved (Q7 is the
   natural touch point). Scope expanded by Q2 to cover plan PR
   cut/gate/persist in addition to impl-slice review-surface curation.
2. **Update Mahler-3's parity audit row #7** wording when ADR-0008's
   shape is settled (do it in one pass to avoid two amendments).

---

### Q2 (closed 2026-06-01) — plan-PR realisation

**Decision:** Build the plan-PR capability for v0. A `plan/<root>` branch
opens a PR against the `feature/<root>` trunk, with the
JSON-rendered-to-markdown plan as the diff. Whether to use the capability
on any given run, and what happens to the plan content downstream, is an
agent-evaluated × user-policy decision deferred to **ADR-0008**.

**The framing that closed it (Daniel's reframe):** the original Q2 binary
("real PR vs virtual markdown in the feature-PR body") was a false
dichotomy. The plan PR's lifecycle has three staged decisions, each of
which is an agent-evaluated policy call, not a static design choice:

| Stage | Question | Owner |
|---|---|---|
| **Cut** | Should we open a plan PR for *this* initial planning pass? | Agent evaluates complexity × user policy. Below threshold → no plan PR; the run keeps going. |
| **Gate** | If cut, impl waits on the plan PR's trunk merge. | Deterministic from the cut decision. |
| **Persist** | When the feature trunk merges to main, do plan docs ride along, get transformed (squash to an as-built summary), or get stripped? | Agent evaluates "does this still add post-impl value?" × user policy. |

**Invariant amendment:** INV-PLAN-PR-PRECEDES-IMPL is reworded from
"plan PR merges before impl branches are cut" to:

> **INV-PLAN-PR-PRECEDES-IMPL (revised):** *If* the curator elects to
> cut a plan PR, the plan PR's merge to the feature trunk precedes any
> impl-branch cut for the same run. If the curator skips the plan PR
> (initial-planning complexity below review threshold per agent
> evaluation × user policy), this invariant is vacuous and impl
> proceeds directly.

**Why "build the surface" was the v0 call:** without the capability the
curator has no lever to pull and the gate/replan shape is foreclosed
forever. With the capability the curator can decide per-run whether to
use it — including "skip for trivial roots, auto-confirm without
operator review." Same ~1-day engineering cost as Stravinsky's original
recommendation; what changes is *when* the capability gets used.

**ADR-0008 scope expansion (this question grew it):** the ADR now covers
**curated artifacts across the run lifecycle**, not just impl-slice
review-surface curation. The pattern generalises:

| Family | Cut? | Gate? | Persist? |
|---|---|---|---|
| Plan | curator | conditional on cut | curator |
| Impl slice | `auto_merge` / `local_review` / `pr_review` | depends on review intent | always (it's the code) |
| *(future)* design docs, ADRs, scratch notes | same pattern | same pattern | same pattern |

**Replan carve-out:** replan policy may differ from initial-plan policy.
Punted to Q6 (replan in scope for v0). If Q6 = "no replan in v0," replan
curator policy doesn't need to be settled today.

**Follow-up actions:**

1. **ADR-0008 must address all three stages** (cut, gate, persist) for
   the plan family in addition to impl-slice review-surface curation.
2. **Strip-on-persist needs a concrete mechanism.** Candidate: a final
   commit on the feature trunk before trunk→main merge that removes the
   plan sidecar (or transforms it). Spec lives in ADR-0008.
3. **`plan_pr` verb spec.** Lives in implementation work after ADR-0008
   resolves the policy hooks. Should accept the curator's cut decision
   as input rather than always cutting.

---

### Q5 (closed 2026-06-01) — atomic co-merge recovery when a leaf fails mid-trunk

**The problem (grounding scenario):** under Option D a decomposable root
opens a `feature/<root>` trunk and lands impl leaves on it one at a time.
If leaf B fails to integrate *after* sibling leaf A already merged to the
trunk, the trunk holds A's real, reviewed code but has no path to `main`
(the trunk→main step only fires once the whole root is satisfied). Five
failure modes produce this state: (1) trunk merge conflict between
siblings, (2) post-integration test failure surfaced by a later leaf,
(3) coder agent gives up / operator skips a leaf, (4) reviewer demands a
replan, (5) drift from `main` during a long-lived trunk.

**Decision:** Recovery is **curator-decided** — the same agent that
selects a leaf's review surface (Q1) and the plan's cut/gate/persist
(Q2) also selects the recovery action when a leaf fails to integrate.
The selection is per-slice, agent-evaluated × user-policy. **Human-gate
roll-forward is a required member of the recovery-action menu** — the
policy must always be able to halt at a `needs_human` gate and let the
operator roll forward (retry the leaf, patch it manually, skip with
justification, or abort). Abandon-trunk is retained as the terminal
selectable action, **not** the default.

**Daniel's framing:** *"curator decides is generally right, but policy
should allow for human gate roll forward."*

**Recovery-action menu (curator selects per failure):**

| Action | Typical trigger | Effect |
|---|---|---|
| **auto-retry** | mechanical failure (modes 1–2) | re-run the failing leaf's coder once with the conflict / failure context. |
| **rebase + retry** | drift from main (mode 5) | rebase the trunk onto `main`, re-run the failing leaf. |
| **human-gate roll-forward** *(required)* | semantic failure / ambiguity (modes 3–4) | halt at `needs_human`. Operator: retry, patch, skip-with-justification, or abort. Already-merged leaves preserved. |
| **abandon-trunk** *(terminal)* | unrecoverable / operator-elected | delete trunk + impl branches, escalate, re-run root from scratch. Already-merged work is lost. |

**Why not abandon-trunk-by-default (the polyphony posture):** it produces
the worst single-operator UX — "four impl PRs merged, the fifth failed,
now we throw all four away." Worse, with the Q1 reframe it makes
`auto_merge` slices *less* durable than `pr_review` slices (an
auto-merged leaf already on the trunk gets discarded when a sibling
fails), which inverts the curator's intent. Roll-forward preserves all
already-merged work regardless of which review surface produced it.

**Invariant note:** the atomic-co-merge promise is scoped to the
**trunk→main** step (either the whole root lands on `main` or `main`
never sees it). It is *not* a promise that intermediate trunk
integrations are atomic — those are recoverable per the menu above.

**Forward dependency on Q9 (drift-rebase verb):** the rebase+retry
action and long-lived trunks mean Q9 must handle "rebase a trunk that
holds partially-completed work" cleanly. Carried into Q9.

**ADR-0008 home:** the recovery-policy menu is part of the curator's
remit and is specified in ADR-0008 alongside review-surface and
plan-artifact policy. Same pattern: agent evaluates → deterministic
action against policy, with human-gate as a guaranteed fallback.

---

### Q6 (closed 2026-06-01) — replan mid-flight, narrowly gated to CRITICAL invalidity

**Decision:** Mid-flight replan **is** in scope for v0, but gated behind a
CRITICAL-class invalidity trigger. The plan is otherwise frozen once impl
begins; only a *fundamental* invalidity discovered after impl started
earns a re-decomposition.

**Daniel's framing:** *"replanning midflight is only in scope if
something about the plan is found to be fundamentally invalid. We should
get concrete about what that means."*

**"Fundamentally invalid" defined concretely — adopt speckit's CRITICAL
severity definition** (from `/speckit.analyze` + `/speckit.plan` gate
semantics). A plan is fundamentally invalid (→ replan) only when one of
these holds:

| Trigger | Speckit analogue | Why it's structural, not patchable |
|---|---|---|
| Constitution / invariant violation surfaced mid-impl | analyze CRITICAL — MUST violation | the decomposition placed work where it can't legally live; no leaf-level fix is legal. |
| Foundational assumption falsified | plan ERROR — a resolved `NEEDS CLARIFICATION` proves false | every downstream leaf inherits the false premise; patching one leaf doesn't repair the premise. |
| Dependency structure proven impossible | analyze CRITICAL — ordering contradiction | a cycle / unsatisfiable ordering in the leaf graph; structural, not a leaf bug. |
| Baseline-blocking coverage gap | analyze CRITICAL — zero-coverage requirement blocking baseline | a required leaf was omitted entirely; the gap is in the plan shape, not in an existing leaf. |

**Non-triggers (HIGH and below → Q5 roll-forward within the existing
plan structure, NEVER replan):** buggy leaf code (retry/patch), reviewer
wants cleaner naming or smaller diffs (patch), sibling merge conflict
(rebase+retry), "this could be nicer" (defer/patch). Speckit's rule
holds: CRITICAL blocks; HIGH-and-below may proceed.

**Structural consequences:**

1. **Stable planner-declared item ids are now REQUIRED for v0** (not
   merely reserved). Narrow-replan means the executor must reconcile a
   new plan against existing impl branches: keep completed leaf A, retire
   the invalidated leaf, add replacement leaves (B1/B2). Without stable
   ids the executor can't tell "same leaf A" from "new leaf." This is
   polyphony's P7 stable-MG-ids requirement, scoped down to ids-only.

2. **Detection owner: both-layered.**
   - Adopt speckit's CRITICAL definition as the **contract** for what
     "fundamentally invalid" means (not vibes — the table above).
   - Make **replan a Q5 recovery-menu action**, selectable *only* when a
     CRITICAL-class finding is present, and human-gateable (the operator
     confirms the replan per Q5's required roll-forward option).

**Cross-references:** depends on Q5 (replan is a recovery-menu action);
feeds Q7 (stable ids interact with review-group labels); the CRITICAL
contract is a candidate adoption point for a broader **spec-kit
alignment** investigation (see ADR-0009, forthcoming).

---

## References

### North-star invariants (in priority order for this decision)
- **INV-EVENT-LOG-AUTHORITATIVE** — [north-star §2](../north-star.md).
  The single most important reason option D is viable: branches no
  longer need to encode workflow state.
- **INV-SUBWORKFLOW-LOG-ISOLATION** — [north-star §2](../north-star.md),
  ratified by [ADR-0005](0005-subworkflow-invocation-primitive.md). Each
  per-leaf impl runs as a sub-workflow with its own log; recursive
  fan-out is a solved problem at the kernel layer.
- **INV-RESTART** — [north-star §2](../north-star.md). Constrains the
  branch-rename migration — every new verb must be idempotent under
  the new `(root_id, item_id)` key.
- **INV-NO-CORRUPT-FORWARD** — [north-star §2](../north-star.md). The
  feature → main merge gate is a hard "refuse to proceed if any leaf's
  requirements unsatisfied" check, not a "best-effort" merge.
- **INV-SINGLE-PROCESS** — [north-star §2](../north-star.md), ratified
  by [ADR-0001](0001-single-process-architecture.md). The reason
  Requiem exists. Option A would erode the payoff; option D preserves
  it.

### Polyphony prior art
- [`polyphony-branch-model` skill (Rev 4)](file:///C:/Users/dangreen/projects/polyphony-squad-spike/.github/skills/polyphony-branch-model/SKILL.md)
  — the source of truth for option A. 628 lines.
- [`polyphony-sdlc` skill](file:///C:/Users/dangreen/projects/polyphony-squad-spike/.github/skills/polyphony-sdlc/SKILL.md)
  — SDLC vocabulary.
- [`polyphony-workflow-author` skill](file:///C:/Users/dangreen/projects/polyphony-squad-spike/.github/skills/polyphony-workflow-author/SKILL.md)
  — the workflow-YAML idioms option A would have to port.

### Requiem prior art
- [`docs/references/v0-parity-readiness.md`](../references/v0-parity-readiness.md)
  — Mahler-3 audit. §2.2 workflow catalogue (rows for
  `polyphony.yaml`, `plan-level.yaml`, `implement-merge-group.yaml`,
  `feature-pr.yaml`), §2.3 state model (branch-model row), §9 #7
  non-negotiable.
- [`docs/references/polyphony-parity-inventory.md`](../references/polyphony-parity-inventory.md)
  §3 state model (run manifest, branch model, worktree layout,
  authoritative-source ranking), §9 #7 non-negotiable.
- [`docs/decisions/0005-subworkflow-invocation-primitive.md`](0005-subworkflow-invocation-primitive.md)
  — the primitive every per-leaf impl will use.

### Current Requiem code
- [`src/requiem/workflows/implementation.py:385`](../../src/requiem/workflows/implementation.py)
  — `branch_name = f"feature/{inputs.item_id}"`. The thing this ADR
  changes.
- [`src/requiem/workflows/implementation.py:312-326`](../../src/requiem/workflows/implementation.py)
  — `ImplementationInputs`. Will need a `root_id` field under option D.
- [`src/requiem/workflows/planning.py:160-176`](../../src/requiem/workflows/planning.py)
  — `PlanResult` recursive tree. The tree-of-plan-output that an
  option-D executor consumes.
- [`src/requiem/workflows/planning.py:181-198`](../../src/requiem/workflows/planning.py)
  — `ChildPlan` / `PlannerOutput`. Adding a `review_group` field
  (open question 5) lands here.
- [`src/requiem/workflows/full_sdlc.py:55-59`](../../src/requiem/workflows/full_sdlc.py)
  — the docstring's "Multi-leaf plans: … the demo collapses them to
  one leaf … Real fan-out is Berlioz-Phase-D's job." Option D's
  executor *is* that work.
- [`src/requiem/workflows/full_sdlc.py:419-481`](../../src/requiem/workflows/full_sdlc.py)
  — the five-stage linear pipeline. Adding a fan-out node between
  `plan` and `implement` is the structural change.
- [`src/requiem/workflows/root_dispatch.py:1-44`](../../src/requiem/workflows/root_dispatch.py)
  — `validate_root`, `compute_run_id`, `write_manifest`. Same-root
  lock (open question 6) extends this.

### Daniel's framing for this rethink
> "I'm open to rethinking the merge-group topology design by the way.
> Not convinced we had it right before."

Stravinsky, Wave 6.
