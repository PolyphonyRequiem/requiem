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

## Status: pre-v0 — design phase

Requiem is in its **seam-shaping phase** (Phase A of [the v0 roadmap](docs/roadmap.md), TBD).

In this phase the project produces 2-3 runnable prototypes per load-bearing architectural seam, demonstrated hands-on for product-direction decisions before any production code is written.

There is no shipping artifact yet. Do not depend on this project.

## Design inputs

Requiem inherits its design vocabulary from three pieces of prior work:

1. **[Error-handling deep dive](docs/references/error-handling-deep-dive.md)** — eight Opus-4.7-high analyses + cross-reviewer grilling that codified the invariants Requiem must hold from line one (`INV-RESTART`, `INV-NO-CORRUPT-FORWARD`, the 20-signal domain enum, the discriminated-outcome verb contract, the receipts-as-anti-hallucination pattern, etc.).
2. **[Polyphony parity inventory](docs/references/polyphony-parity-inventory.md)** — exhaustive catalogue of what polyphony+conductor does today, defining what "no meaningful regression" means in v0.
3. **[Workflow visualization research](docs/references/workflow-viz-research.md)** + **[platespinner survey](docs/references/platespinner-survey.md)** — state-of-the-art UI design references for the live-traversal view.

## Repo layout (provisional — will be reshaped during Phase A)

```text
docs/
├── north-star.md              # Invariants, terminology, contracts that survive across decisions
├── roadmap.md                 # Phase A → D sequencing
├── decisions/                 # ADRs (numbered, dated, immutable once accepted)
└── references/                # Inherited design inputs from polyphony era

prototypes/                    # Phase A artifacts — throwaway by design
└── <seam>/<variant>/          # Runnable demos for hands-on review

# (Phase B+ structure TBD — engine, ui, verbs, harness, etc.)
```

## Naming

**Requiem.** The org [`PolyphonyRequiem`](https://github.com/PolyphonyRequiem) was named in anticipation of this project; the requiem was always coming. Continues the musical-form tradition of polyphony and conductor — and Mozart, Brahms, Verdi, and Fauré (all seats in the squad that designed Requiem's invariants) each wrote a Requiem of their own.

## License

TBD. Likely MIT, matching polyphony, conductor, and platespinner.
