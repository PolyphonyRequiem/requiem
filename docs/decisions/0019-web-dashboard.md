# ADR 0019 — Web dashboard (read-only event-log viewer)

**Status:** Accepted (2026-06-09) — phase 1 (read-only) implemented; gate
resolution (write path) deferred to phase 2.
**Date:** 2026-06-09
**Relates to:** ADR-0001 (single-process architecture), ADR-0002
(event-log-authoritative persistence), v0 non-negotiable **#8** (human gates in
both terminal *and* web dashboard).

## Context

v0 non-negotiable #8 requires human gates to be serviceable from **both** a
terminal and a **web dashboard**. The terminal half is done (`requiem events`,
`requiem watch`, the `rich` renderer registry, `requiem unblock`/gate handlers).
The web half was the last fully-absent non-negotiable with no external blocker —
no credentials, no Hermes-core seam, no ADO. The parity audit listed it as `❌`.

Requiem is deliberately a **single-process, light-dependency** engine (ADR-0001):
runtime `dependencies` are only `pydantic` + `pyyaml`; even `rich` is an optional
`cli` extra. A web dashboard must not betray that — pulling FastAPI + uvicorn +
starlette into the dependency tree for a status viewer would be a poor trade.

The event log is the single source of truth (ADR-0002,
INV-EVENT-LOG-AUTHORITATIVE): a `.events.jsonl` file per run, folded by
`persistence.replay()`. Everything the CLI shows is a *projection* of that log.
A dashboard is therefore "just another projection + a transport."

## Decision

Ship a **stdlib-only, read-only** dashboard as a new `requiem.dashboard` package.

### 1. No new runtime dependencies

The server uses Python's stdlib `http.server` (`ThreadingHTTPServer` +
`BaseHTTPRequestHandler`). The UI is a single self-contained HTML page (vanilla
JS `fetch`, no build step, no framework, no CDN). There is **no** new entry in
`[project.dependencies]` and no `dashboard` extra is required — it runs on a bare
`pip install -e .`. This mirrors the `rich`-optional discipline: the dashboard is
a convenience surface, not a load-bearing dependency.

### 2. Projection is pure and shared

`requiem.dashboard.projection` holds **pure functions** over a `--log-dir`:

- `list_runs(log_dir) -> list[RunSummary]` — one row per `*.events.jsonl`,
  reusing the exact status logic the CLI's `_summarize_run` uses (run_started →
  workflow; gate_opened → Suspended; run_completed.terminal → Completed/Failed/
  Cancelled).
- `run_detail(log_dir, run_id) -> RunDetail` — the full event timeline, each
  event humanized into `{glyph, kind, node, summary, ts}` without importing
  `rich` (the CLI renderer registry stays CLI-only).
- `pending_gates(log_dir) -> list[PendingGate]` — every run whose last gate is
  `gate_opened` with no following `gate_resolved`/`run_completed`: the
  actionable human-gate queue #8 is fundamentally about.

These functions return plain dataclasses/dicts (JSON-able), so they are unit
tested directly with **no browser and no socket** — the same way every other
requiem projection is tested.

### 3. The server is a thin transport

`requiem.dashboard.server` exposes:

- `GET /` → the HTML page (static string).
- `GET /api/runs` → `list_runs` as JSON.
- `GET /api/runs/<run_id>` → `run_detail` as JSON.
- `GET /api/gates` → `pending_gates` as JSON.

Bound to `127.0.0.1` by default (a local operator tool, not a public service).
Read-only: no route mutates anything. Launched via
`python -m requiem.dashboard --log-dir .runs --port 8770` (and a
`requiem-dashboard` console script).

### 4. Gate resolution (write path) is phase 2

The dashboard #8 endgame is *servicing* a gate from the browser (the equivalent
of `requiem unblock`). That introduces a mutation surface (auth, CSRF-ish
concerns even on localhost, writing a `gate_resolved`/cancel event into a live
run's log) and must be designed carefully against INV-EVENT-LOG-AUTHORITATIVE and
the kernel's resume semantics. **Phase 1 ships read-only** — it already closes
the "operators can *observe* runs and the gate queue in a browser" half and is
safe to land without touching the write path. Phase 2 (a `POST /api/gates/<run>`
that appends a gate-resolution event the next resume consumes) is a follow-up
ADR amendment.

## Consequences

**Positive:** closes the last externally-unblocked #8 surface with **zero** new
dependencies; the projection layer is pure and reuses the CLI's status semantics,
so the two can't drift; trivially testable without a browser; the read-only slice
is safe to land and review on its own.

**Negative / open:** gate *resolution* from the browser is deferred (phase 2),
so #8 remains 🟡 (terminal-complete, web-observe-only) until that lands; the
stdlib `http.server` is single-purpose and not hardened for hostile networks —
it binds to localhost and is explicitly an operator-local tool, not a
multi-tenant service; the HTML page is intentionally minimal (no live websocket
push — the page polls `/api/*`), which is adequate for an operator glance and
keeps the transport stdlib-only.

**Why an ADR:** the dependency-discipline decision (stdlib `http.server` over a
web framework) and the explicit read-only-first / gate-resolution-deferred split
are choices a future contributor will question ("why isn't this FastAPI?",
"why can't I click to unblock?") — recorded here with the reasoning.
