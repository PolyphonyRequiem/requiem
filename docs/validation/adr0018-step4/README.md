# ADR-0018 step 4 — live validation artifacts

Reproducible proof scripts for the trunk-integration driver wiring (#7). Both
run against a **throwaway scratch repo** you may push branches/PRs to (the live
runs used `PolyphonyRequiem/requiem-scratch-adr0018`). They need `gh` on PATH,
authed with `repo` scope.

## `q1_drift_probe.sh` — the Q1 branch-drift experiment

Resolves ADR-0018's "Open refinement" question: does a later leaf PR conflict
against `feature/<root>` once an earlier leaf has merged? Cuts leaves from `main`
HEAD (the Hermes worktree model), names them `impl/<root>-<item>`, advances the
trunk, and re-checks mergeability. Runs two regimes — **disjoint** leaves
(distinct files) and **overlapping** leaves (same line) — with real PRs and real
`gh` merges.

```bash
bash docs/validation/adr0018-step4/q1_drift_probe.sh
```

**Finding (2026-06-09):** drift is real but **conditional** — overlapping leaves
conflict, disjoint leaves don't, and it's **invisible pre-merge**. See ADR-0018
§"Open refinement — RESOLVED".

## `live_wiring_check.py` — end-to-end wiring proof

Drives the *same helper functions* `end_to_end.run_pipeline` /
`integrate_pipeline` call (`_resolve_base_branch`, real `trunk_bootstrap` →
`leaf_pr` → persist map → `feature_pr`) against the real `GhClient` and the
scratch repo. Asserts: base-branch resolution, trunk create + idempotent re-run
(`exists`, no force-move), leaf PRs opened `base=feature/<root>`, map round-trip,
and the trunk→base PR opened off the persisted map after a merge.

```bash
python docs/validation/adr0018-step4/live_wiring_check.py
```

> These are **manual** live checks (they mutate a real GitHub repo), deliberately
> kept out of the unit suite. The stub-engine equivalents that run in CI live in
> `tests/test_end_to_end_topology.py`.
