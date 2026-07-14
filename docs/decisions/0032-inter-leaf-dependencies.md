# ADR 0032 — Inter-leaf dependency dispatch + straggler-tolerant self-merge

**Status:** Accepted + **implemented** (2026-06-27).
**Date:** 2026-06-27
**Relates to:** ADR-0014 (Hermes fan-out executor — the kanban backend), ADR-0018
(trunk integration contract), ADR-0020 (child-seam propagation), ADR-0021
(in-process fan-out orchestrator), ADR-0022 (worktree isolation / parallel
fan-out). Postmortem: dogfood run #36 (AB#62759077, CVAPI SKU fallback).

## Context

Run #36 dispatched 23 leaves against a plan whose text implied a build order
(a shared SKU schema + probe/ranking "producer" leaf, several "consumer"
leaves building on top of it). Both fan-out backends dispatch a flat list of
leaves with **no notion of order** — every leaf branches from the same base
ref and runs concurrently or in an arbitrary sequential order. The result: 19
leaves landed, but 3 of the 4 `needs_human` leaves were *not* bugs — the
coder correctly refused to build against a producer sibling's code that
hadn't landed in the worktree yet (dispatch treats all leaves as
independent). The 4th `needs_human` was an unrelated, legitimate
`apply_changes` contract failure (a `FileChange.content=None` "no-op modify"
signal misusing the delete-only field), not something this ADR addresses.

A second, independent bug compounded the damage: `end_to_end.py`'s
self-merge loop was gated on "zero leaves need a human/failed **anywhere in
the fan-out**" before it would merge *any* leaf, and aborted on the first bad
merge even when it did run. Four unrelated stragglers left all 19 good,
mergeable PRs sitting open.

Investigating "is there an existing mechanism for this" turned up a third
fact: `kanban_executor.py` (ADR-0014, the Hermes backend) **already had**
complete, tested dependency-graph machinery — `LeafSpec.deps`, dependency
validation, wave-release logic in `poll_kanban` — but it was structurally
inert for any real committed plan, because nothing upstream
(`plan_tree.py`) ever populated real `deps` edges. Meanwhile `fanout.py` (the
**in-process** backend that live dogfood runs actually use) had zero
dependency awareness at all: a flat `asyncio.gather` over every leaf.

## Decision

### 1. A shared dependency-graph seam: `leaf_deps.py`

Both backends dispatch a flat list of leaves that MAY declare `deps` on
sibling leaf ids; both need identical validation (self-dep, unknown-dep,
cycle detection) and identical wave-release semantics (a leaf is releasable
once every dependency has *delivered*; a leaf is *blocked*, not silently
retried forever, once any dependency settles non-delivered). Extracted
`kanban_executor`'s inline implementation into
`requiem/workflows/leaf_deps.py`: three pure functions —
`validate_dep_graph`, `compute_blocked`, `releasable_leaves` — over plain
`dict[str, tuple[str, ...]]` / `set[str]`. Both backends call the *same*
functions now; a fix or a bug lands in exactly one place. 13 unit tests own
the graph logic directly (no board, no git repo, no LLM in the loop).

### 2. Planner-authored `depends_on`, threaded to real ids

`planning.ChildPlan` gains `depends_on: list[int] | None = None` — free-form,
unvalidated at the planner layer, mirroring the existing `review_group`
precedent. The planner/reviewer prompts gained guidance + rendering (0-based
"slot N" + declared deps) so the reviewer can sanity-check declared order.
`plan_tree.ResolvedLeaf` gains `deps: tuple[int, ...]`, resolved by a new
dependency flattener that validates self-reference and out-of-range slots.
A dependency may target either a leaf sibling or a decomposable sibling.
For subtrees, flattening connects every prerequisite **exit leaf** to every
dependent **entry leaf**. Internal edges then carry the ordering transitively,
so unrelated leaves remain parallel and redundant all-to-all edges are avoided.
Declarations remain same-parent sibling references: each `depends_on` index is
interpreted only within its own node's `proposals[]` list.

### 3. Wave-gated dispatch + interleaved merge in `fanout.py`

The critical remaining gap: `fanout.py`'s `_dispatch_leaves` now branches
between two paths:

- **Flat (unchanged)** — the exact pre-existing `asyncio.gather` / sequential
  loop, taken whenever `FanoutInputs.leaf_merge` is `None` **or** no leaf in
  the batch declares a non-empty `deps` tuple. This is a deliberate
  safe-by-default choice: since no dogfood run before this feature ever had
  `depends_on` populated, every historical/current plan gets **zero**
  behavior change until a plan actually declares a dependency.
- **Wave-gated (`_dispatch_waves`)** — active only when both conditions
  above are met. Uses the same `leaf_deps` primitives as `kanban_executor`:
  release a wave of leaves whose dependencies have all *merged* (not merely
  landed), dispatch the wave, then call `FanoutInputs.leaf_merge(real_id,
  pr_number)` for each landed leaf before computing the next wave. A leaf
  whose dependency never merges is reported with a new **`blocked`**
  disposition — never dispatched at all, not silently dropped from the
  roll-up (it buckets into `leaves_failed` for free, since the existing
  rollup already treats "not completed, not needs_human" as failed).

**Why merge, not just land, gates release:** a dependent leaf's coder can
only see a producer sibling's code once the producer's PR is actually
**merged** into the trunk — each leaf branches fresh `from_ref=base_branch`.
Landing (opening a PR) is not enough. This is why wave-gating is coupled to
a `leaf_merge` hook rather than being purely structural.

`end_to_end._dispatch_in_process` builds the `leaf_merge` hook (under the
same `live and github_repo is not None and trunk_branch is not None`
condition its own tail self-merge loop already required): the hook drives
`leaf_lifecycle` exactly as the tail loop does, then re-syncs the local
trunk ref via the existing `trunk_sync` hook (ADR-0018 — trunk branches are
platform-API-only, so the persistent local worktree never learns about a
merge through ordinary git operations otherwise) so the *next* wave's
`create_branch` sees the merge. The tail loop, in turn, adopts
`outcome.merge_state` directly for any leaf the hook already attempted,
instead of re-running `leaf_lifecycle` — avoiding a double-merge attempt on
the same PR.

### 4. Self-merge gating fix (independent of dependencies)

`end_to_end._dispatch_in_process`'s self-merge tail no longer skips the
entire loop when *any* leaf needs a human/fails, and no longer aborts on the
first bad merge. Every landed leaf gets its own `leaf_lifecycle` attempt
regardless of siblings; the aggregate verdict reflects the worst outcome
(failed > needs_human > merged) with per-leaf notes in the detail message.
This closes the run #36 gap on its own, independent of whether any plan ever
declares a dependency.

## Consequences

**Positive:** a plan whose text implies a build order can now express it and
have it enforced; a straggler (whether a legitimate producer/consumer
dependency or an unrelated bug) no longer blocks every other leaf's
self-merge; the fix is symmetric across both fan-out backends via the
shared `leaf_deps` seam; the wave-gated path is opt-in and provably a no-op
for any plan/caller that doesn't declare dependencies or wire a merge hook
(see `test_wave_gating_is_a_noop_without_declared_deps` /
`test_wave_gating_is_a_noop_without_a_hook` in `tests/test_fanout_workflow.py`).

**Negative / open:** dependency declarations are still local to one sibling
list; the planner cannot directly name an arbitrary cousin leaf in another
subtree. A merge-hook crash for one leaf is caught and reported as a synthetic
`failed:<exception>` merge_state for that leaf (so the rest of the wave still
proceeds and its dependents are correctly blocked) rather than aborting the
whole fan-out. `_DemoGhClient` (the fanout module's demo/test double) was
missing `find_open_pr_for_branch` — a pre-existing gap unrelated to
dependencies that only surfaced once a fanout-level test drove `create_pr` for
real; fixed alongside as a one-line parity addition.

**Why an ADR:** three separate, previously-undocumented facts converge here
— an existing-but-inert dependency-graph implementation in one backend, a
completely absent one in the other, and a doubly-gated self-merge loop that
made the absence far more damaging than it needed to be. The
safe-by-default activation condition (hook wired AND a declared dependency)
is load-bearing for why this change carries near-zero regression risk
despite touching the live dispatch path; worth recording explicitly.

## Addendum (2026-06-28): a fourth, deeper root cause — self-merge had *never* worked

Run #37 validated the wave-gating fix above end-to-end (10/22 leaves
correctly landed, 11 correctly reported `blocked` on their unmet
dependencies) but surfaced a **separate, structural** reason self-merge
still landed 0/10: `leaf_lifecycle.check_tests_passed` requires
`pr_mergeability(...).checks_state == "success"`, and that signal is read
from ADO's `commits/{sha}/statuses` feed (`_ado_commit_status_signal` in
`azuredevops.py`). The ephemeral `feature/<root>` trunk has **no
build-validation branch policy attached** — nothing ADO-side ever posts a
commit status there — so `checks_state` was permanently `"unknown"`, never
transient. Every leaf's merge attempt hit `needs_human.tests_status_unknown`
on the first try, with no retry loop (this node has none, unlike its
siblings). This is very likely why self-merge has never once succeeded
across runs #34–#37, independent of (and compounding) the wave-gating bug
above.

`check_tests_passed`'s docstring is explicit that this is a **deliberate
fail-closed gate** ("must be explicitly green before review… never an
optimistic merge") — so the fix is to give it real evidence, not to relax
it. `implementation.py`'s `push_branch` verb now calls a new best-effort
`post_commit_status(repo, sha, *, context, state, description)` method
(implemented on both `AdoClient` and `GhClient`, deliberately **not** added
to the `RepoPlatform`/`MergeCapableRepoPlatform` Protocols — see the note in
`clients/repo.py` — since it's a narrow, optional capability most call
sites never need) right after a successful push, posting
`context="requiem/local-tests", state="success"` for the commit that
`run_tests` has *just* verified locally (the only path into `push_branch` is
`apply_changes -> run_tests -> commit_changes -> push_branch`, so this is
honest evidence, not a shortcut). `check_tests_passed` itself is unchanged
— it still requires a genuine `"success"` signal, it just now has one to
find.

`leaf_lifecycle.push_addressal` (the post-review-fix push path) was
deliberately left unchanged at the time: it pushed revision commits with no
fresh `run_tests` re-run before them, so posting a status there would have been
the optimistic-merge shortcut the gate exists to prevent. Revision commits
therefore stayed at `checks_state="unknown"` and routed to
`needs_human.tests_status_unknown` until re-verified.

## Addendum (2026-07-13): close the post-review verification gap

Live runs 51 and 53 confirmed that the conservative gap above was permanent,
not transient: reviewer fixes created a new leaf SHA, but no actor reran the
configured tests or published required-status evidence for that SHA. The
workflow now runs the same configured or auto-detected test command after
applying review fixes and before committing or pushing them. A passing result
is published as `requiem/local-tests` on the exact pushed SHA; a failed,
undetected, or crashed test run stops before push.

`tests_status_unknown` also has a bounded evidence-first recovery branch.
Requiem rereads the authoritative commit-status feed only when ADO reports an
actual pending status or when Requiem has just published the required status
for that exact SHA. A terminal success continues, a failure remains fail-closed,
and three unresolved reads produce a resumable escalation brief. An unknown
status with neither pending evidence nor an exact-SHA publication is treated as
genuinely missing validation and escalates immediately.

The required proof is specifically the latest
`requiem/local-tests` status in the `requiem` genre; unrelated successful commit
statuses cannot satisfy the gate. Mergeability carries the exact inspected head
SHA through to completion, and both ADO and GitHub enforce that SHA as the
platform merge compare-and-swap. Review-fix tests also persist the staged Git
tree identity, so a changed worktree cannot be committed or marked successful
after a crash/resume boundary without another test run.

## Addendum (2026-07-14): preserve dependencies across recursive flattening

Runs 51, 56b, and 57 showed that same-parent dependency declarations at the
Deliverable level vanished when both siblings recursively decomposed. The
resolver either rejected a leaf depending on a decomposable sibling or silently
ignored `depends_on` on a decomposable dependent, so descendant leaves reached
review before prerequisite contracts existed.

Committed-plan flattening now composes sibling subgraphs at their boundaries:
each exit leaf of the prerequisite subtree gates each entry leaf of the
dependent subtree. This is the minimal edge set that preserves the compound
dependency. It keeps internal parallelism, leaves unrelated subtrees untouched,
and survives the plan-artifact, commit real-id mapping, fanout event-log, and
resume boundaries.
