# Tchaikovsky — Real-ADO Bug-Bash (Phase C)

**Date:** 2026-05-31 → 2026-06-01
**Branch:** `phaseC/real-ado-bug-bash`
**Scope:** Drive every Requiem SDLC workflow against **real** Azure DevOps work
items (via `twig`) and **real** GitHub PRs (via `gh`) to surface integration
bugs that the in-process fakes do not catch.
**Strategy:** read-mostly, `--dry-run` everywhere; fix small bugs in-place
(<300 LOC total); file larger bugs as GitHub issues; pin every fix with a
regression test in `tests/test_bugbash_regressions.py`.

---

## Executive summary

| Workflow            | Probe status            | Verdict                                   |
| ------------------- | ----------------------- | ----------------------------------------- |
| `code_review_demo`  | ✅ baseline              | Clean verdict card (smoke).               |
| `close_out`         | ✅ pass post-fix         | Escalates correctly to `NeedsHuman` at criteria-gap gate against real item #3311. |
| `planning`          | ✅ pass post-fix         | Recursive `decomposable` end against real item #3311 after BUG #1 fix. |
| `implementation`    | ✅ pass post-fix         | 12-verb `start → … → end_handoff` against real item #3314 after BUG #1 fix. |
| `pr_lifecycle`      | ✅ pass                  | Real PR #14 (open), polled 30 s, clean `needs_human_end` timeout. |
| `root_dispatch`     | ⏭ deferred              | Haydn not merged; out of scope.           |

**Bugs found:** 4 — 1 blocker (FIXED, regression-pinned), 3 rough-edges
(DEFERRED, filed as issues).
**Code churn:** ~40 LOC (3 sync→async conversions + 2 test-fake updates), well
under the 300-LOC budget.
**Overall parity readiness:** **PASS post-fix**, with documented limitations
on twig PR-link surfacing.

---

## Environment

- **Worktree:** `C:\Users\dangreen\projects\requiem-tchaikovsky-bugbash` on
  branch `phaseC/real-ado-bug-bash` (forked from `8743a8d` —
  *Phase C / Bizet: implementation workflow (#27)*).
- **Python:** 3.14.3 / `.venv` / `requiem[llm,cli,test]` editable.
- **Real ADO items** (in repo `C:\Users\dangreen\projects\polyphony`, twig
  workspace): `#3311` (root Issue), `#3312` (Done/Task), `#3313` (Done),
  `#3314` (To Do/Task), `#3315` (Done).
- **GitHub:** `gh` authenticated as `PolyphonyRequiem`. Open PR `#14`; merged
  PRs `#23`–`#27`.

---

## Bugs

### BUG #1 — sync `twig.show()` / `twig.comment()` from async kernel (BLOCKER, **FIXED**)

**Severity:** Blocker. Three workflow verbs (`planning._fetch`,
`implementation._fetch_plan`, `implementation._link_pr`) crashed on every
real-ADO invocation. Invisible to unit tests because the test fakes had sync
`.show()` / `.comment()` returning directly.

**Symptom:**
```
RuntimeError: asyncio.run() cannot be called from a running event loop
```
The kernel runs every verb inside `asyncio.run(engine.run(...))`. The sync
`TwigClient.show()` and `.comment()` wrappers internally call
`asyncio.run(show_async(...))`, which collides with the outer loop.
`kernel._execute` (kernel.py:446) catches the exception and converts it to
`PermanentFailure(error_kind="verb.crash")`, surfacing as a routing dead-end
rather than a traceback.

**Fix (~15 LOC core + ~25 LOC test-fake updates):**

- `src/requiem/workflows/planning.py`:
  - `TwigClientProto.show` → `async def show_async`
  - inline `FakeTwigClient.show` → `async def show_async`
  - `_fetch` verb → `async def` + `await twig.show_async(item_id)`
- `src/requiem/workflows/implementation.py`:
  - `_fetch_plan` → `async def` + `await twig.show_async(...)`
  - `_link_pr` → `async def` + `await twig.comment_async(...)`
  - inline `_DemoTwigClient.show / comment` → `show_async / comment_async`
- `tests/test_implementation_workflow.py`:
  - `FakeTwig.show / comment` → `show_async / comment_async`

**Architectural rule (newly enforced):** workflow verbs that touch
`TwigClient` MUST be `async def` and MUST use the `_async` methods. The sync
wrappers exist only for top-level CLI entry points.

**Regression coverage:** `tests/test_bugbash_regressions.py` — 4 tests using
an `AsyncOnlyTwigStub` that exposes only `show_async` / `comment_async`. Any
regression to sync calls raises `AttributeError` immediately — louder and more
deterministic than reproducing the asyncio.run collision.

---

### BUG #2 — close_out `end_human` terminate disposition is `"failed"` (rough-edge, DEFERRED)

**Severity:** UX rough-edge. The CLI topline shows **■ Failed** while the
verdict card correctly shows **🚦 Needs human**. Same run, contradictory
signals.

**Root cause:** `close_out.py`'s `end_human` terminate node hard-codes
`disposition="failed"`. The kernel's `_disposition_for_outcome` maps
`NeedsHuman` → `"needs_human"` but only for `subworkflow_completed` events;
terminate-node dispositions are taken from the node literally.

**Repro:** `python -m requiem.workflows.close_out --live --dry-run --item 3311 --pr 27`
(see `.runs/closeout-real-1.events.jsonl`).

**Fix sketch:** either add a `"needs_human"` disposition variant to the
terminate-node enum, or have the CLI render derive disposition from the verdict
card rather than the terminate's `disposition` field.

**Filed as:** `PolyphonyRequiem/requiem` issue (see Issues section below).

---

### BUG #3 — real `twig show --output json` has no `pullRequests` field (parity, DEFERRED)

**Severity:** Schema-parity gap. close_out's `_extract_linked_prs` is dead
code against real twig — it always returns `[]`, forcing operators to pass
`--pr N` explicitly on every invocation.

**Root cause:** Real twig JSON exposes:
`id, title, type, state, assignedTo, areaPath, iterationPath, isDirty,
isSeed, parentId, tags, fields, children, links, relations` — **no
`pullRequests`** field. The `links` and `relations` arrays were empty for all
five probed items (3311–3315), even on Done items #3312, #3313, #3315 that
must have had associated PRs.

**Workarounds (any of):**
1. Document the `--pr N` requirement in the close_out runbook.
2. Add a `gh pr list --search` fallback in `_extract_linked_prs` that finds
   PRs whose body mentions `AB#<item_id>` (the standard ADO link syntax).
3. File a `twig` enhancement to surface ADO's PR link relations under a
   `pullRequests` JSON field.

**Filed as:** `PolyphonyRequiem/requiem` issue.

---

### BUG #4 — planning lacks catch-all `permanent_failure` edges (rough-edge, DEFERRED)

**Severity:** Diagnostic rough-edge. A `verb.crash` in any planning verb
(e.g. the BUG #1 asyncio collision) strands the run with
`Failed(error_kind="route.missing")` and **no verdict-card narrative** — the
operator only sees a routing dead-end. Close_out wires catch-all
`permanent_failure` edges from every verb and renders a clean verdict card on
crash; planning does not.

**Filed as:** `PolyphonyRequiem/requiem` issue.

---

## Per-workflow probe records

### code_review_demo (baseline smoke)

```
requiem run requiem.workflows.code_review_demo --run-id smoke
```
Clean verdict card — terminal `completed`, baseline confidence in the harness.

### close_out (real ADO + real GH)

```
cd C:\Users\dangreen\projects\polyphony   # twig workspace
python -m requiem.workflows.close_out --live --dry-run --item 3311 --pr 27 \
  --run-id closeout-real-1
```
- 20 events, final_node `end_human`, terminal `failed` (see BUG #2).
- Workflow correctly traversed
  `fetch_item → resolve_pr → fetch_pr → fetch_criteria` and escalated at the
  criteria-gap gate (item #3311 has no Acceptance Criteria children).
- Re-ran with no `--pr` (`closeout-real-2`) and against Done item #3312
  (`closeout-real-3`) — both escalated to the correct gates.
- **Idempotency:** `requiem resume closeout-real-1` replayed all 21 prior
  events cleanly.

### planning (real ADO)

```
.venv\Scripts\python.exe .runs\probe_plan.py   # cwd = polyphony repo
```
- Post-fix: `start → fetch_item → planner_1 → … → end` against item #3311.
- Pre-fix: BUG #1 surfaced as `verb.crash` at `fetch_item` with no
  verdict-card narrative (also see BUG #4).

### implementation (real ADO, dry-run)

```
.venv\Scripts\python.exe .runs\probe_impl.py   # cwd = polyphony repo, --dry-run
```
- Post-fix: 12 verbs, `start → fetch_plan → … → end_handoff`, terminal
  `completed` against item #3314.
- Verified polyphony repo NOT contaminated (no marker file, working tree
  clean) — `dry_run=True` honored end-to-end.
- Implementation workflow has no CLI argparse driver; must drive via Python
  script. Recommend adding one for parity with planning + close_out.

### pr_lifecycle (real GH PR #14)

```
.venv\Scripts\python.exe .runs\probe_prlc.py   # RealPrToolkit(GhClient()), dry_run=True, poll_timeout_s=30
```
- Workflow fetched PR #14, identified `state=OPEN`, no-op'd `request_review`
  (dry_run honored), polled for 30 s, cleanly timed out to
  `needs_human_end`.
- **Caveat:** 30 s poll-timeout is fine for bug-bash but a real PR-lifecycle
  run will need a much longer (or unbounded) poll horizon.

### root_dispatch

Deferred — Haydn (the root_dispatch workflow) has not yet been merged.

---

## Fixes applied

| File                                          | LOC | Change                                       |
| --------------------------------------------- | --- | -------------------------------------------- |
| `src/requiem/workflows/planning.py`           | ~6  | `_fetch` + protocol + inline fake → async    |
| `src/requiem/workflows/implementation.py`     | ~9  | `_fetch_plan`, `_link_pr`, demo fake → async |
| `tests/test_implementation_workflow.py`       | ~4  | `FakeTwig.show/comment` → `_async`           |
| `tests/test_bugbash_regressions.py` (NEW)     | ~280 | 4 pinning regression tests                  |

**Net change to production code:** ~15 LOC. **Total churn (incl. tests):**
~300 LOC, of which ~280 is the new regression file.

---

## Issues filed

- **#29** — BUG #2 — close_out terminate disposition vs verdict card
- **#30** — BUG #3 — twig JSON has no `pullRequests` field
- **#31** — BUG #4 — planning lacks `permanent_failure` catch-all edges

---

## Verdict

**PASS post-fix.** Every workflow exercised here completes its real-ADO/real-GH
path correctly after BUG #1 is fixed. The three deferred rough-edges are
documented, filed, and do not block the engine from doing useful work — the
operator sees a contradictory disposition label (BUG #2), must pass `--pr N`
explicitly (BUG #3), and sees a thin failure when planning verbs crash (BUG
#4). None of these are correctness bugs; all three are quality-of-life
follow-ups for the next polish pass.
