# ADR 0011 — Plan-Commit Seeding (`commit_plan` workflow)

**Status:** ACCEPTED (Phase C parity slice — non-negotiable #6, "child seeding")
**Date:** 2026-06
**Author:** Recorded by the design seat; design hardened by a rubber-duck critique pass.
**Supersedes:** none
**Superseded by:** —
**Cross-cuts:** ADR-0005 (sub-workflow invocation — planning's recursion), ADR-0006 (merge-group topology — plan generations / Q6 stable ids), ADR-0008 (curated artifacts, forthcoming — the freeze/supersede lifecycle this ADR defers to).

---

## TL;DR

The recursive `planning` workflow produces **proposals**, not commitments: it
synthesises child ids (`parent*100 + index+1`) and never writes to ADO. This
ADR adopts a **separate `commit_plan` workflow** that consumes the approved
`.plan.tree.json` artifact and **idempotently seeds the proposed children as
real ADO work items**, depth-first, replacing the synthesised ids with real
ones via a recorded `synth → real` id map.

Planning stays a pure decision producer — its recursion and INV-RESTART
guarantees are untouched. Seeding is a deliberate, separately-invoked
plan→reality transition. This is the natural future home for the plan-PR
(ADR-0008) when that lands.

## Decision

### Why a separate workflow (option B), not seeding inside the recursion (option A)

Option A (seed each child before recursing into it) would give every subtree
real ADO ids from the start, but it injects ADO side-effects and create-
idempotency into the **most INV-RESTART-critical, most-tested** code path in
the system. Option B keeps that path pure and confines all ADO writes to one
new, separately-invoked workflow. The `planning` module docstring already
anticipated "a future commit-plan workflow turns proposals into real ADO
items." Lower blast radius, cleaner separation, composes with the plan-PR.

The cost of B — the deferred commit must reproduce the **entire** tree, and the
serialized tree must carry creatable metadata at every depth — is paid by a
small additive enrichment to planning's serialization (below).

### Part 1 — self-describing plan artifact (`schema_version = 2`)

`PlanResult` gains a `proposals` list; `project_plan_result` /
`_plan_result_to_dict` / `_plan_result_from_dict` carry it through the
recursive serialization. The `.plan.tree.json` is stamped with
`schema_version: 2`. Each recursive node now carries both its own `proposals`
(creatable metadata) and its `children` (recursive sub-plans), so `commit_plan`
needs **no** access to per-sub-run event logs — the single artifact is portable
and complete. `commit_plan` refuses artifacts below version 2.

### Part 2 — `commit_plan` topology

`start → load_tree → seed_tree → write_manifest → end_success`
(with `end_failed` / `end_human` terminals).

- **load_tree** parses + *recursively validates* the artifact before any write:
  schema_version ≥ 2; top-level verdict exactly `approved` and `decomposable`;
  every node's verdict approved (including policy-forced leaves);
  `len(children) == len(proposals)` for each
  decomposable node; `children[i].item_id == expected_synth` (alignment is
  explicit, not positional trust); and a total-create **size cap** (refuses
  oversized trees). Bad/missing/oversized → `end_failed`.
- **seed_tree** is one async verb performing a recursive depth-first walk.
- **write_manifest** writes `.plan.committed.json` (synth→real `id_map`, the
  created/reused ledger, `dry_run` flag).

### Idempotency — marker, not (title, type)

Each created item's **description is stamped** with a visible, versioned marker:

```
Requiem-Lineage-v1: scenario_id=<root-id> plan_id=<plan-id> synth_id=<synth-id>
```

ADO strips HTML comments from `System.Description`, so the original hidden
`<!-- requiem-commit ... -->` representation was not durable across fresh
fenced runs. The visible record above survives ADO's HTML conversion and is
written in the same create operation as the item.

On every (re)run, `seed_tree` lists the parent's existing children and matches
by **lineage first**. The lineage is keyed on root Scenario + `plan_id +
synth_id` (NOT the commit run id), so a second commit of the same plan — or a
resume after a crash — reuses the already-seeded item rather than duplicating
it. The authoritative read-back must also prove the exact parent, title, type,
and uniqueness before reuse. A human rename is therefore treated as drift and
routes to reconciliation rather than silently reusing a changed item.

`(title, work_item_type)` is **not** an adoption fallback. If an existing child
matches a proposal but lacks the current plan marker, seeding routes to
`NeedsHuman` rather than guessing or creating a duplicate. Planning must first
regenerate an approved, aligned artifact that explicitly pins the existing
item.

**Pinned proposals** (`item_id` set by the planner) mean "this ADO item already
exists." Before reuse, `commit_plan` revalidates the item through `show_async`
and the parent's authoritative child list: parentage must match, title/type
must match exactly, the item must carry a Requiem marker rooted at the same
Scenario, and it must be the sole exact title/type candidate under that parent.
Conflicting or ambiguous lineage routes to `NeedsHuman`; `create_child` is
never called for a pinned id.

Legacy manifests can be migrated with
`requiem migrate-plan-lineage --item <root> --manifest <path> --twig-cwd
<workspace>`. The command validates the complete live Scenario hierarchy and
every manifest mapping before any write; `--apply` appends durable markers and
then re-reads the hierarchy. Partial application is restart-safe because
already-correct markers are accepted and conflicting markers fail closed.

### Failure mapping (Ravel L-1)

Inside `seed_tree`: `TwigRateLimitedError → RetryableFailure`;
`TwigItemNotFoundError → PermanentFailure(not_found)`;
timeout-like `TwigUnknownError` (exit -1 / empty stderr) →
`RetryableFailure`; other `TwigUnknownError` cases still route to
`NeedsHuman(retry/abort)`. Whole-verb re-run is safe by construction
(marker dedupe). On failure the outcome carries the partial-progress
ledger so the operator sees what was already created.

### Dry-run default ON

Per the established convention (`close_out`), this ADO-mutating workflow
defaults to `dry_run=True`. Dry-run walks + lists but never creates, and
reports an **explicit** preview shape — `would_create`, `would_reuse`,
`ambiguous`, `missing_pinned` — so it never silently masquerades as a
deterministic plan. Operators opt into real seeding with `dry_run=False`.

## Deferred (NOT in this slice)

**Freeze / supersede lifecycle.** Committing a plan is conceptually a
**plan-generation freeze point**: re-planning after a commit should *supersede*
the prior generation and explicitly close/retire orphaned items, rather than
silently leaving them. The mechanism for this depends on the unbuilt
merge-group + plan-generation subsystem (ADR-0006 plan generations /
`RetiredMergeGroupIds`) and the curated-artifact lifecycle (ADR-0008). This
ADR records the intent and **scopes the mechanism out**; for now `commit_plan`
is idempotent within a single plan generation and a second commit of the same
plan is a safe no-op, but cross-generation supersession is future work.

## Consequences

- Recursive planning's seeding half (non-negotiable #6) is delivered without
  destabilising the recursion.
- The plan artifact becomes a portable, versioned, self-describing contract.
- A single recursive verb (rather than one kernel node per create) is
  acceptable **because** marker-based idempotency makes whole-verb re-run safe;
  the trade-off is coarser-grained checkpointing, mitigated by partial-progress
  reporting and ADO-reconstructable state.
- Open: cross-generation supersession, the size-cap default value, and whether
  `commit_plan` eventually folds into a higher-level root orchestrator.
