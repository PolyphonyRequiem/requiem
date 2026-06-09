# ADR 0020 — In-process child-seam propagation (ADR-0013 blocker B1)

**Status:** Accepted + **implemented** (2026-06-09). Shipped the non-breaking
variant — see "Implementation notes" below.
**Date:** 2026-06-09
**Relates to:** ADR-0013 (fan-out executor — names B1 as the critical-path
blocker), ADR-0014 (external kanban executor — *sidesteps* B1 but does not close
it), ADR-0001 (single-process), ADR-0005 (sub-workflow log isolation),
INV-RESTART, INV-NO-CORRUPT-FORWARD.
**Parity:** unblocks non-negotiables **#4** (in-process tree-walking root
orchestrator) and **#5** (per-item worktree isolation for in-process parallel
dispatch).

## Context

The v0 parity audit names **B1 — child-seam propagation** as the blocker that
gates *all* in-process real dispatch (`full_sdlc`, the root orchestrator, and any
in-process fan-out). The external `kanban_executor` (ADR-0014) deliberately
sidesteps B1 by dispatching leaves to a separate Hermes worker process that
brings its own provider/toolbelt — but the *in-process* sub-workflow seam is
still broken, so `full_sdlc` and a future in-process orchestrator can't safely
run a real child.

### The exact defect

When a parent workflow invokes a sub-workflow, the kernel reconstructs the child
engine from the parent's **recorded inputs** (`kernel.py:543-567`):

```python
recorded_inputs = ...  # from the subworkflow_started event's inputs_summary
sig = inspect.signature(factory)
kwargs = {k: v for k, v in recorded_inputs.items() if k in sig.parameters}
child_engine = factory(log_dir=self.log_dir, **kwargs)  # or factory(self.log_dir, **kwargs)
```

`recorded_inputs` is `inputs_summary` — a **JSON-flat** dict (it must be, to be
durable in the event log per INV-EVENT-LOG-AUTHORITATIVE). The live runtime seams
— `provider` (the LLM), `toolbelt` (real git/gh/twig/fs), and `gate_handler` —
are **not JSON-serialisable** and so are never in `recorded_inputs`. They are
silently dropped.

The child `factory` (e.g. `implementation.build_engine`,
`implementation.py:1399-1410`) treats missing seams as a request for a **canned
demo**:

```python
if provider is None: provider = happy_path_provider()
if toolbelt is None: toolbelt = Toolbelt(git=RealGitClient(), gh=_DemoGhClient(),
                                          twig=_DemoTwigClient(), ...)
```

So a fanned-out `implementation` child runs the **happy-path fake LLM** and
**fake gh/twig** over a **real git client** — it reports `Completed(completed)`
without doing real work. The kernel then maps any child `Completed(completed)` →
parent `Success` (kernel.py:765-772). Net effect: **a silent-success footgun** —
the orchestrator believes a leaf was implemented when nothing happened. This
violates the *spirit* of INV-NO-CORRUPT-FORWARD (a fake success forwarded as
real) even though no outcome is mislabelled at the kernel layer.

### The proven fix pattern already in the tree

`planning.py` already solves exactly this for its own recursive children using
module-level **contextvars** (`planning.py:139-163`, installed at
`planning.py:1679-1682`):

- `_active_twig_cv`, `_active_provider_cv`, `_active_gate_handler_cv`,
  `_active_process_config_cv`.
- `build_engine` resolves each seam as **explicit arg → contextvar → demo
  fallback**, then `.set()`s the contextvars so a recursively-spawned child
  engine *constructed in the same asyncio task* inherits the live seams.
- Recorded inputs (and the `start_run` snapshot) remain authoritative across a
  restart (INV-RESTART); the contextvar is **only** a convenience for in-process
  construction, never a correctness dependency on resume.

This pattern is sound and tested for planning. B1 is fundamentally *"generalise
this so every dispatched child — `implementation` first — consumes the same
seam, instead of each workflow reinventing it or silently faking."*

## Decision (proposed)

Promote the planning contextvar-seam idiom to a **shared kernel-level seam
module** and have child factories consume it.

### 1. A `requiem.seam` module (shared contextvars)

Move the four contextvars out of `planning.py` into `requiem/seam.py`:

```python
# requiem/seam.py
active_provider:     ContextVar[AgentProvider | None]
active_toolbelt:     ContextVar[Toolbelt | None]    # NEW vs planning (it splits twig out)
active_gate_handler: ContextVar[Any]
active_process_config: ContextVar[ProcessConfig | None]

@contextmanager
def install(*, provider=None, toolbelt=None, gate_handler=None, process_config=None):
    """Set the seams for the current task; restore prior tokens on exit."""
```

`planning.py` keeps its public names as thin re-exports for back-compat (its
twig-only contextvar becomes a `toolbelt`-derived accessor). The kernel installs
the seam **once** at the top of a run from the root engine's own
provider/toolbelt/gate_handler, so any in-process child sees them.

### 2. Child factories resolve seams uniformly

`implementation.build_engine` (and `full_sdlc`, and the eventual orchestrator)
change their fallback from "synthesize a demo" to:

```python
provider = provider or seam.active_provider.get()    # explicit arg → seam → ...
toolbelt = toolbelt or seam.active_toolbelt.get()
# demo synthesis ONLY when BOTH are absent AND an explicit `demo=True` is set
```

Crucially: **the canned demo must become opt-in** (`demo=True` / a dedicated
`build_demo_engine`), not the silent default. A dispatched child with no seam and
no demo flag should **fail closed to NeedsHuman/PermanentFailure**, never
fabricate a happy-path success. That single change kills the footgun even before
the orchestrator exists.

### 3. Restart safety is unchanged

The seam is in-process only. On resume, the kernel reconstructs children from
recorded inputs exactly as today; the seam contextvar is re-installed from the
resuming root engine before any child is rebuilt. No new event types, no log
schema change — INV-RESTART and INV-EVENT-LOG-AUTHORITATIVE are untouched. (This
mirrors the explicit guarantee planning already documents at planning.py:156-163.)

### 4. B2 and B3 are follow-ups, not part of this ADR

- **B2 (handoff≠failure):** once children run for real, a per-slot classifier on
  `child_final_node` (e.g. `end_handoff` → NeedsHuman, not Success) is needed so
  the orchestrator doesn't treat a tests-failed handoff as done. `end_handoff` is
  `disposition="completed"` today (implementation.py:1057). Separate change.
- **B3 (branch model):** `implementation` hard-codes `feature/{inputs.item_id}`
  (implementation.py:394); reconciling with ADR-0006's `feature/<root>` +
  `impl/<root>-<item>` is a scope decision (Option-B stopgap vs Option-D). The
  external executor already owns the Option-D shape via worktrees; the in-process
  path needs the same. Separate change.

Recommended order stays as the audit states: **B1 (this ADR) → B3 → B2 +
executor**.

## Implementation notes (what actually shipped, 2026-06-09)

Shipped the **non-breaking variant** of §1–§2:

- **`requiem/seam.py`** — the shared seam module: `active_provider` /
  `active_toolbelt` / `active_gate_handler` contextvars, plus `set_seams(...)`
  (task-lifetime install, non-None-overwrites-only) and an `install(...)`
  context manager (scoped, restores prior tokens). Light imports (TYPE_CHECKING
  only) so the kernel can import it without a cycle.
- **Kernel** — `_invoke_subworkflow` calls `_seam.set_seams(provider=self.provider,
  toolbelt=self.toolbelt, gate_handler=self.gate_handler)` immediately before
  constructing the child engine. In-process only; recorded inputs stay
  authoritative on resume (INV-RESTART) — no log-schema change.
- **`implementation.build_engine`** — resolves each seam as **explicit arg →
  active seam → demo fallback**. This is the key softening vs. the original
  proposal: rather than *remove* the silent demo (a hard break for every demo
  caller), the demo stays as the final fallback, so a bare call with no seam
  installed is byte-for-byte unchanged. The footgun closes because a *dispatched*
  child now finds the parent's real seam *before* reaching the demo fallback.
- **`demo=True` opt-in** — `build_engine(..., demo=True)` forces the demo seams
  and never consults the contextvar, for harnesses that must stay hermetic even
  under a real-seam parent. The `full_sdlc` implementation shim (a deliberate
  self-contained demo) passes `demo=True` so it keeps faking gh/twig instead of
  inheriting the orchestrator's `Toolbelt.real()`. This replaces the original
  "make the demo opt-in everywhere" break with a targeted, opt-in flag.
- **Tests** — `tests/test_seam_propagation.py` (8): the seam module
  (get/set/install-restore), `build_engine` resolution (no-seam→demo,
  seam-inherited, explicit-wins, `demo=True`-forces-demo), and an end-to-end
  kernel test (a parent carrying a sentinel provider dispatches a child that
  reads the seam). Full planning/subworkflow/implementation/full_sdlc/kernel/
  resume suites stay green.

**Deferred:** consolidating `planning.py`'s own (older, separate) seam contextvars
into `requiem.seam` — planning works and is tested; folding it in is a follow-up
to avoid widening this change. B2 (handoff classifier) and B3 (branch model)
remain as noted.

## Consequences

**Positive:** kills the silent-success footgun immediately (the opt-in-demo
change alone); gives `full_sdlc` and a future in-process orchestrator a real,
single-process dispatch path (#4); is a prerequisite for in-process worktree
isolation (#5); reuses a pattern already proven for planning rather than
inventing a mechanism; no log-schema or restart-semantics change.

**Negative / open:** contextvars couple correctness to "child constructed in the
same asyncio task as the seam install" — fine for the current sequential
sub-workflow topology (planning.py:135-137, `MAX_CHILDREN`, no `parallel_fork`
yet), but **a true parallel fork would need the seam captured per-branch** (each
forked task re-`install`s from a captured snapshot). Called out now so the
parallel-dispatch work (#5) designs for it. Also: making the demo opt-in is a
**behaviour change** for any current caller relying on the silent demo fallback —
those call sites (CLI demo paths, some tests) must pass `demo=True` explicitly;
this is a deliberate, surfaced break, not a silent one.

**Why an ADR:** B1 is the named critical-path blocker; the choice to generalise
the planning contextvar seam (vs. e.g. threading seams through `inputs_summary`,
which would break INV-EVENT-LOG-AUTHORITATIVE, or a global singleton, which
breaks test isolation) and the decision to make the canned demo opt-in are
load-bearing and deserve review **before** code lands. This ADR is the
design-doc-first checkpoint.
