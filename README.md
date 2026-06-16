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

# Pick a run id (any short string), then run the bundled demo:
requiem run requiem.workflows.code_review_demo --run-id demo1
```

You should see ~10 lines of customer-English narration ending in a verdict
card — the demo runs end-to-end (script verbs, flaky-lint retry, parallel-fork
reviewer team, structured-output synthesizer, human gate, archive) in under
100 ms with zero API keys.

```powershell
# Replay the run from its event log (humanized — the log records workflow
# identity, so no --workflow flag needed):
requiem events demo1

# Tail a live run in another terminal:
requiem events demo1 --follow

# CI consumers: get the raw JSONL stream
requiem events demo1 --raw

# Discover recent runs under .runs/
requiem list-runs

# Stop a stuck or suspended run (writes cancel_requested to the log; the
# next resume short-circuits per INV-CANCEL-SHORT-CIRCUITS-RETRY):
requiem cancel demo1 --reason "operator changed their mind"

# Drive the human gate yourself instead of auto-resolving:
requiem run requiem.workflows.code_review_demo --interactive

# Inspect the workflow's topology:
requiem describe requiem.workflows.code_review_demo
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
- a **sub-workflow** (invoke another workflow as a node; child gets its
  own isolated event log per INV-SUBWORKFLOW-LOG-ISOLATION),
- a **terminate** (end the run with a disposition).

You wire those with edges keyed on outcomes: `on="success"`,
`on="permanent_failure"`, `on="needs_human:approve"`. The engine handles
retries, parallel fan-out, gate suspension, durable resume, and rendering.

Workflows live in your own repo or in `requiem.workflows.*`. The demo
under `requiem.workflows.code_review_demo` is the canonical small example;
`requiem.workflows.planning` / `requiem.workflows.kanban_executor` /
`requiem.workflows.feature_pr` are the production-scale ones. The live
end-to-end driver is `python -m requiem.end_to_end --item <ado-id> --board
requiem-<id> [--commit] [--live]`; it chains planning → `commit_plan` →
`trunk_bootstrap` → `kanban_executor` → `leaf_pr` → `feature_pr` per
ADR-0018 Option C (requiem owns the integration trunk because
`hermes kanban create` has `--branch` but no `--base` flag).

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
| `requiem run <module>` | Run a workflow by importable module path. `--interactive` prompts at each human gate; default auto-resolves per the workflow. |
| `requiem resume <module> <run_id>` | Resume a partially-finished run from its event log. |
| `requiem events <run_id>` | Render the run's event log as customer English. `--follow`/`-f` tails; `--raw` emits JSONL for CI; `--workflow MOD` overrides the auto-loaded module. |
| `requiem list-runs` | List runs under `--log-dir` with workflow / start time / status / duration / event count. |
| `requiem cancel <run_id>` | Write a `cancel_requested` event into the log; the engine short-circuits at the next loop tick or on the next resume. |
| `requiem describe <module>` | Print nodes, edges, registered agents, retry budgets, humanize map. |

The `module` argument is any importable Python module exposing
`build_engine(log_dir) -> Engine` or `build_workflow() -> Workflow`. See
`src/requiem/workflows/code_review_demo.py` for the canonical shape; the
workflow itself records its own module path (Wagner builder `.module(...)`)
so post-hoc commands re-import without an explicit flag.

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Run completed. |
| `1` | Run failed. |
| `2` | Run is suspended at a human gate. |
| `130` | Run was cancelled (`Ctrl+C` or operator cancel). |

## Status

**v0.0.1.** Per the
[parity scorecard](docs/references/v0-parity-readiness.md) (Mahler-3,
re-assessed 2026-06-09): **GO for the v0 code surface; the remaining gate
is live Azure DevOps validation, not code.** Every one of the ten §9
non-negotiables is built and tested — 3 ✅ at-parity, 1 🔵 better, 6 🟡
partial (code-complete but pending live-ADO exercise), 0 ❌ missing. The
load-bearing invariants (INV-RESTART, INV-NO-CORRUPT-FORWARD,
INV-EVENT-LOG-AUTHORITATIVE, INV-DISCRIMINATED-OUTCOMES,
INV-SUBWORKFLOW-LOG-ISOLATION, INV-LOG-STRICT-STOP-ON-CORRUPTION,
INV-CANCEL-RESUME-IDEMPOTENT) are pinned by 200+ resume-fidelity tests
across the 14-class crash-point matrix.

What landed between the original NO-GO audit and "code complete" (PRs
#61–#75): full ADR-0018 trunk topology (`trunk_bootstrap` / `leaf_pr` /
`feature_pr` + driver wiring), the requirement-disposition gate, the
read-only web dashboard + browser gate resolution + opt-in auto-resume
(closes #8), in-process fan-out orchestrator + per-item worktree
isolation (ADR-0021/0022, advances #4/#5), the ADO PR lifecycle
(ADR-0023, closes #10's ADO half), and the ADR-0013 B1/B2/B3 closures.

The auth model on the live container resolves ADR-0007 Q4 (OIDC required;
PATs not supported in the primary v0 org) by mounting the host's
`~/.twig` / `~/.config/gh` token stores read-only into the fleet
container; run `twig auth login` + `gh auth login` once on the host
before bringing the container up (see `deploy/README.md`).

## Design inputs

Requiem inherits its design vocabulary from three pieces of prior work:

1. **[Error-handling deep dive](docs/references/error-handling-deep-dive.md)** — eight Opus-4.7-high analyses + cross-reviewer grilling that codified the invariants Requiem must hold from line one (`INV-RESTART`, `INV-NO-CORRUPT-FORWARD`, the 20-signal domain enum, the discriminated-outcome verb contract, the receipts-as-anti-hallucination pattern, etc.).
2. **[Polyphony parity inventory](docs/references/polyphony-parity-inventory.md)** — exhaustive catalogue of what polyphony+conductor does today, defining what "no meaningful regression" means in v0.
3. **[Workflow visualization research](docs/references/workflow-viz-research.md)** + **[platespinner survey](docs/references/platespinner-survey.md)** — state-of-the-art UI design references for the live-traversal view.

## Repo layout

```text
docs/
├── north-star.md              # Invariants, terminology, contracts
├── decisions/                 # ADRs (numbered, dated, immutable once accepted)
└── references/                # Inherited design inputs from polyphony era

src/
└── requiem/                   # The engine package (v0.0.1)
    ├── outcomes.py            # Discriminated outcome union (6 variants)
    ├── events.py              # Execution-event envelope + emitter
    ├── persistence.py         # Append-only event log
    ├── kernel.py              # Data-driven interpreter + resume cursor
    ├── dsl.py                 # Fluent workflow builder + pydantic model
    ├── agent.py               # Protocol AgentProvider + FakeProvider
    ├── toolbelt.py            # Per-tool external-process clients
    ├── clients/               # Per-tool typed clients (gh, twig, ...)
    ├── teams.py               # parallel_fork sugar
    ├── cli.py                 # `requiem` entry point
    └── workflows/             # Stdlib / example workflows
        └── code_review_demo.py

tests/                         # Unit tests per module + one integration suite
```

## Running against PolyphonyRequiem (`gh` auth caveat)

The `GhClient` in `requiem.clients.gh` wraps the `gh` CLI but does **not**
manage authentication — `gh auth` is `gh`'s job. The development box has
two `gh` accounts configured:

| Account              | Access to `PolyphonyRequiem/*` |
|----------------------|--------------------------------|
| `dangreen_microsoft` (EMU) | locked OUT                |
| `PolyphonyRequiem`         | active                    |

When running verbs that touch this org, the `PolyphonyRequiem` account
must be the active one (`gh auth status` to confirm; `gh auth switch` to
change). If the wrong account is active, the client raises
`GhAuthError`, which verbs map to `NeedsHuman` — by design, we surface
to an operator rather than silently retry.

## Naming

**Requiem.** The org [`PolyphonyRequiem`](https://github.com/PolyphonyRequiem) was named in anticipation of this project; the requiem was always coming. Continues the musical-form tradition of polyphony and conductor — and Mozart, Brahms, Verdi, and Fauré (all seats in the squad that designed Requiem's invariants) each wrote a Requiem of their own.

## License

MIT, matching polyphony, conductor, and platespinner.
