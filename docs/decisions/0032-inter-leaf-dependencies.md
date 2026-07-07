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
`_resolve_deps` helper that validates self-reference, out-of-range, and
(only on the normal/leaf branch) that the target is itself a leaf, not a
decomposable subtree — fail-closed on a malformed declaration rather than
silently dropping it. **v1 scope is same-parent-sibling deps only** —
cross-subtree dependencies are not resolvable from a single parent's
`proposals[]` index and are explicitly out of scope.

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

**Negative / open:** v1 dependency scope is same-parent-sibling only — a
dependency across subtrees (different parents) is not resolvable from the
committed plan tree's `proposals[]` indexing and needs a follow-up if a real
plan needs it. A merge-hook crash for one leaf is caught and reported as a
synthetic `failed:<exception>` merge_state for that leaf (so the rest of the
wave still proceeds and its dependents are correctly blocked) rather than
aborting the whole fan-out. `_DemoGhClient` (the fanout module's demo/test
double) was missing `find_open_pr_for_branch` — a pre-existing gap unrelated
to dependencies that only surfaced once a fanout-level test drove
`create_pr` for real; fixed alongside as a one-line parity addition.

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

`leaf_lifecycle.push_addressal` (the post-review-fix push path) is
deliberately left unchanged: it pushes revision commits with no fresh
`run_tests` re-run before them, so posting a status there would be the
optimistic-merge shortcut the gate exists to prevent. Revision commits
correctly stay at `checks_state="unknown"` and route to
`needs_human.tests_status_unknown` until re-verified — a pre-existing,
intentionally conservative gap, not a regression from this fix.

