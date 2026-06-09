# ADR 0022 — Per-item worktree isolation + parallel fan-out (parity #5)

**Status:** Accepted + **implemented** (2026-06-09). Worktree primitive on
`fs.py` + parallel mode in `fanout.py` shipped; verified on this Windows host.
**Date:** 2026-06-09
**Relates to:** ADR-0021 (in-process fan-out — sequential v0, this closes its
deferred half), ADR-0020 (child-seam propagation), ADR-0006 (branch topology),
ADR-0005 (sub-workflow log isolation), INV-RESTART, INV-SINGLE-PROCESS.
**Parity:** non-negotiable **#5** (per-item worktree isolation for *parallel*
dispatch). Builds directly on `fanout.py` (#4).

## Context

`fanout` (ADR-0021) dispatches implementable leaves into `implementation`
**in-process but sequentially** — each leaf runs to completion before the next
starts. Parity #5 wants **parallel** dispatch, which needs **per-item git
worktree isolation**: two `implementation` children running concurrently each do
`git checkout -b`, write files, and commit. If they share one working tree they
corrupt each other (one's checkout/index clobbers the other's). A git *worktree*
gives each leaf its own working directory + HEAD + index over the same object
store, so they can run truly in parallel.

### Two things verified before designing

1. **The ADR-0020 contextvar seam survives parallel `asyncio.gather`.** A
   contextvar set in the parent *before* `gather` propagates into every gathered
   child task, and a child that sets a contextvar does **not** leak to siblings
   or back to the parent (asyncio copies the context per task). So the provider
   seam `fanout.build_engine` installs is safe under parallel dispatch — no
   cross-leaf contamination. (ADR-0021 flagged this as an open worry; it is now
   resolved in favour of "safe".)

2. **`git worktree add` works** and yields independent HEAD/branch/index per
   worktree over the shared repo. **Gotcha:** a worktree's `.git` is a *file*
   (a gitdir pointer), not a directory — so `FilesystemClient._is_git_tree()`,
   which today checks for a `.git` **directory**, must accept a `.git` file too,
   or every git op in a worktree-bound child raises `FsNotAGitRepoError`.

## Decision (proposed)

### 1. A worktree primitive on `FilesystemClient`

Add `git_worktree_add(path, *, branch, from_ref)` and
`git_worktree_remove(path, *, force=False)` to `fs.py`, plus fix
`_is_git_tree()` to recognise a `.git` *file* (worktree) as well as a `.git`
*directory* (main checkout). The primitive is created on the **main** repo's
`FilesystemClient`; each leaf then gets its own `FilesystemClient` bound to the
new worktree path.

### 2. Parallel mode in `fanout`

Add `FanoutInputs.parallel: bool = False` (+ optional `max_parallel`). When set,
`dispatch_leaves`:

1. For each leaf, `git worktree add <repo>/.requiem-worktrees/<root>-<leaf>` off
   the base branch (idempotent: reuse an existing worktree dir on re-entry).
2. Build each child's `implementation` engine with `repo_path` = the leaf's
   worktree and an `fs` bound there. **The branch already exists** because the
   worktree was created with `-b impl/<root>-<leaf>` — so `implementation`'s
   `create_branch` must see the branch as already-current (its existing
   idempotent "exists and is current" path handles this).
3. `await asyncio.gather(*child_runs)` (optionally bounded by a semaphore of
   `max_parallel`).
4. Roll up exactly as the sequential path does (landed / needs_human / failed).
5. Best-effort `git worktree remove` per leaf after roll-up (leave on failure
   for inspection — mirrors `implementation`'s "branch left on disk" handoff
   contract).

Sequential mode stays the default and is unchanged.

### 3. Branch creation under a pre-made worktree

The cleanest seam: the worktree is created with `-b impl/<root>-<leaf>` so the
branch and worktree are born together. `implementation.create_branch` then finds
the branch already checked out in its bound worktree and takes its idempotent
"already current" success path — no double-create, no foreign-branch handoff.
(Alternative considered: create the worktree on a detached base and let
`create_branch` make the branch. Rejected — it duplicates branch-creation logic
across two places and risks `create_branch` not finding the branch current.)

### 4. Resume / INV-RESTART

Per-leaf child logs are unchanged (`fanout-<root>__leaf-<id>.events.jsonl`), so
idempotent re-entry still skips already-terminal leaves. A re-run reuses an
existing worktree dir rather than re-adding it (a second `git worktree add` on the
same path errors). Worktree creation is therefore guarded by a dir-exists check.

### 5. Explicitly deferred

- **Bounded global parallelism across multiple roots** — `max_parallel` bounds
  one fan-out; a fleet-wide slot budget is out of scope.
- **Worktree GC of abandoned leaves** beyond best-effort remove — a `requiem
  worktree prune` housekeeping verb is a follow-up.

**Update (2026-06-09): worktree GC landed.** `fs.py` gained
`git_worktree_list()` (porcelain enumeration, surfaces `prunable`) and
`git_worktree_prune()` (clears stale admin entries from a crashed run). `fanout`'s
parallel `dispatch_leaves` now prunes once before dispatch, so a reused
`.requiem-wt-*` path from a prior crash doesn't collide on `git worktree add`.
(`tests/clients/test_fs.py` list + prune-after-crash cases.) A standalone
`requiem worktree prune` CLI verb is still a possible follow-up; the in-fan-out
GC covers the operational gap.

## Consequences

**Positive:** closes #5 — true parallel dispatch with real isolation, built on the
proven `fanout` roll-up + the seam that survives `gather`; reuses git's own
worktree mechanism (no bespoke isolation); the `_is_git_tree` fix also makes
*any* worktree-bound `FilesystemClient` work, not just fan-out.

**Negative / open:** parallel runs interleave child log *writes* across files
(fine — each child owns its file) but the orchestrator's own single log records
dispatch as one `dispatch_leaves` step regardless of N (acceptable — per-leaf
detail lives in the child logs); worktrees consume disk (mitigated by
best-effort removal); a crash mid-parallel-run can leave orphan worktree dirs (a
re-run reuses them; a prune verb is the follow-up). Windows worktree paths +
the `.git`-file handling are covered by the `_is_git_tree` fix and exercised in
tests on this Windows host.

**Why an ADR:** the choice to create the branch *with* the worktree (vs. letting
`create_branch` make it), the `gather`-safety determination, and the
`_is_git_tree` semantics change are load-bearing. Design-doc-first, matching the
ADR-0020/0021 rhythm.
