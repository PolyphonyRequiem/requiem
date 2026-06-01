# Full SDLC demo — Verdi-3 / Phase C

The vertical-integration slice. One `requiem run` invocation drives
an AB work item from inbox to closed through five sub-workflows
without you nursing the handoffs.

```
dispatch  →  plan  →  implement  →  pr_lifecycle  →  close_out
   │          │           │              │              │
   └ root-id  └ leaf      └ PR #19       └ merged       └ docs/closeouts/
```

This is the Walking-Skeleton-β demo: proof that the five Phase-B
workflows compose end-to-end without manual context-switching, and
that the kernel's sub-workflow primitive (ADR 0005) holds under five
levels of stacked invocation.

## What the demo proves

* **Composition**: five sub-workflows execute in sequence, each with
  its own log file, with the parent narrating at the right altitude.
* **State propagation**: the implementation stage's PR number flows
  through to PR-lifecycle and close-out via a cross-stage script
  (`capture_impl`) that folds the child's log.
* **Failure routing**: each stage has a `paused_X` gate; if the child
  emits `NeedsHuman`, the parent suspends there so an operator can
  `requiem resume` after manual triage.
* **Idempotency**: rerunning the same `run_id` re-attaches to the
  durable log instead of re-doing work.
* **Dry-run safety**: the default invocation mutates nothing outside
  `log_dir` (no real PRs, no real ADO writes).

## Run the demo

```powershell
requiem run requiem.workflows.full_sdlc --run-id verdi-demo
```

That's it. No flags. The default `FullSdlcInputs` runs in dry-run
mode against item `AB#12345` and a freshly-seeded throwaway repo
under `log_dir/demo_repo`. Live narration scrolls past as each
sub-workflow executes; the verdict card prints at the end.

## Expected verdict card

```
═══ Requiem v0 — Full SDLC demo ═══════════════════════════════════
Item: AB#12345 — 'AB work item 12345'

  ◐ Dispatched          root-12345-2026-06-01 (dry-run)
  ◐ Planned             1 leaf (no decomposition needed) (dry-run)
  ◐ Implemented         feature/12345 — PR #19 (dry-run)
  ◐ PR Lifecycle        #19 — handoff complete (dry-run)
  ◐ Closed out          AB-12345 → docs/closeouts/AB-12345.md (dry-run)

Total: 5/5 sub-workflows — DRY RUN
Receipts: <repo>\.runs\root-12345-2026-06-01*
════════════════════════════════════════════════════════════════════════
```

The `◐` glyph marks each stage as completed-in-dry-run mode. A live
(non-dry-run) run would use `✓` instead and drop the `(dry-run)`
suffix on each line.

## When the demo pauses

If a sub-workflow returns `NeedsHuman` (or `PermanentFailure`), the
parent routes to the corresponding `paused_X` gate and the default
auto-handler picks `abort` — safe default; we never auto-resume past
a child's pause. The verdict card shows where it stopped:

```
  🚦 Implemented       — PAUSED (implementation → ...)
  ⚠ Demo paused at stage: implement
  → Resume:  requiem resume requiem.workflows.full_sdlc --run-id <ID>
```

Stage-to-gate mapping:

| stage          | paused gate         |
| -------------- | ------------------- |
| `dispatch`     | `paused_dispatch`   |
| `plan`         | `paused_plan`       |
| `implement`    | `paused_implement`  |
| `pr_lifecycle` | `paused_pr`         |
| `close_out`    | `paused_close`      |

## Architecture: shim-module pattern

The kernel's `SubWorkflowNode` only forwards `log_dir` to the child's
`build_engine` (ADR 0005). To thread our `FullSdlcInputs` through
five levels we register five **shim modules** in `sys.modules` at
import time:

```
requiem.workflows._full_sdlc_shims.dispatch
requiem.workflows._full_sdlc_shims.planning
requiem.workflows._full_sdlc_shims.implementation
requiem.workflows._full_sdlc_shims.pr_lifecycle
requiem.workflows._full_sdlc_shims.close_out
```

Each shim's `build_engine(log_dir)` reads a module-level
`_CURRENT_INPUTS` cell (mutated by `full_sdlc.build_engine`) and
constructs the real child engine with closure-baked inputs. This is
a single-process, single-run idiom — concurrent `full_sdlc` runs in
the same process would collide on the cell.

Live narration uses a similar pattern: the CLI assigns
`parent_engine.on_event = observer` after construction; the parent
engine subclass mirrors that assignment to an `_OBSERVER` cell, and
each shim's `build_engine` reads the cell to install the observer on
the child engine it just built. Operators see one continuous stream
of events across all five children.

## Architecture diagram

```
                  full_sdlc (parent, ~30 events)
                          │
       ┌────────────┬─────┴─────┬──────────────┬───────────────┐
       ▼            ▼           ▼              ▼               ▼
   dispatch     planning  implementation  pr_lifecycle   close_out
  (~10 ev)     (~10 ev)     (~30 ev)        (~20 ev)      (~12 ev)
                                │
                                ▼
                          capture_impl
                       (parent script,
                        folds child log)
```

Each child writes its own `{parent_run_id}__{stage}.events.jsonl`
under the same `log_dir`. The parent's log carries only
`subworkflow_started` / `subworkflow_completed` markers per child
plus its own scripts and gates. This is INV-SUBWORKFLOW-LOG-ISOLATION:
the parent's history reads as a five-stage SDLC story; the child logs
hold the verb-level detail.

## Demo Contract §3 checklist

| Box                                                  | Where it lives                                             |
| ---------------------------------------------------- | ---------------------------------------------------------- |
| §3.1 workday vignette                                | `preamble()` — "it's Monday morning…"                      |
| §3.2 stakes named                                    | `preamble()` — "if any seam breaks, you lose the morning…" |
| §3.4 verdict card                                    | `verdict_card()` — 5-stage card, totals, receipts          |
| §3.5 no chrome                                       | render hints flag `end` / `fail_end` / `cancel_end` silent |
| §3.6 a renderer per kind                             | inherits the shared `EVENT_RENDERERS` registry             |
| §3.9 auto-resolved gates flagged                     | `_default_gate_handler.__requiem_auto__ = True`            |
| §3.10 ≤5s to meaningful output                       | first `▶ run_started` lands in <1s                         |

## Tests

The Phase-C scenarios live in `tests/test_full_sdlc.py`:

1. **happy path** — all five stages green; verdict card renders with
   "Total: 5/5" and "PR #19".
2. **per-stage failure (parameterised over 5 stages)** — each
   stage's `NeedsHuman` outcome suspends the parent.
3. **INV-RESTART** — second engine instance on the same `run_id`
   re-attaches without appending a second `run_started`.
4. **INV-SUBWORKFLOW-LOG-ISOLATION** — parent's log holds no child
   node ids; each child writes its own log file.
5. **dry-run no side effects** — an external workspace handed to the
   demo as `repo_path` is byte-identical before vs after.
6. **preamble / verdict-card shape** — naming, stakes, mode mentions.
7. **topology sanity** — all three terminal dispositions present.

Run them with `pytest tests/test_full_sdlc.py -q`.
