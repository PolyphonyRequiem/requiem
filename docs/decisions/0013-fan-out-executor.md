# ADR 0013 — Fan-Out Executor (dispatch implementation per implementable leaf)

**Status:** PROPOSED — **blocked** (design recorded; not implemented this slice)
**Date:** 2026-06
**Author:** Recorded by the design seat; design pressure-tested by a rubber-duck
critique that verified every blocking claim against the code.
**Supersedes:** none
**Superseded by:** —
**Cross-cuts:** ADR-0005 (sub-workflow invocation primitive), ADR-0006
(merge-group topology — the branch-shape decision this design is blocked on),
ADR-0011/0012 (the plan→reality transitions that produce this design's input).

---

## TL;DR

The "fan-out executor" is the single biggest remaining v0 parity gap: recursive
`planning` produces a tree, `commit_plan` (ADR-0011) seeds its children into ADO,
and `plan_pr` (ADR-0012) surfaces the plan — but **nothing dispatches the
implementable leaves into the `implementation` workflow**. Recursion + seeding
are inert without it.

This ADR records the *design* of that executor and the **three architectural
blockers** that make a correct, production-real fan-out premature to build today.
It is published PROPOSED-blocked so the analysis (and the footguns it avoids) are
durable, and so the unblocking decisions can be made deliberately rather than
discovered by shipping a silent-failure.

## Intended design (once unblocked)

A new `requiem.workflows.fan_out` workflow that:

1. `load_committed` — reads the approved `.plan.tree.json` (+ the
   `.plan.committed.json` synth→real `id_map`), fails closed unless
   `schema_version >= 2` and top-level `verdict == "approved"`, structurally
   validates node shapes (mirroring `commit_plan.load_tree`,
   `commit_plan.py:304-376`), and enumerates **implementable leaves =
   `decomposable == False`** nodes depth-first, mapping each synth `item_id`
   through `id_map` to its real ADO id.
2. Dispatches `implementation` **once per leaf** using the canonical
   bounded-slot fan-out idiom (`prep_leaf_i` script + `impl_i` subworkflow for
   `i` in `1..MAX_LEAVES`, `prep_leaf_i` short-circuiting empty slots to
   `aggregate`), exactly as `planning.py:1264-1319` fans out over children.
3. `aggregate_impls` — walks the completed slots, reads each child log, projects
   the per-leaf PR result, and renders a batch verdict card.

`MAX_LEAVES` is an explicit product cap (overflow → human gate), `dry_run`
defaults on, and the projector/`verdict_card`/`_default_gate_handler`/argparse
shell mirror `plan_pr.py` and `commit_plan.py`.

## Blockers (all verified against the code)

### B1 — Dispatched children cannot receive real `provider` / `toolbelt`

The kernel forwards a sub-workflow's `inputs_verb` dict to the child
`build_engine` **as kwargs filtered by `inspect.signature`** (kernel.py:543-567)
— only JSON-serializable flat keys that appear in the child's signature land.
`provider`, `toolbelt`, and `gate_handler` are live objects, never forwarded.
`implementation.build_engine` with no `provider`/`toolbelt` falls back to
`happy_path_provider()` (a canned LLM) and a toolbelt with `_DemoGhClient` /
`_DemoTwigClient` over a **real** `RealGitClient` (implementation.py:1390-1401).

**Consequence:** a "real" fan-out would run real git operations while using a
fake ADO/GitHub and canned coder output — and *look successful*. That is worse
than failing. **A real fan-out is impossible until child-engine seam propagation
exists** — via contextvars (the established pattern: planning propagates
`gate_handler` through `_active_gate_handler_cv`, planning.py:139-154) or a
fan-out shim that injects the real seams. Flat `ImplementationInputs` kwargs are
necessary but **not sufficient**.

### B2 — `impl_i` success ≠ leaf implemented

The kernel maps **any** child `Completed(disposition == "completed")` to parent
`Success`, regardless of the child's `final_node` (kernel.py:765-772). But
`implementation`'s `end_handoff` terminal — reached on bad coder output, tests
still failing after revision, push failure, or PR-create failure — is
`disposition="completed"` (implementation.py:1048). So a naive
`impl_i.success → next leaf` edge would treat those handoffs as successes and
march on.

**Required mitigation:** a per-slot `classify_impl_i` script that reads the
child's `final_node` (the kernel *does* surface it as `child_final_node` in the
Success value, kernel.py:768-771) and routes handoff/failure outcomes to a human
gate. Do **not** route raw subworkflow `success` to the next leaf.

### B3 — Conflicts with the accepted branch topology (ADR-0006)

`implementation` hard-codes `feature/{item_id}` (implementation.py:385). ADR-0006
recommends `feature/<root>` trunk + `impl/<root>-<item>` leaf branches
(0006-merge-group-topology.md:323-360, 437-460). Flat children straight to `main`
is ADR-0006 **Option B**, explicitly acceptable only for a *re-scoped* v0 — it
does not provide atomic co-merge, cross-sibling dependencies, or trunk-level
gating. A fan-out built on `feature/{item_id}` bakes in the topology ADR-0006
moves away from.

### B4 (secondary) — Fresh-run idempotency is weaker than resume

`implementation` refuses a dirty worktree (implementation.py:475-500) and cuts
each `feature/<leaf>` from `main`, so *resume* of one fan-out run is safe. But a
**fresh** fan-out over the same committed plan finds the prior `feature/<leaf>`
branches already present; `implementation.create_branch` then returns
`NeedsHuman("branch_exists_foreign")` (implementation.py:516-550) before PR reuse.
Fan-out is **resume-idempotent, not fresh-run-idempotent** — must be documented
honestly, or fixed via branch adopt/worktree isolation (#5).

## Decision

**Do not build the fan-out executor until B1–B3 are resolved.** Specifically:

* **B1** depends on a child-engine real-seam propagation primitive (contextvars
  or a shim) — a foundational change that unblocks *all* real sub-workflow
  dispatch (full_sdlc production wiring, the root orchestrator), not just
  fan-out. Build that first, on its own merits.
* **B3** depends on the v0 branch-model decision: ship ADR-0006 **Option B** as a
  named stopgap for *independent* leaves, or wait for the `impl/<root>-<item>`
  topology. This is a product call (scope vs. correctness) for the maintainer.
* **B2** and **B4** are then mechanical once B1/B3 are settled (the classifier and
  the resume-only-idempotency contract).

When unblocked, the design above stands: bounded-slot dispatch + per-slot
classifier + id-map-resolved leaves + explicit `MAX_LEAVES` cap.

## Consequences

* The parity tracker's "fan-out executor" row is **blocked-by-B1/B3**, not merely
  "missing" — closing it requires the seam-propagation and branch-model work, not
  just this workflow.
* `commit_plan` + `plan_pr` (non-negotiable #6) remain the furthest-right shipped
  parity slices; the executor that consumes their output waits on the above.
* The two footguns (B1 silent-success, B2 handoff-as-success) are now recorded so
  the next attempt starts from the correct contract.
