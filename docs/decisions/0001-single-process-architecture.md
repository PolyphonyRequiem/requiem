# ADR 0001 — Single-process architecture

**Status:** Accepted
**Date:** 2026-05-31
**Author:** Architect (composite — Daniel + AI agent based on deep-dive findings)
**Supersedes:** the polyphony+conductor process split

---

## Context

Polyphony today is a .NET 11 CLI that the conductor Python workflow engine invokes as a subprocess at every workflow `script:` node. The split was inherited rather than designed: polyphony was built as a CLI before conductor existed, and conductor was adopted as the orchestration layer afterwards.

This split is the source of disproportionate complexity in polyphony's error-handling, observability, retry, and cancellation stories:

1. **Three-vocabulary problem** — event names, state names, and category names must agree across C# verbs ↔ Python engine ↔ YAML workflows. Every workflow change requires triple-validation.
2. **Exit-code contract** — typed outcomes cannot cross a process boundary cleanly. The 5-class exit-code taxonomy (success/usage/permanent/transient/corruption) is a retrofit for what would be a discriminated-union return value in-process.
3. **Retry semantics** — the engine cannot see verb internals. Constructs like `retry_key:` and the `_MISSING` sentinel exist to express across the seam what would be one method call in-process.
4. **Observability** — runs must be reconstituted from disk because there is no shared in-memory event stream. The trace tooling, the manifest, the journal, and the UI all parse the same JSONL files independently.
5. **Cancellation** — `CONDUCTOR_CANCEL_TOKEN` exists as a sentinel-file IPC because Windows lacks `SIGTERM` and the process boundary can't pass a `CancellationToken`.
6. **Notification** — watermark-polling exists because there is no in-process event bus the workflow author can subscribe to.

A substantial fraction of the [error-handling deep dive](../references/error-handling-deep-dive.md) is a catalogue of retrofits paying down this single architectural debt.

## Decision

**Requiem runs the engine, the verb library, and the UI backend as a single Python process.**

The only out-of-process invocations are:
- Genuine external dependencies: `git`, `gh`, `twig`, the LLM provider, OS tooling.
- The UI frontend (a separate JS process) talking to the backend over Server-Sent Events for live state and a WebSocket for user input.

The verb library is a Python package, not a separate binary. Verb invocations are function calls. Verb outcomes are discriminated-union return values (see ADR 0002, TBD).

## Consequences

**Positive:**
- Three-vocabulary problem collapses to two (Python + UI). The verb library and the engine share types directly.
- The exit-code contract becomes unnecessary internally; it survives only at the genuine-external-script boundary, where the 5-class contract still applies.
- Retry can hold typed state in memory; the `retry_key` / `_MISSING` sentinel machinery becomes a clean function signature.
- The event log is written by the engine directly; UI, manifest, harness, and reconcile verb all read the same in-memory event stream as it's written.
- Cancellation uses Python's `asyncio.CancelledError` — no sentinel files.
- The harness can drive the engine in-process with fakes injected at the function-call boundary, not the subprocess boundary. This dramatically expands what's testable.

**Negative:**
- Verb language lock-in: verbs must be Python. Adding a verb in another language requires either a Python wrapper or a subprocess call (with the same out-of-process penalties polyphony pays today). This is judged acceptable because Python is the project's chosen language for both engine and verbs.
- Memory footprint: one process holds engine + verbs + UI backend. For Requiem's single-operator target, this is not a concern. For a hypothetical future multi-tenant deployment, this would need re-examination — but multi-tenancy is explicitly out of scope (see north-star §5).
- A crash in a verb crashes the engine. Mitigated by: (a) every verb call is wrapped in an exception boundary that converts uncaught exceptions to `PermanentFailure` variants of the discriminated outcome; (b) the engine's process supervisor (e.g., a thin watchdog) restarts the engine and replays from the event log on restart.

**Neutral:**
- The UI frontend remains a JS process (React/TS, following platespinner's stack). This is not a violation of single-process architecture; it is a deliberate consequence of "the UI runs in a browser." The SSE/WS protocol between UI backend (Python) and frontend (JS) is the only IPC Requiem owns.

## Alternatives considered

1. **Fork conductor into Requiem** — keep conductor's engine, copy in-tree, sever upstream. Rejected: inherits conductor's YAML schema (which we don't want, per Daniel) and conductor's design decisions (including the assumption that verbs are external processes). The fork would not solve the seam.
2. **Embed conductor as a Python library** — keep the engine as conductor source, import it into polyphony. Rejected: polyphony is .NET; there is no clean embedding story. Would still pay the language-split tax.
3. **Rewrite polyphony in Python, keep conductor separate** — solves the three-vocabulary problem (Python on both sides) but does not collapse the IPC seam. Retry, observability, and cancellation all still cross a process boundary. Rejected as a half-measure.
4. **Single binary with embedded Python interpreter** — rejected on the basis of "not v0" — adds packaging complexity without changing the architecture.

## References

- `../north-star.md` §2: `INV-SINGLE-PROCESS`
- `../references/error-handling-deep-dive.md` — the ~50% of findings that this ADR dissolves
- `../references/polyphony-parity-inventory.md` — defines what the new process must still do
