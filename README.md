# Requiem

> It's Monday morning. A pull request landed overnight. Before your first
> coffee, you want to know whether it's safe to merge — not because a CI
> badge turned green, but because three reviewers with different jobs
> actually read the code, disagreed, and a fourth synthesised a verdict
> you can argue with. Requiem runs that review on the snippet, surfaces
> the verdict, and pauses for your approval — in under 100 ms, with no
> API keys.

## Try it

```powershell
pip install -e .[cli]
requiem run requiem.workflows.code_review_demo
```

A run will end with:

```
─── Verdict ─────────────────────────────────────────────────────────
  🚫 Don't merge
      Top finding:  unhandled ValueError on int(x)
      Rationale:    1 blocking + 1 warn; correctness reviewer's unhandled ValueError must be fixed before merge.
  → summary: .runs/<run-id>.summary.md
─────────────────────────────────────────────────────────────────────
```

## What is Requiem?

Requiem is a single-process Python engine that runs YOUR software-development
workflows — code review, planning, implementation, close-out — with AI agents
as first-class steps inside them. You write the workflow once as a small
Python file (a graph of nodes). Requiem runs it, narrates what's happening
in plain English, pauses when it needs a human decision, and records
everything to a durable event log so a crash never costs you work.

It is the successor to [`polyphony`](https://github.com/PolyphonyRequiem/polyphony)
+ `conductor` — a two-process .NET/Python pair — collapsed into one Python
package with a built-in renderer and harness. The old split is described
in [ADR 0002](docs/decisions/0002-phase-a-integrated-design.md).

## What you'll see

Here is the complete, verbatim output of one `requiem run` against the
demo workflow:

```
requiem run — requiem.workflows.code_review_demo  (run_id=run-XXXXXXXXXX)
log: .runs/run-XXXXXXXXXX.events.jsonl
────────────────────────────────────────────────────────────────────────
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
────────────────────────────────────────────────────────────────────────

─── Verdict ─────────────────────────────────────────────────────────
  🚫 Don't merge
      Top finding:  unhandled ValueError on int(x)
      Rationale:    1 blocking + 1 warn; correctness reviewer's unhandled ValueError must be fixed before merge.
  → summary: .runs/run-XXXXXXXXXX.summary.md
─────────────────────────────────────────────────────────────────────

Completed (completed, final_node=end, 34 events, 71 ms)
log: .runs/run-XXXXXXXXXX.events.jsonl
```

Fits on one screen. Tells you what happened, in your words. No JSON to
`jq`, no log file to `cat`, no test report to parse. If you wanted the
raw stream — say, for CI — `requiem events <run_id> --raw` emits the
JSONL log verbatim.

## What does this mean for my workday?

A normal weekday looks like this:

- **Morning:** `requiem run code_review` on each overnight PR. Three
  reviewers and a synthesiser run in parallel. You read four verdict
  cards, approve the safe merges from the terminal, leave the questionable
  ones for after coffee.
- **Mid-morning:** A workflow stops at a 🚦 gate — "should I rebase or
  squash this merge train?" The narration shows you what it already
  decided and what it's stuck on. You answer. It continues from exactly
  where it stopped, even if you closed your laptop in between.
- **Anytime:** A run dies — your laptop sleeps, the LLM provider returns
  500s, you `Ctrl+C` because you spotted a problem. `requiem resume
  <module> <run_id>` picks up at the last completed node. Nothing has to
  be re-done unless it's actually unfinished.
- **Debugging:** Something looks wrong. `requiem events <run_id>` shows
  you the whole story in plain English. `--raw` gives you the JSONL if
  you need to grep.

You aren't supervising the engine. The engine is doing the boring
orchestration so you can spend attention on the few decisions that
actually want a human.

## How does this fit my SDLC?

Requiem is a runtime, not a methodology. A workflow is a graph of nodes
where each node is one of:

- a **script** (your Python function — `read file`, `call git`, `write
  artifact`),
- an **agent** (one LLM call with a typed response shape),
- a **team** (N agents running in parallel — adversarial reviewers,
  multi-perspective planners, anything),
- a **gate** (pause and ask a human),
- a **terminate** (end the run with a disposition).

You wire those with edges keyed on outcomes: `on="success"`,
`on="permanent_failure"`, `on="needs_human:approve"`. The engine handles
retries, parallel fan-out, gate suspension, durable resume, and rendering.

Workflows live in your own repo or in `requiem.workflows.*`. The demo
under `requiem.workflows.code_review_demo` is the canonical example —
read it before writing your first one.

## Where do I write my own workflows?

- [`docs/getting-started.md`](docs/getting-started.md) — install, run the
  demo, write your first workflow, resume, view events. 5-minute read.
- [`docs/concepts.md`](docs/concepts.md) — the vocabulary (workflow, verb,
  outcome, agent, team, gate, event log) with one code snippet per term.
- [`docs/writing-workflows.md`](docs/writing-workflows.md) — line-by-line
  walkthrough of the code-review demo. Read this before authoring.
- [`docs/cookbook.md`](docs/cookbook.md) — short recipes ("how do I retry
  on transient failure?", "how do I run agents in parallel?").

## Where's the engine internals?

For the architecture, invariants, and decision provenance:

- [`docs/north-star.md`](docs/north-star.md) — the seven invariants that
  rule everything.
- [`docs/decisions/`](docs/decisions/) — ADRs. Start with
  [0002](docs/decisions/0002-phase-a-integrated-design.md) (the engine
  design), [0003](docs/decisions/0003-agent-teams-first-class.md) (agent
  teams), [0004](docs/decisions/0004-cross-cutting-defaults.md)
  (cross-cutting defaults).
- [`docs/references/`](docs/references/) — the prior-art deep-dives the
  invariants are derived from.
- `src/requiem/` — the engine itself. Eight modules, ~2 KLOC.

## CLI reference

| Command | What it does |
|---|---|
| `requiem run <module>` | Run a workflow by importable module path. |
| `requiem resume <module> <run_id>` | Resume a partially-finished run from its event log. |
| `requiem describe <module>` | Print nodes, edges, registered agents. |
| `requiem events <run_id> [--workflow MOD] [--raw]` | Print a run's event log; default is rendered English, `--raw` is JSONL. |
| `requiem cancel <run_id>` | *(coming — see [issue](https://github.com/PolyphonyRequiem/requiem/issues) tracker for Dvorak's PR)* |

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Run completed. |
| `1` | Run failed. |
| `2` | Run is suspended at a human gate. |
| `130` | Run was cancelled (`Ctrl+C` or operator cancel). |

## Status

**v0.0.1.** Phase A — engine integration — is closed. Phase B (real
workflows, real UI binding, real harness) is in flight. The demo
workflow runs end-to-end with no external dependencies; agents are
scripted via `FakeProvider` so the demo is reproducible and key-free.
Real LLM providers ship in Phase B.

## License

TBD. Likely MIT, matching polyphony, conductor, and platespinner.
