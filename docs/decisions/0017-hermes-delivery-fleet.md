# ADR 0017 — Hermes Delivery Fleet (Option A, applied)

**Status:** Accepted
**Date:** 2026-06-05
**Relates to:** ADR-0014 (Hermes fan-out executor), ADR-0016 (legible
accumulation), ADR-0006 (merge-group topology), ADR-0007 (PR lifecycle)
**Supersedes:** none

## Context

ADR-0014 made each implementable leaf a Hermes kanban task. The 2026-06-05
design discussion settled the larger shape:

- **Option A (chosen):** Hermes kanban is a *dumb delivery substrate* + a
  *specialist-profile fleet*. Requiem stays the **decomposition authority** and
  **ADO system of record**. It emits its committed leaf graph as explicit
  kanban create+link tasks routed to profiles, with **Manual orchestration**
  (Hermes `decompose`/`swarm`/auto-orchestrator OFF on Requiem boards).
- **Option B (rejected):** hand orchestration to Hermes (`decompose`/`swarm`
  own the work graph + completion judging). Rejected — it cedes the
  type-agnostic plan tree Requiem exists to own.

A rubber-duck critique surfaced three correctness traps that this ADR must
resolve, not gloss.

## Decision

### 1. Roles are data; profiles are a hydrated fleet — IMPLEMENTED

`process.yaml` gains a **role→profile routing** dimension (agnostic, same
pattern as the tier model — never hardcoded):

```yaml
# .requiem-config/process.yaml
roles:
  implementer: { profile: requiem-implementer, skills: [coder], model: null }
  reviewer:    { profile: requiem-reviewer,    skills: [code-review] }
  closer:      { profile: requiem-closer,       skills: [] }
```

The `requiem-*` profiles ship as **profile distributions** (each a directory
installed via `hermes profile install`), under `fleet/` in this repo. Their
durable knowledge is repo-resident doctrine (ADR-0016), not profile memory.

Each profile carries an **identical** `skills/handoff-receipt/SKILL.md` — the
emit side of the §4 wire contract, single-sourced so a fleet member cannot
speak a divergent dialect. `tests/test_fleet_distribution.py` keeps the skill,
`requiem.handoff.parse_handoff`, and `tests/fixtures/handoff_v1_golden.json` in
lockstep: it round-trips each skill's documented receipt through the real
parser, asserts the skill is byte-identical across all three profiles, and
asserts `config.yaml` ships Manual orchestration (`auto_decompose: false`).

### 2. Hermetic, Requiem-managed, containerized fleet — IMPLEMENTED (pure half)

The fleet runs in a **pinned container Requiem ensures per run** (not an
operator precondition). The container, by construction, provides:
- pinned Hermes version + gateway/dispatcher config + **Manual** orchestration;
- **clean profile homes per run** → no cross-run task-state leakage (this is
  the *enforcement* of ADR-0016's house-style/task-state line);
- a reproducible substrate whose identity (image hash, Hermes version, profile
  distribution version, doctrine hash, models) is **snapshotted into the
  Requiem event log** — same durability pattern as ADR-0015's config snapshot.

The two halves are kept distinct (the load-bearing correction): **reproducibility
comes from immutable inputs** (the baked `fleet/` distributions — `deploy/`
Dockerfile bakes them read-only) and **clean execution comes from a fresh
per-run `HERMES_HOME`** (`deploy/entrypoint.sh` provisions one per run and
installs the template into it — a persisted profile home is never reused).

A **preflight** fails closed if: a required profile is missing, orchestration
is not Manual, the gateway dispatcher is disabled, a profile home escapes the
run root, a profile enables unauthorized writable memory, or any pinned
version/hash cannot be corroborated. This is implemented as PURE, fully
unit-tested logic in `requiem.fleet_preflight.evaluate_fleet`
(`FleetInventory`/`ExpectedFleet` contracts; `fleet_identity_snapshot` does the
schema-first repo-local artifact hashing). Two fail-closed postures:
"no roles configured" is NOT "no fleet required" (the baseline delivery roles
are required regardless), and "cannot verify a pinned dimension" is a failure,
not a skip. The single deferred piece is the operator-side adapter that turns a
**live** container into a `FleetInventory` (parsing `hermes profile list` /
`hermes config get`) — it requires a real gateway to build, and everything it
would feed is already judged by the tested pure logic.

### 3. Acceptance-gated release (the sharpest trap) — IMPLEMENTED

A kanban `done` is **evidence**, not authority. A worker can mark a leaf done
with a bad PR. Requiem's verifier/close_out adjudicates and reconciles ADO.

Therefore a plan-tree dependency edge means **"child may start only after
Requiem *accepts* the parent"**, NOT "after the parent worker finishes". Pure
mirror-with-links (letting Hermes promotion be the acceptance boundary) is
**unsafe**. The rule, as built in `kanban_executor.py`:

- Links are mirrored onto the board for a faithful view, but **Requiem owns
  release**: at dispatch only the dependency-free *ready frontier* is assigned;
  dependent children are created+linked but left UNASSIGNED. The `poll_kanban`
  loop releases a child (assigns it) only once every parent reaches a
  `delivered` disposition (a receipt: `done` + `completed` + result), then
  re-dispatches. Kanban link-promotion is never the acceptance authority.
- A child whose parent settled non-delivered (`needs_human` / `permanent_failure`)
  is transitively **blocked** and never released — not waited on forever.

**Scope correction (verified during build):** the committed plan tree is a pure
*decomposition* hierarchy (`plan_tree.ResolvedLeaf` carries no inter-leaf
dependency edge), so in the real path every leaf is independent and released
immediately — there is no tree-derived ordering to mirror. The acceptance-gating
machinery earns its keep on **explicit** `deps` (the demo path and future
callers). The earlier "derive real deps from the plan tree" framing was
inaccurate. What the build does deliver: unknown/self/cyclic deps **fail closed**
in a dispatch pre-flight (`_validate_dep_graph`) rather than being silently
skipped, and requiem — not Hermes promotion — owns child release.

### 4. The handoff metadata is an untrusted wire contract

The worker's `kanban_complete(metadata={...})` payload is Requiem's read-side
API and must be schema-versioned and contract-tested across the process
boundary (a golden-fixture test). Minimum shape:

```json
{
  "schema_version": 1,
  "leaf_id": "...", "root_item": "...", "plan_hash": "...",
  "branch": "...", "commit_sha": "...", "pr_url": "...",
  "changed_files": [], "tests_run": [],
  "worker_profile": "...", "worker_profile_version": "..."
}
```

Requiem independently verifies branch/PR/test claims where it can; it does not
trust the worker's self-report as truth. This contract is also the **executable
seam** between the two builder-specialists (see "Division of labor").

### 5. Idempotency keyed on plan identity — IMPLEMENTED

Idempotency keys carry the immutable plan hash:
`requiem:{root}:{plan_hash}:{leaf}`. `_plan_hash` digests every leaf's identity
fields (id, title, body, branch, skills, deps) in canonical order, so any plan
change moves the hash → fresh keys → a superseded plan's tasks are never
silently reused. `_reconcile` is now defence-in-depth: it compares the reused
task's idempotency_key, title, branch, and skills and **fails closed** (escalates
to a human gate) on any mismatch.

### 6. State translation table — IMPLEMENTED

An exhaustive kanban-state → Requiem-outcome map, realized in
`kanban_executor.translate_state` and threaded into every `poll_kanban`
per-leaf row as `requiem_outcome`:

| kanban state | Requiem outcome |
| --- | --- |
| non-terminal (todo/ready/running) | `in_flight` — keep polling (crash-reclaim retries land here; dispatcher owns bounded retry) |
| `done` + `completed` + result + valid evidence | `delivered` → verifier adjudicates downstream |
| `done` + `completed` but misattributed/invalid metadata | `needs_human` (never silently accepted) |
| `done` without a receipt (no result/outcome) | `needs_human` (ambiguous green) |
| `blocked` after a failure outcome (breaker tripped) | `permanent_failure` |
| `blocked` (worker asked for human) | `needs_human` gate |
| `archived` | `permanent_failure` (removed) |
| missing profile | preflight fail-closed (never dispatched) |

Worker evidence (`metadata` on the run row) is parsed through the untrusted
`requiem.handoff` contract; a payload whose `leaf_id`/`root_item`/`plan_hash`
does not match the leaf it is attached to **downgrades to `needs_human`** rather
than being trusted.


### 7. Write-back loop — legible core IMPLEMENTED (ADR-0016 §4)

When a run produces a durable learning, Requiem opens a PR against the
repo-resident doctrine. The **legible core** — the part that makes write-back
legitimate under ADR-0016 — ships in `requiem.doctrine_writeback`: a candidate
learning becomes a **reviewable, provenance-bearing, idempotent** proposed
doctrine edit (`propose_doctrine_section` → `DoctrineProposal`, with
`render_pr_title`/`render_pr_body` tracing the change to the run id, rationale,
and evidence, and `is_stale` guarding against doctrine drift). Sections are
fenced by a stable marker so a re-discovered learning updates in place or
no-ops, never stacking duplicates. Fully unit-tested
(`tests/test_doctrine_writeback.py`).

Two pieces remain deferred (each genuinely out of the current foundation):
the **trigger** (deciding *what* is a durable learning — no run has yet produced
a learning-extraction signal) and the **live PR-open** (a thin composition over
the existing `FsClient` branch/push + `GhClient.pr_create`, validated against a
live repo). Structured `process.yaml` edits are a separate, harder proposal
shape and are also deferred; the implemented target is doctrine (free-form
markdown).

## What this does NOT solve

Option A supplies *primitives*, not the logic for:
- **Merge-group branch topology** (ADR-0006, `mg/`+`impl/`) — Requiem sequences
  kanban tasks and passes `--branch`; Hermes won't build the stack.
- **ADO PR lifecycle** (ADR-0007) — the worker may open a PR, but ADO PR-state
  reconciliation, criteria, and close_out remain Requiem's, driven off the
  handoff metadata contract (§4).

## Division of labor (builder squad)

Two specialist tracks, split *only after* the contract (§4) + foundation are
locked by a single owner, to avoid Tchaikovsky-class fake/real divergence:

- **Profile-distribution specialist** — authors the `requiem-*` distribution
  (SOUL/skills/description/model), the doctrine artifact, fleet image.
- **Kanban-delivery specialist** — real deps + acceptance-gated release,
  plan-hash idempotency, state translation, metadata ingestion.

They **co-own the contract test** (§4); neither changes behaviour without
updating the shared golden fixture.

## Consequences

### Positive
- Reproducible delivery; ADR-0013 B1 (in-process children lack a real provider)
  stays permanently moot for implementation.
- `implementation.py` trends toward a thin delivery shim.

### Negative
- New external dependency surface (containerized gateway) Requiem must manage
  and preflight.
- A cross-process contract to maintain (mitigated by the golden-fixture test).

## References

- ADR-0014, ADR-0016, ADR-0006, ADR-0007, ADR-0015
- `src/requiem/workflows/kanban_executor.py`, `src/requiem/clients/kanban.py`
- rubber-duck critique, 2026-06-05 (acceptance-gating, idempotency, contract)
