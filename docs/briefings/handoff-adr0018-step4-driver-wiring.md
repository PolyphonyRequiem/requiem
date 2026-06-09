# Handoff — ADR-0018 step 4 (driver wiring) for a Hermes implementer on a live device

> **Audience:** the Hermes implementer picking up requiem's #7 trunk-integration
> work on a device **with live Hermes + GitHub credentials**.
> **Author:** Copilot CLI (Daniel's box, no live creds — that is exactly why
> this is being handed off).
> **Date:** 2026-06-09.
> **Status of the branch:** everything below is on `main` at commit `82b23fc`
> (already pushed to `origin/main`). Working tree clean. Pull `main` and you
> have all of it.
> **Authority:** this briefing is *not* authoritative — decisions close in
> [ADR-0018](../decisions/0018-trunk-integration-contract.md) and the
> [parity scorecard](../references/v0-parity-readiness.md) §2.9 #7. Update those,
> not this file.

---

## 1. One-paragraph orientation

Requiem delivers an ADO work item end-to-end by fanning each *implementable
leaf* out to a **Hermes kanban worker** (ADR-0014). v0 non-negotiable **#7** is
the **merge-group / trunk branch topology** (ADR-0006 Option D): one integration
trunk `feature/<root>` per run, each leaf delivered on `impl/<root>-<item>`, the
plan on `plan/<root>`. **ADR-0018** decided how that topology gets built on the
live Hermes path: **requiem owns it** (the worker can't be told a PR base —
`hermes kanban create` has `--branch` but no `--base`). The build is a 4-step
sequence. **Steps 1–3 are landed and tested. Step 4 (wiring it into the driver)
is the remaining work, and it must be done on a live device** because the one
behaviour unit fakes cannot exercise — branch drift — only shows up live.

---

## 2. What is already done (do NOT rebuild these)

All four are independent, contract-tested, ruff-clean, and mirror each other's
shape (`build_engine` / `build_workflow` / `*_result` / `verdict_card` / `main`
demo). Each has a zero-arg side-effect-free demo: `python -m requiem.workflows.<x>`.

| Step | Module | What it does | Tests |
|---|---|---|---|
| topology names | `src/requiem/branch_model.py` | The single authority for `feature/<root>`, `plan/<root>`, `impl/<root>-<item>`, `evidence/<root>-<item>`; fail-closed `parse_branch`. | `tests/test_branch_model.py` (33) |
| **1** | `src/requiem/workflows/trunk_bootstrap.py` | Ensures `feature/<root>` exists **before** fan-out. GET base SHA → create the trunk ref only if absent. Idempotent; **never force-moves** an existing trunk; fail-closed on a missing base; read-only dry-run probe. | `tests/test_trunk_bootstrap_workflow.py` (7) + `tests/test_gh_branch_ref.py` (8) |
| **2** | `src/requiem/workflows/leaf_pr.py` | After delivery, opens/reuses each `impl/<root>-<item>` → `feature/<root>` leaf PR. Fail-closed on wrong-base / ambiguous / errored leaf. Emits the `{leaf_id: pr_number}` map step 3 consumes (re-exports `feature_pr.LeafPr`). | `tests/test_leaf_pr_workflow.py` (11) |
| **3** | `src/requiem/workflows/feature_pr.py` | The trunk-readiness gate + `feature/<root>` → `main` opener. Verifies every expected leaf PR is `head=impl/<root>-<item>`, `base=feature/<root>`, **merged** (via `gh.pr_view`, reliable for merged state). Fail-closed to a human on any not-ready leaf. No self-merge. | `tests/test_feature_pr_workflow.py` (14) |

**The narrow git-mutation surface (step 1's enabler):** the toolbelt `GitClient`
is **read-only** (`show` only), so the trunk is created **remotely** via the
GitHub refs API. Two enumerated methods were added to `GhClient`
(`src/requiem/clients/gh.py`) — and *only* these two; do not widen them:
- `branch_sha(repo, branch) -> str` — the source SHA (`GET git/ref/heads/...`).
- `ensure_branch_ref(repo, branch, source_sha) -> bool` — idempotent create
  (`POST git/refs`); returns `True` if created, `False` if already present;
  **never force-moves**; reconciles a lost 422 create-race to `False`.

The remote-ref mechanism (vs. a local git-mutation client) was **ratified by
Daniel 2026-06-07** — see ADR-0018 §"Trunk-bootstrap mechanism" for the
trade-off.

---

## 3. The remaining work — step 4: driver wiring

**File:** `src/requiem/end_to_end.py` (the `run_pipeline` coroutine). It is a
thin operator command (NOT a workflow) that runs `planning` → `commit_plan` →
`kanban_executor` as independent top-level engines, threading artifact paths
between them (this sidesteps ADR-0013 §B1 — each stage gets a real
provider/toolbelt). Phase 3 (dispatch) is around **lines 182–211**.

**The driver already extracts `leaf_ids`** from the executor's `resolve_leaves`
node (lines 194–199) — that is exactly the input `leaf_pr` needs.

Wire the three landed workflows into the pipeline in this order:

```
planning → commit_plan
  → trunk_bootstrap(root, repo, base)        # STEP 1: before dispatch
  → kanban_executor(...)                      # existing fan-out
  → (workers deliver; executor's aggregate gate approves)
  → leaf_pr(root, repo, leaf_ids)             # STEP 2: open leaf PRs, get {leaf_id: pr_number}
  → (human/pr_lifecycle merges each leaf PR into the trunk)
  → feature_pr(root, repo, leaves=[(leaf_id, pr_number)...])   # STEP 3: gate + open trunk→main PR
```

Notes / gotchas the implementer must respect:

- **Bootstrap must run before `dispatch_leaves`.** The trunk has to exist when
  requiem opens leaf PRs against it (`base=feature/<root>`).
- **`leaf_pr` runs *after* the executor reports delivery**, not before — it needs
  the `impl/<root>-<item>` branches to exist on the remote (pushed by the
  worker).
- **`feature_pr` runs after the leaf PRs are *merged*.** Between `leaf_pr` and
  `feature_pr` there is a merge step requiem does **not** own (GhClient has no
  `pr_merge`; that's `pr_lifecycle` / a human). So step 4 may be two driver
  invocations either side of a human gate, not one straight-through call.
- **Repo identity.** The landed workflows take a `repo` string (`"Owner/Repo"`).
  The driver currently doesn't thread one — you'll need to source it (operator
  arg / config / `gh repo view`).
- **Keep the dry-run discipline.** `live=False` must stay genuinely
  side-effect-free end-to-end (no ref create, no PR open). Each landed workflow
  already honours its own `dry_run`; thread the driver's `live` flag through.
- **Persist the leaf-PR map.** `leaf_pr`'s `{leaf_id: pr_number}` output is the
  authoritative input to `feature_pr`. A default `gh pr list` is open-only and
  **cannot** re-derive merged leaf PR numbers, so the driver must persist this
  map (it already writes artifacts per stage — follow that pattern). This is
  called out in ADR-0018 step 2.

---

## 4. Why this needs a LIVE device — the three open questions

Step 4 was deliberately **not** wired blind. The one behaviour no unit fake can
reproduce is **branch drift**, and it must be observed live before the wiring is
trustworthy. These are the questions to resolve, in priority order:

### Q1 — Does branch drift actually bite? (the blocker)

**The hypothesis (ADR-0018 "Open refinement"):** a Hermes worker cuts its
worktree from the **board repo's current HEAD**, and requiem cannot make a leaf
branch *descend from* `feature/<root>` — only *name* it `impl/<root>-<item>`. So
a leaf branch is rooted at `main`, not the trunk. A leaf PR merges cleanly into a
*fresh* trunk (initially `== main`), but once **earlier leaves merge into the
trunk**, a later leaf branch (still rooted at old `main`) can **conflict** with
the now-advanced trunk.

**What to do:** run ONE real multi-leaf root end-to-end on a scratch repo and
**watch whether the 2nd/3rd leaf PR merges cleanly** after the 1st has merged
into `feature/<root>`. The answer decides the shape of step 4:
- **No conflict in practice** → wire step 4 as the straight sequence above.
- **Conflict** → step 4 needs a `rebase_onto_target` step (ADR-0018 names it as
  the eventual answer; it would be the *sole* trunk writer, per
  INV-NO-DIRECT-TRUNK-COMMITS). Build it as a 5th workflow before wiring.

**What you need:** Hermes gateway + auth on the device; a throwaway GitHub repo
you may push branches/PRs to; a **dedicated `requiem-*` kanban board** (never
the `default` board — it has live tasks; see the hermes-kanban memory/ADR-0014).

### Q2 — What is the base branch?

`trunk_bootstrap` defaults the source/base to `main`. If target repos use
`master` / `develop`, resolve the repo's actual default branch instead of
hardcoding. Cheap to add (`gh repo view --json defaultBranchRef`) — decide
whether to do it now or assume `main` for the first live run.

### Q3 — Teardown convention

A live run leaves `feature/<root>` + `impl/*` branches and open PRs. Decide the
cleanup policy (auto-delete on success? leave for inspection?) and wire step 4 to
match. Daniel's call — flag it to him if unsure.

---

## 5. How to validate your work

```powershell
# install (one-time)
pip install -e ".[cli]"

# the landed #7 surface — all green today (run targeted; a full `pytest` run hangs)
python -m pytest tests/test_branch_model.py tests/test_trunk_bootstrap_workflow.py `
  tests/test_gh_branch_ref.py tests/test_leaf_pr_workflow.py `
  tests/test_feature_pr_workflow.py tests/test_fake_surface_contract.py `
  tests/test_docs_smoketest.py -q

# the driver's own suite (wire step 4 here with stub engines, like the existing tests)
python -m pytest tests/test_end_to_end.py -q

# lint
ruff check src/requiem tests
```

- `asyncio_mode=auto` — async tests need no decorator.
- **Do not run the bare `pytest` whole-suite** — it hangs in a heavy suite; run
  targeted files.
- `tests/test_fake_surface_contract.py` enforces async-shape parity between every
  local `Fake*` client and the real client — if you add a method to `GhClient`,
  any fake that implements it must match its async-ness or this test fails.
- Each workflow has a demo: `python -m requiem.workflows.trunk_bootstrap` (and
  `leaf_pr`, `feature_pr`) renders a dry-run verdict card with zero deps.

---

## 6. Commit / handoff conventions

- Conventional-commit subjects; include the trailer
  `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`.
- Branch model for the work itself is up to you, but the *product's* branch names
  must go through `requiem.branch_model` — never hand-roll a branch f-string.
- Close decisions in **ADR-0018** and the **parity scorecard** (§2.9 #7), not in
  this briefing. When step 4 lands, flip #7's "Pending" clause and the
  scorecard summary line.
- The fleet worker profiles live in `fleet/` (ADR-0017); the handoff *wire*
  contract (worker→requiem receipts) is `src/requiem/handoff.py` +
  `tests/test_handoff_contract.py` — distinct from this human briefing.

---

## 7. TL;DR

`branch_model` + `trunk_bootstrap` + `leaf_pr` + `feature_pr` are **done and
tested** (commit `82b23fc` on `main`). The whole of ADR-0018 Option C is built
**up to the live boundary**. Your job: **resolve Q1 (drift) on a live device**,
answer Q2 (base branch) and Q3 (teardown), then **wire step 4 in
`end_to_end.run_pipeline`** — bootstrap before dispatch, `leaf_pr` after
delivery, `feature_pr` after the leaf PRs merge. Test against
`tests/test_end_to_end.py` with stub engines; validate the real topology on the
scratch repo.
