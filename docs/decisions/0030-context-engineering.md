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

## References

- `docs/references/sdlc-vibe-coding-applicability.md` — analysis that promoted this gap.
- Osmani/Saboo/Kartakis (May 2026) pp. 15–18 (context engineering, static vs dynamic), pp. 41–42 (model routing as financial lever).
- ADR-0017 §1 — the `roles:` block this ADR's `models:` block mirrors.
- ADR-0018 — the trunk-integration contract where the leaf-pack commit fits in.
- ADR-0029 — the evals discipline that depends on `models.judge` from this ADR.
- `src/requiem/providers/_common.py:37` — `make_receipt` (the data source for the cost rollup, already populated by every real provider).
- `src/requiem/agent.py:39` — `AgentSpec.model`, the per-agent override field that already exists.
