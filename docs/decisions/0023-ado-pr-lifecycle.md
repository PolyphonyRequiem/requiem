# ADR 0023 — Azure DevOps PR lifecycle (parity #10)

**Status:** Accepted + implemented (2026-06-09, `requiem.workflows.ado_pr`).
Logic unit-tested against a fake; **live ADO validation is a deploy-time step**.
**Date:** 2026-06-09
**Relates to:** `pr_lifecycle.py` (the GitHub sibling — the structural template),
ADR-0013 B2 (the `needs_human` surrender disposition), INV-NO-CORRUPT-FORWARD,
INV-EVENT-LOG-AUTHORITATIVE.
**Parity:** non-negotiable **#10** ("platform-specific PR lifecycles — GitHub
*and* ADO"). GitHub was ✅ via `pr_lifecycle`; ADO was ❌ (no module). This closes
the ADO half.

## Context

#10 requires requiem to drive a pull request to completion on **both** GitHub and
Azure DevOps. GitHub uses the `gh` CLI (`pr_lifecycle.py`). ADO uses the **Azure
DevOps REST API** authenticated with a PAT (`ADO_PAT`) — a different transport, a
different PR state model (`active`/`completed`/`abandoned`, `mergeStatus`,
completion via `PATCH status=completed`), and ADO work-item linkage.

The box this was built on has **no `ADO_PAT`**, so a literal live ADO run isn't
possible here. But the *entire codebase* is unit-tested against faithful fakes —
`pr_lifecycle` itself is tested against `FakePrToolkit`, not live GitHub, with
live GitHub validation done at deploy. An ADO lifecycle tested against a faithful
`FakeAdoPrToolkit` meets the identical bar; the PAT-bearing live run is the same
deploy-time step `pr_lifecycle` already has.

## Decision

Add `requiem.workflows.ado_pr` — the ADO sibling of `pr_lifecycle`, mirroring its
`Protocol`-toolkit seam and its event-log/outcome discipline.

### 1. Toolkit seam (`AdoPrToolkit` Protocol)

- `RealAdoPrToolkit` — Azure DevOps REST v7.1. `repo` is
  `"<org>/<project>/<repository>"`; PAT from `ADO_PAT` (HTTP Basic, empty user).
  `pr_view` / `mergeability` (GET the PR) and `complete_pr`
  (`PATCH status=completed` + `completionOptions.mergeStrategy`). Every non-2xx →
  `AdoPrError`; verbs translate to Outcomes (never swallow).
- `FakeAdoPrToolkit` — in-memory double seeded with a PR; `complete_pr` flips it
  to `completed` and records the call. Mirrors every Real method's async shape.

### 2. Workflow (core merge lifecycle)

```
start → fetch_pr → check_state → check_mergeable → complete_pr → update_item
                       │               │                              ↓
        already_completed→end    conflicts/policies→needs_human   end_completed
        abandoned/draft →needs_human
```

- `check_state`: `completed` short-circuits to `end_already_completed`
  (idempotent re-entry); `abandoned` → human; `draft` → a NeedsHuman gate.
- `check_mergeable`: `conflicts` or unsatisfied branch policies → human
  (INV-NO-CORRUPT-FORWARD — never complete over a red PR).
- `complete_pr`: `dry_run` is genuinely side-effect-free (no REST PATCH).
- `update_item`: best-effort `twig.set_state_async` on the linked work item(s) to
  the closed state — a twig hiccup after a successful completion must not fail the
  run.
- Surrender terminal is `needs_human_end` with `disposition="needs_human"`
  (B2-consistent), so a parent/driver treats an ADO surrender as a human handoff,
  not a false success.

### 3. Scope (v0)

The GitHub `pr_lifecycle` has an agentic review-comment **addressal loop**
(synthesize → address → push → re-poll). ADO's v0 parity bar is the **lifecycle +
work-item linkage**, not that agent loop — so `ado_pr` omits it (no agents). The
loop can be added later if ADO deployments need automated comment addressal.

## Consequences

**Positive:** closes the ADO half of #10 with the same seam/outcome/event-log
discipline as the GitHub side; fully unit-tested (10 cases) against a faithful
fake; `dry_run`-safe; fails closed; the Real toolkit's REST addressing + auth are
unit-checked (URL construction, repo split) even without a live PAT.

**Negative / open:** the Real toolkit's *network* paths (`pr_view`/`mergeability`/
`complete_pr` actually hitting ADO) are `# pragma: no cover` — they need a live
`ADO_PAT` + reachable org/project to exercise, which is a **deploy-time validation
step** (honestly flagged, not silently claimed). There is no ADO PR *opener* yet
(the merge-group `feature_pr`/`leaf_pr` equivalent for ADO repos) — v0 ADO targets
the completion lifecycle; opening is a follow-up.

**Why an ADR:** introducing a second PR-platform transport + its state model and
deciding the v0 scope (lifecycle without the agent loop) is a load-bearing parity
decision worth recording alongside the GitHub side.
