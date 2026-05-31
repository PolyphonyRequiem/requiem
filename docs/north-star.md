# Requiem North Star

> Invariants and load-bearing concepts that survive across decisions. This document is short by design: changes here are decisions about *what Requiem is*, not implementation choices. New entries must be earned through an ADR in [`docs/decisions/`](decisions/) or a prior-art citation in [`docs/references/`](references/).

---

## §1 Mission

Requiem is an SDLC orchestration engine that runs a single, AI-agent-heavy software development lifecycle reliably enough that a single operator can trust it to make durable changes to real code and real work-item state without supervising every step.

It must be **restart-friendly** without ever letting **corrupt state move forward** — these two constraints together are the project's load-bearing requirement.

---

## §2 Architectural invariants

These invariants are absolute. Any code, design, or workflow that violates them is rejected.

### INV-SINGLE-PROCESS
The engine, the verbs, and the UI backend run in a single Python process. The UI frontend is a separate JS process talking over SSE/WebSocket. The only other processes Requiem invokes are genuine external dependencies (`git`, `gh`, `twig`, the LLM provider, occasionally OS tools).

> *Why load-bearing:* this is the core reason for Requiem's existence. Polyphony+conductor's split into a Python engine + a .NET verb subprocess produced ~half of polyphony's error-handling complexity (the three-vocabulary problem, the exit-code contract negotiation, cross-process retry semantics, the trace-reconstitution gap). Requiem dissolves that seam by construction.

### INV-RESTART
Any run, at any moment, may be killed and restarted. The restarted run produces an outcome consistent with the original run having succeeded. No state-mutating verb may rely on in-memory continuation; all required context lives in durable storage.

> *Operationalized as:* every verb is idempotent or refuses to start; every retry path has an explicit `retry_key`; the event log is the recovery substrate; in-memory caches are derivable.

### INV-NO-CORRUPT-FORWARD
A verb that cannot verify its prerequisites refuses to act. A workflow that suspects state corruption surrenders to a human gate. The engine never auto-rerolls a suspected hallucination, never silently retries past a state-drift signal, and never "best-efforts" past an unverified precondition.

> *Operationalized as:* the `receipts` pattern (every state-mutating verb emits `inspected_artifacts`); verify-then-act in workflow nodes; receipts violations route to human gate, never to auto-retry; `polyphony reconcile`-style verbs to converge incomplete state explicitly.

### INV-EVENT-LOG-AUTHORITATIVE
The append-only event log (`run.events.jsonl`) is the source of truth for everything observable about a run. The manifest, the UI's view, the harness's assertions, the reconcile verb's diagnostics — all are projections of the event log. Anything not in the event log did not happen.

> *Why load-bearing:* this is the single decision that makes restart, observability, UI, harness, and debug-after-the-fact all work without separate machinery.

### INV-DISCRIMINATED-OUTCOMES
Every verb returns a discriminated union of one of: `Success | RetryableFailure | PermanentFailure | NeedsHuman | Cancelled`. The workflow router consumes this shape directly. There is no convention of "exit 0 means success" or "look for `error` field in JSON" — the variant tag *is* the outcome.

> *Why load-bearing:* this eliminates the exit-code-contract retrofit problem that polyphony is currently paying down. Routing, retry, UI rendering, and harness assertions all key off the variant tag.

### INV-CANCEL-SHORT-CIRCUITS-RETRY
A `Cancelled` outcome aborts the retry loop immediately. No fresh attempt is made after a cancel signal, regardless of `retry.max` budget remaining.

> *Why load-bearing:* without this, a 24-hour-deadline cancel can trigger another 24-hour attempt — a bug we've already designed around in polyphony.

### INV-NO-ENGINE-ABANDONMENT
The engine does not initiate abandonment. Retry exhaustion routes to *surrender* — an operator-mediated decision. Workflow timeouts produce a domain signal, not a terminal state. Supersession (a newer plan replacing an older one) is its own terminal, distinct from abandonment.

> *Why load-bearing:* this closes the "what is abandonment" question definitively. The engine refuses to make irreversible decisions on the operator's behalf.

---

## §3 Vocabulary

Three vocabularies in Requiem; mixing them is a defect.

### §3.1 Execution events
What the engine emits at every transition. Sequential, ordered by `event_id`, written to `run.events.jsonl`. Categories include: `workflow_started`, `node_entered`, `node_completed`, `route_taken`, `retry_attempted`, `human_gate_presented`, `human_gate_resolved`, `workflow_terminated`, etc. The closed taxonomy ships in the engine.

### §3.2 Domain signals
What the *workflow domain* observes about itself. Separate stream (`run.notifications.jsonl`). 20-signal seed catalogue (open by evolution policy, but additions require an ADR): `seeded`, `planned`, `implemented`, `merged`, `surface_opened`, `surface_closed`, `auto_decision_taken`, `retry_exhausted`, `state_drift_detected`, `manifest_corruption_suspected`, etc. Domain signals are *what happened in SDLC terms*; execution events are *what the engine did mechanically*.

### §3.3 Verb outcomes
The discriminated union every verb returns (see INV-DISCRIMINATED-OUTCOMES). The variant tag is the contract; downstream consumers never inspect the inner payload to determine success/failure.

> A producer of domain signals is also an emitter of execution events and a returner of a verb outcome. Three vocabularies, three audiences (the engine, the workflow author, the operator), no overlap.

---

## §4 Hard constraints

User-supplied, immovable:

- **3 retries on network/auth, never more.** Applies to all HTTP transport. Hardcoded ceiling, not configurable.
- **No GitHub Issues integration.** Domain signals deliver through other channels (UI, Teams, Hermes). GH Issues is explicitly out of scope.
- **5-class exit-code contract for external scripts:** `0` success, `2` usage, `3` permanent, `4` transient, `5` corruption. Any script Requiem invokes that doesn't honour this is an integration defect, not a runtime decision.
- **Test surface ships with v0.** The harness is not retrofitted. Every workflow node category and every verb outcome has at least one scenario.

---

## §5 What is out of scope (for v0)

Listed to prevent scope creep. Each may be reconsidered post-v0 with an ADR.

- Multi-operator simultaneous use (single power-user audience, single 4K monitor, 8+ hr/day)
- Teams / RBAC / multi-tenant deployment
- A workflow-authoring GUI (workflows are authored in code; the UI shows execution)
- Real-time collaborative inspection (one operator viewing a run at a time)
- The `polyphony` CLI as a permanent dependency (Requiem replaces it; old polyphony is a reference, not a runtime)
- Conductor as a permanent dependency (Requiem replaces it; conductor source is a reference, not a fork)

---

## §6 Decision provenance

The invariants in §2 are derived from prior analyses captured in [`docs/references/`](references/):

- **error-handling-deep-dive.md** + Boulez/Ravel reviews — the eight-seat Opus-4.7-high deep dive that produced INV-RESTART, INV-NO-CORRUPT-FORWARD, the receipts pattern, the 20-signal seed catalogue, the cancel-short-circuit, the abandonment typology, and the discriminated-outcome shape.
- **polyphony-parity-inventory.md** — the catalogue of what "no meaningful regression" means at v0.
- **platespinner-survey.md** — design source for the UI binding contract (`run.events.jsonl` → SSE → live trace).
- **workflow-viz-research.md** — state-of-the-art reference for the UI's visual primitives and layout.

When in doubt, the deep-dive reviews (Boulez + Ravel) define the bar for adding to or changing §2.
