# ADR 0018 — Trunk integration on the live (Hermes) path

**Status:** Proposed (needs Daniel's call on Option, below)
**Date:** 2026-06-05
**Relates to:** ADR-0006 (merge-group topology / Option D), ADR-0007 (PR
lifecycle), ADR-0014 (Hermes fan-out executor), ADR-0017 (Hermes delivery
fleet)
**Supersedes:** the *placement* recommendation in ADR-0007 §5.1 (not its
spirit) — see §4.

## Context

ADR-0006 Option D (ACCEPTED) prescribes a per-run integration trunk:

```
main
 └── feature/<root>            integration trunk (one per run)
      ├── plan/<root>          plan PR  → trunk
      ├── impl/<root>-<item>   one per leaf → trunk
      └── evidence/<root>-<item>
```

`branch_model.py` (landed 2026-06-05, commit `c863d21`) makes the **names**
authoritative. `plan_pr.py` (ADR-0012) opens `plan/<root>`. The fan-out
executor (`kanban_executor.py`, ADR-0014) emits each implementable leaf as a
Hermes kanban task whose worktree branch is `impl/<root>-<item>`.

The remaining #7 gap is **integration**: who creates `feature/<root>`, who
makes the leaf branches target it, and who opens the trunk → `main` PR. While
scoping that build (with a rubber-duck pass), two facts surfaced that change
the shape of the answer.

### Fact 1 — the live path is the external executor, not `implementation.py`

ADR-0007 §5.1 (DRAFT, not ratified) argued feature_pr should **not** be a
separate workflow: the drift-integration verb "belongs on
`implementation.py`'s exit." That reasoning is explicitly grounded in *"we
never have a feature branch that drifts because we open the PR right after
`commit_changes` **in implementation.py**."* But ADR-0014 moved live delivery
off the in-process path: `implementation.py`/`full_sdlc.py` are blocked by
ADR-0013 blocker B1 (nested in-process children get no real
provider/toolbelt). On the live path **`implementation.py` does not run** —
leaves are delivered externally by Hermes workers. So §5.1's placement target
no longer exists on the live path.

### Fact 2 — Hermes `kanban create` has no base-branch / PR-target contract

Verified against the binary (`hermes kanban create --help`, v0.15.1):

```
--branch BRANCH   Branch name for worktree tasks, e.g. wt/t6-wire
```

There is `--branch` (the worktree branch the worker commits to) but **no
`--base`, `--target`, or PR-target flag.** The worktree is cut from the
board repo's HEAD, and whether/where the worker opens a PR is a property of
its skill/profile, not of the task spec. Concretely, `kanban_executor`'s
`dispatch_leaves` passes `branch=impl/<root>-<item>`, `workspace="worktree"`
and nothing else (kanban_executor.py:295-303). And `kanban_executor`'s
`aggregate` verb (kanban_executor.py:517-553) only produces a NeedsHuman
*verdict* ("All N leaf task(s) delivered. Approve the batch?") — **no trunk is
created and no trunk→main PR is opened anywhere today.**

**Consequence:** Requiem cannot, through the current Hermes kanban contract,
instruct a worker to branch *from* `feature/<root>` or target its PR *at*
`feature/<root>`. Establishing the Option-D topology on the live path is
therefore **not** a pure requiem-code change — it bottoms out at an
external-system contract.

A third unknown rides along: **who opens the leaf PR** (`head=impl/<root>-<item>`)?
The Hermes kanban-worker skill may or may not open one, and if it does, it
targets the worker's notion of the default branch. This is undefined and is
part of what an Option below must pin down.

## The non-negotiable invariants (from the rubber-duck pass)

1. **Trunk-before-fan-out.** `feature/<root>` must exist *before* leaves are
   dispatched. Creating it only in a final feature_pr step is too late — the
   leaves will already have branched from / PR'd against the wrong base.
2. **Leaf-integration means a merged PR, not a delivered task.** A leaf is
   trunk-integrated only when a PR exists with `head==impl/<root>-<item>`,
   `base==feature/<root>`, `merged==true`. "Hermes task delivered" ≠ "merged
   into trunk."
3. **No self-merge.** Whatever opens the trunk→main PR must **not** merge leaf
   branches or the trunk itself — merge stays owned by `pr_lifecycle` / the
   human, consistent with "GhClient has no `pr_merge`" (plan_pr.py:21-23) and
   INV-NO-DIRECT-TRUNK-COMMITS (ADR-0006).
4. **Expected-leaf set is authoritative.** The integration gate must read the
   committed plan / executor leaf set, not discover leaves by GitHub search
   alone (avoids adopting a partial/stale set).
5. **feature_pr is a readiness + final-PR opener, NOT a polyphony aggregator.**
   ADR-0007's deeper warning still stands: do not recreate the heavy
   `feature-pr.yaml` machinery.

## Options for the base-branch / PR-target contract

### A. Hermes gains a `--base` / `--pr-target` capability
Cleanest topology (the worker branches from and PRs against the trunk
natively). **Blocked:** not in v0.15.1; requires a Hermes feature + version
bump. Not actionable from this repo today.

### B. Soft contract via task body + dedicated worker skill
Requiem pre-creates `feature/<root>`, and encodes "branch from / PR against
`feature/<root>`" in the task body plus a requiem-authored kanban-worker
skill. **Unenforced** — a worker can ignore it; verification still needs
invariant 2's PR check. Fragile as the *sole* mechanism.

### C. Requiem owns topology end-to-end (RECOMMENDED)
Requiem stays the integration authority (consistent with ADR-0017's "Requiem
is the decomposition authority + system of record" and
INV-EVENT-LOG-AUTHORITATIVE). The Hermes worker is treated as a *commit
producer* on `impl/<root>-<item>`; requiem owns everything around it:

1. **Trunk bootstrap (pre-fan-out):** a verb ensures `feature/<root>` exists
   off `main` (idempotent), before `dispatch_leaves`.
2. **Leaf-PR ownership moves to requiem:** after a leaf's worktree branch is
   delivered, **requiem** opens (or reconciles) the leaf PR with
   `base=feature/<root>` via `GhClient.pr_create` (idempotent via pr_search,
   head+base match — exactly plan_pr.py's pattern). This sidesteps the
   missing Hermes `--base` flag entirely: requiem never needed Hermes to
   target the PR, because requiem opens it. The worker just supplies commits.
3. **feature_pr readiness + final PR:** once every expected leaf PR is
   `merged==true` into the trunk (invariant 2) and requirement dispositions
   are satisfied (ADR-0006), a small `feature_pr` workflow/step opens (reuses)
   `feature/<root>` → `main` and hands off to `pr_lifecycle` / the human. No
   self-merge.
4. **Drift policy:** for v0, `feature_pr` only *opens* the trunk→main PR and
   lets `pr_lifecycle` surface an unmergeable (drifted) PR to the human. A
   `rebase_onto_target` verb is deferred (ADR-0007 §5.1 foresaw it) and, if
   added, must be the *only* writer to the trunk to respect
   INV-NO-DIRECT-TRUNK-COMMITS.

Option C is the only one that is fully actionable from this repo today, keeps
requiem authoritative, and degrades gracefully without a Hermes release.

## Decision (proposed)

Adopt **Option C**. Specifically:

- ADR-0007 §5.1's *placement* recommendation ("fold drift integration into
  `implementation.py`'s exit") is **superseded for the live path** by ADR-0014
  — `implementation.py` is not the live integration point. Its *spirit*
  (don't rebuild polyphony's heavy aggregator; feature_pr is a small
  opener/gate) is **retained**.
- `feature_pr` is a small standalone readiness/final-PR workflow mirroring
  `plan_pr.py`'s mechanics (idempotent branch/PR, head+base match, full
  dry-run validate, Fake-client tests, backlink) but with a
  `verify_trunk_readiness` core verb instead of "commit an artifact."
- Leaf PRs are opened by **requiem** with `base=feature/<root>`, not left to
  the Hermes worker — this is the lever that makes the missing `--base` flag
  irrelevant.

### Minimum-safe build sequence (for when this is ratified)

1. **Trunk bootstrap verb** — ensure `feature/<root>` (idempotent, off the
   detected default branch) before `dispatch_leaves` in `kanban_executor`.
2. **Requiem-owned leaf-PR open/reconcile** — `head=impl/<root>-<item>`,
   `base=feature/<root>`, after delivery; idempotent.
3. **`feature_pr.py`** — load expected leaves from the committed plan; verify
   trunk exists; verify every expected leaf PR is head/base-correct and
   merged; (optionally) verify requirement dispositions or gate if omitted;
   open/reuse `feature/<root>` → `main`; backlink; end. No self-merge.
4. **Driver wiring** — invoke after `aggregate` approval in `end_to_end`.

Each step is independently testable; the whole is wired only at step 4, so the
tree never sits half-integrated.

## Consequences

**Positive:** unblocks #7 on the live path without a Hermes release; keeps
requiem the single topology authority; reuses the proven plan_pr.py shape;
the leaf-PR-base decision (the rubber-duck's "Q3") is settled (requiem opens,
leaf→trunk, never self-merge).

**Negative / open:** requiem now opens leaf PRs (more GitHub surface +
idempotency to get right); requirement-disposition gating is scoped out of the
first slice and must land before this is "production"; trunk drift is handled
only by surfacing an unmergeable PR until a `rebase_onto_target` verb is added;
Option C assumes a GitHub-style `GhClient` — the ADO leaf-PR path (#10,
ADR-0007) is a separate, still-draft track.

**Why this is recorded as an ADR:** the trunk-integration shape is hard to
reverse (it dictates how every leaf lands and how runs reach `main`),
surprising without context (a future reader will ask "why does requiem open
the leaf PRs instead of the worker?" — answer: Hermes `kanban create` has no
`--base`), and the result of a real trade-off (Options A/B/C above).
