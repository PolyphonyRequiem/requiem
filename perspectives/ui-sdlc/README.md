# Debussy — UI + SDLC Perspectives

> **Seat:** Debussy
> **Charter:** Brainstorm the *feel* of driving Requiem through a real SDLC day and surface the UX + SDLC implications of seam decisions the engine seats are making in parallel.
> **Status:** Living. Updates land when seam PRs do.
> **Not a decision document.** This is brainstorming with strong opinions. Where I would bet, I mark `[BET]`. Where I'm offering one of N options, I mark `[OPTION]`. Where I'm reaching, I mark `[BLUE-SKY]`.

---

## Table of contents

| File | One-line summary |
|---|---|
| [`01-feel-of-the-loop.md`](01-feel-of-the-loop.md) | What driving Requiem *feels* like across a workday — morning kickoff through end-of-day triage. |
| [`02-polyphony-rough-edges.md`](02-polyphony-rough-edges.md) | The current rough edges in polyphony+conductor, organized by SDLC phase, with each symptom traced to a deep-dive source. |
| [`03-ui-pattern-catalogue.md`](03-ui-pattern-catalogue.md) | Every concrete UI pattern from the platespinner survey and the 10-system viz research, scored against the rough edge it would address. |
| [`04-sdlc-open-questions.md`](04-sdlc-open-questions.md) | Decisions the engine seats are about to make whose downstream SDLC ergonomics need to be on Daniel's radar. |
| [`05-forward-looking-deferred.md`](05-forward-looking-deferred.md) | UI/SDLC patterns I think we'll want eventually but should explicitly defer past v0, with rationale. |
| [`06-perspectives-on-seams.md`](06-perspectives-on-seams.md) | Per-seam UX implications, populated as engine PRs land. Currently a scaffold. |

---

## How to read this

- If you only have 5 minutes: read [`01-feel-of-the-loop.md`](01-feel-of-the-loop.md) and the **§ "Strongest signals"** block at the bottom of [`03-ui-pattern-catalogue.md`](03-ui-pattern-catalogue.md).
- If you're an engine seat about to make a seam decision: skim [`04-sdlc-open-questions.md`](04-sdlc-open-questions.md) for the section that maps to your seam.
- If you're Daniel triaging: the open questions in `04` are the ones I want pushback on.

## Source provenance

Every claim in these files is tagged with one of:

- `DD:§n` — section of `docs/references/error-handling-deep-dive.md`
- `PI:§n` — section of `docs/references/polyphony-parity-inventory.md`
- `PS:§n` — section of `docs/references/platespinner-survey.md`
- `WV:<system>` — system in `docs/references/workflow-viz-research.md`
- `NS:INV-X` — invariant from `docs/north-star.md`
- `ADR-0001` — `docs/decisions/0001-single-process-architecture.md`

If a claim has no tag, it's my own synthesis and I own it.
