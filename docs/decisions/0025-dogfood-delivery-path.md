# ADR 0025 — Dogfood delivery path: from "planning works" to "feature delivered via requiem"

**Status:** Proposed (2026-06-17)
**Date:** 2026-06-17
**Relates to:**
ADR-0010 (process-config tier model — implementable/decomposable types),
ADR-0011 (commit_plan seeding),
ADR-0017 (Hermes delivery fleet),
ADR-0021 (in-process fanout orchestrator),
ADR-0024 (RepoPlatform Protocol — steps 1-5 shipped).

## Context

The 2026-06-17 dogfood session against CVAPI Scenario `#62759077`
("Capacity-aware VMSS SKU fallback") drove the requiem `--commit`
end-to-end pipeline against live ADO + live Copilot and surfaced a
clear gap map between **what works today** and **what's needed to
deliver a real feature end-to-end via requiem**. This ADR captures
the gap map, the proposed delivery sequence, and the exit criteria
for each gap.

### Session results that shipped (sets context for the gaps)

- `d155ae8` — CopilotProvider: strip outer markdown code fences
- `28fc4e9` — recursive child fetch_item uses parent ChildPlan
  (planning recursion works without twig)
- `78249c7` — `requiem clean` subcommand
- `f121dcd` — CopilotProvider: extract JSON from anywhere in response
  (handles prose preamble + mid-response fences)
- Local: `cloudvault-service-api/.requiem-config/process.yaml`
  configuring CVAPI's type hierarchy
  (`Objective|Key Result|Epic|Scenario|Feature` decomposable;
  `Task|Bug|User Story` implementable). Disposition pending —
  see "Decisions to make" §1 below.

### What works today (proven against live ADO + live Copilot)

| Stage | What it does | Proof |
|---|---|---|
| 1. **Planning** | LLM proposes children; reviewer approves; recurses on decomposable types | 4 dogfood runs; recursion proven 4 levels deep on #62759077 attempt 2 |
| 2. **commit_plan** | Seeds real ADO children under a Scenario | UNIT-tested only; never reached in any live run |
| 3. **trunk_bootstrap** | Creates `feature/<root>` branch from `main` | Live dry-run proven (#62597743 + #62758386) |
| 4. **exec / dispatch** | Spawns workers per leaf | DRY-RUN only; backends broken (see Gap B) |
| 5. **leaf_pr** | Opens per-Task PRs into trunk | Live dry-run proven |
| 6. **integrate_pipeline** | Opens trunk→main feature PR | Live dry-run proven |

**The pipeline is dry-run-proven end-to-end against live ADO. Zero
ADO mutations have been made by requiem to date.** Every run has
terminated in planning before commit_plan could fire.

### The three gaps blocking real delivery

#### Gap A — Planning escalation cascade kills runs at the leaf level

When the planner produces a Task summary the reviewer deems "vague"
(e.g. "Implement error handling and fallback-of-fallback scenarios"
from #62759077 attempt 3), the reviewer returns `verdict='escalate'`.
`escalation_gate` auto-aborts in batch mode (no human in the loop)
→ `subworkflow_completed` propagates `permanent_failure` →
parent's `escalation_gate` cascades → whole run dies → commit_plan
never reached.

**Five of seven leaves planned cleanly** in #62759077 attempt 3 with
substantive summaries and rationales citing `Per repo policy, Task
is implementable...`. The two failures hit the cascade and killed
the whole tree.

**Root cause:** when the type policy already says "this is an
implementable leaf, do not decompose," there is no useful work left
for the planner OR the reviewer to do. The planner can only restate
the title in prose; the reviewer is then forced to evaluate that
prose without the authority to do anything actionable about it.
We're paying for two LLM calls per leaf to produce no decision the
type policy didn't already make.

#### Gap B — `implementation` workflow is GitHub-only (ADR-0024 step 6)

The per-leaf `implementation` workflow that spawns inside
`fanout`-backend dispatch hard-codes `_require_gh(ctx)` and
`toolbelt.gh` (line 438 of `src/requiem/workflows/implementation.py`).
ADR-0024 steps 1-5 made the trunk-topology workflows
(`trunk_bootstrap`, `leaf_pr`, `feature_pr`) platform-neutral via the
`RepoPlatform` Protocol; step 6 (the per-leaf workflow that pushes
and opens a PR for each leaf's branch) was scoped but not shipped.

Result: even with Gap A fixed and the trunk created, a `--live`
run with the `fanout` backend would crash inside each leaf's
implementation workflow trying to invoke `gh` against an ADO repo.

The `kanban` backend doesn't have this problem because it dispatches
to a Hermes worker subscribed to the board — but no such worker is
configured for CVAPI today (see Gap C).

#### Gap C — No worker fleet is doing the actual coding

Even with Gaps A and B fixed and the topology set up, **requiem
orchestrates the SDLC; it does not write the SKU-probe code itself**.
The CopilotProvider is wired for *planning + review*, not "make
this branch implement the feature." That's the worker's job:

- **`kanban` backend** expects a Hermes worker fleet subscribed to
  the board. None configured for CVAPI today. Would require either
  a long-running Hermes fleet on a dev machine OR a CI-scheduled
  worker pool, plus authentication for each worker into ADO + the
  build system.
- **`fanout` backend** runs the `implementation` workflow in-process,
  which itself invokes an LLM provider (CopilotProvider per default
  precedence) to write the code. Requires Gap B fixed and probably
  also requires the worktree-isolation work from ADR-0022 if multiple
  leaves are to run in parallel.

## Decision

Pursue a **multi-session, gap-at-a-time** delivery path. Each gap
landing leaves `main` working and the dogfood incrementally closer
to end-to-end. We commit to the **purist path** (Framing 1 from
the session conversation): the SKU-fallback feature **WILL be
delivered through requiem from planning to merged PR**. That is
the dogfood proof-of-value.

### Sequence

The gaps are intentionally ordered by **smallest-blast-radius first**
so each landing buys evidence for the next:

1. **Gap A** — implementable-types short-circuit
2. **Gap B** — implementation workflow takes RepoPlatform (ADR-0024
   step 6, with its own ADR if needed)
3. **Gap C** — pick + stand up a worker backend
4. **End-to-end live run** of #62759077

Each step has its own STATUS block landed under it as work ships.

---

## Gap A: skip planner+reviewer for implementable types

### Scope

Inside `branch_decomposable` (or earlier — at `fetch_item`'s policy
check), when the type policy classifies the work item as
`implementable`, **route directly to `record_plan` with a
synthesised leaf PlanResult**. Skip both `planner_*` and
`reviewer_*` LLM calls entirely.

### Why this is correct, not a hack

- ADR-0010 §1 names this exactly: *"config owns the tier model;
  the planner's judgement does NOT decide tier for items the config
  has classified."* Today's code respects this for the
  `decomposable=true` override (forces leaf) but STILL calls the
  planner and reviewer first, paying for two LLM calls to produce
  a decision the config already made.
- The reviewer's escalation feedback on these leaves ("the plan
  lacks critical specificity required for implementation") is
  literally not actionable: the planner can't decompose, can't add
  technical detail (it doesn't have repo context), and the
  reviewer can't accept partial answers. The whole interaction is
  noise.
- The plan recorded should be a faithful echo of the work item's
  title + description, marked `decomposable=false`, with an
  explicit `policy_tier="implementable"` field so downstream
  consumers know this is policy-driven, not planner-driven.

### Implementation sketch (concrete enough for the next session)

Files:
- Modify: `src/requiem/workflows/planning.py` (the `branch_decomposable`
  verb and the workflow topology — short-circuit the planner →
  reviewer → router_1 chain when policy says implementable)
- Test: `tests/test_planning_type_policy.py` (already exists; add
  cases pinning "no LLM call when implementable"; assert the
  recorded plan has `policy_tier="implementable"` and the
  inspected_artifacts include `policy:implementable` rather than
  any `agent:planner/*` artifact)

### Tasks (bite-sized, 2-5 min each)

**Task A1: Write failing test for "no LLM call on implementable type"**

- Test: `tests/test_planning_type_policy.py::test_implementable_skips_planner_and_reviewer`
- Setup: build_engine with process_config that has
  `implementable_types=frozenset({"Task"})`, a FakeProvider with
  `calls=[]` tracking, an item_id with `work_item_type="Task"` in
  FakeTwigClient.
- Assert: `engine.run("r1")` completes with `final_node="end"`,
  `len(fake_provider.calls) == 0` (no planner OR reviewer LLM call),
  the recorded plan has `decomposable=False`, `summary` equals the
  twig item's title, and `policy_tier == "implementable"`.

**Task A2: Run test to verify failure**

Run: `pytest tests/test_planning_type_policy.py::test_implementable_skips_planner_and_reviewer -v`
Expected: FAIL — planner gets called

**Task A3: Add the short-circuit in the planning workflow topology**

In `planning.py`'s `build_workflow()`, add a new node
`policy_classifier` after `fetch_item` that reads the policy and
routes:
- `policy == "implementable"` → new `record_leaf_from_policy` verb
  → `end`
- otherwise → existing `planner_1` path

`record_leaf_from_policy` synthesises a `PlannerOutput`-shaped
record with `decomposable=False`, `children=[]`, `summary=<title>`,
`estimated_complexity="unknown"`, `rationale="forced leaf per
process config policy: type 'Task' is in implementable_types"`,
and a single inspected_artifact `policy:implementable/<work_item_type>`.

**Task A4: Run test to verify pass**

Run: `pytest tests/test_planning_type_policy.py::test_implementable_skips_planner_and_reviewer -v`
Expected: PASS

**Task A5: Add the "decomposable-type forces planner" guard test**

Test: when a `Scenario` (in decomposable_types) is the root,
planner IS called. Pins that the short-circuit doesn't accidentally
swallow the decomposable path.

**Task A6: Broad sweep**

Run: `pytest tests/test_planning_*.py tests/providers/ tests/test_end_to_end*.py -q --timeout=120`
Expected: all green, plus new tests.

**Task A7: Commit**

```bash
git add src/requiem/workflows/planning.py tests/test_planning_type_policy.py
git commit -m "feat(planning): implementable types skip planner+reviewer LLM calls

Per ADR-0010 §1, the process config tier model is authoritative over
the planner's decomposable verdict. Today the workflow respects this
for the override (force-leaf when planner said decomposable=true),
but still invokes planner+reviewer first — paying for two LLM calls
to produce a decision the config already made.

This is also the root cause of the 2026-06-17 SKU-fallback dogfood
escalation cascade: reviewer escalated leaf Tasks for vagueness,
escalation_gate auto-aborted, the cascade killed the whole tree
before commit_plan could fire. The fix short-circuits planner+
reviewer entirely when policy says implementable; no LLM call, no
escalation surface.

Closes the first of three gaps documented in ADR-0025."
```

### Exit criteria

- [ ] `policy == "implementable"` skips ALL planner + reviewer calls
- [ ] Recorded plan carries `policy_tier="implementable"` marker
- [ ] No LLM provider call recorded in fake's `.calls` for an
  implementable-type root
- [ ] All existing `tests/test_planning_*` cases still pass
- [ ] Re-run of #62759077 attempt 3 produces 7 successful child
  plans (vs today's 5/7 with 2 escalation casualties), commit_plan
  fires, real ADO Tasks appear under `#62759077`

### STATUS

**STATUS: shipped 2026-06-17** (commit: this one).
- New `policy_classifier` verb runs after `fetch_item` and reads
  `_effective_config(ctx).tier_for_type(work_item_type)`. When
  the tier is `implementable`, returns
  `PermanentFailure(error_kind="short_circuit_implementable")`
  which the workflow routes to a new `record_leaf_from_policy`
  verb (uses the established `permanent_failure:<error_kind>`
  routing convention from `branch_decomposable`'s `recurse` branch).
- `record_leaf_from_policy` synthesises a planner-shape dict
  (summary = item title, decomposable=False, children=[],
  estimated_complexity="unknown", rationale citing the policy),
  writes the sidecar via the existing `_write_plan_sidecar` helper,
  and returns a `record_plan`-shape Success value with
  `policy_tier="implementable"` and `final_verdict="policy-forced-leaf"`.
- `project_plan_result` extended to read `record_leaf_from_policy`
  as a third candidate (alongside `record_plan` and
  `record_needs_human`) so downstream consumers reconstruct the
  PlanResult identically regardless of which path produced it.
- 4 new regression tests in `tests/test_planning_type_policy.py`:
  - `test_implementable_type_skips_planner_and_reviewer_entirely`
    — load-bearing pin: empty scripts on both agents, asserts
    `provider.calls == []` and the plan reaches `end`.
  - `test_implementable_type_skip_records_policy_artifact` —
    inspected_artifacts must include `policy:implementable/<type>`
    and must NOT include `agent:planner/*` or `agent:plan_reviewer/*`.
  - `test_decomposable_type_still_calls_planner_no_regression` —
    Scenario root with Task children: 1 planner + 1 reviewer call
    for root only, Task children short-circuit per Gap A.
  - `test_no_tier_policy_still_calls_planner_no_regression` —
    polyphony default config (no implementable_types): planner
    runs as before for all types.
- 11/11 type-policy tests green (was 7); 168 passed + 2 skipped
  across the broad surface — no regressions.

---

## Gap B: implementation workflow takes RepoPlatform (ADR-0024 step 6)

### Scope

`src/requiem/workflows/implementation.py` line 438 hardcodes
`_require_gh(ctx)`. Replace with `_require_repo_platform(ctx)` that
reads `ctx.toolbelt.repo or ctx.toolbelt.gh` (back-compat fallback
exactly as ADR-0024 steps 2-4 established). The implementation
workflow needs these methods today:

- `ensure_branch_ref(repo_id, branch, base_sha)` — create the
  per-leaf branch
- `pr_search` / `find_open_pr_for_branch` — idempotent existence check
- `pr_create(repo_id, head, base, title, body)` — open the leaf PR
- `push_branch` (via the toolbelt's git client, not the platform
  client) — already platform-neutral, no change needed

All of these are already in the `RepoPlatform` protocol from
ADR-0024 step 2. This work is pure refit of one workflow.

### Why this follows the protocol-seam-refactor reference

The 5-step pattern from `writing-plans/references/protocol-seam-refactors.md`
applies, but **steps 1-3 are already done** by ADR-0024:

1. ✓ Auth fix on existing GhClient (ADR-0024 step 1)
2. ✓ Extract Protocol (ADR-0024 step 2)
3. ✓ Build AdoClient + fake (ADR-0024 step 3)
4. ⚠ Refit callers — trunk-topology workflows refitted (ADR-0024
   step 4), implementation workflow NOT YET (this gap)
5. ⚠ Driver wiring — partially done (`--ado-repo` accepts at top
   level, but implementation workflow doesn't propagate the
   `repo_client` through the executor → fanout → implementation chain)

So Gap B is ADR-0024 step 4 continuation + ADR-0024 step 5
continuation, with the same shape as the original ADR-0024 work.
Reference that ADR's STATUS block additions for the exact pattern.

### Implementation sketch

Files (audited via grep at session time, will re-verify before
implementing):
- Modify: `src/requiem/workflows/implementation.py`
  - Replace `_require_gh(ctx) → GhClient | PermanentFailure`
    with `_require_repo_platform(ctx) → RepoPlatform | PermanentFailure`
  - Replace all `gh.pr_create(...)` / `gh.ensure_branch_ref(...)`
    / `gh.pr_search(...)` call sites with `repo_client.X(...)`
  - Update except-clauses to use the `_REPO_*_ERRORS` tuples
    already established in ADR-0024 step 4
- Modify: `src/requiem/workflows/fanout.py` if it injects
  `Toolbelt(gh=...)` into per-leaf engines — must inject
  `Toolbelt(repo=...)` for ADO paths
- Modify: `src/requiem/end_to_end.py` — the `_topology_toolbelt`
  helper from ADR-0024 step 5 should also be used by the
  executor/fanout dispatch path so `toolbelt.repo` propagates
- Tests: `tests/test_implementation_workflow.py` — add cases
  exercising the workflow with `Toolbelt(repo=FakeAdoClient())`,
  same shape as `tests/test_trunk_topology_against_ado.py`

### Tasks (TDD pattern, bite-sized — written in detail in a
follow-up plan because this is ~2-4 hours of work and deserves its
own task breakdown the next time we sit down)

Stub:
- **B1**: Audit `gh.` call sites in implementation.py + executor
- **B2**: Write `tests/test_implementation_workflow_against_ado.py`
  with full RED→GREEN cycles for each call site
- **B3**: Refit `implementation.py` (one call site per commit)
- **B4**: Refit `fanout.py` toolbelt wiring
- **B5**: Refit `end_to_end.py` executor → fanout → implementation
  threading of `repo_client`
- **B6**: Broad sweep across `tests/test_implementation_*`,
  `tests/test_kanban_executor*`, `tests/test_end_to_end*`
- **B7**: Live dry-run against `microsoft/CloudVault/cloudvault-service-api`
  (kanban backend stub; fanout backend would crash on Gap C)

### Exit criteria

- [ ] `implementation.py` works against `Toolbelt(repo=FakeAdoClient())`
  and `Toolbelt(gh=FakeGhClient())` identically
- [ ] No remaining `_require_gh` / `toolbelt.gh` reference in
  `implementation.py`
- [ ] Live dry-run of `requiem-end-to-end --ado-repo ... --commit`
  reaches the executor stage WITHOUT crashing in implementation
  (it may stall at Gap C, but no GitHub-shaped errors)
- [ ] ADR-0024's STATUS block adds a "step 6" entry pointing to
  this gap's landing commit

### STATUS

**STATUS: SHIPPED 2026-06-22.**

- **Implementation refit landed 2026-06-17** (commit `7ee429a`,
  `src/requiem/workflows/implementation.py`). Per-leaf workflow now
  uses `_require_repo_platform(ctx)` (preferring `toolbelt.repo` and
  falling back to `toolbelt.gh`); `create_pr` calls
  `find_open_pr_for_branch` (Protocol-surface, structured) instead of
  `pr_search` (GitHub-only free-form text); except-clauses cover both
  `GhClientError` and `AdoClientError`.
- **Executor toolbelt propagation landed 2026-06-22**
  (`src/requiem/end_to_end.py:660-678`). `exec_toolbelt` now threads
  `repo=repo_client` through to the executor stage so future
  kanban-backend workers and the in-process fanout path (running
  per-leaf `implementation` engines) both see the right `RepoPlatform`
  impl. Mirrors the `_topology_toolbelt` pattern from ADR-0024 step 5.
  Without this, the executor's `Toolbelt.real()` fell back to a
  GitHub `repo` regardless of `--ado-repo`.
- **Load-bearing tests landed 2026-06-22**
  (`tests/test_implementation_workflow_against_ado.py`, 6 tests; new
  `tests/test_end_to_end_ado.py
  ::test_ado_repo_threads_repo_client_to_executor_toolbelt`). Each is
  the proof per the protocol-extraction-refactor skill §"don't skip
  step 4 load-bearing test": wires
  `Toolbelt(repo=FakeAdoClient(), gh=None, ...)` and runs the
  per-leaf workflow end-to-end against ADO with `toolbelt.gh=None`.
  Without these tests, the refactor would "compile" but a `--commit`
  run might silently route through the legacy GitHub path.
- **Cleanup landed 2026-06-22**: legacy `_require_gh` helper deleted
  from `implementation.py` now that the load-bearing test confirms no
  surviving callers. `GhClient` import retained (still referenced by
  the CLI live path that constructs a real `GhClient`).
- **39 tests across the surface green** (`test_implementation_workflow.py`
  33 + new 6); **11 tests in `test_end_to_end_ado.py` green** including
  the new exec-toolbelt propagation pin.

After Gap B's full landing, the only remaining piece for a first real
`--commit` end-to-end against CVAPI is **Gap C** (stand up a worker
backend — recommendation `Path C1`, in-process fanout, is the path).
The per-leaf code path is no longer the blocker.

---

## Gap C: stand up a worker backend for the dogfood

### Scope

For #62759077 specifically (the SKU-fallback feature), we need a
worker capable of executing each leaf Task — i.e. reading the
task's description, opening the relevant CloudVault files, writing
the code, running tests, committing. Two paths:

#### Path C1: in-process fanout backend

Uses `requiem.workflows.fanout` (ADR-0021). Each leaf becomes an
in-process `implementation` workflow invocation that itself drives
the CopilotProvider to write code. Requires Gap B fixed (Gap B is
literally what makes this work against ADO). Pros: no extra
infrastructure, runs on the dev's laptop, easy to debug. Cons:
serial by default (or N parallel via worktree isolation per
ADR-0022); the LLM is doing all the work; quality depends on
CopilotProvider's effectiveness at code generation, not just
planning.

#### Path C2: Hermes kanban worker fleet

A long-running Hermes process (or several, one per profile)
subscribes to the kanban board `requiem-62759077-commit`. Each
worker picks up leaf cards as they're seeded, does the work, and
PRs back. Pros: matches the production architecture; lets
specialist worker profiles (e.g. `requiem-implementer` skill
package) be tailored; supports human-in-the-loop where useful.
Cons: requires standing up at least one worker profile that has
write access to CVAPI's ADO + can spawn coding agents; significant
setup overhead.

### Recommendation

**Path C1 for THIS dogfood.** It's the fastest path to "feature
delivered via requiem" because it doesn't require us to design and
stand up a worker fleet. The lesson learned will inform Path C2
when we're ready to invest in the production architecture.

**Tasks** (to be planned in detail after Gap B lands, since the
worker shape depends on what the implementation workflow ends up
looking like with a `RepoPlatform`).

### Exit criteria

- [ ] One leaf task from #62759077 successfully implemented by the
  worker (code committed to a branch, tests pass)
- [ ] Worker reads the leaf Task's title + description from ADO and
  uses both to scope the work
- [ ] Worker opens a PR via the leaf_pr workflow (already in place)
- [ ] Human can review + merge the PR

### STATUS

**STATUS: not started**

---

## Decisions to make (BEFORE Gap A starts)

### 1. Disposition of `cloudvault-service-api/.requiem-config/process.yaml`

The file was written during the 2026-06-17 session to provide
CVAPI's actual type hierarchy to requiem's planning. It's not
committed yet. Three options:

- **A** — commit to CVAPI's main branch. Cost: a dotfile appears
  in the repo for every contributor; security reviewers ask "what
  is this." Benefit: works for everyone, no per-machine setup.
- **B** — local-only via `.git/info/exclude` + build a
  `--process-config <path>` CLI flag for `requiem-end-to-end`.
  Cost: 30-45 min of CLI work. Benefit: doesn't pollute CVAPI's
  repo; per-operator config.
- **C** — keep the file local-only but DON'T build a CLI flag yet
  — just leave it in CVAPI worktree as untracked. Cost: zero;
  works today. Risk: if another machine runs requiem against CVAPI,
  it won't have the config and will fall back to defaults (which is
  what got us into trouble in the first place).

**Recommendation: B.** Build the CLI flag as part of Gap A's
landing (it's small enough to fit), commit the example process.yaml
to `docs/references/cvapi-process-config.example.yaml` for
discoverability, and add to CVAPI worktree's `.git/info/exclude`.

**STATUS: shipped 2026-06-17** (commit: this one). Path B taken:
- `--process-config <path>` CLI flag wired into both
  `requiem-end-to-end` and `requiem-integrate` (see
  `_build_arg_parser` / `_build_integrate_arg_parser`).
- Central `_resolve_process_config(explicit_path, repo_path)` helper
  in `end_to_end.py` — explicit path beats discovery, missing/malformed
  raises `ProcessConfigError` loudly (no silent default fallback).
- 8 tests in `tests/test_process_config_cli.py` pinning precedence
  + argparse plumbing + error surfacing.
- CVAPI worktree's `.requiem-config/` removed; the file lives at
  `~/.config/requiem/cvapi-process.yaml` (per-operator) and the bare
  repo's `.git/info/exclude` ignores `.requiem-config/` so future
  accidental commits are blocked.
- Example doc at `docs/references/cvapi-process-config.example.md`
  documents the shape + usage for other operators.

### 2. Reviewer charter improvement

Separate ADR. The reviewer's escalation on `Task` leaves was
arguably the right call from the reviewer's perspective ("this
plan is too vague to implement") — it just doesn't matter because
Gap A removes the reviewer from the leaf path entirely. But for
decomposable-type plans, the reviewer DID produce useful "this
child title is too vague to decompose" feedback. That's worth
keeping. The reviewer charter improvement work is **defer to a
later ADR** once Gap A has removed the leaf-level noise so we can
see what the reviewer's signal looks like at the decomposable layer.

### 3. Escalation routing policy

Same logic: defer. The current `escalation_gate` auto-abort is the
right safety default once we've removed the leaf-level noise.
"Restart-parent-with-feedback" and "mark-as-needs-human" remain
attractive but premature.

---

## Open questions

- **Will Gap A's short-circuit also affect the recursion behavior?**
  When the recursion `child_inputs` carries a child_proposal of
  type=Task, the child engine's `branch_decomposable` should now
  also short-circuit. Confirm in testing.
- **Does ADR-0024 step 5's `_topology_toolbelt` helper already pass
  `repo_client` to the executor stage?** Probably not — the
  executor seam was scoped as a step-6 follow-up. Audit during Gap B.
- **What does the worker look like for Path C1?** The
  `implementation.py` workflow as it exists today has an
  `invoke_coder` verb that calls a coder agent — is that already
  CopilotProvider-driven? Audit during Gap C scoping.
- **Should `ITER_CAP` be configurable per-run?** Bumped 3→5 in
  2b1979e (2026-06-17) as a temporary lever after the SKU-fallback
  dogfood retry showed complex Scenarios genuinely need more
  iterations to converge. The right shape is an `iter_cap` param on
  `build_engine` + a `--max-plan-iterations` CLI flag so operators
  can dial up complex Scenarios without paying the cost on every
  plan. ITER_CAP is referenced in 8 places across `planning.py`;
  threading a parameter through is its own refactor. Follow-up after
  Gap B/C land.
- **Reviewer charter improvements.** The dogfood run 5 reviewer
  produced genuinely substantive feedback — dependency analysis,
  scope overlap detection, cross-cutting concerns. Still escalated
  at iter 5 because the planner couldn't fully converge. The
  reviewer's escalation feedback ("Task 3 and Task 4 still have
  unresolved overlap on rollout controls") is the kind of thing a
  human reviewer would either (a) accept and tell implementation to
  resolve, or (b) explicitly re-scope. Neither path is currently
  expressible from the reviewer's structured output. Worth a
  separate ADR after we have more data on which feedback shapes
  actually block convergence vs which represent acceptable scope.

## STATUS log

- **2026-06-17 PROPOSED.** Plan written; no code committed against
  this ADR yet. Three gaps identified; Gap A is the immediate next
  session's work.
- **2026-06-17 §1 SHIPPED** (8fea529). `--process-config <path>`
  CLI flag + `_resolve_process_config` helper + 8 tests. Operator
  state: `.requiem-config/` ignored at CVAPI bare repo level,
  config lives at `~/.config/requiem/cvapi-process.yaml`.
- **2026-06-17 Gap A SHIPPED** (785a4b2). `policy_classifier` +
  `record_leaf_from_policy` short-circuit; 4 new tests; 168 passed
  in the broad sweep.
- **2026-06-17 Gap A* SHIPPED** (6d8bbb5). Post-Gap-A finding from
  dogfood retry: reviewer prompt rendered `children: N proposed`
  (just the count) which forced reviewers to escalate as
  "cannot evaluate". Fix renders full child list (title + type +
  description, capped at 400 chars) so reviewer can do real
  evaluation. 1 new test + extended `FakeProvider.calls` to record
  `user_message` for end-to-end prompt content assertions.
- **2026-06-17 ITER_CAP bump SHIPPED** (2b1979e). 3→5 iterations
  after the reviewer-prompt fix unlocked substantive per-iteration
  feedback that the planner needed more rounds to fully address.
  Temporary lever; configurable per-run is a follow-up (see open
  questions).
- **2026-06-17 Gap B core SHIPPED** (7ee429a). Per-leaf
  `implementation` workflow takes `RepoPlatform` Protocol —
  `_require_repo_platform` helper, `create_pr` refit to
  `find_open_pr_for_branch`, except-clauses cover `AdoClientError`.
- **2026-06-22 Gap B closure SHIPPED.** Executor toolbelt now
  propagates `repo=repo_client` (`end_to_end.py:660-678`); load-bearing
  ADO tests landed (`tests/test_implementation_workflow_against_ado.py`,
  6 tests; `tests/test_end_to_end_ado.py
  ::test_ado_repo_threads_repo_client_to_executor_toolbelt`); legacy
  `_require_gh` helper deleted. Gap B is now fully closed — the per-leaf
  code path is no longer the blocker for a first `--commit` end-to-end
  against an ADO repo. The remaining v0 blocker is Gap C (stand up a
  worker backend; recommendation Path C1).
