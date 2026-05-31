# Polyphony + Conductor Parity Inventory for "Requiem"

Scope: `polyphony`, `conductor`, `polyphony-conductor-workflows` (historical/docs references in `polyphony`), and the dogfood repo `polyphony-squad-spike`.

## 1) Verb catalogue

Ground truth: `polyphony/src/Polyphony/Program.cs:24-52` and `polyphony/docs/polyphony-cli-reference.md:123-178`.

### Top-level / routing / validation

- **`validate`** — validate a lifecycle event on a work item.
- **`validate-config`** — schema-validate `.polyphony-config/process-config.yaml`.
- **`hierarchy`** — walk the work-item tree and annotate nodes.
- **`health`** — diagnostic checks for environment / twig / sqlite / dotnet.

### `state/*`

- **`state preflight`** — full SDLC entry check.
- **`state preflight-lite`** — lighter entry check for planning.
- **`state detect`** — root routing payload; combines phase, plan artifacts, git/PR state.
- **`state next-ready`** — decide dispatchable requirements for a work item.

### `plan/*`

- **`plan depth-guard`** — recursion guard.
- **`plan next-child`** — list immediate plannable children.
- **`plan load-type`** — load type definition + template + guidance.
- **`plan load-guidance`** — load `.polyphony-config/agent-guidance/*.md`.
- **`plan review`** — aggregate reviewer JSON and emit pass/fail.
- **`plan seed-children`** — reconcile architect's child list into ADO children (idempotent via manifest-aware reconciliation).
- **`plan write-plan`** — creates the seed manifest / plan artifacts.
- **`plan derive-ancestor-chain`** — derive ancestor chain root→leaf.

### `policy/*`

- **`policy load`** / **`policy validate`** / **`policy resolve`** (`(domain, scope)` → resolved rule JSON).

### `branch/*`

- **`branch route`** — classify PR group and next action.
- **`branch load-tree`** — discover hierarchy + PG groups + completion status.
- **`branch ensure-feature`** — idempotently create/push feature branch.
- **`branch next-impl`** — choose next implementable item and transition it in-progress.
- **`branch check-deps`** — check ADO predecessors for blocking dependencies.
- **`branch close-scope`** — close non-terminal items in a PG scope.
- **`branch ensure-mg`** — create/check out merge-group branch.
- **`branch ensure-impl`** — create/check out impl branch `impl/{root}-{item}`.

### `pr/*`

- **`pr create-feature-pr`** — create feature PR or reuse existing open PR.
- Plus: `open-plan-pr`, `merge-plan-pr`, `poll-status`, `post-comment-ado`, `open-impl-pr`, `merge-impl-pr`, `open-mg-pr`, `merge-mg-pr`, `open-ado-pr`, `merge-feature-ado`, etc.

### `scope/*`, `root/*`, `requirements/*`, `merge-group/*`, `manifest/*`, `lock/*`, `worktree/*`, `worklist/*`, `edges/*`, `agent/*`, `research/*`, `reset/*`, `guidance/*`, `journal/*`

- Registered in `Program.cs:27-52`. Load-bearing for the workflow suite.
- Known durable-state verbs: `manifest init`, `manifest read`, `root declare`, `root resolve`, `worklist build`, `edges check`, `worktree` management, `lock` acquisition/release, `journal*` audit verbs.

---

## 2) Workflow catalogue

Ground truth: `polyphony/.conductor/registry/workflows/*.yaml`.

- **`polyphony.yaml`** — root tree-walking SDLC orchestrator. Preflight, worklist build, edge checking, batch dispatch, recursive plan/impl/PR legs, outer iterate-until-stable loop. Sub-workflows: `plan-level`, `actionable`, `implement-merge-group`, `feature-pr`, fast-paths.

- **`plan-level.yaml`** — recursive planning core. Ancestor derivation, depth guard, guidance load, architect, plan write, plan PR open/merge, reviewer poll, child seeding, recursive child dispatch.

- **`implement-merge-group.yaml`** — one merge-group lifecycle on Rev 4 branch model. MG branch ensure, next item routing, impl branch ensure, coder/reviewer loop, scope review, impl PRs, MG PR, scope close.

- **`feature-pr.yaml`** — feature PR creation, review, remediation, merge. Drift integration, platform router, PR creation, platform-specific lifecycle (`github-pr` or `ado-pr`), remediation planner/seeder, MG replay, review re-request, escalation gate.

- **`github-pr.yaml`** — GitHub PR lifecycle. Advisory bot review, comment post, poll status, feedback analysis, fix loop, notification, reaction handling.

- **`ado-pr.yaml`** — ADO PR lifecycle. ADO-native equivalent of github-pr.

- **`close-out.yaml`** — post-implementation observation generation.

- **Support workflows:** `root-item-dispatch.yaml`, `root-batch-dispatch.yaml`, `root-fallback-gate.yaml`, `restack-remedy.yaml`, `reset-root.yaml`, `research.yaml`, `remedy-stale-descendant.yaml`, `actionable.yaml`.

---

## 3) State model

- **Run manifest:** `.polyphony/state/{rootId}/seed-manifest.json`. Fields: `plan_generation{id,parent,cause,created_at}`, `created_at`, `root_id`, `items[]` with `child_id`, `type`, `title`, `parent_id`, `facets`, `introduced_in`, `ado_id`. Durable reconciliation anchor for seeding.

- **Watermark/checkpoint:** Conductor checkpointing is explicitly **not** durable cross-run state; lives in temp space and is crash-only. Polyphony itself writes no checkpoint state of its own in the root workflow.

- **Git branch model:** `feature/{root}`, `feature/{root}-{slug}`, `plan/{root}`, `mg/{root}_{mg_path}`, `impl/{root}-{item_id}`. `evidence/` and closeout artifacts exist in workflow/runtime conventions.

- **Worktree layout:** Per-item worktrees for parallel isolation. `..\polyphony-{N}\` siblings.

- **What is authoritative?**
  - **ADO tree** = actual work-item state.
  - **Seed manifest** = desired planned children.
  - **Git branches/PRs** = implementation and review state.
  - **Worktree dirs** = ephemeral execution state.
  - **Conductor runtime state** = reconstructible from workflow inputs + durable state; not authoritative.

---

## 4) External integrations

- **Azure DevOps (ADO)** — work items, tags, state transitions, comments, PRs. Via `twig` and ADO REST.
- **Git** — branch create/push/rebase/merge/worktree. Failure modes: non-ff push, conflicts, missing branch, remote stale.
- **GitHub** — PR creation/review/merge via `gh` CLI. Auth prompts disabled in `Program.cs:9-12`.
- **`twig` CLI** — Polyphony's write-side companion to ADO. `CreateChildAsync`, state transitions, sync, tags, PR posting.
- **LLM providers** — Conductor supports Copilot and Claude. Polyphony workflows use Claude models in several places (`claude-opus-4.6`, etc.).
- **Conductor** — orchestrates YAML workflows, gates, agents, web dashboard.
- **GitHub CLI (`gh`)** — PR lifecycle and comments.
- **PowerShell** — workflow scripts; launcher is `Invoke-PolyphonySdlc.ps1`.

---

## 5) Agent model

- **Definition:** declared in YAML under `agents:`. Types: `agent` or `script`. Have model, prompt, tools, output schema, and routes.
- **Invocation:** script nodes call `polyphony` verbs. LLM nodes call provider models (Claude/Copilot). Prompts Jinja-templated with workflow inputs and prior outputs.
- **Binding to workflow nodes:** output of one node becomes condition inputs for routes.
- **Prompt template locations:** inline in workflow YAMLs + repo-specific guidance via `plan load-guidance` from `.polyphony-config/agent-guidance/*.md`.
- **How outputs flow back:** agent JSON exposed as `<agent>.output.*`. Parent workflow `output:` maps pick from those fields. Conductor strict-undefined means nullable fields must be guarded carefully.

---

## 6) Gates & human-in-the-loop

- **Gate types:** `human_gate`, retry/abort style gates, conflict gates, depth-exceeded gates, remediation/escalation gates, approval/review gates, failed/error gates.
- **Presentation:** both TTY and web dashboard. Dashboard provides clickable prompt rendering and in-browser gate response.
- **Response flow:** gate options route back into workflow nodes (`retry`, `abort`, `override`, etc.). Human input is consumed as route choice, not free text.
- **Twig / 3259 seam:** current seam is `twig` + `twig-cli` config and local state files. Polyphony intentionally uses `twig` for writes, not direct ADO.

---

## 7) Harness / testing surface

- **`tests/harness/`** — workflow execution harness / scenario runner. Routing and script envelopes.
- **`tests/Polyphony.Tests/`** — CLI contract tests. `JsonOutputContractTests` and command exit behavior.
- **`FakeProvider`** — fakes LLM/provider calls in tests.
- **.NET shim binary** — Polyphony CLI executable contract. AOT-safe JSON serialization via `PolyphonyJsonContext`.
- **Pester scenarios** — dogfood / PowerShell workflow validation.

**Can test today:** verb JSON shapes, exit codes, routing envelopes, config validation, branch / manifest / worklist / edges pure decisions. Some workflow legs end-to-end with fake providers.

**Cannot fully test today:** gate routing across the full human-in-the-loop path is still partially gap-prone. Some external integrations (ADO/PR/live merges) remain hard to fully simulate. Conductor strict-undefined behavior makes template-shape regressions easy to miss without dogfood.

---

## 8) CLI/UX surface

- **Launcher:** `Invoke-PolyphonySdlc.ps1` (dogfood/release artifact). Install scripts `install.ps1` / `install.sh`.
- **Dashboard:** Conductor web dashboard on its port with live DAG, logs, outputs, gates. Started with `conductor run ... --web` or `--web-bg`.
- **Dogfood-conductor skill workflow:** Polyphony repo ships skills for runtime/bootstrap/conductor mechanics.

**Daniel-facing day:** start in a repo with `.polyphony-config/`. `twig set`, `twig sync`. Launch `conductor run polyphony@polyphony --input root_id=<N> --web`. Watch batch progress in dashboard. Respond to gates in TTY or browser. Merge PRs / resolve conflicts / re-run as needed.

---

## 9) 10 non-negotiable features for v0

1. Type-agnostic routing from process config, not hardcoded ADO type names.
2. Polyphony CLI as deterministic decision layer with JSON stdout contract.
3. `twig` as the write-side bridge to ADO.
4. `polyphony@polyphony` as the root SDLC orchestrator.
5. Per-item worktree isolation for parallel dispatch.
6. Recursive planning (`plan-level`) with child seeding and PR lifecycle.
7. Merge-group implementation (`mg/`, `impl/`) with idempotent re-entry.
8. Human gates in both terminal and web dashboard.
9. Durable seed manifest for partial-seed recovery.
10. Platform-specific PR lifecycles (GitHub and ADO).

---

## 10) 5 features that could probably be dropped or deferred

1. The outer iterate-until-stable loop complexity in `polyphony.yaml` (if successor wants simpler single-pass behavior).
2. Some remediation/restart auto-cycles (especially platform-aware loops).
3. Cross-platform dual leg support if initial Requiem is ADO-only or GitHub-only.
4. Rich dashboard extras (DAG animations, breadcrumbs, live streaming) if a thinner UX is acceptable.
5. Legacy PG-tag bridge (`PG-N`) once MG-path-aware routing fully replaces it.

---
