# Requiem v0 Parity-Readiness Audit — Mahler-3

**Date:** 2026-06 (Wave 5)
**Author:** Mahler-3 (parity audit seat)
**Scope:** Compare current Requiem `main` (`HEAD = 495609e`, post-PR #37) against
the polyphony+conductor feature surface enumerated in
[`polyphony-parity-inventory.md`](polyphony-parity-inventory.md), and rule on
v0 cutover readiness.

---

## 1. Executive Verdict

**Verdict: NO-GO for v0 as defined by §9 of the parity inventory (the ten
non-negotiables)** — though the gap has narrowed materially since the original
audit. Requiem is structurally healthy and the load-bearing invariants are
demonstrably enforced — the architecture has paid off — but **four of the ten
non-negotiables still have material gaps** that no amount of polish on the
existing surface can close. Specifically: per-item worktree isolation, the
tree-walking root orchestrator with in-process batch dispatch, ADO PR lifecycle,
and browser-side gate *resolution* remain absent or blocked. (The web dashboard
itself now exists read-only — see the 2026-06-09 update below.)

**Update (2026-06-09):** the seventh non-negotiable — merge-group implementation
topology — has since been **built end-to-end**: `branch_model` + `trunk_bootstrap`
+ `leaf_pr` + `feature_pr` + the `end_to_end` driver wiring (PR #61) + the
requirement-disposition gate (PR #62), all live-validated on a scratch GitHub
repo. What remains for #7 is the live **ADO** worker loop, which is
credential-gated, not a code gap. The sixth non-negotiable (recursive planning
*with child seeding and PR lifecycle*) is likewise closed — child seeding into
ADO (`commit_plan`, PR #59) and the plan PR (`plan_pr`, PR #60) both landed. The
Tchaikovsky rough edges (#29/#30/#31) are all closed.

If the operator chooses to **re-scope v0** to the demoable single-root linear
pipeline (dispatch → planning → implementation → GitHub PR lifecycle →
close-out) on GitHub-only, with a terminal-only UX, then the verdict flips to
**GO with caveats** — that slice works end-to-end and is covered by the test
suite (run targeted; a bare full `pytest` hangs).

The strong news: every Phase-A architectural bet held up under audit. The
event log is authoritative; sub-workflow log isolation is enforced by
construction; the 14-class crash-point matrix for INV-RESTART (incl. the new
class-13/14 sub-workflow points) is exhaustively pinned. Requiem's
*foundation* is at-or-better-than parity. The *workflow library and UI* are
not.

---

## 2. Parity Matrix

Status legend: `✅ at-parity` · `🔵 better-than-polyphony` · `🟡 partial` · `❌ missing` · `➖ N/A by design`

### 2.1 CLI verb catalogue (polyphony Program.cs:27-52)

Most polyphony verbs were JSON-stdout decision endpoints designed to span the
.NET-CLI ↔ Python-engine seam that ADR-0001 dissolves. Per
INV-SINGLE-PROCESS, Requiem replaces them with in-process Python functions
returning discriminated outcomes — there is no "verb CLI" to audit row-by-row.
The interesting question is *which decisions* the polyphony verbs encode and
whether those decisions are encoded *somewhere* in Requiem.

| Polyphony verb group | Decision encoded | Requiem location | Status | Evidence |
| --- | --- | --- | --- | --- |
| `validate`, `validate-config` | YAML schema validation | N/A — workflows are Python (`WorkflowBuilder`), validated at construction | ➖ N/A by design | `src/requiem/dsl.py:164-329` (`build()` raises on unreachable nodes, missing entry, etc.); `tests/test_dsl.py` (5 tests) |
| `hierarchy` | Walk ADO tree + annotate | Partial — `twig.show` returns `children`; no dedicated walk verb | 🟡 partial | `src/requiem/clients/twig.py:65-188`; no tree-walker workflow yet |
| `health` | Env / dep diagnostics | Missing | ❌ missing | (no equivalent) |
| `state preflight*`, `state detect` | Pre-run gate + root classification | Partial — `root_dispatch.validate_root` classifies, no preflight gate | 🟡 partial | `src/requiem/workflows/root_dispatch.py:1-44` |
| `state next-ready` | Dispatchable requirements | Missing — no edges-graph / facet model | ❌ missing | (no equivalent) |
| `plan depth-guard`, `plan derive-ancestor-chain` | Recursion safety | `guard_depth` + `ancestor_item_ids` in planning | ✅ at-parity | `src/requiem/workflows/planning.py:60-75` |
| `plan next-child`, `plan write-plan`, `plan seed-children` | Plan emission + ADO child create | Partial — plan emitted in-memory + (root) manifest; **no `twig create_child` call** | 🟡 partial | `src/requiem/workflows/planning.py:50-83` (recursion); no `twig.create_child_async` anywhere |
| `plan load-type`, `plan load-guidance` | Process-config-aware type defs | Missing — no `.requiem-config/` analog | ❌ missing | (no equivalent) |
| `plan review` | Aggregate reviewer pass/fail | Inline reviewer agent in planning | 🔵 better | `src/requiem/workflows/planning.py` reviewer arms — typed `BaseModel`, no JSON-stdout reaggregation |
| `policy/*` | Domain-scoped rule resolution | Missing | ❌ missing | (no equivalent) |
| `branch/*` | Branch + MG routing | Partial — implementation creates `feature/<item_id>`, no `mg/`, no `impl/` | 🟡 partial | `src/requiem/workflows/implementation.py:1-50` |
| `pr/*` (GitHub) | PR create / poll / merge | Implementation + PR-lifecycle | ✅ at-parity (GH only) | `src/requiem/workflows/{implementation,pr_lifecycle}.py` |
| `pr/*` (ADO) | ADO PR lifecycle | Missing | ❌ missing | (no `ado_pr` workflow module) |
| `worktree/*`, `lock/*`, `manifest/*`, `worklist/*`, `edges/*` | Run state primitives | Partial — root_dispatch writes manifest; no worktree, lock, edges, worklist verbs | 🟡 partial | `src/requiem/workflows/root_dispatch.py` write_manifest verb |
| `journal/*` | Audit verbs | Replaced by event log — `run.events.jsonl` is the journal | 🔵 better | `src/requiem/persistence.py`; ADR-0002 |
| `reset/*`, `research/*`, `guidance/*` | Support verbs | Missing | ❌ missing | (no equivalents) |

### 2.2 Workflow catalogue

| Workflow | Polyphony | Requiem | Status | Evidence |
| --- | --- | --- | --- | --- |
| `polyphony.yaml` — root tree-walker, preflight, worklist, edges, batch dispatch, iterate-until-stable | yes | Linear `full_sdlc.py` (dispatch → plan → impl → pr → close-out, single leaf) — no tree-walk, no batch, no outer loop | 🟡 partial | `src/requiem/workflows/full_sdlc.py` (679 LOC); `tests/test_full_sdlc.py` (12 tests) |
| `plan-level.yaml` — recursive planning, child seeding, plan PR | yes | Recursive planning (`planning.py`) — recursion + cycle/depth gates work; **no plan PR**, **no `twig create_child`** | 🟡 partial | `src/requiem/workflows/planning.py` (1527 LOC); `tests/test_planning_workflow.py` (10 tests) + `tests/test_planning_recursion.py` (6 tests) |
| `implement-merge-group.yaml` — `mg/`+`impl/` branch model, coder/reviewer loop, scope review, MG PR | yes | Single-leaf `implementation.py` — one `feature/<id>` branch, coder + revision arm, single PR. No `mg/`, no scope review, no MG PR | 🟡 partial | `src/requiem/workflows/implementation.py` (1262 LOC); `tests/test_implementation_workflow.py` (29 tests) |
| `feature-pr.yaml` — drift integration, platform router, remediation planner, MG replay, escalation | yes | Missing — `pr_lifecycle.py` is the GH leg only, with no remediation planner or MG replay | ❌ missing | (no `feature_pr` module) |
| `github-pr.yaml` — PR lifecycle, advisory review, comment synth, fix loop, notification | yes | `pr_lifecycle.py` — fetch → request_review → poll → synthesise → address → push → re-poll; loop cap; idempotent merge | ✅ at-parity | `src/requiem/workflows/pr_lifecycle.py` (1167 LOC); `tests/test_pr_lifecycle_workflow.py` (14 tests) |
| `ado-pr.yaml` — ADO PR lifecycle | yes | Missing | ❌ missing | (no `ado_pr` module; twig client has no PR helpers) |
| `close-out.yaml` — post-impl observation | yes | `close_out.py` — fetch_item → resolve_pr → fetch_pr → fetch_criteria → verifier → write_closeout → close_item | ✅ at-parity | `src/requiem/workflows/close_out.py` (1387 LOC); `tests/test_close_out_workflow.py` (12 tests) |
| `root-item-dispatch.yaml` / `init-root` | yes | `root_dispatch.py` — Haydn seat | ✅ at-parity (single-root) | `src/requiem/workflows/root_dispatch.py` (1063 LOC); `tests/test_root_dispatch_workflow.py` (11 tests) |
| `root-batch-dispatch.yaml` — parallel batch dispatch | yes | Missing | ❌ missing | (no batch primitive) |
| `restack-remedy.yaml`, `remedy-stale-descendant.yaml` | yes | Missing | ❌ missing | (no remediation workflows) |
| `reset-root.yaml` | yes | Missing — no `reset` verb at all | ❌ missing | (no equivalent) |
| `research.yaml` | yes | Missing | ❌ missing | (no equivalent) |
| `actionable.yaml` — satisfaction gate / evidence branch | yes | Missing | ❌ missing | (no equivalent) |
| `root-fallback-gate.yaml` | yes | Implicit — every workflow surrenders to human gate per Ravel L-1 | 🔵 better | every workflow's `needs_human_*` terminals |

### 2.3 State model

| Item | Polyphony | Requiem | Status | Evidence |
| --- | --- | --- | --- | --- |
| Run manifest | `.polyphony/state/{rootId}/seed-manifest.json` | `{manifest_dir}/{run_id}.manifest.json` (root_dispatch) | ✅ at-parity (single-root) | `src/requiem/workflows/root_dispatch.py` write_manifest; idempotent re-read on resume |
| Event log / journal | Conductor checkpoint (ephemeral) + scattered journal files | `{run_id}.events.jsonl` — single source of truth, append-only | 🔵 better | `src/requiem/persistence.py`; INV-EVENT-LOG-AUTHORITATIVE |
| Sub-workflow log isolation | Not enforced; conductor cursor cross-contamination was an open hazard | `{sub_run_id}.events.jsonl` sidecar; parent has only markers | 🔵 better | ADR-0005; INV-SUBWORKFLOW-LOG-ISOLATION; `tests/test_subworkflow.py` (10 tests); `test_resume_fidelity_matrix.py` classes 13/14 |
| Branch model | `feature/{root}`, `feature/{root}-{slug}`, `plan/{root}`, `mg/{root}_{path}`, `impl/{root}-{id}` | `feature/<item_id>` only (implementation.py) | ❌ missing | `src/requiem/workflows/implementation.py:1-50` topology |
| Worktree layout | Per-item worktrees `..\polyphony-{N}\` for parallel isolation | None — single workspace | ❌ missing | (no worktree code) |
| Authoritative-source ranking (ADO > manifest > git > worktree) | Documented + enforced | Documented intent; manifest scope limited to single-root manifest | 🟡 partial | `docs/north-star.md`; no cross-source reconciler |

### 2.4 External integrations

| Integration | Polyphony surface | Requiem | Status | Evidence |
| --- | --- | --- | --- | --- |
| ADO via twig | full read+write (state, tags, PR comments, etc.) | `TwigClient` with `show_async`, `comment_async`, `set_state_async` | 🟡 partial | `src/requiem/clients/twig.py` (265 LOC); `tests/clients/test_twig.py` (35 tests). **No `create_child_async`**, **no PR-link surfacing** (issue #30) |
| Git | branch / push / rebase / merge / worktree | `RealGitClient` + `FilesystemClient` with `git_is_clean`, `git_commit`, `git_push`. **No worktree, no rebase** | 🟡 partial | `src/requiem/clients/fs.py` (324 LOC); `tests/clients/test_fs.py` (25 tests) |
| GitHub | gh CLI: PR create/poll/merge/comment | `GhClient` — `pr_create`, `pr_view`, `pr_merge`, `pr_request_review`, `pr_search`, error taxonomy (`GhAuthError`, `GhRateLimitedError`, `GhUnknownError`) | ✅ at-parity | `src/requiem/clients/gh.py` (425 LOC); `tests/clients/test_gh.py` (29 tests) |
| LLM providers | Claude + Copilot via conductor | OpenAI + Anthropic providers in-process | ✅ at-parity | `src/requiem/providers/{openai,anthropic}.py`; `tests/providers/` (32 tests across openai/anthropic/default) |
| Conductor orchestrator | external runtime, YAML workflows | replaced — `Engine` in `src/requiem/kernel.py` (848 LOC), workflows in Python | 🔵 better | ADR-0001; `tests/test_kernel.py` (12 tests) |
| PowerShell `Invoke-PolyphonySdlc.ps1` launcher | yes | replaced by `requiem` CLI | 🔵 better | `src/requiem/cli/main.py` (599 LOC); `tests/test_cli_polish.py` (13 tests) |

### 2.5 Agent model

| Item | Polyphony | Requiem | Status | Evidence |
| --- | --- | --- | --- | --- |
| Agent declaration | inline YAML `agents:` blocks | `AgentSpec` Python dataclass (typed) | 🔵 better | `src/requiem/agent.py` (92 LOC); `tests/test_agent.py` (5 tests) |
| Script vs agent nodes | `script` or `agent` types | `ScriptNode`, `AgentNode`, `TeamNode`, `HumanGateNode`, `TerminateNode`, `SubWorkflowNode` | ✅ at-parity | `src/requiem/dsl.py:20-107` |
| Agent teams | conductor has parallel-fork branches | `TeamNode` + `TeamBranch` + `.team()` builder sugar | ✅ at-parity | `src/requiem/teams.py`; ADR-0003; `tests/test_teams.py` |
| Output binding | Jinja-templated; strict-undefined surface bugs | Pydantic `BaseModel` validation at agent boundary; `BadOutput` discriminated outcome on schema mismatch | 🔵 better | `CoderOutput`, `VerifierOutput`, `ReviewSummary` etc. in workflow modules |
| Prompt templates | inline YAML + `plan load-guidance` external | inline `AgentSpec.charter` + per-call prompt closures; no external guidance loader | 🟡 partial | (no `agent-guidance/*.md` loader) |

### 2.6 Gates & human-in-the-loop

| Item | Polyphony | Requiem | Status | Evidence |
| --- | --- | --- | --- | --- |
| Gate types | many (`human_gate`, retry/abort, conflict, depth, remediation, escalation) | one node kind (`HumanGateNode`) with arbitrary option strings; workflows pick semantics | ✅ at-parity | `src/requiem/dsl.py:55-67` |
| Terminal presentation | TTY + web dashboard | TTY only (`_make_interactive_gate_handler`) | 🟡 partial | `src/requiem/cli/main.py:521-565` |
| Web dashboard | Conductor live DAG, logs, gate UI | **MISSING** — no UI process exists | ❌ missing | Phase A seam 6 (`UI ↔ event-stream binding`) decided but not implemented |
| Programmatic / auto handler | YAML-style auto-pick | Pass `gate_handler` kwarg to `Engine.run`; `full_sdlc` auto-aborts on `paused_*` gates | ✅ at-parity | `src/requiem/kernel.py` `Engine.run(gate_handler=...)` |
| Cancel signal | `CONDUCTOR_CANCEL_TOKEN` sentinel file | `requiem cancel` writes `cancel_requested` event into the log directly; INV-CANCEL-SHORT-CIRCUITS-RETRY | 🔵 better | `src/requiem/cli/main.py:335-374` (`cmd_cancel`); `tests/test_resume_fidelity.py::test_m4_cancel_mid_flight_short_circuits` |

### 2.7 Harness / testing

| Item | Polyphony | Requiem | Status | Evidence |
| --- | --- | --- | --- | --- |
| Workflow harness | `tests/harness/` Python+shim binary | `tests/harness/` (assertions, fakes, scenario, pytest plugin) | ✅ at-parity | `src/requiem/harness/*.py` (1047 LOC); `tests/harness/test_harness_self.py` (9 tests) |
| FakeProvider | yes | yes — `FakeProvider` in `requiem.agent` | ✅ at-parity | `src/requiem/agent.py`; used by every workflow test |
| CLI contract tests | `JsonOutputContractTests` (.NET) | N/A — no JSON stdout contract (in-process) | ➖ N/A by design | INV-SINGLE-PROCESS dissolves the seam |
| Pester scenarios / dogfood | yes | partial — Tchaikovsky bug-bash exercised real ADO + GH manually; no Pester analog | 🟡 partial | `docs/bug-bash/2026-05-31-tchaikovsky.md`; `tests/test_bugbash_regressions.py` (4 tests) |
| Resume-fidelity matrix | not present in polyphony | exhaustive 14-class crash-point matrix (110+91+7 tests across `test_resume_fidelity{,_matrix,_pathological}.py`) | 🔵 better | `tests/test_resume_fidelity_matrix.py` (110 tests); `tests/test_resume_fidelity.py` (91 tests); `tests/test_resume_pathological.py` (7 tests) |

### 2.8 CLI / UX

| Item | Polyphony | Requiem | Status | Evidence |
| --- | --- | --- | --- | --- |
| Verbs | dozens of `polyphony …` subcommands | `requiem run / resume / describe / events / list-runs / cancel` | 🟡 partial | `src/requiem/cli/main.py:624-700` |
| Live narration (TTY) | yes (Pester + dashboard) | yes — `_tail_rendered` + `RenderContext` + per-workflow `render_hints()` | ✅ at-parity | `src/requiem/cli/render.py` (317 LOC); `tests/test_renderer_registry.py` (11 tests) |
| Verdict card | yes (web) | yes — per-workflow `verdict_card(completed)` rendered on terminate | ✅ at-parity | `src/requiem/cli/main.py:590-622` |
| Web dashboard | yes | **MISSING** | ❌ missing | (no UI process) |
| Install scripts | `install.ps1` / `install.sh` | none — `pip install requiem` | 🟡 partial | `pyproject.toml` only |

### 2.9 §9 Non-negotiables for v0 — scorecard

| # | Non-negotiable | Status | Note |
| -- | --- | --- | --- |
| 1 | Type-agnostic routing from process config | 🟡 partial | `requiem.process_config` loads `.requiem-config/process.yaml` into a frozen `ProcessConfig`; `root_dispatch.validate_root` classifies root tier from `config.root_parent_types` (snapshotted into the event log by `start_run` for resume fidelity) instead of the old hardcoded `{Epic,Feature}` literal (ADR-0015). `decomposable_types`/`implementable_types` are now **consumed by `planning`**: `branch_decomposable` enforces the tier policy over the planner's `decomposable` flag (implementable → forced leaf; decomposable the planner left as a leaf → fail-closed `type_policy_gate`), the config is snapshotted into `start_run` and threaded into recursive child inputs (INV-RESTART), and `tier_for_type` honours aliases on both sides. Remaining: the live ADO worker loop (creds-gated). The tier policy is now **surfaced read-only in the web dashboard** (ADR-0019 run detail shows root/decomposable/implementable types + aliases + config provenance from the `start_run` snapshot, 2026-06-09). |
| 2 | Polyphony CLI as deterministic decision layer (JSON stdout) | 🔵 better | Replaced by in-process Python verbs returning discriminated outcomes (INV-DISCRIMINATED-OUTCOMES). The *requirement underneath* — deterministic decisions — is honoured |
| 3 | `twig` as write-side bridge to ADO | 🟡 partial | `TwigClient` has show/comment/set_state/`create_child_async` (used by `commit_plan` seeding, PR #59). PR-link surfacing still rough (issue #30) |
| 4 | Root SDLC orchestrator (`polyphony@polyphony`) | 🟡 partial | `full_sdlc.py` is a five-stage linear pipeline. **Fan-out is no longer blocked: the three ADR-0013 blockers (B1/B2/B3) are CLOSED** (PRs #65/#66/#67). **In-process fan-out orchestrator landed (`fanout.py`, ADR-0021, 2026-06-09):** walks a committed plan tree's implementable leaves (`plan_tree.load_committed_leaves`) and dispatches each into `implementation` **in-process** (single process, INV-SINGLE-PROCESS) — each leaf inherits the orchestrator's real provider/toolbelt via the ADR-0020 seam (B1), builds `impl/<root>-<leaf>` (B3), writes its own isolated `fanout-<root>__leaf-<id>.events.jsonl` (INV-SUBWORKFLOW-LOG-ISOLATION), and rolls up landed / **needs_human** (a surrendered leaf, B2) / failed. Idempotent re-entry skips already-terminal leaves (resume-driven iterate-until-stable). Sibling to the external `kanban_executor` (ADR-0014); a single-process deployment uses `fanout`, a fleet uses the executor (`tests/test_fanout_workflow.py`, 10). **Wired into the driver (2026-06-09):** `run_pipeline(dispatch_backend="fanout", repo_path=…, fanout_parallel=…)` runs the in-process fan-out as Phase 3 instead of the kanban executor — leaf source resolved identically (inline atomic leaf or committed plan tree), the fan-out roll-up surfaces landed/needs_human/failed, fail-closed without a `repo_path`, `live=False`⇒dry-run. The default `dispatch_backend="kanban"` is byte-for-byte unchanged (`tests/test_end_to_end_fanout_backend.py`, 4). **Remaining:** an outer tree-walking-root iterate-until-stable loop across multiple plan levels (the single-level fan-out + recursion via `planning` covers v0); a CLI subcommand for the pipeline driver. |
| 5 | Per-item worktree isolation for parallel dispatch | 🟡 partial | **Worktree isolation + parallel dispatch landed (`fs.py` + `fanout.py`, ADR-0022, 2026-06-09):** `FilesystemClient.git_worktree_add`/`git_worktree_remove` (+ `_is_git_tree` now accepts a worktree's `.git` *file*); `fanout`'s `parallel=True` mode gives each leaf its own git worktree (`impl/<root>-<leaf>` born with the worktree) and dispatches concurrently via `asyncio.gather` bounded by `max_parallel`. The ADR-0020 seam is `gather`-safe (a parent-set contextvar propagates into gathered children; a child-set one doesn't leak — verified). Landed-leaf worktrees are cleaned best-effort; surrendered/failed leaves are left on disk for inspection. Per-leaf isolated child logs unchanged; idempotent re-entry reuses an existing worktree dir (`tests/test_fanout_workflow.py` parallel cases + `tests/clients/test_fs.py` worktree cases, 8 new). **Remaining:** a global cross-root parallelism budget + a `worktree prune` GC verb for orphaned dirs after a crash (ADR-0022 deferred) |
| 6 | Recursive planning with child seeding and PR lifecycle | ✅ at-parity | Recursion ✅. Child seeding into ADO ✅ (`commit_plan`, PR #59, ADR-0011). Plan PR open + handoff ✅ (`plan_pr`, PR #60, ADR-0012); merge owned by `pr_lifecycle` (GitHub) |
| 7 | Merge-group implementation (`mg/`, `impl/`) with idempotent re-entry | 🟡 partial | Branch topology authority landed (`branch_model.py`, ADR-0006 Option D): `feature/<root>`/`plan/<root>`/`impl/<root>-<item>`/`evidence/<root>-<item>` constructors + `parse_branch`, consumed by `kanban_executor`/`end_to_end`/`plan_pr`. **Trunk integration decided + building (ADR-0018, Option C accepted):** Hermes `kanban create` v0.15.1 has no `--base`/PR-target flag, so requiem owns the trunk (driver-side). **`feature_pr.py` landed** — the trunk-readiness gate (verify every expected leaf PR `head=impl/<root>-<item>`, `base=feature/<root>`, `merged` via `pr_view`) + idempotent `feature/<root>`→`main` opener, no self-merge (`tests/test_feature_pr_workflow.py`, 14). **`leaf_pr.py` landed** — requiem-owned leaf-PR opener (build-sequence step 2): reuse-open-or-create `impl/<root>-<item>`→`feature/<root>` per delivered leaf, fail-closed on wrong-base/ambiguous/error, emits the `{leaf_id: pr_number}` map `feature_pr` consumes (`tests/test_leaf_pr_workflow.py`, 11). **`trunk_bootstrap.py` landed** — build-sequence step 1: idempotently ensures `feature/<root>` exists before fan-out via a ratified remote GitHub-refs capability (`gh.branch_sha`/`gh.ensure_branch_ref`; never force-moves an existing trunk; fail-closed on a missing base), since the toolbelt git client is read-only (`tests/test_trunk_bootstrap_workflow.py` + `tests/test_gh_branch_ref.py`, 15). **Driver wiring (step 4) landed + live-validated (ADR-0018, 2026-06-09):** `end_to_end.run_pipeline` now bootstraps `feature/<root>` before dispatch (fail-closed — a failed bootstrap never fans out), opens leaf PRs after delivery, and persists the `{leaf_id: pr_number}` map; the new `end_to_end.integrate_pipeline` opens the trunk→base PR after the leaf PRs are human-merged, reading the persisted map (a default `gh pr list` is open-only). The base branch is resolved from the repo's real default (not hardcoded `main`). Branch drift was confirmed live (overlapping leaves conflict against the advanced trunk; disjoint leaves do not — invisible until an earlier leaf merges) and handled per the ratified v0 policy: an unmergeable leaf PR surfaces to the human, `rebase_onto_target` deferred (`tests/test_end_to_end_topology.py`, 10; live proof `docs/validation/adr0018-step4/`). **Requirement-disposition gate landed (ADR-0006 INV-DRIVER-GATES-FEATURE-MERGE, 2026-06-09):** `feature_pr` now runs a `verify_dispositions` node between readiness and PR-open — every in-scope item's requirement disposition must be satisfied or the feature→base merge is fail-closed to a human (`ItemDisposition` set threaded through `integrate_pipeline`; empty set = no-op pass for back-compat; INV-NO-CORRUPT-FORWARD hard refuse, not best-effort). Also fixed `feature_pr`'s `end_human` terminate to report `needs_human` (not the misleading `failed`). Covered by 6 gate tests in `tests/test_feature_pr_workflow.py` + 3 driver-forwarding tests. **#7 build sequence complete.** |
| 8 | Human gates in both terminal and web dashboard | 🟡 partial | Terminal ✅. **Web dashboard landed (ADR-0019, 2026-06-09) — observe + resolve:** `requiem.dashboard` — a stdlib-only (`http.server`, zero new deps) projection of the event logs. Lists runs with status, renders a run's humanized event timeline, and surfaces the **pending human-gate queue** (`/api/gates`). **Phase 2 (gate resolution) also landed:** `POST /api/gates/<run_id>/resolve` appends a guarded, append-only `gate_resolved` event via the kernel's own `EventStore`/`EventEmitter` (byte-identical envelope) — fail-closed on unknown-run/not-at-gate/invalid-choice (no half-writes), continuation left to `requiem resume`. A real kernel resume provably routes on the dashboard-written choice. Pure projection layer reuses the CLI's `_summarize_run` status semantics (`tests/test_dashboard.py`, 28). Launch: `python -m requiem.dashboard --log-dir .runs` (or the `requiem-dashboard` script). The lingering ergonomic gap: resolving a gate appends the decision but a human still triggers `requiem resume` to continue the run (no auto-dispatcher, by design). |
| 9 | Durable seed manifest for partial-seed recovery | ✅ at-parity | `root_dispatch.write_manifest` is idempotent read-or-create; INV-RESTART covers re-entry |
| 10 | Platform-specific PR lifecycles (GitHub and ADO) | 🟡 partial | GitHub ✅ (`pr_lifecycle.py`). **ADO lifecycle landed (`ado_pr.py`, ADR-0023, 2026-06-09):** the Azure DevOps sibling of `pr_lifecycle` — fetch PR → check state (active/abandoned/already-completed/draft) → check mergeability (conflicts + branch policies) → complete (squash/merge per repo policy) → transition the linked work item via `twig`. Same `Protocol` toolkit seam as `pr_lifecycle`: `RealAdoPrToolkit` (Azure DevOps REST v7.1, PAT-auth via `ADO_PAT`, `org/project/repository` addressing) + `FakeAdoPrToolkit` in-memory double. Fail-closed (conflicts/unsatisfied-policies/abandoned/fetch-error → human; draft → gate); `dry_run` genuinely side-effect-free; `needs_human` disposition on surrender (B2-consistent). Covered by `tests/test_ado_pr_workflow.py` (10) against the fake — **live ADO validation (a real PAT + reachable org/project) is a deploy-time step**, exactly as `pr_lifecycle` is unit-tested against `FakePrToolkit` and live-validated on GitHub. **Remaining:** an ADO PR *opener* (the merge-group `feature_pr` equivalent for ADO repos) + live PAT validation in a real Azure DevOps project. |

**Scorecard:** 2 ✅ at-parity, 1 🔵 better, 7 🟡 partial, 0 ❌ missing. **No non-negotiable is fully missing anymore** — every one of the ten is at least partially met. The remaining partials are creds-gated or polish (#1 process-config live ADO loop; #3 twig write-side PR-link; #10 ADO PR lifecycle — all need ADO creds; #4/#5 in-process fan-out shipped, remaining bits are a cross-root parallelism budget + worktree GC; #8 auto-resume ergonomic). #5 advanced ❌→🟡 by `fs.py` worktree primitives + `fanout` parallel mode (ADR-0022). #4 advanced by the in-process `fanout` orchestrator (ADR-0021) once the ADR-0013 blockers (B1/B2/B3) were closed. #8 web dashboard advanced ❌→🟡 by `requiem.dashboard` (ADR-0019) — observe + resolve. #7 merge-group: the full build sequence lands — only the live ADO worker loop (creds-gated) remains. (#6 closed by PRs #59 + #60; #1 advanced by ADR-0015 + planning tier-policy wiring; #7 advanced ❌→🟡 by `branch_model.py` + the full ADR-0018 build sequence — `trunk_bootstrap` + `leaf_pr` + `feature_pr` + driver wiring + the requirement-disposition gate — now complete.)

### 2.10 Fan-out executor — the critical-path blocker (ADR-0013)

The biggest single parity gap is that nothing dispatches the seeded implementable
leaves into `implementation` — recursive planning + seeding (#6) are inert without
it. A rigorous design exists (bounded-slot dispatch per `decomposable==False`
leaf), but a *correct, production-real* fan-out is **blocked** on three verified
architectural issues (ADR-0013):

- **B1 — child-seam propagation:** ✅ **CLOSED (2026-06-09, ADR-0020).** Was: the
  kernel forwarded only JSON-flat inputs to a dispatched child's `build_engine`
  (filtered by signature, kernel.py:543-567); `provider`/`toolbelt`/`gate_handler`
  were never forwarded, so a dispatched `implementation` fell back to a canned LLM
  + fake gh/twig over **real** git — a silent-success footgun. Fixed by a shared
  `requiem.seam` contextvar module the kernel installs from the parent engine
  before building each child; `implementation.build_engine` now resolves each seam
  as **explicit arg → active seam → demo fallback** (so a dispatched child
  inherits the parent's real seams; a bare demo call is unchanged; `demo=True`
  forces a hermetic demo). `tests/test_seam_propagation.py` (8). The pattern
  generalises the one planning used for `gate_handler` (planning.py:139-154); its
  own contextvars are a deferred consolidation.
- **B2 — handoff≠failure:** ✅ **CLOSED (2026-06-09).** Was: `implementation`'s
  single `end_handoff` terminal used `disposition="completed"` for BOTH the
  success-handoff (a green PR is open) and the surrender paths (red tests / bad
  coder output / push failure), and the kernel maps any child
  `Completed(completed)` → parent `Success` — so a fan-out parent treated a
  surrendered leaf as done. Fixed by splitting the terminal: the success-handoff
  stays `end_handoff` (`completed`); the surrender paths route to a new
  `end_needs_human` (`disposition="needs_human"`). The kernel's
  `_child_result_to_outcome` now maps a child `needs_human` disposition →
  parent `NeedsHuman` (kernel.py), so a fan-out orchestrator pauses for a human
  instead of charging ahead. `tests/test_subworkflow.py::test_parent_routes_needs_human_on_child_handoff_terminal`
  + 4 updated implementation surrender tests. (Planning's own `end_needs_human`
  still uses `disposition="completed"` — a same-class but lower-risk follow-up,
  since it routes within recursive planning, not a leaf dispatch.)
- **B3 — branch model:** ✅ **CLOSED (2026-06-09).** Was: `implementation`
  hard-coded `feature/{item_id}`, conflicting with ADR-0006's `feature/<root>` +
  `impl/<root>-<item>`. Fixed by an optional `root` field on
  `ImplementationInputs`: when set, `create_branch` builds
  `branch_model.impl_branch(root, item_id)` → `impl/<root>-<item>` (the ratified
  Option-D shape `feature_pr`/`leaf_pr` consume); when absent (standalone/legacy)
  it keeps `feature/<item_id>` (the Option-B stopgap) — so existing callers are
  byte-for-byte unchanged. `tests/test_implementation_workflow.py::test_root_yields_impl_topology_branch`.
  A live in-process orchestrator now just passes `root` to get full Option-D
  topology end-to-end.

**Recommended order:** B1 (unblocks *all* real dispatch — full_sdlc, root
orchestrator, fan-out) → B3 (a v0 scope decision: Option-B stopgap vs. Option-D
topology) → then the executor (B2 classifier + resume-only idempotency are
mechanical once B1/B3 land).

**Update (2026-06) — shipped via an external executor (ADR-0014).** Rather than
wait on B1's in-process child-seam work, the `kanban_executor` workflow
dispatches each implementable leaf to a **real external** executor — a Hermes
kanban worker (`requiem.workflows.kanban_executor` + `requiem.clients.kanban`).
This **sidesteps B1** (the executor brings its own real provider/toolbelt),
satisfies B4 via a stable `requiem:{root}:{leaf}` idempotency key, lets the
worker own B3's branch shape via worktree workspaces, and maps B2 onto a
receipt check (`task_runs.outcome == completed` **and** a worker `result`),
surfacing weak completions to a human. It does **not** unblock *in-process*
sub-workflow seam propagation — that B1 work is still open for `full_sdlc` and
the root orchestrator. See ADR-0014.

Leaf resolution is now spec-faithful and **type-agnostic**: `requiem.plan_tree`
enumerates every `decomposable == False` node depth-first from the approved
committed plan tree + `id_map` (ADR-0013's `load_committed`), carrying each
leaf's metadata from its parent proposal — *planning* decides the facet, not a
hardcoded ADO-type set. `requiem.end_to_end` is a thin top-level driver
(`python -m requiem.end_to_end --item <id> --board <b> [--commit] [--live]`)
that chains planning → `commit_plan` → `kanban_executor` against any ADO item,
including the **atomic-root** case (an item planning calls a leaf is dispatched
as itself). The remaining type-agnosticism gap is `root_dispatch`'s hardcoded
`ROOT_PARENT_TYPES` (non-negotiable #1 — process-config loader, still missing).

---

## 3. Invariant Coverage

For each ratified invariant in `docs/north-star.md` §2, the audit found at
least one concrete code or test reference demonstrating enforcement.

| Invariant | Status | Primary evidence |
| --- | --- | --- |
| **INV-SINGLE-PROCESS** | ✅ enforced | `src/requiem/kernel.py` (848 LOC, single Python process); the only subprocesses spawned are `git`/`gh`/`twig`/test commands via `RealGitClient`, `GhClient`, `TwigClient`. No .NET, no conductor. ADR-0001. |
| **INV-RESTART** | ✅ pinned exhaustively | `tests/test_resume_fidelity.py` (91 tests — M1–M5 matrices on the demo workflow), `tests/test_resume_fidelity_matrix.py` (110 tests including the 14-class crash-point taxonomy on gate + loop fixtures + sub-workflow classes 13/14), per-workflow `INV-RESTART` scenarios in `test_close_out_workflow.py`, `test_implementation_workflow.py`, `test_pr_lifecycle_workflow.py`, `test_root_dispatch_workflow.py`, `test_full_sdlc.py`. |
| **INV-NO-CORRUPT-FORWARD** | ✅ enforced | Every workflow routes unclassified errors to `needs_human_*` terminals (Ravel L-1). `tests/test_resume_pathological.py` (7 tests) defends the kernel layer. `BadOutput` outcomes never auto-retry (`tests/test_close_out_workflow.py` "Verifier BadOutput → NeedsHuman, NO auto-retry"). |
| **INV-EVENT-LOG-AUTHORITATIVE** | ✅ enforced | `src/requiem/persistence.py` `EventStore` is the only writer; `replay()` is the only reader. UI/CLI/verdict cards / harness assertions all project from the log. ADR-0002. |
| **INV-DISCRIMINATED-OUTCOMES** | ✅ enforced | `src/requiem/outcomes.py` six-variant union (`Success / RetryableFailure / PermanentFailure / BadOutput / NeedsHuman / Cancelled`). `tests/test_outcomes.py::test_six_variants_tagged` pins the contract. ADR-0004. |
| **INV-CANCEL-SHORT-CIRCUITS-RETRY** | ✅ enforced | `tests/test_kernel.py` cancel-short-circuit test; `tests/test_resume_fidelity.py::test_m4_cancel_mid_flight_short_circuits` (strict `extra == 0` assertion). |
| **INV-NO-ENGINE-ABANDONMENT** | ✅ enforced by topology | Every workflow's exhaustion routes to a `needs_human_*` terminate, not an `abandoned` state. No `abandon` outcome variant exists. (No dedicated test; defended by the absence of an abandon path in the discriminated-outcome union and `_disposition_for_outcome`.) |
| **INV-SUBWORKFLOW-LOG-ISOLATION** | ✅ enforced | `src/requiem/kernel.py::SubWorkflowNode` invocation path; child writes to `{sub_run_id}.events.jsonl`; parent has only markers. `tests/test_subworkflow.py` (10 tests) + `test_resume_fidelity_matrix.py::test_class13_crash_mid_subworkflow` and `test_class14_crash_post_subworkflow_completed`. ADR-0005. |
| **INV-LOG-STRICT-STOP-ON-CORRUPTION** | ✅ enforced | `src/requiem/persistence.py` `replay()` raises `CorruptLogError` on partial JSON lines. `tests/test_persistence.py::test_corrupt_log_halts_replay`, `tests/test_resume_pathological.py::test_truncated_mid_line_refuses_to_resume`, `tests/test_resume_fidelity.py::test_m1_truncate_mid_line_refuses_to_resume`. |
| **INV-CANCEL-RESUME-IDEMPOTENT** | ✅ enforced | `tests/test_resume_fidelity.py::test_m4_cancel_mid_flight_short_circuits` with strict `extra == 0` assertion across re-resume. Re-resume of completed runs is byte-idempotent. |

**Verdict: every §2 invariant has at least one regression-pinning test or
code citation.** No `⚠ unverified` rows. (The §7 candidate invariants
remain candidates and are explicitly pinned to current behaviour — see
`test_resume_pathological.py` documented-behaviour tests.)

This is the strongest section of the audit. The architectural bets the squad
made in Phase A all held up under construction. The current shortfall is
*surface area*, not foundations.

---

## 4. Known v0 Risks

### 4.1 Open issues — disposition by Mahler-3

| # | Title | Severity | Mahler-3 verdict |
| --- | --- | --- | --- |
| #29 | `close_out`: terminate disposition vs verdict card | UX rough-edge | **RESOLVED** — added a `needs_human` disposition variant to the terminate enum (`dsl.py`) and routed `close_out`'s `end_human` to it, so the run-completed disposition now agrees with the verdict card (and with `close_out_result`'s `needs_human` verdict). Subworkflow routing in `full_sdlc` is unchanged (`subworkflow.needs_human` still takes the `permanent_failure` edge to `paused_close`). |
| #30 | `close_out`: real twig JSON has no `pullRequests` field | Schema-parity gap | **RESOLVED** — `resolve_pr` now falls back to a `gh pr list --search "head:feature/<item_id>"` query when the item carries no linked PR; a single hit auto-resolves (`source=gh_search`), multiple hits raise the ambiguous gate, and zero hits escalate as before. Operators can still pass `--pr N` explicitly. Covered by `test_pr_resolved_via_gh_search_fallback` and the updated `test_pr_not_linked_raises_needs_human`. |
| #31 | `planning`: no `permanent_failure` catch-all edges | Diagnostic rough-edge | **RESOLVED** — every planning script/agent verb now routes its catch-all `permanent_failure` to the narrated `fail_end_crash` terminal, so a `verb.crash` produces a verdict-card narrative instead of stranding the run with `route.missing`. Covered by `test_planning_workflow.py::test_planning_verb_crash_routes_to_narrated_terminal` and the planner-crash test. |

### 4.2 Tchaikovsky-class regression hazard — RESOLVED (guard added)

The BUG #1 fix (sync `twig.show()` → `async show_async`) in PR #32 broke
Haydn's `root_dispatch.FakeTwigClient` because the test fake was not updated
in lockstep. This was caught only when Haydn's tests ran post-merge (PR #36
patched it up two days later).

**Risk:** the workflow modules each carry their own `FakeTwigClient` /
`FakePrToolkit` / `FakeFileClient`. They diverge unless a cross-workflow
contract test enforces a common surface. The same hazard exists for any
future fake-surface change.

**Recommendation:** add a shared `tests/fakes/` module (or extend the
existing `tests/fakes/` directory if it exists) with a single
`AsyncOnlyTwigStub`-style fake that every workflow test imports. Failing
that, add a `tests/test_fake_surface_contract.py` that introspects all
`Fake*` classes in `tests/` and asserts they implement the matching
Protocol. Either keeps a Tchaikovsky-class regression from re-emerging
silently.

**Closed:** shipped `tests/test_fake_surface_contract.py`. It AST-walks the
whole `tests/` tree (no imports — avoids the heavy-fixture hang), discovers
every `Fake*` client class and its methods, maps each to its real client
(`TwigClient`/`GhClient`/`FilesystemClient`) by class-name token, and asserts
**async-ness parity** for every method name shared with the real client —
the exact `sync → async` drift that caused the original regression. Partial
fakes stay legal (only overlapping methods are checked); a `checked > 0`
guard prevents a vacuous pass if discovery ever breaks. Currently validates
17 real overlaps across the shared and local fakes.

### 4.3 `full_sdlc.py` dispatch-shim post-merge fixup

PR #37 patched `full_sdlc.py` to reference `RootDispatchInputs` rather than
the older `DispatchInputs` name — a rename made in the verdi3 worktree that
the squash-merge of PR #33 did not include. This is a one-off and is fixed,
but it surfaced a structural fragility: **the shim modules in `full_sdlc.py`
duplicate the per-child input shape by name**, so any rename in a child
workflow can desync silently.

**Risk (low but real):** future child-workflow rename → shim breaks at import
time, not at test time. The test suite caught this one because
`test_full_sdlc.py` imports the shim; not all shim contracts are
import-checked.

**Recommendation:** import the child input dataclasses directly into
`full_sdlc.py` rather than re-declaring their field names. Any rename then
breaks at import.

### 4.4 Per-stage shim and `_CURRENT_INPUTS` cell

`full_sdlc.py:115` documents: *"For v0 this is a single-process, single-run
idiom; concurrent `full_sdlc` runs in the same process would collide."* This
is acceptable for the single-power-user audience (north-star §5), but
worth flagging — any future `requiem run` call that triggers two `full_sdlc`
invocations in the same Python process (e.g., a UI batch submit) will produce
silently-wrong child engine inputs. INV-SINGLE-PROCESS amplifies this:
process-level globals are visible across runs.

**Recommendation:** keep this as a known limitation, but add a guard rail —
raise on second `build_engine` call inside the same process if the previous
run hasn't completed.

### 4.5 Missing workflows enumerated

The biggest v0 risk is simply *absence*:

- **No `feature_pr.yaml` analog.** The remediation/escalation loop on the
  feature-PR is not modelled. Without it, a failing feature PR escalates
  straight to `needs_human` with no chance for the planner to propose a
  remediation. Polyphony's loop here is non-trivial; reproducing it is
  Berlioz-Phase-D work.
- **No `actionable.yaml` analog.** The satisfaction-gate / evidence-branch
  decomposition for "this requirement is satisfied by a human action, not by
  agent work" has no Requiem equivalent.
- **No reset / reconcile verbs.** Polyphony's reset workflow (`reset-root`)
  and reconcile verbs are the operator's escape hatch when a run leaves
  state in a half-applied condition. Without them, a stuck Requiem run
  requires manual file surgery.
- **No web UI.** The roadmap describes v0 as "UI-from-day-one foundation";
  Phase A's UI-binding seam decision (seam 6) is documented but no
  implementation exists. The event log is UI-ready (INV-EVENT-LOG-AUTHORITATIVE);
  the SSE/WebSocket bridge and the JS frontend are not.

### 4.6 Implementation-workflow CLI gap (Tchaikovsky observation) — RESOLVED

Per bug-bash report §"implementation": *"Implementation workflow has no CLI
argparse driver; must drive via Python script. Recommend adding one for
parity with planning + close_out."* **Closed:** `implementation.py` now has a
`main()` + `_build_arg_parser()` driver (`python -m requiem.workflows.implementation
[--item N] [--repo R] [--repo-path P] [--test-command C] [--dry-run] [--live]`),
mirroring `close_out`'s pattern (live narration via the render context + verdict
card). Defaults run the self-contained demo; `--live` wires the real Toolbelt and
requires `--item`. Covered by three CLI tests in `test_implementation_workflow.py`.

### 4.7 Long-poll PR lifecycle ceiling

Per bug-bash report §"pr_lifecycle": the workflow's `poll_timeout_s` was set
to 30 s for the bug-bash. A real PR-review cycle needs hours-to-days of
polling. The `pr_lifecycle` workflow has the loop cap and the
no-progress detector, but the `poll_timeout_s` default is brief. Confirm
this is operator-configurable per run and document a reasonable default
before cutover.

---

## 5. Recommended Cutover Decision

### **NO-GO** for v0 against §9's ten non-negotiables.

Three independently blocking gaps:

1. **Merge-group topology absent (non-negotiable #7).** The single-leaf
   `implementation.py` cannot represent polyphony's `mg/` + `impl/` branch
   structure. Backfilling this is multi-day work and touches branch naming,
   PR ordering, scope-close logic.
2. **Web dashboard absent (non-negotiable #8).** Roadmap describes v0 as
   "UI-from-day-one"; no UI process exists. The event log is wired for it,
   but SSE bridge + JS frontend are unbuilt.
3. **ADO PR lifecycle absent (non-negotiable #10).** GitHub-only is fine
   for the inaugural demo but not for parity. Polyphony's `ado-pr.yaml`
   has no Requiem peer.

Two material partials (worktree isolation #5, tree-walking root #4) push the
scorecard further from parity.

### Path to GO (estimated)

Sequenced by dependency and impact:

1. **Close issue #31** (planning catch-all `permanent_failure` edges) — ~30 LOC, blocks no other work.
2. **Add `twig.create_child_async`** + wire `planning` seed-children verb — unblocks non-negotiable #6's "child seeding" half.
3. **Add `implement-merge-group` topology** to `implementation.py` (or split it into a sibling workflow) — non-negotiable #7.
4. **Add `ado_pr` workflow + `twig` PR helpers** — non-negotiable #10.
5. **Add `worktree` primitive** + parallel dispatch in `full_sdlc.py` — non-negotiable #5 and partial #4.
6. **Add UI process** (SSE bridge in Python; minimal JS frontend reading
   `run.events.jsonl` projections) — non-negotiable #8.
7. **Add reset / reconcile verbs** + restack-remedy workflow — observability + recovery story.

### Alternative: re-scoped v0

If Daniel re-scopes v0 to **"the linear pipeline, GitHub-only, terminal-only,
single-root demo workflow"**, the audit verdict flips to:

### **GO with caveats**

Caveats:
- v0 deliberately ships without merge-group topology, worktree parallelism,
  ADO PR lifecycle, web dashboard, and the support workflows (reset,
  remediation, research, actionable, batch dispatch). These are post-v0.
- Issue #31 closed before cutover (planning narrates verb crashes).
- Issue #29 either closed or documented in operator runbook.
- Issue #30 documented (`--pr N` required) and a `gh pr list --search`
  fallback filed as a fast-follow.
- Implementation workflow gets an argparse `__main__` driver.
- `full_sdlc` re-entry guard added (raise on concurrent same-process run).
- `tests/test_fake_surface_contract.py` (or equivalent shared-fake module)
  added to prevent Tchaikovsky-class regressions.

The re-scoped slice **does work end-to-end** today, the architecture is
sound, the foundations are exhaustively tested, and the path from re-scoped
v0 to full-§9 v0 is incremental rather than rewrite-shaped.

---

## 6. Appendix: Test inventory

Counts taken from `pytest --collect-only` against the audit worktree; 526
collected, 376 pass / 150 skipped / 0 fail at HEAD `495609e`.

### Kernel / persistence / DSL / outcomes (39 tests)
- `test_kernel.py` (12) — `_reconstruct` cursor fold; `BadOutput` routing; INV-CANCEL-SHORT-CIRCUITS-RETRY.
- `test_persistence.py` (4) — `EventStore.append` id assignment; replay empties; corrupt-log halt; id recovery after restart.
- `test_dsl.py` (5) — `WorkflowBuilder` validation (entry, edges, terminates).
- `test_outcomes.py` (4) — six-variant union; serialization round-trip; tag stability.
- `test_events.py` (2) — event emitter.
- `test_teams.py` (1) — `TeamBranch` round-trip.
- `test_agent.py` (5) — `AgentSpec` + `FakeProvider`.
- `test_toolbelt.py` (4) — file/git client factories.
- `test_subworkflow.py` (10) — `SubWorkflowNode` invocation + log isolation; child outcome translation; resume across sub-workflow boundary.

### Resume-fidelity matrices (208 tests) — INV-RESTART central evidence
- `test_resume_fidelity.py` (91) — M1–M5 truncate / kill / resume-completed / cancel / concurrent matrices on `code_review_demo`.
- `test_resume_fidelity_matrix.py` (110) — gate + loop fixtures + sub-workflow class 13/14 coverage.
- `test_resume_pathological.py` (7) — INV-NO-CORRUPT-FORWARD pathological log shapes (partial line, empty/missing log, duplicate / out-of-order `event_id`, run-id isolation).

### Workflow modules (84 tests)
- `test_planning_workflow.py` (10) — flat happy/escalate/depth-gate/twig-error matrix.
- `test_planning_recursion.py` (6) — single/two-level decomposable; depth cap; cycle detection; cross-level INV-SUBWORKFLOW-LOG-ISOLATION; cross-level INV-RESTART.
- `test_implementation_workflow.py` (29) — happy / no-changes / BadOutput / tests-fail revision / dirty-workspace / branch idempotency / path-traversal / INV-RESTART.
- `test_pr_lifecycle_workflow.py` (14) — fetch / poll / address-comments loop / merge / cancel / loop-cap / no-progress / `gh` error taxonomy.
- `test_close_out_workflow.py` (12) — happy / partial verdict / PR-not-merged / PR-not-linked / BadOutput / INV-RESTART / dry-run / topology.
- `test_root_dispatch_workflow.py` (11) — auto_plan happy / no-plan / not-a-root / idempotent re-dispatch / dry-run / INV-RESTART / INV-SUBWORKFLOW-LOG-ISOLATION.
- `test_full_sdlc.py` (12) — vertical-integration happy / per-stage failure / INV-RESTART-MID-PIPELINE / INV-SUBWORKFLOW-LOG-ISOLATION / dry-run.

### CLI / render / docs (33 tests)
- `test_cli_polish.py` (13) — argparse plumbing; version; describe; events; list-runs; cancel.
- `test_renderer_registry.py` (11) — `RenderContext`, per-workflow `render_hints()`, line emitters.
- `test_docs_smoketest.py` (9) — verify documented code snippets execute.

### Clients (89 tests)
- `tests/clients/test_twig.py` (35) — `TwigClient` show/comment/set_state, error taxonomy (`TwigRateLimitedError`, `TwigItemNotFoundError`, `TwigUnknownError`).
- `tests/clients/test_gh.py` (29) — `GhClient` pr_create/view/merge/request_review/search, full error taxonomy.
- `tests/clients/test_fs.py` (25) — filesystem and git-shell helpers; idempotent commit/push; clean-tree assertion.

### Providers (32 tests)
- `tests/providers/test_openai.py` (14) — OpenAI provider boundary.
- `tests/providers/test_anthropic.py` (14) — Anthropic provider boundary.
- `tests/providers/test_default.py` (4) — provider selection logic.

### Harness / integration / bug-bash (23 tests)
- `tests/harness/test_harness_self.py` (9) — assertions, fakes, scenario engine.
- `test_integration_code_review.py` (6) — end-to-end demo workflow.
- `test_bugbash_regressions.py` (4) — Tchaikovsky BUG #1 pins (AsyncOnlyTwigStub).

### Notes for Purcell (downstream migration-guide author)

- The 14-class crash-point matrix in `test_resume_fidelity_matrix.py` is the
  single best document for explaining "why operators can trust Requiem to
  restart cleanly." Cite it heavily.
- `test_subworkflow.py` + the cross-level recursion tests in
  `test_planning_recursion.py` are the load-bearing evidence for
  INV-SUBWORKFLOW-LOG-ISOLATION; this is the invariant that conductor never
  had and that unblocks recursive planning safely.
- `test_bugbash_regressions.py` documents the architectural rule "workflow
  verbs that touch `TwigClient` must be async-only." Worth pulling into the
  migration guide as a known gotcha.
- The discriminated-outcome union (`test_outcomes.py`) is the operator-facing
  contract that replaces polyphony's 5-class exit-code taxonomy. The
  in-process variant tag is the migration's biggest ergonomic win and worth
  spotlighting.

---

*End of report — Mahler-3, Wave 5.*
