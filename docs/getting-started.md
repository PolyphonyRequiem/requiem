# Getting started with Requiem

Five minutes from `pip install` to your first resumed workflow.

## 1. Install (one command)

```powershell
pip install -e .[cli]
```

That gives you the `requiem` console script plus `rich` for coloured
terminal output. (Drop `[cli]` if you don't care about colour; the
output degrades gracefully to plain text.)

```powershell
requiem --version
# requiem 0.0.1
```

## 2. Run the demo (one command)

```powershell
requiem run requiem.workflows.code_review_demo
```

You should see something like this, in under a second:

```
▶ run_started — code-review on sample_snippet.py
✓ Read sample_snippet.py — 7 lines
🔁 Lint failed: linter spawned a child process that exited 137 (OOM) — retrying (attempt 2)
✓ Lint passed on attempt 2
▶ Started 3 reviewers in parallel
  ✓ style_reviewer: warn — mutable default argument `cache={}` will leak state across calls
  ✓ correctness_reviewer: blocking — `int(x)` raises ValueError on bad input; no handling
  ✓ performance_reviewer: info — linear scan of `cache.keys()` could be O(1) dict lookup
✓ Synthesized verdict — don't merge (1 warn, 1 blocking, 1 info)
🚦 Gate: Reviewer team finished. Approve verdict? (auto-approved for demo)
     ↳ verdict: don't merge — top finding: unhandled ValueError on int(x)
✓ Wrote summary — to .runs/run-XXXXXXXXXX.summary.md
■ Completed — code-review on sample_snippet.py
```

What just happened, in one paragraph: the engine ran a 9-node workflow
that read a snippet, deliberately failed a flaky lint once (to prove the
retry path), fanned out three parallel reviewers, synthesised their
findings into a verdict, paused at a human gate (auto-approved here so
the demo doesn't hang), and wrote a summary file. The whole thing took
~70 ms and used no API keys — agents are scripted via `FakeProvider` for
reproducibility.

Look at the printed `log:` line. That path is your durable record.

## 3. Look at what just happened

```powershell
requiem events <run_id> --workflow requiem.workflows.code_review_demo
```

Replays the run from the event log, in the same human English. Use this
as your default debug tool — same lines, no rerun needed.

For CI or `jq` consumers:

```powershell
requiem events <run_id> --raw
```

Emits the event log as JSONL, one event per line. Stable schema.

## 4. Inspect the workflow shape

```powershell
requiem describe requiem.workflows.code_review_demo
```

Prints the topology — nodes, edges, retry budgets, agent registrations,
the `humanize` map (the per-node noun phrases the renderer uses). This
is what your workflow looks like *to* the engine.

## 5. Write your first workflow

Once you've run the demo, open
[`docs/writing-workflows.md`](writing-workflows.md). It walks
`requiem.workflows.code_review_demo` line by line — that's the shortest
path from "I ran the demo" to "I have my own workflow."

## 6. Resume a workflow

Every run is durable. To prove it: run the demo, then resume:

```powershell
requiem run requiem.workflows.code_review_demo --run-id my-test
requiem resume requiem.workflows.code_review_demo my-test
```

The resume re-prints the prior narration from the log, then continues
from the last unfinished node. If the run already completed, resume is a
safe no-op. If you killed it mid-way, resume picks up exactly where the
event log left off — committed nodes are not re-executed.

Try it the destructive way: in one terminal start a long workflow, in
another `Ctrl+C` it, then `requiem resume <module> <run_id>`. That
behaviour is invariant `INV-RESTART` in
[`north-star.md`](north-star.md).

## 7. Cancel a workflow

**Coming.** `requiem cancel <run_id>` ships with Dvorak's PR in Phase B.
For now: `Ctrl+C` the run, then call `resume` to continue. Cancel
already short-circuits any retry loop in flight (`INV-CANCEL-SHORT-CIRCUITS-RETRY`).

## What next?

- Vocabulary: [`docs/concepts.md`](concepts.md) — workflow, verb,
  outcome, agent, team, gate, event log.
- Author guide: [`docs/writing-workflows.md`](writing-workflows.md) —
  walk through the demo.
- Recipes: [`docs/cookbook.md`](cookbook.md) — short answers to "how do
  I X?"
- Architecture: [`docs/north-star.md`](north-star.md) + ADRs in
  [`docs/decisions/`](decisions/).
