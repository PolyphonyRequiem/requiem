# ADR 0031 — R4: Read-only work-state projection + first-class dates

**Status:** Proposed (2026-06-22)
**Date:** 2026-06-22
**Relates to:**
ADR-0024 (RepoPlatform — the abstraction that lets the projection inspect branches/PRs on either GitHub or ADO),
ADR-0028 (auth — the MSAL refresh chain that lets the projection call ADO without `az login`),
ADR-0030 (context pack + cost rollup — both projection consumers in later phases),
ADR-0017 (handoff wire contract — the leaf-pr-map artifact this projection reads).
**Inspiration:** Starbright consumer requirements doc (2026-06-19), R4 specifically.

---

## TL;DR

Ship a **read-only JSON projection** of any work-item tree, surfacing:
- The full ADO hierarchy (parent → child → grandchild) as nested dataclasses.
- First-class ADO date fields (`Microsoft.VSTS.Scheduling.StartDate`/`TargetDate`/`FinishDate`).
- Per-leaf artifact linkage (impl branch existence, leaf PR number/URL/state).

Three surfaces:
1. `requiem.projections.work_state.compute_work_state(...)` async function — programmatic API.
2. `requiem state --item N` CLI subcommand — operator-readable rich tree + `--json` for machine consumption.
3. `GET /api/state/<item_id>` dashboard endpoint — JSON only; consumers wire their own UI.

This ADR closes R4 from Starbright's consumer-requirements doc. **R3 (computed roll-up) is explicitly OUT of scope** — that's the next ADR, and it will consume this projection's `tree` as input.

---

## Context

A consumer (Starbright) needed a "where are we vs. launch" projection of any work-item tree. Of their four requirements:

| Req | What | Status before this ADR |
|---|---|---|
| R1 | Structured work-item ↔ artifact links | Already met (commit_plan markers + branch grammar) |
| R2 | Queryable hierarchy tree | Already met (plan_tree.json + ADO hierarchy links) |
| R3 | Computed roll-up status (no hand-set parents) | Genuine new work; next ADR |
| R4 | Read-only projection + typed dates | Mostly met; this ADR formalizes it as a public surface |

R4 was the natural starting point because R3 needs R4's data model. Building R3 directly would have entangled "what state surface do we project?" with "how do we derive roll-ups?" — better to land the data layer first and let R3 ratify the derivation rules on top of stable inputs.

---

## Decision

### 1. `WorkItemNode` + `WorkStateProjection` dataclasses

```python
@dataclass(frozen=True)
class WorkItemNode:
    item_id: int
    title: str
    work_item_type: str        # Scenario / Deliverable / Task / etc.
    state: str                 # raw ADO state — Proposed / Active / Resolved / Closed
    start_date: str | None     # ISO 8601 from Microsoft.VSTS.Scheduling.StartDate
    target_date: str | None
    finish_date: str | None
    parent_id: int | None
    children: list["WorkItemNode"]
    # Artifact linkage (R1 surfacing)
    impl_branch: str | None    # branch_model.impl_branch(root, item) if branch exists
    leaf_pr_number: int | None # from leaf-pr-map or active search
    leaf_pr_url: str | None
    leaf_pr_state: str | None  # open / merged / closed

@dataclass(frozen=True)
class WorkStateProjection:
    root_item_id: int
    computed_at: str           # ISO 8601 UTC
    tree: WorkItemNode
```

Both dataclasses expose `to_dict()` for JSON round-tripping.

**Carries raw ADO state.** A `Deliverable` whose three child Tasks all merged still shows `state="Proposed"` if nobody flipped it in ADO. R3 will add a `computed_state` field derived from the children — that derivation is deliberately deferred.

### 2. `compute_work_state(...)` driver

Async function that:
1. Walks ADO hierarchy via `twig.show_async` (uses the existing parallel-fetch path that twig already provides).
2. For each node, reads `state`, dates, title, type, parent from the raw ADO payload.
3. Computes `impl_branch` via `branch_model.impl_branch(root_item_id, item_id)`.
4. For PR linkage: 3-tier resolution
   - Tier 1: `repo_client.find_open_pr_for_branch(impl_branch)` — open PRs only.
   - Tier 2: leaf-pr-map artifact + `repo_client.pr_view(pr_number)` — surfaces *merged* PRs (the open-only search can't see them).
   - Tier 3: None.
5. Returns `WorkStateProjection` with `computed_at` (UTC ISO 8601, injectable via a `clock` kwarg for testability).

Errors degrade silently to no-PR rather than failing the whole tree. **Projections are best-effort by design** — a transient ADO blip on one leaf must NOT torch a 24-leaf fetch.

### 3. `requiem state` CLI

```
requiem state --item 62759077 \
  [--ado-repo microsoft/CloudVault/cloudvault-service-api | --github-repo Owner/Repo] \
  [--log-dir .runs] \
  [--json]
```

Default: rich tree rendering with `item_id  type  state   title  [PR#NNN merged]  [target: 2026-08-01]` per row.
`--json`: full `WorkStateProjection.to_dict()` for downstream consumers.

### 4. `/api/state/<item_id>` dashboard endpoint

Mirrors the existing `/api/runs/<run_id>` JSON-handler pattern. Query string carries `ado_repo` or `github_repo`. Returns 400 on invalid item_id, 503 on construction failure (auth missing), 200 + JSON otherwise.

**JSON only.** No HTML rendering — the consumer (Starbright) wires their own UI on top. v0 is data plumbing.

### 5. `AdoClient.get_work_item(...)` helper

Direct ADO REST `GET /{org}/{project}/_apis/wit/workitems/{id}?fields=...` helper that lets callers project the minimum field set rather than fetching the default hundreds-of-fields payload. Not used by `compute_work_state` today (which rides on `twig.show_async`) but available for direct-REST consumers (future R3 roll-up may use it for batch reads).

---

## Scope

### In scope
- `src/requiem/projections/` package (`__init__.py` + `work_state.py`).
- `AdoClient.get_work_item` + `FakeAdoClient.get_work_item` for tests.
- `requiem state` CLI subcommand.
- `/api/state/<item_id>` dashboard endpoint.
- 17 hermetic projection tests + 1 CLI test + 1 dashboard test.

### Out of scope (explicit deferrals)
- **R3 (computed roll-up).** Lives in the next ADR. R4 carries the raw inputs; R3 adds derived state.
- **HTML rendering in the dashboard.** Consumer wires their own UI on top.
- **Cross-tree analytics.** Excluded by north-star §5.
- **Writing back to ADO.** Strictly read-only. Whatever R3 computes will also be read-only — the human owns ADO's `State` field.
- **Pricing dates in $.** Date math (days-to-X, ahead/behind a target) is a consumer concern.

### What this ADR refuses to change
- No invariant changes.
- No new outcome variant.
- No new event kind (the projection reads world state, not the event log; consumers read it from CLI or HTTP).

---

## Consequences

### Positive
- Starbright's R1, R2, R4 are now publicly addressable via one JSON endpoint.
- R3 has stable inputs to design against.
- The `requiem state` CLI is the operator-facing answer to "where is this Scenario?" without leaving the terminal.
- Adding a field to `WorkItemNode` is a one-place change; the CLI + dashboard + tests all consume the dataclass.

### Negative
- Latency: a deep tree (Scenario → 4 Deliverables → 5 Tasks each = 24 ADO reads) is bounded by `twig.show_async`'s parallelism. For now this is fine; a batch-read optimization (using `get_work_item` with comma-separated IDs) is straightforward when needed.
- The leaf-PR-map artifact-discovery fallback assumes the operator's `--log-dir` points at a runs directory with the right artifacts. Empty log_dir → no merged-PR detection (only open PRs surface). Documented in `compute_work_state` docstring.
- The projection silently swallows per-node ADO/repo errors. Operators with strict expectations may want a "fail-loud" flag later — not v0.

---

## STATUS log

- **2026-06-22 PROPOSED.** Plan written.
- **2026-06-22 SHIPPED on feat/r4-projection-dates.** `WorkItemNode`/`WorkStateProjection` dataclasses + `compute_work_state` driver + `requiem state` CLI + `/api/state/<item_id>` dashboard endpoint + `AdoClient.get_work_item` helper. 19 new tests (17 projection + 1 CLI + 1 dashboard). Full suite 1030/1030 passing (1 deselected fanout-worktree-race flake, 150 skipped per existing pattern).

## References

- `docs/decisions/0024-repoplatform-protocol.md` — `RepoPlatform.find_open_pr_for_branch` + `pr_view` (the PR-resolution surface).
- `docs/decisions/0028-ado-auth-refresh-chain.md` — auth chain that lets the projection call ADO.
- `src/requiem/branch_model.py` — `impl_branch(root, item)` derives the per-leaf branch name.
- `src/requiem/projections/work_state.py` — implementation.
- Starbright consumer-requirements doc, R4 (2026-06-19).
