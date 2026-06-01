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
non-negotiables).** Requiem is structurally healthy and the load-bearing
invariants are demonstrably enforced — the architecture has paid off — but
**five of the ten non-negotiables have material gaps** that no amount of
polish on the existing surface can close. Specifically: merge-group
implementation topology, per-item worktree isolation, the tree-walking root
orchestrator with batch dispatch, ADO PR lifecycle, and the web dashboard are
all absent. The seventh non-negotiable (recursive planning *with child
seeding and PR lifecycle*) is partial — the recursion is real and tested, but
the workflow neither seeds children into ADO via `twig` nor opens a plan PR.

If, however, the operator chooses to **re-scope v0** to the demoable
single-root linear pipeline (dispatch → planning → implementation → GitHub PR
lifecycle → close-out) on GitHub-only, with a terminal-only UX, then the
verdict flips to **GO with caveats** — that slice does work end-to-end, is
covered by 526 collected tests (376 pass / 150 skipped / 0 fail at HEAD
`495609e` — verified by `pytest -q` in the audit worktree), and survived
Tchaikovsky's real-ADO bug-bash (one blocker fixed, three rough edges filed
as issues #29/#30/#31).

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
| 1 | Type-agnostic routing from process config | ❌ missing | No `.requiem-config/` / process-config loader exists; ADO type names are referenced inline (`root_dispatch` validates against `Epic`/`Feature` literals) |
| 2 | Polyphony CLI as deterministic decision layer (JSON stdout) | 🔵 better | Replaced by in-process Python verbs returning discriminated outcomes (INV-DISCRIMINATED-OUTCOMES). The *requirement underneath* — deterministic decisions — is honoured |
| 3 | `twig` as write-side bridge to ADO | 🟡 partial | `TwigClient` has show/comment/set_state. Missing `create_child_async` (blocks recursive plan seeding) and PR-link surfacing (issue #30) |
| 4 | Root SDLC orchestrator (`polyphony@polyphony`) | 🟡 partial | `full_sdlc.py` is a five-stage linear pipeline; not a tree-walking root with batch dispatch or outer iterate-until-stable loop |
| 5 | Per-item worktree isolation for parallel dispatch | ❌ missing | No worktree primitive; no parallel dispatch; sub-workflow children run sequentially |
| 6 | Recursive planning with child seeding and PR lifecycle | 🟡 partial | Recursion ✅. Child seeding into ADO ❌ (no `twig.create_child_async` call). Plan PR open/merge ❌ |
| 7 | Merge-group implementation (`mg/`, `impl/`) with idempotent re-entry | ❌ missing | `implementation.py` uses a single `feature/<item_id>` branch; no `mg/` or `impl/` topology |
| 8 | Human gates in both terminal and web dashboard | 🟡 partial | Terminal ✅. Web ❌ |
| 9 | Durable seed manifest for partial-seed recovery | ✅ at-parity | `root_dispatch.write_manifest` is idempotent read-or-create; INV-RESTART covers re-entry |
| 10 | Platform-specific PR lifecycles (GitHub and ADO) | 🟡 partial | GitHub ✅ (`pr_lifecycle.py`). ADO ❌ (no `ado_pr` module) |

**Scorecard:** 1 ✅ at-parity, 1 🔵 better, 5 🟡 partial, 3 ❌ missing. **Five of ten** non-negotiables have material work remaining.

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
| #29 | `close_out`: terminate disposition vs verdict card | UX rough-edge | **v0 acceptable** — contradictory label is misleading but the verdict card carries the truth; operator can read past it. Fix is one-line (add `needs_human` disposition variant to terminate enum). Should be done before cutover but not a blocker. |
| #30 | `close_out`: real twig JSON has no `pullRequests` field | Schema-parity gap | **v0 acceptable** — workflow gracefully escalates to `needs_human` when PR list is empty; operator passes `--pr N` explicitly. The right long-term fix is a `gh pr list --search` fallback or a `twig` enhancement; either is doable post-v0. Document the `--pr N` requirement in the close_out runbook before cutover. |
| #31 | `planning`: no `permanent_failure` catch-all edges | Diagnostic rough-edge | **v0 blocker (recommended)** — `verb.crash` in any planning verb strands the run with `route.missing` and no verdict-card narrative. This silently degrades the observability story that the rest of Requiem invests heavily in. Fix is mechanical (`close_out` already has the pattern); ~30 LOC. Recommend closing before cutover. |

### 4.2 Tchaikovsky-class regression hazard

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

### 4.6 Implementation-workflow CLI gap (Tchaikovsky observation)

Per bug-bash report §"implementation": *"Implementation workflow has no CLI
argparse driver; must drive via Python script. Recommend adding one for
parity with planning + close_out."* This is a minor ergonomics gap but
becomes painful for the operator-facing v0 demo. Recommend adding
`if __name__ == "__main__"` argparse driver before cutover.

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
