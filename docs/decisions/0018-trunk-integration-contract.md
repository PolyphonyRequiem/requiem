# ADR 0018 — Trunk integration on the live (Hermes) path

**Status:** Accepted (2026-06-05 — Option C ratified; sub-fork resolved, §"Component ownership"). Remote-ref trunk-bootstrap mechanism ratified 2026-06-07 (see §"Trunk-bootstrap mechanism"). Step 4 (driver wiring) landed + live-validated 2026-06-09; drift refinement RESOLVED (see §"Open refinement — RESOLVED").
**Date:** 2026-06-05 (updated 2026-06-09)
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

## Decision

Adopt **Option C** (ratified 2026-06-05). Specifically:

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
   **STATUS: landed 2026-06-07** (`src/requiem/workflows/trunk_bootstrap.py`,
   `tests/test_trunk_bootstrap_workflow.py` + `tests/test_gh_branch_ref.py`).
   The git client (`toolbelt.GitClient`) is read-only (`show` only), so there is
   no local-git path to create a branch; the **remote-ref mechanism was
   ratified 2026-06-07** (see §"Trunk-bootstrap mechanism" below) and confined
   to a narrow `gh.branch_sha` / `gh.ensure_branch_ref` pair rather than
   scattered `gh api` mutation. The workflow GETs the base SHA, creates
   `feature/<root>` only when absent (never force-moves an existing trunk, so a
   re-run can't rewind a trunk leaves have advanced), fails closed on a missing
   base, and probes read-only in dry-run. **Contract-tested; live behaviour
   (and the drift wrinkle below) still gated on a live Hermes loop — not yet
   wired into the driver.**
2. **Requiem-owned leaf-PR open/reconcile** — `head=impl/<root>-<item>`,
   `base=feature/<root>`, after delivery; idempotent.
   **STATUS: landed 2026-06-06** (`src/requiem/workflows/leaf_pr.py`,
   `tests/test_leaf_pr_workflow.py`, 11 tests). Reuse-open-or-create per
   delivered leaf; fail-closed on a wrong-base / ambiguous / errored leaf
   (never half-opens a partial set); emits the `{leaf_id: pr_number}` map
   `feature_pr` consumes (re-exports `feature_pr.LeafPr` to keep the hand-off
   type-explicit). Idempotent reuse covers the *pre-merge* window only —
   post-merge re-derivation of the map is the driver's job (a default
   `gh pr list` is open-only), consistent with requiem persisting every
   decision in the event log.
3. **`feature_pr.py`** — load expected leaves from the committed plan; verify
   trunk exists; verify every expected leaf PR is head/base-correct and
   merged; (optionally) verify requirement dispositions or gate if omitted;
   open/reuse `feature/<root>` → `main`; backlink; end. No self-merge.
   **STATUS: landed 2026-06-05** (`src/requiem/workflows/feature_pr.py`,
   `tests/test_feature_pr_workflow.py`, commit `0625cf6`). Takes the expected
   leaf set as `(leaf_id, pr_number)` and reads each via `gh.pr_view` (merged
   state is unreliable through an open-only `gh pr list`).
4. **Driver wiring** — invoke after `aggregate` approval in `end_to_end`.
   **STATUS: landed + live-validated 2026-06-09** (`src/requiem/end_to_end.py`,
   `tests/test_end_to_end_topology.py`, 10 stub-engine tests). `run_pipeline`
   gained `github_repo`/`base_branch`/`gh` params and three injectable engine
   factories. Phase 2.5 runs `trunk_bootstrap` **before** dispatch and fails
   closed (a failed bootstrap never fans out — invariant 1). Phase 4 runs
   `leaf_pr` **after** the executor reports delivery and **persists** the
   `{leaf_id: pr_number}` map to `leaf-pr-map-<item>.json` (a default
   `gh pr list` is open-only — step 2's caveat). `feature_pr` is a **separate**
   `integrate_pipeline` invocation, because the leaf PRs must be merged into the
   trunk between the two calls (a human/`pr_lifecycle`-owned step — no
   self-merge); it reads the persisted map, never re-queries. The base branch is
   resolved from the repo's real default via the narrow `gh.api()` read hatch
   (Q2 — no new mutation surface). The no-`github_repo` path is byte-for-byte the
   legacy executor-only pipeline. `live=False` threads `dry_run=True` to every
   topology step (genuinely side-effect-free). Live proof:
   `.runs/live_wiring_check.py` against a scratch repo created `feature/<root>`
   (idempotent re-run → `exists`, no force-move), opened two leaf PRs
   `base=feature/<root>`, merged them, and opened the trunk→`main` PR off the
   persisted map. (Repro: `docs/validation/adr0018-step4/`.)

Each step is independently testable; the whole is wired only at step 4, so the
tree never sits half-integrated.

### Trunk-bootstrap mechanism — remote GitHub-refs create (ratified 2026-06-07)

**Decision:** requiem bootstraps `feature/<root>` **remotely** via the GitHub
refs API, exposed as a narrow two-method capability on `GhClient`
(`branch_sha(repo, branch)` and `ensure_branch_ref(repo, branch, source_sha)`),
NOT via a local git-mutation client.

**Why this was a real decision (and a trade-off).** The toolbelt `GitClient` is
deliberately read-only (one `show` method). Creating `feature/<root>` therefore
forced a choice between two genuinely different surfaces:

- **(A) Remote ref create** — `GET repos/{repo}/git/ref/heads/{base}` for the
  source SHA, `POST repos/{repo}/git/refs` if the trunk ref is absent. No
  working tree, idempotent, fits the "driver owns trunk topology" split. Cost:
  it turns `GhClient` from "read-only + `pr_create`" into a client that also
  mutates branch refs — a new, broader mutation surface.
- **(B) Local git-mutation client** — a new client with checkout / branch /
  push, plus working-tree lifecycle management. Heavier, and misaligned with the
  current driver/toolbelt split (the executor coordinates a *remote* board).

**Chosen: (A).** Daniel ratified the remote-ref mutation surface (2026-06-07,
"1 sure"). It is materially narrower than (B) and needs no local clone. The
risk the rubber-duck flagged — that this manufactures the branch state every
later gate depends on, and that a fake can prove we `POST /git/refs` but not
that the topology behaves under Hermes branch creation, branch protection, or
the drift wrinkle — is contained two ways: (1) the capability is **two
enumerated methods**, not an open `gh api` mutation hatch handed to workflow
code; (2) `ensure_branch_ref` **never force-moves** an existing ref, so the
worst a buggy re-run can do is no-op, not rewind a trunk leaves have advanced.
Live validation of the end-to-end topology remains an explicit precondition of
**driver wiring** (step 4), which is why the workflow ships standalone and
unwired.

### Open refinement — the Hermes worktree cuts from HEAD (flagged 2026-06-05)

Scoping steps 1–2 surfaced a wrinkle the body above glossed. Because
`hermes kanban create` only takes `--branch` (the worktree branch name) and the
worker cuts that worktree from the **board repo's current HEAD**, requiem cannot
make a leaf branch *descend from* `feature/<root>` — only *name* it
`impl/<root>-<item>`. Two consequences for steps 1–2:

- **"Trunk-before-fan-out" does not auto-correct the leaf base.** Its only real
  job is ensuring `feature/<root>` exists before requiem *opens* the leaf PR
  (`base=feature/<root>`). The leaf commits still originate from default HEAD.
- **Drift accrues as leaves merge.** A leaf branch cut from `main` PRs cleanly
  into a fresh `feature/<root>` (initially == `main`), but once earlier leaves
  merge into the trunk, a later leaf branch (still rooted at old `main`) can
  conflict against the now-advanced trunk. This is the target-drift problem
  ADR-0007 §5.1 foresaw, arriving via the worktree model rather than a
  long-lived feature branch. v0 surfaces an unmergeable leaf PR to the human
  (no auto-rebase); a `rebase_onto_target` verb (the sole trunk writer, per
  INV-NO-DIRECT-TRUNK-COMMITS) is the eventual answer.

Steps 1–2 should therefore be **built against a live Hermes loop** (or a
faithful integration harness) rather than blind, because the base-ancestry and
drift behaviour is exactly what unit fakes cannot exercise. `feature_pr.py`
(step 3) is unaffected — it only reads merged PR state — which is why it landed
first.

### Open refinement — RESOLVED (Q1 live finding, 2026-06-09)

The drift hypothesis was tested live before wiring step 4, on a throwaway scratch
repo with a faithful integration harness (real `feature/<root>` + `impl/*`
branches, real PRs, real `gh` merges; the leaves cut from `main` HEAD exactly as
the Hermes worktree model does, then only *named* `impl/<root>-<item>`). Two
multi-leaf roots were run, advancing the trunk by merging leaf #1 and then
re-checking the downstream leaves' mergeability:

| Regime | Before any merge | After leaf #1 merged | Merge leaf #2 |
|---|---|---|---|
| **Disjoint** leaves (distinct files) | all `MERGEABLE CLEAN` | all `MERGEABLE CLEAN` | **clean — no drift** |
| **Overlapping** leaves (same line) | all `MERGEABLE CLEAN` | `CONFLICTING DIRTY` | **refused — drift bites** |

**Finding:** the hypothesis is **true but conditional**. Drift bites **only when
two leaves touch the same lines**; disjoint leaves never drift. Crucially it is
**invisible pre-merge** — every leaf PR reads `MERGEABLE CLEAN` until an earlier
overlapping leaf actually merges, so no pre-flight gate can predict it. And the
conflict lands on the **leaf-PR → trunk merge**, a step requiem does **not** own
(`GhClient` has no `pr_merge`; `pr_lifecycle`/the human merges).

**Decision (Daniel, 2026-06-09):** because the conflict surfaces exactly where
Option C ¶4 + this section already place it (a human-owned merge, unmergeable PR
surfaced by `pr_lifecycle`), **wire the v0 straight sequence** —
bootstrap → dispatch → `leaf_pr` → [human merges] → `feature_pr` — and **keep
`rebase_onto_target` deferred**. The briefing's binary ("conflict ⇒ build rebase
before wiring") was a false dichotomy against this evidence: drift is real yet
conditional and already has a ratified v0 home. When `rebase_onto_target` is
eventually built it remains the **sole** trunk writer (INV-NO-DIRECT-TRUNK-COMMITS).
Harness + live wiring proof: `docs/validation/adr0018-step4/q1_drift_probe.sh`,
`docs/validation/adr0018-step4/live_wiring_check.py`.

### Component ownership (sub-fork resolved 2026-06-05)

The trunk git/gh operations (bootstrap `feature/<root>`, open leaf PRs, open
the trunk→main PR) require a **local repo checkout + `gh` authority**. Two
candidates held that authority: the `kanban_executor` (which only *coordinates
a remote board* — it may run creds-light and has no guaranteed local checkout)
and the `end_to_end` driver (which already runs each top-level engine with its
own **real local provider/toolbelt** — ADR-0013/0014). **Resolution: the
driver owns trunk git/gh ops.** The executor stays a remote-board coordinator;
trunk bootstrap runs in the driver *before* it invokes the executor, and the
leaf-PR-open + `feature_pr` steps run in the driver *after* the executor's
`aggregate`. This keeps the executor's creds-light remote-coordination role
intact and puts every git/gh mutation where the real toolbelt already lives.

`feature_pr` is therefore built as a **standalone workflow** (its own engine +
Fake gh client, exactly like `plan_pr.py`) that the driver invokes — not a verb
bolted onto the executor.

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
