# Concepts

The vocabulary you need to write Requiem workflows. Every concept gets a
one-sentence definition and a snippet from
[`code_review_demo.py`](../src/requiem/workflows/code_review_demo.py).

## Workflow

A **workflow** is a graph of nodes with a single entry point and at
least one terminator. You build it with a fluent builder; the engine
interprets it as pure data.

```python
return (
    WorkflowBuilder("code-review")
        .entry("start")
        .script("start", verb="start_run")
            .edge("start", on="success", to="read_snippet")
        # ... more nodes and edges ...
        .terminate("end", disposition="completed")
        .build()
)
```

`.build()` validates topology immediately — typos in `to=` die at
construction, not at run time.

## Verb

A **verb** is a Python function that a node calls. It receives a
context, does some work, and returns an [Outcome](#outcome). Verbs are
registered by name so the workflow stays pure data.

```python
@verbs.register("read_snippet")
def _read(ctx):
    outcome = ctx.toolbelt.files.read_text(snippet_path)
    match outcome:
        case FileRead(content=text):
            return Success(value={"snippet": text, "loc": len(text.splitlines())})
        case FileMissing(path=p):
            return PermanentFailure(error_kind="snippet.missing",
                                    message=f"snippet not found at {p}")
```

## Outcome

Every verb returns one of six **outcome** variants. The engine routes on
the variant tag — your edge `on=` strings name the variants. You never
parse a JSON `error` field; the variant *is* the contract.

| Variant | Means | One-sentence example |
|---|---|---|
| `Success` | The verb did its job. | `read_snippet` read the file. |
| `RetryableFailure` | Transient — try again. | The linter spawned a process that OOMed; the engine retries within budget. |
| `PermanentFailure` | Non-retryable — stop or route to a fail branch. | The snippet path doesn't exist; retrying won't help. |
| `BadOutput` | An agent produced output that didn't validate against the response model. | The reviewer agent returned `{"severity": "??"}` instead of a valid `ReviewFinding`. |
| `NeedsHuman` | Pause and ask. | The workflow opens a gate asking "approve verdict?" before archiving. |
| `Cancelled` | Operator/deadline/parent killed the run. | You hit `Ctrl+C`; the retry loop short-circuits immediately. |

```python
# From the demo's flaky_lint verb — RetryableFailure on attempt 1,
# Success on attempt 2.
if ctx.attempt < 2:
    return RetryableFailure(
        retry_key=f"{ctx.run_id}:flaky_lint",
        error_kind="lint.transient",
        message="linter spawned a child process that exited 137 (OOM)",
        attempt=ctx.attempt,
    )
return Success(value={"lint_passed": True, "attempts_used": ctx.attempt})
```

> The variants live in [`requiem.outcomes`](../src/requiem/outcomes.py).
> `BadOutput` is distinct from `PermanentFailure` because it must *not*
> be network-retried — a bad LLM response wants a remediation branch,
> not the same prompt again.

## Agent

An **agent** is a verb that calls an LLM. You define it as an
`AgentSpec` (name + charter + typed response model). The kernel converts
its response into either a `Success` carrying the parsed model, or a
`BadOutput` carrying the validation errors.

```python
CORRECTNESS = AgentSpec(
    name="correctness_reviewer",
    charter="You hunt for bugs. Off-by-one, exception swallowing, "
            "mutable default args, missing error handling. "
            "Severity is `blocking` if the code is observably wrong.",
    response_model=ReviewFinding,
)
```

In the workflow, an agent node looks like this:

```python
.agent("synthesize", agent="synthesizer", prompt_verb="synth_prompt")
```

The `prompt_verb` is a regular verb whose return value is the prompt
string. That keeps prompt-construction (which is just Python) separate
from agent invocation (which is the LLM call).

## Team

A **team** is N agents running in parallel — Requiem's first-class
support for the squad / adversarial-review / multi-perspective pattern.
Each branch is one `(agent_name, prompt_verb)` pair; the branches run
concurrently; the team's `Success.value['findings']` aggregates the
results.

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
```

The decision to make teams first-class is [ADR
0003](decisions/0003-agent-teams-first-class.md). It buys you crash-safe
parallel agents (each branch fans into the same event log with a
`team_id` + `agent_id` so the renderer can interleave them sanely).

## Gate

A **gate** is a pause point asking a human a question. The kernel
suspends the run (exit code `2`) and waits — either for a CLI prompt, a
custom handler, or a future UI click. `resume` continues from the
recorded choice.

```python
.human_gate(
    "human_gate",
    prompt="Reviewer team finished. Approve verdict?",
    options=["approve", "reject"],
)
    .edge("human_gate", on="needs_human:approve", to="archive")
    .edge("human_gate", on="needs_human:reject",  to="fail_end")
```

Edges out of a gate use `on="needs_human:<choice>"`. The demo wires a
default auto-approving handler so it runs unattended; remove that and
the run suspends until you answer.

## Event log

The **event log** is the authoritative record of your run — every
transition, every outcome, every retry, every gate decision, written as
one JSONL line per event. The CLI's narration, the resume cursor, and
(eventually) the UI are all projections of this one file.

> "Your runs are durable. If the process dies — laptop sleep,
> `Ctrl+C`, kernel panic — `requiem resume` picks up exactly where the
> log stopped. Nothing in memory is load-bearing."

This is invariant `INV-EVENT-LOG-AUTHORITATIVE` in
[`north-star.md`](north-star.md).

```powershell
# Default view: rendered English (same lines `requiem run` printed).
requiem events <run_id> --workflow requiem.workflows.code_review_demo

# Raw JSONL, for CI / jq / your tooling.
requiem events <run_id> --raw
```

The log lives at `.runs/<run_id>.events.jsonl` by default. Override with
`--log-dir`.

## Where to next

- Write your first workflow: [`writing-workflows.md`](writing-workflows.md).
- Quick recipes: [`cookbook.md`](cookbook.md).
- Architectural invariants: [`north-star.md`](north-star.md).
