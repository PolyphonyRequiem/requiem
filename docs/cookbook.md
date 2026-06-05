# Cookbook

Short answers to "how do I X?" Each recipe is problem → solution
snippet → why it works.

## How do I retry on a transient failure?

**Problem.** Your verb hit a flaky resource — a network blip, an OOM
linter, a 429 from an API. You want the engine to try again.

**Solution.** Return `RetryableFailure` and set `retry_max` on the node.

```python
@verbs.register("flaky_lint")
def _flaky(ctx):
    if ctx.attempt < 2:
        return RetryableFailure(
            retry_key=f"{ctx.run_id}:flaky_lint",
            error_kind="lint.transient",
            message="linter exited 137 (OOM)",
            attempt=ctx.attempt,
        )
    return Success(value={"lint_passed": True})
```

```python
.script("flaky_lint", verb="flaky_lint", retry_max=2)
    .edge("flaky_lint", on="success",          to="next")
    .edge("flaky_lint", on="retry_exhausted",  to="fail_end")
```

**Why it works.** The engine owns the retry loop. Your verb stays
idempotent because the retry decision lives in the kernel, not in the
verb. `retry_key` makes the retry attributable; `attempt` is passed
through `ctx.attempt`. When the budget runs out, the engine routes
`retry_exhausted` instead of `retryable_failure` so you can branch on
"we tried and it kept failing."

## How do I stop on a permanent error?

**Problem.** Your verb hit a condition where retrying won't help — a
missing file, a 404 with a clear "this resource is gone," a contract
violation.

**Solution.** Return `PermanentFailure` and wire an edge for it.

```python
case FileMissing(path=p):
    return PermanentFailure(
        error_kind="snippet.missing",
        message=f"snippet not found at {p}",
    )
```

```python
.script("read_snippet", verb="read_snippet")
    .edge("read_snippet", on="success",            to="next")
    .edge("read_snippet", on="permanent_failure",  to="fail_end")
```

**Why it works.** `PermanentFailure` bypasses the retry loop entirely.
The engine routes on the variant tag. `error_kind` is yours — pick a
short dotted string and reuse it; the closed-enum policy is described in
[ADR 0004 §4.2](decisions/0004-cross-cutting-defaults.md).

## How do I ask a human?

**Problem.** A workflow step needs an operator decision — "approve this
verdict," "pick a remediation strategy," "should I retry or escalate?"

**Solution.** Use a `human_gate` node.

```python
.human_gate(
    "approve_merge",
    prompt="Three reviewers ran; verdict says merge. Approve?",
    options=["approve", "reject", "needs_changes"],
)
    .edge("approve_merge", on="needs_human:approve",       to="merge")
    .edge("approve_merge", on="needs_human:reject",        to="abort")
    .edge("approve_merge", on="needs_human:needs_changes", to="revise")
```

You can *also* surface a gate from inside a verb by returning
`NeedsHuman` — useful when the need-for-human is discovered dynamically
rather than declared topologically.

**Why it works.** Gates make the engine suspend (exit code `2`) and
record the prompt + options in the event log. `requiem resume` reads the
operator's choice and routes on `needs_human:<choice>`. The run survives
laptop sleep, browser-tab close, anything — the gate state is in the log.

## How do I run agents in parallel?

**Problem.** You want N reviewers, planners, or specialists to read the
same input independently and you don't want to serialise them.

**Solution.** `parallel_fork` via `.team(...)`.

```python
.team(
    "review_team",
    team_id="reviewers",
    branches=[
        ("style_reviewer",        "review_prompt"),
        ("correctness_reviewer",  "review_prompt"),
        ("performance_reviewer",  "review_prompt"),
    ],
)
    .edge("review_team", on="success",            to="synthesize")
    .edge("review_team", on="permanent_failure",  to="fail_end")
```

**Why it works.** The kernel awaits every branch concurrently. The
team's `Success.value['findings']` is a list of
`{"agent": name, "result": outcome_dict}`; your next node (typically a
synthesiser agent) reads that list and produces a single verdict. Each
branch's events stream into the log with a `team_id` + `agent_id` so the
renderer can interleave them sanely. See [ADR
0003](decisions/0003-agent-teams-first-class.md) for the rationale.

## How do I make a workflow resumable?

**Problem.** You want crash-safe runs. Laptop sleeps, the LLM provider
500s, you `Ctrl+C` because you spotted a problem — and you don't want
to redo work.

**Solution.** Nothing. Your workflow is already resumable.

```powershell
requiem run    my.workflow --run-id my-test
# ... crash, ctrl+c, or laptop sleep ...
requiem resume my.workflow my-test
```

**Why it works.** Every state transition is appended to
`<run_id>.events.jsonl` *before* the engine moves forward. Resume folds
the log into a cursor, replays the narration, and starts execution
exactly where the log ends. Committed nodes are not re-executed. This
is invariant `INV-RESTART` in [`north-star.md`](north-star.md), and the
reason the event log is authoritative
(`INV-EVENT-LOG-AUTHORITATIVE`).

The one obligation on you: verbs must be idempotent or refuse to start.
If a verb writes a file, it should either be safe to write the same file
twice, or it should check before writing. The toolbelt clients
(`requiem.toolbelt`) help with this.

## How do I handle bad LLM output?

**Problem.** An agent returns something that doesn't match the response
model — wrong schema, missing field, hallucinated value.

**Solution.** Wire a `bad_output` edge. The engine routes there
*instead of* retrying the network call.

```python
.agent("synthesize", agent="synthesizer", prompt_verb="synth_prompt")
    .edge("synthesize", on="success",     to="human_gate")
    .edge("synthesize", on="bad_output",  to="reprompt_with_errors")  # remediation
    # ... or fall through to fail_end by not wiring bad_output ...
```

**Why it works.** `BadOutput` is a distinct outcome from
`PermanentFailure` specifically so it doesn't trigger a network retry
(the LLM will almost certainly do the same thing again with the same
prompt). The variant carries `validation_errors` so your remediation
branch can feed them back into a re-prompt. If you don't wire a
`bad_output` edge, the engine falls through to `permanent_failure` — so
forgetting to wire it is safe, not silent.

## How do I dispatch real implementation work to an external executor?

**Problem.** Planning produced a tree of work items. You want each
implementable leaf actually *built* — by a real agent that writes code,
pushes a branch, and opens a PR — not by an in-process fake.

**Solution.** Use `requiem.workflows.kanban_executor`. It creates one
Hermes kanban task per leaf and lets a real Hermes worker deliver it.

```powershell
# Dry run (default): plan the tasks on a real board, spawn nothing.
requiem run requiem.workflows.kanban_executor   # key-free in-process demo

# End-to-end against a real ADO item: plan → seed children → dispatch workers.
python -m requiem.end_to_end --item 12345 --board requiem-12345 \
    --assignee my-coder-profile --commit --live
```

**Why it works.** Dispatching to an *external* executor sidesteps the
in-process fan-out blocker (ADR-0013 §B1: a dispatched sub-workflow can't
receive a real provider/toolbelt and silently falls back to fakes). Hermes
brings its own real provider/toolbelt; Requiem just orchestrates. The driver
runs planning → `commit_plan` → `kanban_executor` as sequential top-level
engines. Implementable leaves are read from the **committed plan** (every
`decomposable == False` node, depth-first, type-agnostically — planning decided
the facet, not the ADO type), mapped to real ADO ids via the seed manifest's
`id_map`. An *atomic* root (planning says it's already a leaf) is dispatched as
itself. Each leaf's task carries a stable `requiem:{root}:{leaf}` idempotency
key (fresh runs reuse tasks, not duplicate), tasks are created unassigned →
linked → released (no create→claim race), and a leaf only counts as *delivered*
when its worker run is `completed` **and** recorded a result. `--commit` and
`--live` default off, so the safe default plans without seeding or spawning.
See ADR-0014.


## How do I see what happened, after the fact?

```powershell
# Human English replay — same lines `requiem run` printed.
requiem events <run_id> --workflow my.workflow.module

# Raw JSONL — for jq, CI, your own tooling.
requiem events <run_id> --raw
```

The log lives at `.runs/<run_id>.events.jsonl` (override with
`--log-dir`). Everything `requiem` does is a projection of that file.

## How do I run a workflow unattended (no human at the gate)?

**Problem.** A nightly CI job; a demo recording; a long batch run.
There's nobody to answer the gate.

**Solution.** Pass a `gate_handler` to `build_engine` that picks an
answer programmatically.

```python
def _auto_approve(node_id, prompt, options):
    return "approve"
_auto_approve.__requiem_auto__ = True   # so the renderer shows "(auto-approved for demo)"

def build_engine(log_dir, *, gate_handler=None):
    return Engine(
        # ...
        gate_handler=gate_handler or _auto_approve,
    )
```

**Why it works.** The kernel either suspends the run (no handler →
`Suspended` result, exit code `2`) or invokes the handler synchronously
and continues. The `__requiem_auto__` attribute is read by the renderer
so the operator can tell at a glance which gates a human actually
clicked.

## See also

- [`concepts.md`](concepts.md) — the vocabulary used in these recipes.
- [`writing-workflows.md`](writing-workflows.md) — the full walkthrough.
- [`north-star.md`](north-star.md) — invariants that make these recipes
  safe.

