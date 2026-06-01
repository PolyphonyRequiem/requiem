# Migrating from polyphony to Requiem (v0)

> Audience: an engineer who has been driving the polyphony + conductor
> dogfood pipeline for months and wants to cut over **this week**.
> Practical, not philosophical — concrete commands first, mental-model
> deltas second, surprises and known rough edges last.

If you want the *why*, read [`north-star.md`](north-star.md) and the
ADRs in [`decisions/`](decisions/). This document is the *how*.

---

## Table of contents

1. [What changes conceptually](#1-what-changes-conceptually)
2. [What carries over verbatim](#2-what-carries-over-verbatim)
3. [What's gone or different](#3-whats-gone-or-different)
4. [Step-by-step cutover for an existing polyphony repo](#4-step-by-step-cutover-for-an-existing-polyphony-repo)
5. [Behavioural deltas (the surprises)](#5-behavioural-deltas-the-surprises)
6. [What's NOT in v0 (and the workarounds)](#6-whats-not-in-v0-and-the-workarounds)
7. [Operational runbook](#7-operational-runbook)
8. [Known rough edges](#8-known-rough-edges)
9. [Glossary cross-reference](#9-glossary-cross-reference)

---

## 1. What changes conceptually

A polyphony+conductor run is two processes wired through a YAML
registry, a CLI verb table, a watermark file, and a dashboard websocket.
A Requiem run is **one Python process** that interprets a workflow
graph defined in Python and writes a single append-only JSONL log.

| Concept                       | Polyphony + conductor                                                  | Requiem v0                                                                 |
|-------------------------------|------------------------------------------------------------------------|----------------------------------------------------------------------------|
| Engine                        | Python `conductor` invoking `.conductor/registry/workflows/*.yaml`     | Python `requiem.kernel.Engine`, data-driven interpreter                    |
| Verbs                         | .NET `polyphony` CLI verbs, called as subprocesses at `script:` nodes  | `async def` Python functions decorated via `VerbRegistry.register`         |
| Workflow definition           | YAML in `.conductor/registry/workflows/`                               | Python module exposing `build_engine(log_dir)` / `build_workflow()`        |
| Authoritative state           | `seed-manifest.json` + ADO + git branches + conductor temp checkpoints | `{run_id}.events.jsonl` (INV-EVENT-LOG-AUTHORITATIVE) + tiny manifest      |
| Verb success/failure contract | Exit-code 5-class taxonomy (0/2/3/4/5) + JSON envelope                 | Discriminated outcome union (see §1.2)                                     |
| Retry semantics               | YAML `retry:` + `retry_key:` + `_MISSING` sentinel across IPC          | `RetryableFailure` outcome; kernel owns the retry budget in-process        |
| Cancellation                  | `CONDUCTOR_CANCEL_TOKEN` sentinel file (Windows lacks SIGTERM)         | `cancel_requested` event in the log; `asyncio.CancelledError` in-process   |
| Observability                 | Multiple tools (`conductor trace`, dashboard, journal) parse JSONLs    | One log file; `requiem events` / `requiem list-runs` are projections       |
| Dashboard                     | Conductor web UI                                                       | **Not in v0** — `requiem events --follow` + `--raw` for JSONL              |
| Sub-workflows                 | `script:` calling out, or YAML `subgraph:`                             | `SubWorkflowNode` with **isolated** child log (INV-SUBWORKFLOW-LOG-ISOLATION) |

### 1.1 One process, no IPC

Verbs are Python function calls now. No subprocess fork at every
`script:` node, no exit-code negotiation, no AOT-safe JSON contract.
This is the load-bearing change — see ADR 0001
([`docs/decisions/0001-single-process-architecture.md`](decisions/0001-single-process-architecture.md))
for the rationale.

The only out-of-process invocations Requiem makes are genuine external
dependencies: `git`, `gh`, `twig`, the LLM provider, and OS tools. The
Polyphony+Conductor process split is gone by construction.

### 1.2 Discriminated outcomes replace the verb envelope

Polyphony verbs ended with `exit 0` and a JSON payload on stdout; the
conductor router keyed off the exit code and inspected the JSON for
`error` / `state` fields. Requiem verbs return one of six variants
defined in [`src/requiem/outcomes.py`](../src/requiem/outcomes.py):

```python
Outcome = Success | RetryableFailure | PermanentFailure | BadOutput | NeedsHuman | Cancelled
```

(Note: at the *engine* terminal level, an entire run resolves to one
of `Completed | Suspended | Failed` — see
[`src/requiem/kernel.py:66-89`](../src/requiem/kernel.py). The
discriminated *verb* outcomes above are the per-node contract.)

The kernel pattern-matches on the variant tag. There is no convention
of "exit 0 means success" or "look for `error` in JSON" — the variant
tag *is* the contract. See INV-DISCRIMINATED-OUTCOMES in
[`north-star.md`](north-star.md).

### 1.3 The event log is the truth

`{log_dir}/{run_id}.events.jsonl` is authoritative for everything
observable about a run (INV-EVENT-LOG-AUTHORITATIVE). The manifest,
the rendered CLI view, the resume cursor — all are projections of the
event log. Anything not in the event log did not happen.

Polyphony's `RunManifest` is gone as the source of truth. A small
manifest sidecar still exists (`{manifest_dir}/{run_id}.manifest.json`,
written by `root_dispatch.write_manifest` at
[`src/requiem/workflows/root_dispatch.py:540-610`](../src/requiem/workflows/root_dispatch.py))
but it's a *projection* the root-dispatch verb maintains for
human-readable inspection. Lose it, and you can rebuild it from the
event log.

### 1.4 Strict-stop on log corruption

A partial or unparseable JSON line anywhere in the log halts `replay()`
immediately with `CorruptLogError`
([`src/requiem/persistence.py:21-30, 56-58`](../src/requiem/persistence.py)).
No forward progress past a bad line.
INV-LOG-STRICT-STOP-ON-CORRUPTION is the strict reading of
INV-NO-CORRUPT-FORWARD applied to the substrate itself.

Polyphony's looser eventual-consistency posture (silently advancing
past a truncated trace line in some paths) is gone. Operationally:
if your laptop sleeps mid-write and the last line is half-flushed,
**the trailing partial line is dropped** (INV-PARTIAL-LINE-DROP), but
**corruption mid-stream halts replay** — you fix the log or restart
the run from scratch.

---

## 2. What carries over verbatim

You don't have to rebuild your environment.

### 2.1 Twig

Requiem's `TwigClient` still shells out to the polyphony `twig` binary.
Same env vars, same auth, same `.twig/config`, same `twig set` /
`twig sync` / `twig show` semantics. See
[`src/requiem/clients/twig.py:1-50`](../src/requiem/clients/twig.py).

```python
# From a verb in a Requiem workflow:
item = await ctx.toolbelt.twig.show_async(item_id)
await ctx.toolbelt.twig.comment(item_id, "Done.")
```

### 2.2 GitHub PR lifecycle

The `GhClient` wraps the `gh` CLI under the hood for read paths
(`pr_view`, `pr_search`, `api`) —
[`src/requiem/clients/gh.py:1-40`](../src/requiem/clients/gh.py). PR
mutations use a small filesystem-and-git helper
[`src/requiem/clients/fs.py`](../src/requiem/clients/fs.py) that runs
`git` against your worktree the same way polyphony did.

Active `gh` auth account matters the same way it did before — `gh auth
status` to confirm, `gh auth switch` to change. `GhAuthError` is
classified as `NeedsHuman` (never auto-retried), preserving polyphony's
behaviour but with explicit routing rather than implicit dashboard
escalation.

### 2.3 ADO topology

Work-item IDs, area paths, parent/child relationships — unchanged.
Requiem reads the same ADO state polyphony does, via the same `twig`
seam. No data migration. Your existing roots, plan trees, and
in-flight work items are addressable from Requiem without any export
step.

### 2.4 The mental model

The big shape — **root → planning → implementation → pr_lifecycle →
close_out** — survives. Requiem ships exactly that pipeline as
[`requiem.workflows.full_sdlc`](../src/requiem/workflows/full_sdlc.py)
(an end-to-end demo composing the five stages with `SubWorkflowNode`s).

The five shipped workflows are at
[`src/requiem/workflows/`](../src/requiem/workflows/):

| Module                         | Polyphony analog                          |
|--------------------------------|-------------------------------------------|
| `requiem.workflows.root_dispatch`   | `root-item-dispatch.yaml` + `init-root`   |
| `requiem.workflows.planning`        | `plan-level.yaml`                         |
| `requiem.workflows.implementation`  | `implement-merge-group.yaml`              |
| `requiem.workflows.pr_lifecycle`    | `github-pr.yaml` / `ado-pr.yaml`          |
| `requiem.workflows.close_out`       | `close-out.yaml`                          |
| `requiem.workflows.full_sdlc`       | `polyphony.yaml` (the root orchestrator)  |

---

## 3. What's gone or different

### 3.1 No YAML registry

There is no `.conductor/registry/` directory in Requiem. To add a
workflow, write a Python module under your own package (or under
`src/requiem/workflows/`) that exposes:

```python
def build_workflow() -> Workflow: ...
def build_engine(log_dir: Path) -> Engine: ...
def render_hints() -> dict: ...    # optional, drives CLI narration
```

`build_engine(log_dir)` is the only required hook for `requiem run`.
See [`src/requiem/workflows/code_review_demo.py`](../src/requiem/workflows/code_review_demo.py)
for the canonical shape, or
[`docs/writing-workflows.md`](writing-workflows.md) for the walkthrough.

### 3.2 No `polyphony state next-ready` shell-out

The engine drives itself. There is no out-of-process decision verb the
workflow asks "what's next?". Routing happens inside the kernel based
on the discriminated outcome returned by the verb that just ran.

A workflow that used to read like:

```yaml
# polyphony YAML (gone)
- id: pick_next
  script: polyphony state next-ready --root-id ${root_id}
  output: { ... }
- id: route
  on:
    "next.kind == plan":   plan_node
    "next.kind == impl":   impl_node
```

becomes:

```python
# requiem Python
@verbs.register("pick_next")
async def _pick_next(ctx):
    next_item = await pick_next_ready(ctx.toolbelt, root_id)
    if next_item is None:
        return PermanentFailure(error_kind="no_work", message="...")
    if next_item.kind == "plan":
        return Success(value={"kind": "plan", "id": next_item.id})
    return Success(value={"kind": "impl", "id": next_item.id})
```

…and the router edges key off `Success` / `PermanentFailure` plus a
small inspection of `value` in the verb that does the actual work next.
The branching idiom is "verbs return discriminated outcomes, edges
route on the variant tag".

### 3.3 No `polyphony policy load`

Policies that used to live as YAML files loaded at workflow start are
now **inline Python**. There is no separate policy-resolution step.
A workflow that needed a (domain, scope) → rule lookup expresses it
as a Python helper imported into the verb that uses it.

If you have a substantial policy table you don't want inlined, load it
yourself in a Python module at import time and treat it as a constant.

### 3.4 No dashboard websocket

There is no web UI in v0. Resume-from-JSONL replay is the recovery
model — see `tests/test_resume_fidelity_matrix.py` for the proof that
truncating the log at every event still produces a determinate
restart.

For live visibility during a run:

```pwsh
# Stream the run in plain English
requiem events <run_id> --follow

# Stream raw JSONL (for CI / your own tail)
requiem events <run_id> --follow --raw
```

For human gates: the kernel calls a Python `gate_handler` callable in
the same process — there is no browser prompt. The CLI's
`--interactive` flag wires an `input()`-driven handler so you can
respond from the terminal; otherwise a workflow's default handler
auto-resolves (used by the demos so they don't hang).

### 3.5 Sub-workflows isolate their event logs

A `SubWorkflowNode` (see ADR 0005
[`docs/decisions/0005-subworkflow-invocation-primitive.md`](decisions/0005-subworkflow-invocation-primitive.md))
writes the child's events to its **own** `{sub_run_id}.events.jsonl`
file. The parent's log carries only `subworkflow_started` /
`subworkflow_completed` / `subworkflow_cancelled` markers. This is
INV-SUBWORKFLOW-LOG-ISOLATION.

Polyphony's recursive planning (a planning workflow that calls a
planning workflow) had no analog of this isolation — child events
landed in the parent stream and the resume cursor could be confused
on deep recursion. In Requiem, even if a child's events bled into the
parent log by accident, `_reconstruct` filters by envelope `run_id`
and would not advance the parent's cursor.

---

## 4. Step-by-step cutover for an existing polyphony repo

A working assumption: you have an existing dogfood checkout
(`polyphony-squad-spike` or similar) with `.twig/config`, working
`gh auth`, and a recent ADO root item you've been driving.

### 4.1 Install Requiem

```pwsh
git clone https://github.com/PolyphonyRequiem/requiem.git
cd requiem
pip install -e .[llm,cli,test]
```

`[llm]` pulls Anthropic + OpenAI SDKs; `[cli]` adds `rich` for coloured
output; `[test]` adds pytest, asyncio plugin and `httpx` for the test
suite. Pick the subsets you need.

Confirm the install:

```pwsh
requiem --version
# requiem 0.0.1
```

### 4.2 Provide an LLM provider

Requiem's `default_provider()` resolves Anthropic first, then OpenAI
([`src/requiem/providers/__init__.py:44-65`](../src/requiem/providers/__init__.py)):

```pwsh
$env:ANTHROPIC_API_KEY = "sk-ant-..."
# OR
$env:OPENAI_API_KEY = "sk-..."
```

The kernel owns the retry budget; both providers are constructed with
`max_retries=0` so transient failures surface as `RetryableFailure`
rather than being silently swallowed by the SDK.

For dry runs / tests, use `FakeProvider` — every shipped workflow
accepts a `provider=` kwarg that defaults to a scripted fake when the
canned happy-path is good enough.

### 4.3 Keep your existing twig setup

Don't touch `.twig/config`. Requiem's `TwigClient` shells out to the
same `twig` binary you've been using; no migration needed.

```pwsh
twig set 12345         # same as before
twig sync              # same as before
twig show 12345        # same as before
```

### 4.4 Pick your entry point

For the full SDLC pipeline (the polyphony.yaml analog):

```pwsh
requiem run requiem.workflows.full_sdlc --run-id root-12345-demo
```

This builds the `FullSdlcInputs` defaults (item_id=12345, dry_run=True)
and runs all five stages with FakeProvider. For a real run against
ADO + GitHub:

```python
# in your own script / module:
from pathlib import Path
from requiem.workflows import full_sdlc
import asyncio

inputs = full_sdlc.FullSdlcInputs(
    item_id=3311,
    repo="PolyphonyRequiem/polyphony",
    repo_path=Path(r"C:\Users\me\projects\polyphony-squad-spike"),
    base_branch="main",
    dry_run=False,
)
engine = full_sdlc.build_engine(Path(".runs"), inputs=inputs)
asyncio.run(engine.run("root-3311-2026-06-10"))
```

For just the root-dispatch leg (write the manifest, optionally trigger
planning):

```python
from requiem.workflows import root_dispatch

inputs = root_dispatch.RootDispatchInputs(
    item_id=3311,
    repo="PolyphonyRequiem/polyphony",
    repo_path=Path.cwd(),
    base_branch="main",
    dry_run=False,
    auto_plan=True,
)
engine = root_dispatch.build_engine(Path(".runs"), inputs=inputs)
```

### 4.5 Map your existing ADO root

The mapping is direct. Whatever number you were passing to polyphony's
`init-root` (`--root-id 12345`) goes into `RootDispatchInputs.item_id`
or `FullSdlcInputs.item_id`. The deterministic run id Requiem computes
is `root-{item_id}-{YYYY-MM-DD}` (see
[`src/requiem/workflows/root_dispatch.py`](../src/requiem/workflows/root_dispatch.py)
`compute_run_id`); a run with the same id is **idempotent** — it
re-uses an existing manifest if one matches.

### 4.6 Run; consult the JSONL log

```pwsh
requiem run requiem.workflows.full_sdlc --run-id root-3311-2026-06-10 --log-dir .runs
```

What you see:

```
requiem run — requiem.workflows.full_sdlc  (run_id=root-3311-2026-06-10)
log: .runs/root-3311-2026-06-10.events.jsonl
────────────────────────────────────────────────────────────────────────
▶ run_started — full-sdlc on AB#3311
... stage-level narration ...
■ Completed — full-sdlc on AB#3311
────────────────────────────────────────────────────────────────────────
```

To replay / inspect later:

```pwsh
requiem events root-3311-2026-06-10               # rendered English
requiem events root-3311-2026-06-10 --raw         # raw JSONL
requiem events root-3311-2026-06-10 --follow      # tail live
requiem list-runs                                 # everything under .runs/
```

To resume after a crash, kill, or `Ctrl+C`:

```pwsh
requiem resume requiem.workflows.full_sdlc root-3311-2026-06-10
```

Same `run_id`, same `log_dir`. The kernel folds the log into a resume
cursor and picks up exactly where the last committed verb ended.

---

## 5. Behavioural deltas (the surprises)

These will bite you if you don't internalise them.

### 5.1 Verbs are async Python; CPU-heavy work is your problem

Verbs run as `async def` functions in the engine's event loop. A
synchronous CPU-bound loop in a verb **blocks the whole engine**,
including the cancel-signal poll. Polyphony's per-verb subprocess
absorbed this naturally because the subprocess was its own scheduling
unit; in Requiem you don't get that for free.

Guideline: wrap CPU-heavy work in `asyncio.to_thread(...)` or split
the workflow into smaller verbs that yield. Verbs that fan out tools
should `await` them, not spin on `subprocess.run`.

### 5.2 A crashed verb is NOT auto-retried unless it says so

Polyphony's "retry on transient" behaviour was partly engine policy and
partly a side effect of the verb-envelope contract. In Requiem there
is exactly one rule: if a verb returns `RetryableFailure`, the kernel
retries within the node's `retry_max` budget; anything else (including
an uncaught Python exception, which is wrapped into `PermanentFailure`)
does not retry.

To preserve a polyphony "transient on 5xx" behaviour:

```python
@verbs.register("call_some_api")
async def _call(ctx):
    try:
        return Success(value=await ctx.toolbelt.gh.api(...))
    except GhServerError as e:
        return RetryableFailure(
            retry_key="call_some_api",
            error_kind="provider_unavailable",
            message=str(e),
            after=30.0,
        )
```

(The provider clients already do this for HTTP-level transience — see
the per-client table in
[`src/requiem/clients/gh.py:16-24`](../src/requiem/clients/gh.py) and
[`src/requiem/clients/twig.py:16-23`](../src/requiem/clients/twig.py).)

### 5.3 Gate handlers are Python callables, not dashboard prompts

A workflow with a `human_gate` node calls `engine.gate_handler` when
it suspends. There are three answers in v0:

* **Auto handler.** The workflow ships a `default_gate_handler` that
  picks a safe default (typically `abort` for irreversible actions —
  see `full_sdlc.py`'s docstring). Used by the demos and CI.
* **`--interactive`.** Pass `requiem run ... --interactive` and the
  CLI prompts at every gate. See
  [`src/requiem/cli/main.py:521-560`](../src/requiem/cli/main.py).
* **No handler.** The engine returns `Suspended(node_id, prompt,
  options)` and exits with code 2. You `requiem resume <module>
  <run_id> --interactive` later to answer.

For programmatic callers (tests, your own scripts), assign
`engine.gate_handler = lambda node_id, prompt, options: "approve"`
before `engine.run(...)`.

### 5.4 Cancellation short-circuits retry

INV-CANCEL-SHORT-CIRCUITS-RETRY: a `Cancelled` outcome aborts the
retry loop immediately, regardless of remaining `retry_max`. Polyphony
had a softer eventual-consistency posture here; in Requiem, a 24-hour
deadline cancel **never** triggers another 24-hour attempt.

Operationally:

```pwsh
# Cancel a stuck/suspended run
requiem cancel <run_id> --reason "operator changed their mind"

# The cancel_requested event lands in the log. On the next loop
# tick (in-process) or the next `requiem resume` (out-of-process),
# the run short-circuits. No further retries are attempted.
```

A resumed cancelled run is byte-idempotent — it does not append
another `run_completed` to the log (INV-CANCEL-RESUME-IDEMPOTENT,
pinned by `tests/test_resume_fidelity.py::test_m4_cancel_mid_flight_short_circuits`).

### 5.5 Log strict-stop, partial line drop

Two rules, both in [`src/requiem/persistence.py`](../src/requiem/persistence.py):

* A **trailing partial line** (e.g. the last write was half-flushed
  when the laptop slept) is silently dropped on the next `replay()` —
  INV-PARTIAL-LINE-DROP.
* A **garbled line mid-stream** raises `CorruptLogError` with the byte
  offset and refuses to project past it — INV-LOG-STRICT-STOP-ON-CORRUPTION.

If you hit the second case you have to decide: truncate the file at
the bad offset (effectively "abort the run at the last good event")
and re-run, or repair the bad line by hand. The CLI surfaces this as
a recoverable verdict; it does not silently elide bytes.

---

## 6. What's NOT in v0 (and the workarounds)

Out-of-scope items from [`north-star.md`](north-star.md) §5 plus a
handful of deliberate Phase-D deferrals.

### 6.1 No web dashboard

**Workaround:** tail the JSONL log with `requiem events <run_id>
--follow`, or use `--raw` and pipe to your own tool. `requiem
list-runs` gives you a single-screen table of recent runs with status,
duration, and event count.

### 6.2 No multi-root parallel dispatch

The kernel runs one root per process. Polyphony's per-item worktree
fan-out is not reproduced at the engine level.

**Workaround:** orchestrate at the shell level — one
`requiem run requiem.workflows.full_sdlc --run-id root-N-...` per
worktree, each with its own `--log-dir`. The kernel's same-volume
atomic-write guarantees on `os.replace` still hold inside each run.

### 6.3 No sub-workflow path-coverage harness

The polyphony test harness (path-coverage scenarios driven by a Python
FakeProvider + a .NET shim binary) is gone, because the .NET shim is
gone with it.

**Workaround:** end-to-end tests in
[`tests/test_full_sdlc.py`](../tests/test_full_sdlc.py) — 12 tests
covering the happy path, per-stage failure suspension, mid-pipeline
restart (INV-RESTART), sub-workflow log isolation, and dry-run
mutation hygiene. Run with:

```pwsh
.\..\requiem-promote\.venv\Scripts\python.exe -m pytest tests/test_full_sdlc.py -q
# 12 passed
```

For a single sub-workflow, write tests against its `build_engine` and
inject fakes via the `toolbelt=` / `provider=` / `verbs=` kwargs each
workflow exposes (see e.g.
[`tests/test_planning_workflow.py`](../tests/test_planning_workflow.py),
[`tests/test_pr_lifecycle_workflow.py`](../tests/test_pr_lifecycle_workflow.py)).

### 6.4 No `polyphony reconcile`-style verb

The reconcile-an-incomplete-state pattern lives in receipts, not in a
dedicated verb. If you need polyphony's reconcile behaviour, it's
your job to write a verb that inspects the artifacts (the `receipts:
tuple[Receipt, ...]` peer field on every outcome — ADR 0004 §4.4) and
returns `NeedsHuman` if the state cannot be safely advanced.

### 6.5 No GitHub Issues integration

Per north-star §4, GH Issues is **explicitly out of scope**. Domain
signals route through other channels (the log, the CLI, future UI /
Hermes integrations). If your polyphony workflow posted to GH Issues,
that path has no v0 successor — file the equivalent in ADO via `twig`
or surface via a NeedsHuman gate.

---

## 7. Operational runbook

### 7.1 Log location

Default `--log-dir` is `.runs/` relative to the cwd. Override per
invocation:

```pwsh
requiem run requiem.workflows.full_sdlc --run-id myrun --log-dir D:\runs
```

Per-run file: `{log_dir}/{run_id}.events.jsonl`. Per-sub-run file:
`{log_dir}/{sub_run_id}.events.jsonl` (siblings of the parent — see
INV-SUBWORKFLOW-LOG-ISOLATION).

### 7.2 Log format

Append-only JSONL. One event per line. Envelope:

```json
{"event_id": 7, "run_id": "root-3311-...", "ts": "2026-06-10T09:42:11Z",
 "kind": "verb_completed", "node_id": "fetch_item",
 "payload": {"outcome": {...}, "attempt": 1}}
```

The closed `kind` taxonomy ships in the engine — `run_started`,
`node_entered`, `verb_completed`, `route_taken`, `retry_attempted`,
`gate_opened`, `gate_resolved`, `cancel_requested`,
`subworkflow_started`, `subworkflow_completed`, `run_completed`, etc.

Domain signals (the polyphony-style 20-signal catalogue: `seeded`,
`planned`, `implemented`, `merged`, etc.) ride on the same stream
with `kind="domain_signal"` per ADR 0004 §4.3. Two lenses, one file,
one fsync watermark.

### 7.3 Manifest

`root_dispatch` writes a small JSON sidecar at
`{manifest_dir}/{run_id}.manifest.json`. Default `manifest_dir` is
`log_dir / .runs`. The manifest is a *projection* — the event log
remains authoritative. The verb is idempotent: an existing matching
manifest is reused, not overwritten
([`src/requiem/workflows/root_dispatch.py:540-610`](../src/requiem/workflows/root_dispatch.py)).

### 7.4 Replay command

Render the run in customer English:

```pwsh
requiem events <run_id> [--log-dir DIR] [--workflow MOD]
```

`--workflow` is only needed if the run was written before the
`workflow_module` field was added to `run_started` (Gap 1 in the CLI
docs) — new runs auto-load the module.

Raw JSONL for CI:

```pwsh
requiem events <run_id> --raw
```

Tail a live or recent run:

```pwsh
requiem events <run_id> --follow
```

### 7.5 Resume after crash

```pwsh
requiem resume <workflow_module> <run_id> [--log-dir DIR]
```

Same `workflow_module` and `run_id` as the original `requiem run`.
The CLI replays prior events for context, then streams continuation.
Idempotent: re-resuming a terminal run does not extend the log.

### 7.6 Abort a run

Two options:

* **In-process operator**: `Ctrl+C` raises `asyncio.CancelledError`;
  the kernel converts to `Cancelled(cause="operator", ...)` and emits
  a clean `run_completed` with `terminal="cancelled"`. CLI exit code
  130.

* **Out-of-process operator**: `requiem cancel <run_id> --reason "..."`
  writes a `cancel_requested` event into the log. The run short-circuits
  on its next loop tick (if running) or on the next `requiem resume`
  (if killed). INV-CANCEL-SHORT-CIRCUITS-RETRY guarantees no further
  retries.

* **Suspended at a gate**: the gate handler returns `"abort"` (or
  whatever option the workflow exposes as the abort path). The
  workflow routes to its `abort` terminate node. There is no separate
  "kill the gate" command — drive the gate.

---

## 8. Known rough edges

These are filed as open issues in the requiem repo. Each will land a
fix in a follow-up; in the meantime, the workarounds are explicit.

### 8.1 close_out terminate disposition contradicts verdict card ([#29](https://github.com/PolyphonyRequiem/requiem/issues/29))

Running `close_out` against a real ADO item with no Acceptance Criteria
children shows the CLI topline as **■ Failed** while the verdict card
correctly says **🚦 Needs human** — same run, contradictory signals.
Root cause: the `end_human` terminate node hard-codes
`disposition='failed'` and the kernel's terminate-node dispositions
are taken from the node literally rather than derived from the outcome.

**Workaround:** trust the verdict card, not the topline. Operationally
harmless — the gate-resolution path still drives the right behaviour;
only the cosmetic "what happened?" line is misleading.

### 8.2 close_out can't auto-link PRs from real twig JSON ([#30](https://github.com/PolyphonyRequiem/requiem/issues/30))

`close_out._extract_linked_prs` always returns `[]` against real
`twig show --output json` because real twig JSON has no `pullRequests`
field — verified against ADO items 3311, 3312, 3313, 3314, 3315 in the
Tchaikovsky bug-bash. Workflow still handles the empty list gracefully
(escalates to a `pr-not-linked` NeedsHuman gate), but ergonomics are
poor.

**Workaround:** always pass `--pr N` explicitly when invoking
close_out against a real ADO item. A follow-up will either add a
`gh pr list --search "AB#<item_id>"` fallback or file a twig
enhancement to surface PR link relations.

### 8.3 planning crashes route to `route.missing` with no narrative ([#31](https://github.com/PolyphonyRequiem/requiem/issues/31))

A `verb.crash` in any `planning` verb strands the run with
`Failed(error_kind='route.missing')` and **no verdict-card narrative**
— the operator sees a routing dead-end rather than a diagnostic
message. Comparison: `close_out` wires catch-all `permanent_failure`
edges from every verb and renders cleanly on crash; `planning` does
not.

**Workaround:** if you hit `route.missing` from planning, inspect the
raw log (`requiem events <run_id> --raw | Select-String verb_completed`)
to find the actual crash. A follow-up adds the catch-all edges so
crashes narrate properly.

---

## 9. Glossary cross-reference

| Polyphony / Conductor                    | Requiem v0                                                       | Notes                                                                          |
|------------------------------------------|------------------------------------------------------------------|--------------------------------------------------------------------------------|
| `RunManifest` (`seed-manifest.json`)     | `{log_dir}/{run_id}.events.jsonl` + small `{run_id}.manifest.json` | Event log is authoritative (INV-EVENT-LOG-AUTHORITATIVE). Manifest is a projection. |
| Verb envelope (exit code + JSON)         | Discriminated `Outcome` union                                    | See [`src/requiem/outcomes.py`](../src/requiem/outcomes.py); 6 variants.       |
| Conductor YAML node (`script:`, `agent:`) | `Workflow.script()` / `.agent()` / `.team()` / `.human_gate()` / `.subworkflow()` / `.terminate()` builder calls | See [`src/requiem/dsl.py`](../src/requiem/dsl.py).                             |
| `polyphony state next-ready` shell-out   | In-process Python verb                                           | Engine drives itself; no out-of-process decision step.                         |
| `polyphony policy load`                  | Inline Python helpers                                            | No separate policy resolution layer.                                           |
| Sub-workflow (YAML subgraph)             | `SubWorkflowNode` with isolated log                              | ADR 0005; INV-SUBWORKFLOW-LOG-ISOLATION.                                       |
| `CONDUCTOR_CANCEL_TOKEN` sentinel file   | `cancel_requested` event in the log                              | `requiem cancel <run_id>` writes it; `asyncio.CancelledError` propagates in-process. |
| Conductor web dashboard                  | `requiem events --follow` + `requiem list-runs`                  | No web UI in v0.                                                               |
| Gate prompt in dashboard / TTY           | Python `gate_handler` callable                                   | `--interactive` wires an `input()` handler.                                    |
| `polyphony.yaml` (root orchestrator)     | `requiem.workflows.full_sdlc`                                    | Same five-stage shape: dispatch → plan → implement → pr_lifecycle → close_out. |
| `init-root` verb                         | `requiem.workflows.root_dispatch`                                | Writes the manifest; optionally spawns planning.                               |
| `plan-level.yaml`                        | `requiem.workflows.planning`                                     | Recursive planning via `SubWorkflowNode`.                                      |
| `implement-merge-group.yaml`             | `requiem.workflows.implementation`                               | MG / impl branch lifecycle.                                                    |
| `github-pr.yaml` / `ado-pr.yaml`         | `requiem.workflows.pr_lifecycle`                                 | One workflow, platform abstracted via the toolbelt seam.                       |
| `close-out.yaml`                         | `requiem.workflows.close_out`                                    | Same shape; see §8 for known rough edges.                                      |
| `Invoke-PolyphonySdlc.ps1` launcher      | `requiem run <module> --run-id <id>`                             | Plain console script; no launcher needed.                                      |
| `conductor trace` / journal / dashboard  | `requiem events <run_id>` (rendered) / `--raw` (JSONL)           | One stream, two lenses (ADR 0004 §4.3).                                        |
| `_MISSING` sentinel + cross-IPC retry    | `RetryableFailure.retry_key` + in-process retry budget           | Kernel owns the budget; no cross-process state.                                |
| Polyphony 5-class exit codes (0/2/3/4/5) | Discriminated outcomes (in-process); 5-class codes survive only at external-script seam | Per north-star §4; honoured when invoking external scripts that already implement the contract. |
| Polyphony harness (`tests/harness/`)     | `tests/test_*_workflow.py` + `tests/test_full_sdlc.py`           | End-to-end Python tests; no .NET shim required.                                |
| `FakeProvider` (polyphony harness)       | `requiem.agent.FakeProvider`                                     | Same idea; one scripted LLM provider per test.                                 |
| `twig` CLI                               | `requiem.clients.twig.TwigClient`                                | Same binary, same env, same auth.                                              |
| `gh` CLI                                 | `requiem.clients.gh.GhClient`                                    | Same binary, same auth. Read paths in v0; mutations via `FilesystemClient`.    |

---

## Appendix A — Quick command reference

```pwsh
# Install
pip install -e .[llm,cli,test]

# Run a workflow
requiem run requiem.workflows.full_sdlc --run-id root-3311-2026-06-10

# Resume
requiem resume requiem.workflows.full_sdlc root-3311-2026-06-10

# Inspect
requiem events root-3311-2026-06-10              # rendered English
requiem events root-3311-2026-06-10 --raw        # raw JSONL
requiem events root-3311-2026-06-10 --follow     # tail live
requiem list-runs                                # all runs under .runs/
requiem describe requiem.workflows.full_sdlc     # workflow topology

# Cancel
requiem cancel root-3311-2026-06-10 --reason "..."

# Interactive gate handling
requiem run requiem.workflows.full_sdlc --interactive
```

## Appendix B — Verifying your install

```pwsh
# From the requiem checkout:
.\..\requiem-promote\.venv\Scripts\python.exe -m pytest tests/test_full_sdlc.py -q
# Expect: 12 passed
```

If you see 12/12 green, the full-SDLC pipeline (dispatch → plan →
implement → pr_lifecycle → close_out, all five sub-workflows composed
via `SubWorkflowNode` with isolated logs) is wired correctly on your
box. Move on to a real run.
