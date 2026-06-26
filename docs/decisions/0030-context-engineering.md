# ADR 0030 — Context engineering: per-leaf AGENTS.md, model routing, and token observability

**Status:** Proposed (2026-06-22)
**Date:** 2026-06-22
**Relates to:**
ADR-0004 §4.4 (receipts pattern — `model`/`input_tokens`/`output_tokens`/`latency_ms` already captured per call),
ADR-0010 (process-config tier model — same shape extended to model-routing policy),
ADR-0017 (Hermes delivery fleet — §1 role→profile routing is the dual of role→model routing),
ADR-0018 (trunk-integration contract — Requiem owns leaf-PR open, which is where the per-leaf AGENTS.md slice gets dropped),
ADR-0025 (dogfood path — Gap C requires deciding what the implementer worker actually reads),
ADR-0029 (evals — LM-judged rubrics need the model-routing policy from this ADR to pick the cheapest judge automatically).
**Inspiration:** `docs/references/sdlc-vibe-coding-applicability.md` §§7–8; Osmani/Saboo/Kartakis (May 2026) pp. 15–18, 41–42.

---

## TL;DR

Make context engineering — *what each agent sees, in what shape, at what cost* — an explicit, configured concern rather than a per-workflow accident. Three additions, all additive, all using surfaces that already exist:

1. **Per-leaf `AGENTS.md` slice** committed alongside the leaf branch by Requiem (not by the worker), carrying the planner's rationale + relevant doctrine excerpt + expected-files list + acceptance criteria. Turns Requiem from "orchestrator outside the coding agent" into "orchestrator that configures the harness of the coding agent for each piece of work" (paper p. 29).
2. **Role→model routing policy** in `process.yaml`, mirroring the existing `roles:` block (ADR-0017 §1). `AgentSpec.model` is already a field; this ADR adds the resolver + propagation.
3. **Token + latency rollup on the event stream** — receipts already carry per-call `model`/`input_tokens`/`output_tokens`/`latency_ms` (see `requiem.providers._common.make_receipt`). This ADR aggregates them into a `run_cost_summary` event at run completion + surfaces per-role breakdown in `requiem events` and the dashboard.

No invariant changes. No new outcome variant. Token data exists today — this ADR makes it *legible* rather than introducing it.

---

## Context

### What the paper says, and what's already true

The paper's §"Context engineering" (pp. 15–18) splits agent context into six types (Instructions, Knowledge, Memory, Examples, Tools, Guardrails) and into **static** (always loaded) vs **dynamic** (loaded on task match). The economics chapter (pp. 41–42) names *intelligent model routing* — cheap models for deterministic work, frontier models for hard work — as the primary OpEx lever.

Mapping onto Requiem today:

| Context type | Static or dynamic | Status                                                                                                                        |
|--------------|-------------------|-------------------------------------------------------------------------------------------------------------------------------|
| Tools        | static            | ✅ Typed `Toolbelt`, not free-form text                                                                                        |
| Guardrails   | static            | ✅ `INV-NO-CORRUPT-FORWARD` + `process_config` + preflight (`fleet_preflight.py`)                                              |
| Instructions | static            | ✅ Doctrine (ADR-0016) + profile SOULs (ADR-0017 §1)                                                                           |
| Examples     | dynamic           | ⚠️ Implicit in agent charters; no programmatic per-task selection                                                              |
| Knowledge    | dynamic           | ❌ Per-leaf implementer receives the work item's title/body and *that's it* — no curated knowledge slice                       |
| Memory       | both              | ⚠️ No cross-run agent memory (ADR-0003 explicitly defers); the event log is the only persistent memory                         |

The hole that bites in production is **Knowledge for the per-leaf implementer**. The planner already produced — and recorded into the event log — the rationale ("why this leaf"), the inferred file set, the acceptance criteria, the relevant doctrine slice. When the Hermes worker (or in-process fanout implementer per ADR-0025 path C1) picks up the leaf, none of that flows into its prompt. The worker reads the ADO work-item description (which is exactly the title + body the planner already saw) and starts from scratch. The paper's diagnostic on p. 30 — *"most agent failures are configuration failures"* — applies precisely: the implementer is failing not because the model is bad but because the harness around it is empty.

The same gap drives the **model-routing waste** the paper flags on p. 42. Today Requiem runs every agent through `default_provider()` (`src/requiem/providers/__init__.py:49`), which picks one provider for the whole process based on env vars. The planner (complex; benefits from frontier reasoning) and the closer (mechanical summarization; cheap model is fine) use the same provider. Token spend is bounded above by frontier prices for tasks that don't need them. With `--commit` runs against live ADO costing ~$X per dogfood today, the lever is real even at single-operator scale.

The **token observability** half is the easiest of the three: the data is already in receipts. The only missing piece is making it operator-visible (and therefore actionable) rather than buried in JSON.

### What we already have

- `AgentSpec.model: str = "fake"` (`src/requiem/agent.py:39`) — per-agent model override is a field that already exists.
- `requiem.providers._common.make_receipt(model, input_tokens, output_tokens, latency_ms, request_id)` (`src/requiem/providers/_common.py:37`) — every real provider already emits this. Token data is captured for every `AnthropicProvider`, `OpenAIProvider`, and `CopilotProvider` call.
- ADR-0004 §4.4 puts receipts on every outcome variant as a peer field, plus a legacy in-`value` copy.
- ADR-0017's `roles:` block in `process.yaml` is the exact shape we need for `models:` — same precedence, same resolver pattern, same fail-closed preflight.
- The planner already emits the planner output sidecar (`<run_id>.plan.tree.json`, written by `_write_plan_sidecar` in `planning.py`). The per-leaf rationale + acceptance criteria are in there. ADR-0009 L1 (speckit alignment) further structures this as `spec.md`/`plan.md`/`tasks.md` when fully landed.
- ADR-0018 ratified that **Requiem owns leaf-PR open** (`base=feature/<root>`) — which means Requiem already has a write seam onto the leaf branch. Dropping an `AGENTS.md` slice as part of the leaf-PR-open is a one-file extension of work already happening.

So none of the three additions is greenfield. All three are *making first-class* something Requiem already does partially or implicitly.

---

## Decision

Three additions, ordered by dependency.

### 1. Per-leaf `AGENTS.md` slice — committed by Requiem before the worker runs

When Requiem dispatches a leaf (whether to the Hermes fleet via `kanban_executor` or in-process via `fanout`), it **first commits a `.requiem/AGENTS.md` file** to the leaf's working tree:

```
.requiem/
  AGENTS.md          # the curated context slice for THIS leaf
  rationale.md       # the planner's full rationale (linked from AGENTS.md)
  acceptance.md      # the criteria the verifier will check (linked from AGENTS.md)
```

The `AGENTS.md` file is **synthesised from the planner's output** and contains:

```markdown
# Context for leaf: <leaf_id>

You are working on one slice of a larger feature. Requiem (the SDLC
orchestrator that dispatched you) has already done the planning, branch
setup, and review wiring. Your job is the implementation of THIS leaf
only.

## What this leaf is

<leaf summary, from PlannerOutput.children[i].summary>

## Why this leaf exists

<planner rationale, from PlannerOutput.children[i].rationale>

## Acceptance criteria

Requiem's verifier will check these after you mark the task complete:

- <criterion 1, from process_config or speckit tasks.md>
- <criterion 2>
- ...

## Files Requiem expects you to touch

<inferred file list, from PlannerOutput.children[i].expected_files OR `[]` if planner didn't infer them>

DO NOT modify files outside this list without explicitly noting why in your
PR description. The handoff verifier (ADR-0017 §4) compares your
`changed_files` claim against the actual diff.

## Doctrine relevant to this leaf

<curated doctrine slice — the sections from ../doctrine/*.md whose tags
overlap with the leaf's labels OR the work-item type>

## Out of scope

- The trunk PR (Requiem opens it after every leaf merges).
- Coordinating with sibling leaves (Requiem owns acceptance-gated release).
- Long-term refactors (this is one slice).
```

The `rationale.md` and `acceptance.md` are full-fidelity dumps of the planner's structured output for forensic use; `AGENTS.md` is the prose summary that the agent reads.

**Implementation surface:**

- New module `src/requiem/context_pack.py` exposing `build_context_pack(leaf, plan_tree, process_config, doctrine_dir) -> ContextPack`. Pure; no I/O.
- New verb in the trunk-bootstrap or leaf-dispatch path: `commit_context_pack(repo_client, leaf_branch, pack) -> Outcome`. Idempotent (a re-run on the same leaf branch with the same `plan_hash` produces a byte-identical pack and no-ops via existing branch-ref check).
- The pack is **committed to the leaf branch before the worker is dispatched**, so when the worker checks out `impl/<root>-<item>` the `.requiem/AGENTS.md` is already there for whatever coding agent the worker uses.
- For the in-process `fanout` backend (ADR-0021), the same module writes the pack to the worktree directly — no PR needed.

**Why a file, not a prompt parameter:** the worker's coding agent (Claude Code, Codex CLI, Copilot agent, Hermes coder, whatever) discovers `AGENTS.md` via its existing convention. Requiem doesn't have to know which coding agent is on the other side. This is the only design that survives the worker being a different tool tomorrow than it is today.

**What this does NOT do:**

- Does NOT replace the worker's own `AGENTS.md` at repo root if one exists. Requiem writes to `.requiem/AGENTS.md`; coding agents that walk multiple `AGENTS.md` files (Claude Code, Cursor, Codex CLI) read both. If a coding agent only reads root-level `AGENTS.md`, the leaf-pack is still consumable via `cat .requiem/AGENTS.md` and shows up in any tool that lists changed files.
- Does NOT carry per-run secrets or PAT-style content. Curated public-style summary only.
- Does NOT carry the full event log — just the planner-produced rationale + acceptance + doctrine slice. The event log stays the source of truth, not duplicated into the leaf branch.

### 2. Role→model routing policy in `process.yaml`

Mirror the existing `roles:` block from ADR-0017 §1 with a `models:` block, addressed by **role name** (planner, reviewer, implementer, closer, judge):

```yaml
# .requiem-config/process.yaml — illustrative shape; user-editable per repo
models:
  planner:    { provider: anthropic, model: claude-opus-4.7,   max_tokens: 4096 }
  reviewer:   { provider: anthropic, model: claude-sonnet-4,   max_tokens: 2048 }
  implementer: { provider: copilot,  model: gpt-4.1 }
  closer:     { provider: openai,    model: gpt-4o-mini,        max_tokens: 1024 }
  judge:      { provider: anthropic, model: claude-haiku-4,    max_tokens: 1024 }

  # Default for any role not explicitly listed (today's behaviour preserved)
  default:    { provider: copilot,   model: gpt-4.1 }
```

**Resolver:** new module `src/requiem/model_routing.py`. Single function `resolve_model_for_role(role, config) -> (provider, model)` with the same precedence chain ADR-0017 §1 uses for roles. Falls back to `default_provider()` if no policy is configured. Fail-closed if the policy names a provider the host doesn't have credentials for.

**Propagation:**

- `AgentSpec.model` is already a field. The model-resolver populates it at workflow-build time.
- `AgentSpec` gains an optional `role: str | None = None` field so the resolver can key off role rather than agent name. Workflows that already pass `role` informally as a charter convention gain a typed slot.
- Each provider's `invoke()` already honours `AgentCall.spec.model` (verified: `anthropic.py` docstring "per-call override via `AgentSpec.model`"). No provider-side changes.

**What this does NOT change:**

- Workflows that don't set a role in their `AgentSpec` keep today's behaviour (use the `default_provider()`-resolved model). Backward-compatible by default.
- The `roles:` block from ADR-0017 stays — it maps role → Hermes profile (which worker picks up the task). The new `models:` block maps role → LLM provider/model (which LLM the agent inside any workflow uses). Different axes, different concerns.

**Sequencing dependency for ADR-0029:** the eval runner's LM-judge rubrics MUST resolve their model via `models.judge` in this policy, so Phase 3 of ADR-0029 picks the cheapest configured judge automatically. Don't ship LM judges before this block exists, or the cost discipline never lands.

### 3. Token + latency rollup on the event stream

The receipt data is already there. Three additions surface it:

**3a. Aggregate the data Requiem already has into a final event.**

When a run terminates (any disposition), the kernel emits one final `run_cost_summary` event after the existing `run_completed` event:

```json
{
  "kind": "run_cost_summary",
  "run_id": "...",
  "event_id": <last>,
  "totals": {
    "input_tokens": 12345,
    "output_tokens": 6789,
    "agent_call_count": 42,
    "total_latency_ms": 87123,
    "retry_count": 3
  },
  "per_role": {
    "planner":  { "calls": 8, "input_tokens": 4500, "output_tokens": 2100, "latency_ms": 32000 },
    "reviewer": { "calls": 9, "input_tokens": 5200, "output_tokens": 1800, "latency_ms": 28000 },
    "implementer": { ... },
    "closer": { ... }
  },
  "per_model": {
    "claude-opus-4.7":   { "calls": 17, "input_tokens": 9700, "output_tokens": 3900 },
    "claude-sonnet-4":   { "calls": 9,  "input_tokens": 5200, "output_tokens": 1800 },
    "copilot/gpt-4.1":   { "calls": 14, "input_tokens": ...,  "output_tokens": ... },
    "openai/gpt-4o-mini": { "calls": 2,  "input_tokens": ...,  "output_tokens": ... }
  }
}
```

Pure projection over the events already in the log — implementable as `kernel.summarize_costs(event_log) -> CostSummary` plus a single emit call at terminal-state transitions. Receipts on `RetryableFailure` / `PermanentFailure` / `BadOutput` count too (the retry path's token spend is part of the bill).

**3b. Surface it in `requiem events`.**

`requiem events <run_id>` gains a closing block:

```
─── Cost ──────────────────────────────────────────────
  42 agent calls · 19,134 tokens · 87.1s aggregate latency
   planner:    8 calls · claude-opus-4.7    · 6,600 tok · 32.0s
   reviewer:   9 calls · claude-sonnet-4    · 7,000 tok · 28.0s
   implementer: 14 calls · copilot/gpt-4.1 · ...
   closer:     2 calls · openai/gpt-4o-mini · ...
   judge:      9 calls · claude-haiku-4    · ...
───────────────────────────────────────────────────────
```

Operator can immediately see whether the bill went somewhere they expected. Cost regressions become a visible-on-every-run signal — the operator's pre-eval canary.

**3c. Dashboard exposes the same data per run (ADR-0019).**

Dashboard run-detail view gets a small cost panel reading the `run_cost_summary` event. No new SSE channel; the panel renders on every transition like the other event consumers.

**What this does NOT do:**

- Does NOT price tokens in dollars in the engine. Models change pricing weekly; baking $/token into Requiem creates a maintenance debt for no operator benefit (the token count IS the operator-visible signal). A separate `requiem cost <run_id>` subcommand could apply user-configured price tables later; out of scope here.
- Does NOT alert on cost regressions. That's eval (ADR-0029) — specifically the `system-no-cost-regression` rubric, which reads the `run_cost_summary` event.
- Does NOT aggregate across runs. Per-run only. North-star §5 excludes cross-run analytics.

---

## Architecture sketch

```
src/requiem/
  context_pack.py         # NEW: build_context_pack(leaf, plan_tree, process_config, doctrine_dir) -> ContextPack
  model_routing.py        # NEW: resolve_model_for_role(role, config) -> (provider, model)
  agent.py                # MODIFY: AgentSpec.role: str | None = None
  events.py               # MODIFY: register `run_cost_summary` event kind
  kernel.py               # MODIFY: emit run_cost_summary on terminal state
  cli.py                  # MODIFY: requiem events <run_id> renders the cost block

src/requiem/workflows/
  trunk_bootstrap.py      # MODIFY: after `feature/<root>` exists, write .requiem/AGENTS.md for each leaf at dispatch time
  kanban_executor.py      # MODIFY: call commit_context_pack before dispatch (Hermes fleet path)
  fanout.py               # MODIFY: write context pack to worktree before in-process implementer runs

evals/rubrics/             # ADR-0029 dependency, NOT in scope of this ADR
  system_no_cost_regression.py    # reads run_cost_summary event

tests/
  test_context_pack.py            # pure unit tests on the synthesiser
  test_model_routing.py           # precedence, fail-closed, default fallback
  test_run_cost_summary.py        # event emission + per-role/per-model rollup
  test_per_leaf_agents_md_lifecycle.py   # commit-once idempotency, plan-hash invalidation
```

### Idempotency and resume safety

All three additions must respect `INV-RESTART`:

- **Context pack commit:** the pack content is a deterministic function of `(leaf_id, plan_hash, doctrine_hash, process_config_hash)`. Re-running on the same hashes produces a byte-identical commit; the existing branch-ref check no-ops. A plan replan with a new `plan_hash` produces a fresh pack — same idempotency story as ADR-0017's plan-hash-keyed kanban tasks.
- **Model routing:** the resolved model is recorded in the `agent_call_started` event envelope (free; loose envelope per ADR-0004 §4.3). Resume reads the recorded model rather than re-resolving — a `process.yaml` edit mid-run cannot change which model a partially-completed agent call attributes its cost to.
- **Cost summary:** emitted only on terminal-state transitions; the projection is deterministic over the log. Re-running cost summary on a resumed run produces an identical value. `INV-CANCEL-RESUME-IDEMPOTENT` covers the no-op case.

### Failure modes worth pinning in tests

- **`models:` block names a provider the host has no credentials for.** Preflight fail-closed with actionable text (mirrors ADR-0017 `fleet_preflight` shape).
- **A workflow's `AgentSpec` carries `role="planner"` but no `models.planner` entry exists.** Fall back to `models.default`, log a warning, do not fail.
- **The doctrine directory contains 200 files; the leaf is small.** Pack synthesiser must cap the doctrine slice (configurable, default ~4 KB). Overrun → emit a `context_pack_truncated` event (loose envelope kind, observability only) and continue.
- **`expected_files` is empty in the planner output.** Pack omits the file list; the verifier accepts any non-empty diff (today's behaviour).
- **The pack commit conflicts with a worker's prior commit on the leaf branch.** The pack is committed *before* dispatch; this conflict means the leaf branch existed before bootstrap, which is already a preflight failure. Fail-closed with operator guidance.
- **`run_cost_summary` written before all receipts have flushed.** Pinned by an integration test that asserts `sum_of_per_call_tokens == summary.totals.input_tokens` for a representative run.

---

## Scope of this ADR

### In scope

- `context_pack.py` module + `commit_context_pack` verb + integration into `trunk_bootstrap`/`kanban_executor`/`fanout`.
- `model_routing.py` module + `models:` block in `process.yaml` (extension to existing `process_config.py`).
- `AgentSpec.role` field + propagation.
- `run_cost_summary` event emitted on terminal-state transitions.
- `requiem events` renders the cost block.
- Dashboard renders the same data per run.
- Unit tests for each component + one end-to-end pin (cost summary totals match per-call receipts).

### Out of scope (explicit deferrals)

- **$/token pricing in the engine.** A `requiem cost <run_id> --price-table <path>` subcommand could apply user-configured pricing later. Not v0.
- **Cross-run cost analytics.** Excluded by north-star §5.
- **Cost-based routing** (auto-route to cheaper models when budget is tight). Reactive routing is hard to make INV-RESTART-safe. Manual policy editing only in v0.
- **Per-leaf RAG/knowledge retrieval beyond the doctrine slice.** A real retrieval pipeline (vector store, ranking, freshness) is its own multi-ADR effort. The doctrine-slice heuristic is the v0 floor.
- **Persistent agent memory across runs.** ADR-0003 explicitly deferred this. ADR-0030 does not revisit.
- **LM-judge cost guard.** Lives in ADR-0029 Phase 3, dependent on the `models.judge` entry from this ADR.

### What this ADR refuses to change

- No new outcome variant.
- No invariant change.
- `process.yaml` schema gains one block (`models:`) — extension only, existing configs keep working.

---

## Consequences

### Positive

- Per-leaf implementer becomes informed about *its* slice without the operator needing to hand-craft the prompt.
- Model spend becomes a configured concern, not an accident of which env var is set. The paper's OpEx lever (p. 42) is now operator-pullable.
- Per-run cost is operator-visible on every `requiem events` — the canary that catches a model-rev or a prompt-bloat regression before the next dogfood.
- ADR-0029's LM-judge rubrics get a defined home (`models.judge`) and a cost-shaped default (Haiku-class, not Opus-class).
- ADR-0017's handoff wire contract (`changed_files` claim) gains semantic teeth: the leaf-pack writes the *expected* file set, the verifier compares against the *actual* set. Closes the loop between planner intent and worker action.

### Negative

- New surface in `process.yaml` (`models:` block) to maintain. Mitigated by mirroring the well-understood ADR-0017 `roles:` shape and providing a `default:` entry so unconfigured roles fall through.
- The leaf-pack synthesiser is one more module to keep correct. Mitigated by pure-function shape (no I/O, easy to unit-test) and idempotent commit (no resume hazard).
- An operator running `--dry-run` no longer sees an empty leaf branch — there's now a `.requiem/AGENTS.md` commit. Visible in `git log` of the scratch repo; harmless but documentation-worthy.
- `run_cost_summary` adds one event per run. Cheap; deterministic; bounded.

### Sequencing relative to other live work

| Order | Item | Dependency reasoning |
|---|---|---|
| 1 | ADR-0025 Gap B (implementation workflow → `RepoPlatform`) | Required for either delivery path to land on ADO. |
| 2 | ADR-0025 Gap C (worker backend) | Determines whether the per-leaf AGENTS.md flows to a Hermes worker or an in-process implementer — both targets supported here, but Gap C choice shapes which integration test ships first. |
| 3 | **ADR-0030 part 1: model routing + token rollup** (this ADR) | Pure additive, depends on nothing in ADR-0025. Can ship in parallel with Gap B. Unblocks ADR-0029 Phase 3 (LM judges). |
| 4 | **ADR-0030 part 2: per-leaf AGENTS.md slice** | Depends on Gap C choice (file location differs slightly between Hermes worker and in-process fanout). Lands once Gap C is decided. |
| 5 | First `--commit` run with the full stack | Source of truth for ADR-0029 seed corpus. |
| 6 | ADR-0029 (evals) | Captures trajectories from #5; consumes `models.judge` for LM rubrics. |

Parts 1 and 2 are independently shippable; the ADR documents both because they together implement the paper's "context engineering" framing and decoupling them buys nothing.

---

## STATUS log

- **2026-06-22 PROPOSED.** Plan written; no code committed. Implementation order recorded in §Sequencing. Parts 1 (model routing + token rollup) can begin immediately in parallel with ADR-0025 Gap B. Part 2 (per-leaf AGENTS.md) waits on Gap C's worker-backend choice.
- **2026-06-22 PART 1 SHIPPED on feat/adr0030-model-routing-cost-rollup.** §2 model routing + §3 cost rollup: `model_routing.py` resolver with `models.<role>` > `models.default` > empty precedence; `AgentSpec.role` field added; kernel emits `agent_call_started` (recording role/provider/model) BEFORE each provider invocation, with resume-idempotency (recorded event wins on resume); `cost.py` `summarize_costs(events)` pure projection; `run_cost_summary` event emitted ONCE after every terminal `run_completed` (operator cancel, in-loop cancel, missing edge, Cancelled outcome, retry exhausted, success terminate, route miss, bad output, permanent failure); cost block rendered in `requiem events`. 36 new tests (13 model routing + 9 cost summary + 1 integration pin in test_planning_workflow.py + 13 test_resume_fidelity helpers updated to recognize agent_call_started as a re-emit kind); full suite 1001/1001 passing (1 deselected fanout-worktree-race flake, 150 skipped). Part 2 (per-leaf .requiem/AGENTS.md slice) is on sibling branch feat/adr0030-context-pack.
- **2026-06-22 PART 2 SHIPPED on feat/adr0030-context-pack.** §1 per-leaf `.requiem/AGENTS.md` slice: pure synthesiser + idempotent commit verb in `src/requiem/context_pack.py` (736 lines); wired into `implementation` workflow as a new `commit_context_pack` script node between `create_branch` and `invoke_coder`; `coder_prompt` reads `.requiem/AGENTS.md` and appends as "Curated context from Requiem"; `fanout._build_leaf_context_pack` builds the pack from `FanoutLeaf` + `inputs.process_config` + `inputs.doctrine` (both threaded from `end_to_end.run_pipeline`); `context_pack_truncated` event registered; defensive throughout (a synthesiser exception returns None → leaf falls back to baseline prompt). 24 new tests (15 synthesiser + 7 commit verb + 2 integration); full suite 1027/1027 passing (1 deselected fanout-worktree-race flake, 150 skipped). Kanban-path integration deferred to follow-up (Gap C* — kanban needs a `RepoPlatform.commit_file` extension; dogfood today uses `--backend fanout` exclusively).
- **2026-06-23 PART 2 FIELD-PROVEN against AB#62759077 (run #24).** Sequential `--backend fanout --on-escalate accept-last` (NO `--fanout-parallel`) against the live CVAPI dogfood Scenario produced **6/19 leaves landed end-to-end with the context pack**, vs run #22's 1/19 baseline (same scenario, no pack). **~6× lift in success rate.** Total file_changes shipped by successful leaves: 26 (vs 5 in run #22) — DTOs, interfaces, telemetry, services, tests. Failure mode for the remaining 13 leaves was 100% upstream Copilot CLI `network_timeout: copilot session timeout (>180s)` — orthogonal to the context pack, clusters after ~5-7 successful invocations (likely Copilot-side rate-limit or session-resource throttle on the gh-auth-token). On-disk verification: `.requiem/AGENTS.md` lands on the leaf branch's worktree containing the planner's rationale, expected files, acceptance criteria, and doctrine slice; `read_agents_md` splices it into `coder_prompt` under "Curated context from Requiem". Three load-bearing fixes were shipped alongside Part 2 in commit `9898c68` to close the `--on-escalate accept-last` loop: (a) `planning._write_plan_sidecar` writes `.plan.tree.json` for decomposable plans even when verdict=needs_human (previously the guard skipped the tree, so commit_plan got a `.plan.md` markdown and crashed on `bad_artifact: not JSON`); (b) `commit_plan.load_tree` accepts `verdict in {approved, needs_human}` (the escalation policy already gated entry at end_to_end); (c) `implementation.assert_clean_workspace` filters out `.requiem/` porcelain entries (the dry_run context-pack write would otherwise show as "dirty" on every subsequent sequential leaf). Plus commit `7f80723` — `commit_context_pack` dry_run now WRITES the files (just skips the git commit), so `read_agents_md` can find them. Without that fix the verb was a no-op and the read-side wiring never fired in dogfood. See also commit `b3c2289` (R4 projection — independent) and commit `d24be2b` (Part 1 — independent). All three feature branches merged to `main` on 2026-06-23 via `c74a06a` (R4), `2910f70` (Part 1), `bb7fc89` (Part 2). Merged-main test suite: 1081 passed / 150 skipped / 1 deselected.
- **2026-06-23 COPILOT TIMEOUT TRIAGED.** Investigated run #24's 13 `network_timeout` failures and discovered the 180s ceiling was OURS, not Copilot's — `asyncio.wait_for(timeout=_SESSION_TIMEOUT_S)` in `src/requiem/providers/copilot.py`. The pre-ADR-0030 prompts were tiny (no curated AGENTS.md splice); 180s was generous. The §1 context pack adds ~2KB of curated context and pushes genuine successful Claude-on-Copilot calls past 180s on non-trivial leaves. Fix shipped as `243c1c1`: default raised to 600s + made configurable via `CopilotProvider(session_timeout_s=N)`. 41/41 copilot tests passing; 3 new pins.
- **2026-06-24 RUN #25 → schema_mismatch dominates.** First run from merged main + 600s ceiling. Killed early after 5 leaves: 1/5 succeeded (a non-implementable stakeholder-coordination task that correctly returned `0 file_changes`); 3/5 hit `bad_output:schema_mismatch` at 279-487s; 1 in-flight. Root cause: `coder_prompt` placed "Return a CoderOutput …" BEFORE the ~1.7KB AGENTS.md splice. Claude-on-Copilot read the rich rationale+acceptance+doctrine LAST and produced thoughtful prose narrative instead of structured JSON — the bigger the context pack, the worse the regression. Fix shipped on `feat/coder-prompt-tail-instructions` (merged as `551b414`): the schema instruction now appears at the TAIL of the prompt, after the curated context. Two new pin tests assert `instr_idx > pack_idx`. Suite: 1081/150/1 passing (no regression).
- **2026-06-24 RUN #26 → Copilot SDK implicit tool-use cascade.** Re-run with coder_prompt fix. 0/20 leaves landed. Leaf 1 wrote two files to the worktree (`specs/005-sku-fallback-monitoring/`, a 4KB `SkuFallbackMetrics.cs`) during its 600s invoke_coder window, then we cut off the session at the ceiling. Leaves 2-20 all bailed at `assert_clean_workspace` with `workspace.dirty` because the untracked files persisted. Two distinct issues identified: (Issue 2) the Copilot SDK's `create_session(available_tools=None)` defaults to the FULL builtin tool surface (write_file, edit_file, bash, web_fetch, …) — not "no tools" as our docstring assumed; `_allow_all_permissions` was happily approving file-write calls. (Issue 3) `_on_timeout` built an empty receipt (`latency_ms=0, input_tokens=0, request_id=""`) despite the session running for the full ceiling, masking the real picture in the event log. Both fixed in `45f7486` (merged as `89dfd2d`): (a) `create_session` now passes `available_tools=list(BUILTIN_TOOLS_ISOLATED)` — the SDK's own "no host access, no cross-session state, no network" preset (ask_user, task_complete, exit_plan_mode, task, read_agent, write_agent, list_agents, send_inbox, context_board, skill); (b) `_on_timeout` now takes `elapsed_s`/`session_id`/partial token counts and produces a receipt that reflects what actually happened. Three new pin tests: receipt-latency-reflects-elapsed, receipt-fallback-when-elapsed-unknown, session-uses-isolated-tool-preset. Suite: 1089/150/1 passing. The expected run #27 outcome reverts to the original mid-band forecast (10-14/19) since the three intervening regressions (180s timeout, prompt order, tool surface) are now all fixed; only Copilot's actual response quality on context-pack-dense prompts remains as a free variable.
- **2026-06-24 RUN #27 → Issues 2+3 CONFIRMED FIXED; Claude extended-thinking now the limiter.** First run with the cumulative fix stack from runs #24-#26. Killed early after 5 leaves (1/5 succeeded with 2 file_changes at 343s; 2/5 hit clean 600s ceiling with accurate receipts; 1/5 bad_output at 431s; 1/5 in-flight). The Issue-2 fix held — leaf 1's worktree had ONLY `.requiem/` as untracked after a 600s session, no orphan files; leaf 2's `assert_clean_workspace` passed cleanly (no cascade). The Issue-3 fix held — timeout receipts now show `latency_ms=600162, input_tokens=18593, output_tokens=365, request_id="<session-id>"` (vs run #26's `latency_ms=0`), giving operators an accurate view of what Copilot was actually doing. The remaining failure mode: Claude on the Copilot SDK takes >600s and emits some token output (`output_tokens=365` after 600s = ~0.6 tok/s) but never fires `session.idle`, so no final `assistant.message` event lands and `response_text` stays empty. This appears to be the model in extended-thinking mode without surfacing the final answer within the ceiling. The Sonnet-4.5 model's `reasoning_summary` / `reasoning_effort` knobs on `create_session` are likely candidates for a follow-up tune (currently we don't set them — SDK defaults apply). Not a regression of any shipped fix; the structural failures are gone, only the model-throughput question remains.
- **2026-06-24 RUN #28 → ADR-0030 §2 wiring gap closed end-to-end.** Per-role model routing landed as four coordinated fixes spanning the day. (1) `DEFAULT_COPILOT_MODEL` bumped from `claude-sonnet-4.5` to `claude-sonnet-4.6` after `CopilotClient.list_models()` showed 4.5 has `supported_reasoning_efforts=None` (no separate reasoning loop, no tunable knobs) while 4.6 supports `low/medium/high/max` plus a 5× larger prompt window (936K vs 168K); (2) `CopilotProvider` now accepts `reasoning_effort`/`reasoning_summary`/`context_tier` kwargs and threads them into `create_session`; (3) `CODER_SPEC.role="implementer"` plus a matching `models.implementer` block in the operator yaml (`~/.config/requiem/cvapi-process.yaml`) — the operator-supplied routing now actually fires (commits `3ac9bba`, `ecbbb33`); (4) closed an unreachable-loader gap where `ProcessConfig.models` was declared on the dataclass but `_build_from_mapping` never parsed `models:` from YAML (the entire ADR-0030 §2 routing had silently no-op'd in production despite the unit-test suite passing — tests built ProcessConfig directly, bypassing the loader). Field result: 4/23 leaves landed (17%), 19/20 failures clustered at the 600s session ceiling, every leaf provably ran on the operator-pinned `claude-sonnet-4.6`. Total file_changes shipped: 29. The routing infrastructure is now end-to-end exercised; the remaining bottleneck is session-ceiling shape, not routing wiring.
- **2026-06-24 RUN #28 follow-ups → `plan_tree._walk` handles accept-last partial decomp + reasoning_effort per-role plumbing.** Two more fixes between #28 and the eventual run #30. (a) `fba27ec` (`feat/plan-walk-needs-human-leaves`): the `--on-escalate accept-last` path can leave a tree where a decomposable node has `proposals=[6]` but `children=[]` (planner escalated before recursing into children sub-workflows); without a fix `resolve_leaves` raised `PlanArtifactError(misaligned)` and aborted the whole fanout. `_walk` now emits one `ResolvedLeaf` per proposal for `final_verdict='needs_human'` nodes, and `_validate_tree_header` accepts `verdict in {approved, needs_human}` to mirror `commit_plan.load_tree`. (b) `fd67cb5` (`feat/model-routing-reasoning-effort`): extended `ModelSpec` with `reasoning_effort`/`reasoning_summary`/`context_tier` fields; kernel now packs them via `AgentCall.model_options` (new field) so per-call values beat constructor defaults; `_validate_entry` fails closed on malformed shapes. Operator yaml shape: `models.implementer.reasoning_effort: low` is now functional end-to-end (yaml → loader → ProcessConfig → ModelSpec.to_model_options → AgentCall.model_options → CopilotProvider.create_session). Suite: 1106/150/2.
- **2026-06-24 RUN #29 → cancelled mid-run.** Launched with the cumulative fix stack PLUS `reasoning_effort: low` in operator yaml. After 7 leaves landed 1/7 success, user pushed back on the framing: "why is faster important here over accuracy and quality? ideally we would have a high ceiling (an hour?) on a given session, but have lower tolerance for 'seems idle and isn't doing any work'." Killed and pivoted to a proper ceiling-shape fix instead of chasing speed.
- **2026-06-24 DUAL-CLOCK IDLE RECOVERY shipped on `5b5ca33` (run #29 follow-up).** Direct port of conductor's `IdleRecoveryConfig` pattern from `~/projects/conductor/src/conductor/providers/copilot.py`. The single-bucket `asyncio.wait_for(done.wait(), timeout=_SESSION_TIMEOUT_S=600)` is replaced with a dual-clock loop: (1) `max_session_seconds=3600` (1 hour) — hard wall-clock ceiling; (2) `idle_timeout_s=120` — short clock that fires when no MEANINGFUL SDK event has arrived for this long, then sends a recovery prompt (default 3 attempts) before failing. The key insight from conductor: when `wait_for(done, idle)` fires but `time_since_last_event < idle`, EVENTS ARE STILL FLOWING — reset and keep waiting. Bookkeeping events (`session.start`, `pending_messages.modified`, `session.info`) explicitly do NOT reset the idle clock (`_IDLE_IGNORED_EVENTS` frozenset, mirrors conductor). Backward-compat: `CopilotProvider(session_timeout_s=N)` silently maps `max_session_seconds=N` via `__post_init__` so legacy callers keep their pinned ceiling. 7 new pin tests in `tests/providers/test_copilot_dual_clock.py` covering: defaults pinning, legacy-kwarg compat, idle-doesn't-fire-when-events-flow, idle-fires-when-events-truly-stop, recovery-prompt-sent-on-idle, wall-clock-caps-runaway-event-stream, bookkeeping-events-ignored. Suite: 1113/150/2.
- **2026-06-25 RUN #30 → dual-clock CONFIRMED WORKING; new failure mode discovered.** First clean run with dual-clock + `reasoning_effort=low` retained from #29. **8/24 leaves landed (33%), 7 of 8 successes had latency >600s** (would have died under the old single-bucket ceiling: 1212s, 845s, 601s, 1218s, 1433s, 769s, 735s, 431s). Total file_changes shipped: 54 (highest ever). The dual-clock is the headline unlock — leaves that legitimately need 15-25 minutes of actively-emitting work now complete. **However, leaf 9 surfaced a new failure mode: `bad_output:schema_mismatch` after 2648s (44 minutes) with 120K input tokens.** Best read: the recovery_prompt loop fired multiple times, the prompt accumulated, the model emitted lots of tokens but never produced a parseable CoderOutput JSON. The bad_output then poisoned the worktree (20+ uncommitted files written by the Copilot SDK during the recovery loop) and every subsequent leaf hit `assert_clean_workspace` with `permanent_failure:workspace.dirty` and refused to run. So 24 leaves processed = 8 successes + 1 bad_output + 15 cascaded structural failures from a single dirty worktree. **Three follow-ups are clearly indicated**: (a) implementation workflow should clean the worktree (`git reset --hard && git clean -fd` filtered to non-`.requiem`) on bad_output before the run terminates, so the cascade doesn't poison sibling leaves; (b) the recovery_prompt path needs an upper bound on cumulative `tok_in` so we don't trip Copilot's own limits or context window; (c) the Copilot SDK's `BUILTIN_TOOLS_ISOLATED` preset doesn't appear to be holding through the recovery_prompt's new `session.send()` call — needs verification (the model wrote 20+ .cs files despite the isolated preset). The dual-clock itself worked exactly as designed; the regression is the recovery-prompt behavior on a hostile / structurally-broken leaf.
- **2026-06-26 RUN #30 LEAF 9 ROOT-CAUSED → SDK tool-filter is advisory, not authoritative.** A standalone repro harness (`~/c/tmp/repro-excluded-tools.py`) confirmed `available_tools=BUILTIN_TOOLS_ISOLATED` is **NOT** an authoritative whitelist. The SDK forces `toolFilterPrecedence: "excluded"` on the wire (see `copilot/client.py` ~lines 1802/2369), making `available_tools` a weak hint that the runtime's default excluded list overrides. Tested four configurations against the prompt "write Foo.cs"; under `available_tools` alone the model called `powershell`, `task`, `create`, `apply_patch`, `view` — none of which are in `BUILTIN_TOOLS_ISOLATED`. Only `excluded_tools=ToolSet().add_builtin("*")` actually sealed the surface (zero tool calls of any kind). Fix shipped as `67d8978` (merged as `4e5ccf7`): `CopilotProvider.create_session` now passes BOTH `available_tools=BUILTIN_TOOLS_ISOLATED` AND `excluded_tools=ToolSet().add_builtin("*")` — belt-and-suspenders. The requiem coder agent doesn't need any SDK-side tool (its CoderOutput JSON IS the work product; `apply_changes` is a requiem verb, not an SDK tool). Pin tests extended: `test_session_uses_isolated_tool_preset` now asserts both kwargs are present and that `excluded_tools` contains `"builtin:*"`. Suite: 1113/150/2 (same flakes). Captured the SDK gotcha as a skill (`github-copilot-sdk-tool-isolation` under `mlops/`) so a future integrator doesn't lose hours re-deriving it. Two follow-ups from run #30 still pending: (a) implementation workflow cleans worktree on bad_output (defensive, in case the SDK ever changes tool semantics again), (b) recovery_prompt cumulative `tok_in` cap.
- **2026-06-26 RUN #30 FOLLOW-UPS SHIPPED on `feat/run-30-followups` — both defensive fixes landed before run #31.** Two belt-and-suspenders fixes against the leaf-9 failure shape, so run #31 can isolate the dual-clock + tool-isolation stack as its only variables. **(A) Worktree-scrub-on-failure (`cleanup_worktree`).** New best-effort verb in `requiem.workflows.implementation` runs `git reset --hard HEAD && git clean -fd -e .requiem` before the workflow yields to any post-coder terminal (`end_failed`, `end_needs_human`). Two thin `FilesystemClient` primitives back it: `git_reset_hard(ref="HEAD")` and `git_clean_with_excludes(excludes=[...])`. Workflow topology gained two intermediate script nodes (`cleanup_for_needs_human` → `end_needs_human`, `cleanup_for_failed` → `end_failed`) so the disposition is preserved by routing target, not by the verb itself. The verb is best-effort: any `FsClientError` is swallowed into `Success(reason="fs_error", ...)` so cleanup can never block a terminal yield. Routing pin: **pre-coder failures bypass cleanup** (`assert_clean_workspace`, `create_branch` probes route directly to `end_failed`) because that dirt is human-owned and silently scrubbing it would destroy diagnostic state. **Post-commit failures also bypass cleanup** (push/PR-creation failures keep the local branch as the operator's record). 8 new pin tests: 5 on `git_reset_hard` + `git_clean_with_excludes` (drop tracked mods, preserve untracked, remove untracked, preserve `.requiem`, composite contract); 3 on the workflow (polluted worktree scrubbed before `end_needs_human`, `.requiem/` preserved across cleanup, pre-coder dirt NOT scrubbed). The fix is defensive — the actual leaf-9 SDK leak is sealed by the `excluded_tools=ToolSet().add_builtin("*")` change shipped earlier as `4e5ccf7` — but a future SDK change that re-opens the tool surface would otherwise re-introduce the cascade. **(B) Cumulative input-token cap (`max_cumulative_input_tokens`).** New `CopilotProvider` constructor knob (default 80_000, `None` to disable) caps peak observed `assistant.usage.input_tokens` across a single `invoke` call. When exceeded, the dual-clock loop fails fast as `RetryableFailure` via the same `_on_timeout` path as wall-clock and idle exhaustion — so the receipt carries the partial token counts the operator needs to see. Closure now tracks `usage_in = max(usage_in, int(ein))` per `assistant.usage` event so peak-vs-cumulative semantics are robust to future SDK changes (peak ≤ cumulative when counts are cumulative, peak < cumulative when counts are per-turn — both fail the cap later, not earlier). Calibration: successful CVAPI dogfood leaves use 10K-30K input_tokens, run-#30 successful peak hit 18K-22K, the leaf-9 wedge ran to 120K — 80K gives 3-4× headroom over typical success while sitting 50K under the leaf-9 fail point and well under sonnet-4.6's 936K context window. 4 new pin tests on the cap: default value pinned, fires `RetryableFailure` on overrun (with peak input_tokens in receipt), silent under cap, `None` disables. Suite: 1121/150/2 (8 new fs + workflow tests, 4 new copilot tests; same pre-existing flakes).

## References

- `docs/references/sdlc-vibe-coding-applicability.md` — analysis that promoted this gap.
- Osmani/Saboo/Kartakis (May 2026) pp. 15–18 (context engineering, static vs dynamic), pp. 41–42 (model routing as financial lever).
- ADR-0017 §1 — the `roles:` block this ADR's `models:` block mirrors.
- ADR-0018 — the trunk-integration contract where the leaf-pack commit fits in.
- ADR-0029 — the evals discipline that depends on `models.judge` from this ADR.
- `src/requiem/providers/_common.py:37` — `make_receipt` (the data source for the cost rollup, already populated by every real provider).
- `src/requiem/agent.py:39` — `AgentSpec.model`, the per-agent override field that already exists.
