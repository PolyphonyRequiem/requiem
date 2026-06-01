# Requiem

> **Single-process SDLC orchestration engine with built-in UI and harness.**
>
> Successor to [`polyphony`](https://github.com/PolyphonyRequiem/polyphony) and the polyphony+conductor split.
>
> Tagline TBD: *Requiem for the seam.*

---

## What this is

Requiem is the next-generation SDLC orchestration engine for AI-agent-driven software development. It replaces the current `polyphony` (a .NET 11 CLI for SDLC verbs) + `conductor` (a Python YAML workflow engine) two-process system with a single-process Python engine that owns:

- The workflow execution kernel (state machine, routing, gates, retries)
- The verb library (the deterministic decisions agents and operators rely on)
- The agent boundary (how LLM agents are invoked and how outputs flow back)
- The persistence model (durable runs, idempotent re-entry, event-log-as-source-of-truth)
- A built-in web UI for live workflow traversal
- A first-class harness (scenarios, fake providers, chaos testing)

This consolidation dissolves the seam responsible for ~half of the error-handling complexity in polyphony+conductor today — including the three-vocabulary problem, the exit-code contract negotiation, cross-process retry semantics, and the observability gap between engine events and verb outcomes.

## Status: v0.0.1 — engine promoted, Phase B begins

Phase A is closed. The integrated walking-skeleton engine lives under `src/requiem/` as a real Python package with a CLI entry point. Phase B (real workflows, real verbs, real UI binding) builds on top of this surface.

### Quick start

```powershell
pip install -e .[cli]
requiem describe requiem.workflows.code_review_demo
requiem run     requiem.workflows.code_review_demo
requiem events  <run_id_printed_above>
```

The walking-skeleton's `code-review` workflow ships as a runnable example under `requiem.workflows.code_review_demo`. It exercises every Phase A seam — script verbs, retry-then-succeed, parallel-fork team, structured-output agent, human gate, and event-log resume — in ~90 ms with zero API keys.

### CLI

| Command | What it does |
|---|---|
| `requiem run <module>` | Run a workflow by importable module path. |
| `requiem resume <module> <run_id>` | Resume a partially-finished run from its event log. |
| `requiem describe <module>` | Print nodes, edges, registered agents. |
| `requiem events <run_id>` | Print the run's event log with colour hints. `--json` for raw JSONL. |

The `module` argument is any importable Python module that exposes either `build_engine(log_dir) -> Engine` or `build_workflow() -> Workflow`. See `src/requiem/workflows/code_review_demo.py` for the canonical shape.

### Test it

```powershell
pip install -e .[test]
pytest
```

Eight test modules; the heaviest is `test_integration_code_review.py`, which promotes the four walking-skeleton scenarios and adds an end-to-end INV-RESTART assertion (truncate the log mid-workflow, prove resume picks up exactly where the engine left off without re-executing committed nodes).

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

TBD. Likely MIT, matching polyphony, conductor, and platespinner.
