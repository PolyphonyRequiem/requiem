# ADR 0029 — Evals as a first-class discipline

**Status:** Proposed (2026-06-22)
**Date:** 2026-06-22
**Relates to:**
ADR-0002 (event-log-authoritative — trajectory data already exists in the right shape),
ADR-0003 (agent teams — Boulez/Ravel adversarial review is an informal LM judge today),
ADR-0004 §4.4 (receipts pattern — already capture per-call `model`, tokens, latency, request_id),
ADR-0025 (dogfood path — every gap surfaced is "the agent did something we didn't catch until it landed in ADO/the trunk"),
ADR-0027 (escalation handling — without rubrics, reviewer escalations are operator interpretation calls).
**Inspiration:** `docs/references/sdlc-vibe-coding-applicability.md` §5; Osmani/Saboo/Kartakis (May 2026) pp. 14, 22, 44.

---

## TL;DR

Promote evals — *rubric-scored replay of agent trajectories against a golden dataset* — to a first-class engineering practice alongside `pytest`. The substrate already exists (the event log IS trajectory data, `FakeProvider` gives reproducibility, receipts carry per-call metadata). The missing layer is the *discipline*: a golden corpus, a rubric vocabulary, a `requiem eval` CLI, and a CI gate.

This ADR is **purely additive**. It changes no invariant, no outcome variant, no event envelope. It earns its keep by catching the failure mode Requiem cannot catch today: an agent (planner, reviewer, implementer, closer) that *quietly degrades* when its charter, its model, or its surrounding doctrine changes — the failure that lives in the gap between "tests still pass" and "the outcome of the run actually changed shape."

---

## Context

### What we have for verification today

| Surface | What it catches | What it misses |
|---|---|---|
| `pytest` (~200 resume-fidelity tests, ~800 others) | Workflow topology correctness, INV-RESTART, log invariants, kernel + DSL behaviour, per-verb mechanics | **Anything where the agent's output shape is "valid" but its content is wrong** — a planner that decomposes correctly but produces vague leaves (the ADR-0025 Gap A symptom); a reviewer that escalates on prompt-bug grounds (ADR-0027 M1); an implementer whose PR passes lint but contradicts the leaf's rationale |
| Live dogfood (15+ runs since June 5) | End-to-end behaviour against real LLM providers | Expensive, slow, non-reproducible, no scoring vocabulary — operator reads `events --follow` and forms an opinion |
| Boulez/Ravel adversarial review (squad pattern) | New ADRs / new code | Informal: a human operator decides whether the verdict applies. Not run on CI. Not run on the agent charters themselves. |

### The failure mode pytest cannot reach

Run 12 of the dogfood (ADR-0027 M3, "convergence failure"): planner produces five iterations, reviewer finds new (legitimate) concerns in each, ITER_CAP exhausts, the whole tree dies. Every individual agent call passed its schema validation. Every event was correctly written. Every verb returned the right outcome variant. The *workflow* succeeded at its mechanical contract. The *agents* failed at their semantic contract — and nothing in the current test surface notices.

Run 5 (ADR-0027 M1, "reviewer-prompt bug") is the same shape: the reviewer escalated because it believed it lacked context that it actually had. The fix was a one-line prompt change. The detection mechanism today is "operator reads the events.jsonl and notices the reviewer is wrong." No automation. No regression pin.

### Why now

Three forcing functions:

1. **Models will version up under us.** Copilot, Anthropic, OpenAI all ship breaking model changes on quarter-to-half-year cadence. The paper warns (p. 28) that the harness, not the model, dominates behaviour — but only if you *measure* harness behaviour. Without an eval gate, a `claude-opus-4.7` → `claude-opus-5.0` upgrade can silently move the planner's verdict distribution and we will only discover it during the next dogfood (or worse, during a `--commit` run).
2. **The agent population is growing.** Planning has a planner + reviewer. ADR-0025 Gap A introduced a policy classifier. ADR-0027 Shape A proposes a self-classifying reviewer (`escalation_reason`). ADR-0030 will add a per-leaf-context-curator agent. Each addition is a new failure surface that pytest cannot reach.
3. **Charter changes are unprovable.** Today, editing `src/requiem/workflows/planning.py`'s planner system-prompt block is a leap of faith — there's no way to assert "the new prompt is at least as good as the old one on the cases I care about." The squad pattern uses Boulez+Ravel to grade ADR drafts; the same discipline should apply to the agents that ship.

### What we already have that we don't need to rebuild

- **Trajectory data:** every `run.events.jsonl` is, in eval terms, a complete trajectory record. ADR-0004 §4.3 (single events stream, two lenses) means every agent call is already there with model, tokens, latency, retry budget, outcome.
- **Reproducibility:** `FakeProvider` (`src/requiem/agent.py:56`) deterministically replays scripted agent responses. A golden trajectory is "a sequence of agent outputs that, when replayed, produces a known event-log shape."
- **Receipt vocabulary:** `requiem.providers._common.make_receipt(model, input_tokens, output_tokens, latency_ms, request_id)` is already attached to every outcome. An eval run can compare receipts across runs (cost regression, latency regression).
- **Discriminated outcomes:** every agent call already returns a typed `Success | RetryableFailure | PermanentFailure | NeedsHuman | Cancelled | BadOutput`. Rubrics can key off the variant tag without inspecting opaque text.

The missing layer is the *discipline*: a corpus, rubrics, a runner, and a gate.

---

## Decision

Land evals as a first-class discipline alongside `pytest`. Five components, all under `evals/` at repo root:

### 1. Golden trajectory corpus — `evals/corpus/`

A small (~10–20 entries to start, growing) curated set of past runs with curated outcomes, organized by **agent role**:

```
evals/corpus/
  planner/
    cv-scenario-62759077-attempt3.json    # five clean leaves + two M3 convergence casualties
    cv-feature-clean-decomp.json
    polyphony-leaf-only-root.json
    ...
  reviewer/
    m1-reviewer-prompt-bug.json
    m2-human-input-required.json
    m3-convergence-stalled.json
    ...
  implementer/
    impl-clean-pr.json
    impl-hallucinated-import.json
    ...
  closer/
    ...
  end-to-end/
    cv-scenario-62759077-full.json        # whole-run trajectories for system-level evals
    ...
```

Each entry is a **trajectory record** — schema-versioned JSON:

```json
{
  "schema_version": 1,
  "id": "cv-scenario-62759077-attempt3",
  "role": "planner",
  "captured_at": "2026-06-17T...",
  "source_run_id": "...",
  "input": {
    "work_item": { "title": "...", "body": "...", "type": "Feature" },
    "process_config": { ... },
    "doctrine_slice": "..."
  },
  "agent_calls": [
    {
      "agent_name": "planner_1",
      "user_message": "<exact prompt assembled by the workflow>",
      "expected_outcome_kind": "success",
      "expected_response": { ... }      // the original Success.value, schema-validated
    },
    ...
  ],
  "expected_final_event": {
    "kind": "run_completed",
    "disposition": "completed",
    "final_node": "end"
  },
  "rubrics": ["planner-substantive-summary", "planner-no-noise-decomp"],
  "tags": ["dogfood", "cvapi", "scenario", "post-adr0025-gapa"]
}
```

Trajectories are **captured from real runs** via a `requiem capture <run-id> --as-trajectory <role> --out evals/corpus/...` CLI subcommand (Phase 2 below — Phase 1 hand-writes them from existing `.events.jsonl` files).

### 2. Rubric vocabulary — `evals/rubrics/`

A rubric is a small Python module exposing one function:

```python
# evals/rubrics/planner_substantive_summary.py
from requiem.eval import Rubric, RubricResult

NAME = "planner-substantive-summary"
DESCRIPTION = "Planner output must produce leaf summaries that are not just restatements of the title."

def score(trajectory_actual, trajectory_expected) -> RubricResult:
    """Return a 0.0-1.0 score + per-leaf rationale."""
    actual_summary = trajectory_actual.agent_calls[-1].response["summary"]
    title = trajectory_actual.input["work_item"]["title"]
    if _is_paraphrase(actual_summary, title):
        return RubricResult(score=0.0, rationale=f"summary {actual_summary!r} restates title {title!r}")
    return RubricResult(score=1.0, rationale="summary adds substantive detail")
```

Three rubric *kinds* by who/what scores them, mirroring the paper's eval taxonomy (p. 14):

| Kind | Scorer | Use for |
|---|---|---|
| **Deterministic** | pure Python (regex, structural compare, schema delta) | "did the receipt's `model` match what we expected?", "did the planner emit `policy_tier=implementable` when the type policy says it should?" |
| **Reference-bound** | structural compare against expected | "did the agent's `Success.value["parsed"]` match the golden, allowing for ordering / whitespace?" |
| **LM-judged** | a dedicated `judge` `AgentProvider` (defaults to the cheapest model in the model-routing policy from ADR-0030) | "is the planner's rationale convincing?", "did the reviewer's escalation feedback name a specific actionable concern?" |

LM-judged rubrics make eval cost a real concern — see §3 below for the cost guard.

Initial rubric set (the minimum that would have caught known live failures):

| Rubric | Role | What it pins |
|---|---|---|
| `planner-substantive-summary` | planner | leaf summaries add detail beyond the title (would catch ADR-0025 Gap A symptom pre-policy-classifier) |
| `planner-respects-tier-policy` | planner | when `implementable_types` includes the work item's type, planner short-circuits or marks `decomposable=false` (regression pin for ADR-0025 Gap A) |
| `reviewer-no-context-blindness` | reviewer | reviewer's feedback text does not claim to lack data that was in the prompt (regression pin for ADR-0027 M1) |
| `reviewer-escalation-is-actionable` | reviewer | escalation feedback names a specific blocking concern, not a generic "this could be better" |
| `implementer-changed-files-claim-matches-reality` | implementer | the handoff metadata's `changed_files` matches the actual git diff in the worker's branch (against the kanban handoff golden — ADR-0017 §4) |
| `closer-summary-cites-pr` | closer | close-out summary references the merged PR by URL |
| `system-no-cost-regression` | end-to-end | total input+output tokens for a known trajectory does not increase by >20% (model-upgrade canary) |
| `system-no-latency-regression` | end-to-end | total `latency_ms` for a known trajectory does not increase by >50% |

### 3. The runner — `requiem.eval`

A new module + CLI subcommand:

```bash
# Run all rubrics against the whole corpus, using FakeProvider scripted from each
# trajectory's `agent_calls`. Fast, hermetic, free.
requiem eval

# Run only one role
requiem eval --role planner

# Run only one rubric across the whole corpus
requiem eval --rubric planner-substantive-summary

# Run against a *real* provider — uses the trajectory's input as the live
# prompt, scores the actual agent output. Slow, costs tokens, used for the
# model-upgrade canary (§5).
requiem eval --live --provider anthropic --model claude-opus-4.7

# Capture a fresh trajectory from a past run
requiem eval capture <run_id> --role planner --out evals/corpus/planner/<name>.json
```

Output is structured: per-rubric score, per-trajectory pass/fail, aggregate role-level score, total token cost (real provider only). Failures print a unified-diff of `actual` vs `expected` trajectory shape.

Default mode (hermetic / `FakeProvider`) **must run in under 60 seconds on the full corpus** so it can sit on `pre-commit` without friction. Live mode is opt-in.

### 4. The CI gate

`pytest` and `requiem eval` are both pre-commit and pre-merge gates:

```yaml
# Conceptual — actual CI yaml depends on the runner once it exists
pre-commit:
  - pytest -q --timeout=120 -x
  - requiem eval --hermetic-only           # 60s cap, must be 100% pass
pre-merge:
  - requiem eval --live --canary           # the "did the model break?" smoke; opt-in per branch label
```

The hermetic eval is mandatory; the live canary is **opt-in via PR label** (`run-live-eval`) because it costs real tokens. Branches that touch agent charters, system prompts, or provider/model defaults MUST set the label — enforced by a pre-merge check that greps the diff for any file under `src/requiem/providers/`, `src/requiem/workflows/planning.py` reviewer/planner system-prompt blocks, or `evals/rubrics/`.

### 5. The model-upgrade canary

A scheduled cron (operator's choice — weekly? monthly?) runs `requiem eval --live` against the configured production model set and compares scores to the last known-good snapshot stored at `evals/baselines/<model>-<date>.json`. Score regressions surface as an operator-visible alert. New baselines are committed deliberately, not auto-rolled.

This is the *only* mechanism that will catch silent model drift. Without it, a Copilot model rev or an Anthropic version bump can move the planner's behaviour by 20% and we won't know until we lose half a dogfood run.

---

## Architecture sketch

```
evals/                                 # repo-root, NOT under src/
  README.md                            # what evals are, how to add one
  corpus/                              # golden trajectories, by role
    planner/*.json
    reviewer/*.json
    implementer/*.json
    closer/*.json
    end-to-end/*.json
  rubrics/                             # python modules, one per rubric
    __init__.py
    planner_substantive_summary.py
    reviewer_no_context_blindness.py
    ...
  baselines/                           # last-known-good live-eval scores per model
    claude-opus-4.7-2026-06-22.json
    ...

src/requiem/eval/                      # the runner — under src/, importable as `requiem.eval`
  __init__.py
  trajectory.py                        # Trajectory dataclass, schema_version, IO
  rubric.py                            # Rubric protocol, RubricResult, registry, discovery
  runner.py                            # eval orchestration: hermetic vs live, parallel scoring
  capture.py                           # `requiem eval capture` — turn a .events.jsonl into a trajectory
  cli.py                               # `requiem eval` subcommand (registered from src/requiem/cli.py)
  judges.py                            # LM-judge `AgentProvider` wrappers + cost guard

tests/test_eval_*.py                   # unit tests for the runner, rubric protocol, capture round-trip
```

The runner is under `src/requiem/eval/` (importable, unit-testable). The corpus + rubrics are at `evals/` (data, not code) so they can be edited without touching the engine.

### Trajectory ↔ event-log round trip

The trajectory format is *not* the event log — it's a curated view. Capture is one-way (`events.jsonl` → `Trajectory`); replay uses the trajectory's `agent_calls[].expected_response` to script a `FakeProvider` and runs the workflow as normal. The runner asserts the resulting `.events.jsonl` matches the trajectory's `expected_final_event` and that each rubric's score crosses its threshold.

This means the trajectory format must be **lossless for the inputs the rubrics care about** (work item, process config, doctrine slice, the agent's input message) and **selective for the outputs** (only the scored fields, plus the discriminated-outcome variant tag). Schema versioning on the trajectory protects us against the inevitable expansion.

---

## Scope of this ADR

### In scope

- `evals/` directory layout + corpus seed (5–10 hand-curated trajectories pulled from past dogfood runs).
- `src/requiem/eval/` module: `Trajectory`, `Rubric`, `RubricResult`, runner, capture, CLI.
- 6–8 seed rubrics named above (the ones that would have caught known live failures).
- `requiem eval` CLI subcommand with hermetic + live modes.
- Pre-commit hook + CI gate wiring (hermetic only).
- One end-to-end test (`tests/test_eval_runner_end_to_end.py`) that captures a known run, replays it, and scores all seed rubrics.

### Out of scope (explicit deferrals)

- **LM-judge rubrics in the seed set.** All seed rubrics are deterministic. LM judges are a Phase 3 follow-up — they need ADR-0030's model-routing policy first (so they pick the cheapest model automatically) and they need cost telemetry before we let them run unattended.
- **The model-upgrade canary cron.** §5 above describes the shape; the implementation lands after the runner + seed corpus are stable. Three months of live eval data is needed before "baseline" is a meaningful word.
- **Eval-driven prompt optimization.** The paper (p. 23) describes a "continuous quality flywheel" where eval failures feed back into prompt revisions automatically. Out of scope — manual revisions only in v0. The flywheel is a Phase D / post-parity item.
- **Cross-run aggregate analytics** ("how has the planner score trended?"). Explicitly excluded by north-star §5 (no cross-run analytics).
- **Public benchmarks** (Terminal-Bench, SWE-Bench, etc.). Requiem's eval discipline is for *our* agents, not for benchmarking the engine itself. If we later want to enter benchmarks, that's a separate ADR.

### What this ADR refuses to change

- No new outcome variant. Rubric results are external scoring, not engine state.
- No new event kind. Eval is *outside* the engine, reading the log; it never writes to it.
- No new agent. LM judges are deferred (above).
- No invariant change.

---

## Consequences

### Positive

- Charter / prompt edits become provable: the operator can assert "the new prompt is no worse on the cases I care about" before shipping.
- Model upgrades become safe: the canary catches drift before it lands in a `--commit` run.
- New rubrics become the bug-report format: "the reviewer escalated wrongly on run-X" → capture as a trajectory + write a rubric → permanent regression pin.
- Token + latency cost regressions become CI-detectable (paper p. 41 — the OpEx lever).
- The squad pattern's Boulez/Ravel-as-LM-judge can be lifted from "informal practice" to "Phase 3 LM-judged rubric set" without rebuilding it.

### Negative

- New gated CI step (~60s hermetic, paid in tokens for live). Cost mitigated by hermetic default + opt-in live label.
- New discipline: every rubric must have a clear rationale and a failure-mode story. Without that, the corpus becomes a graveyard of "we thought this mattered" pins that block merges without informing anyone.
- The corpus needs maintenance: a workflow refactor that changes the agent-call topology breaks every trajectory in that role's corpus. Mitigated by `schema_version` on the trajectory + a `requiem eval capture --migrate` path (Phase 2).
- LM-judge cost is real if/when Phase 3 ships. The model-routing policy from ADR-0030 must be in place first.

### Sequencing relative to other live work

This ADR lands **after** ADR-0025's two remaining gaps:

- ADR-0025 Gap B (implementation workflow takes `RepoPlatform`) — unblocks ADO dogfood end-to-end.
- ADR-0025 Gap C (worker backend stood up) — unblocks the first real `--commit` run.

Reason: eval discipline needs *more than one live `--commit` run* to bootstrap a meaningful corpus. Today there are zero. Capturing dogfood runs against a still-broken pipeline produces "trajectories of failure to dispatch" — not the useful kind.

Concretely:

| Order | Item | Why this order |
|---|---|---|
| 1 | ADR-0025 Gap B (implementation → RepoPlatform) | The dogfood blocker. Code surface is fully scoped in ADR-0025; small. |
| 2 | ADR-0025 Gap C (worker backend) | Without this, no real implementer trajectories exist to score. |
| 3 | First `--commit` run of CVAPI Scenario `#62759077` | Source of truth for the seed corpus. |
| 4 | ADR-0030 (context engineering) | Drops in alongside Gap B; model-routing policy lands here, which Phase 3 LM judges need. |
| 5 | **ADR-0029 Phase 1** (this ADR — corpus + runner + 6–8 deterministic rubrics + CI gate) | Capture the first real trajectories from steps 3–4. |
| 6 | ADR-0029 Phase 2 (LM-judged rubrics, model-upgrade canary cron) | After ~3 months of baseline data. |

This sequencing also means the seed corpus will be the **product of v0 dogfood** — every entry is a real run, every rubric pins a real observed failure mode. No speculative trajectories.

---

## STATUS log

- **2026-06-22 PROPOSED.** Plan written; no code committed. Implementation order recorded in §Sequencing; Phase 1 implementation only kicks off after ADR-0025 Gap B + Gap C land and the first real `--commit` run captures the seed corpus.

## References

- `docs/references/sdlc-vibe-coding-applicability.md` — analysis that promoted this gap.
- ADR-0027 §"Failure mode taxonomy" — the M1/M2/M3/M4 split that names what a planner/reviewer rubric should grade.
- ADR-0025 §"Gap A" — the specific failure (vague leaf summaries) the `planner-substantive-summary` rubric would have caught.
- ADR-0017 §4 — the handoff wire contract that the `implementer-changed-files-claim-matches-reality` rubric audits.
- Osmani/Saboo/Kartakis (May 2026) pp. 14, 22, 44 — the paper's tests-vs-evals split, trajectory evaluation, and "set the bar at the eval, not the demo."
