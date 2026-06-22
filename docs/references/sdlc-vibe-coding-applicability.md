# SDLC with Vibe Coding — applicability to Requiem

> **Source:** Osmani, Saboo, Kartakis (Google, May 2026), *"The new SDLC with vibe coding: From ad-hoc prompting to Agentic Engineering."* 51-page Google whitepaper, distributed via the Microsoft AzureDeveloperExperience-GeneralChatOpen Teams channel.
>
> **Status:** Reference. Captures the verdict of a one-shot analysis of the paper against Requiem's architecture, north-star invariants, and active ADRs. Used as the source of truth for the follow-up ADRs (0029 evals, 0030 context engineering).
>
> **Date:** 2026-06-22

---

## TL;DR

Requiem is already a deliberate, opinionated implementation of the paper's **agentic engineering** end of the spectrum — applied one layer up (SDLC orchestration) rather than at the per-task coding layer. The paper's framework (factory model, harness, conductor↔orchestrator, "AI amplifies your engineering culture") maps onto Requiem's existing design choices well enough that the paper mostly **validates the direction without redirecting it**.

The paper surfaces **two real gaps worth ADR-ing** (evals; context engineering as a per-leaf operational concern) and a list of recommendations that should be **explicitly declined** because they over-scope a single-operator v0.

**No north-star invariant changes are recommended.** Nothing in 51 pages contradicts §2 of `docs/north-star.md`.

---

## Why this reference exists

The paper is well-cited reading inside Microsoft and Google engineering orgs in mid-2026. Requiem's design predates it but uses overlapping vocabulary (factory, harness, orchestrator, agent teams, eval). Mapping the two carefully serves three purposes:

1. **Defensive**: every adopted framework lures a future reader into "why doesn't Requiem do X from the paper?" — capturing the considered-and-declined answers here saves re-deciding.
2. **Generative**: two of the paper's recommendations expose gaps that are real *for Requiem specifically*. Naming them turns them into ADRs.
3. **Connective**: gives the existing ADRs a shared external vocabulary so a new reader (or LLM seat) can place each one on a familiar map.

---

## Mapping the paper's framework onto Requiem

### 1. Spectrum positioning (paper pp. 12–14)

Paper's spectrum: *Vibe Coding → Structured AI-Assisted → Agentic Engineering*. The differentiator is *how much structure, verification, and human judgment surrounds the AI's output.*

Requiem is **agentic engineering taken to the load-bearing extreme**:

- `INV-NO-CORRUPT-FORWARD` (north-star §2) is the architectural statement of the paper's "verification is not optional" thesis: a verb that can't verify prerequisites refuses to act; suspected hallucination routes to a human gate, **never** to auto-retry.
- `INV-DISCRIMINATED-OUTCOMES` (`Success | RetryableFailure | PermanentFailure | NeedsHuman | Cancelled | BadOutput`) is the contract version of the paper's "a fluent output that skipped its verification steps is more dangerous than one with a visible error" (p. 22). Requiem refuses to pretend an outcome is a success unless the verb says so structurally.
- The `receipts` pattern (ADR-0004 §4.4) — every state-mutating verb emits `inspected_artifacts` — is the paper's "verify before act" pattern made into a peer field on every outcome variant.

**Verdict:** the paper's spectrum is the *exact* axis Requiem locates itself on, at the right end.

### 2. Factory model (paper pp. 24–25)

Paper: *"the developer's primary output is not code — it's the system that produces code"* — five components: specs, agents, tests, feedback loops, guardrails.

| Paper component                | Requiem mechanism                                                                          |
| ------------------------------ | ------------------------------------------------------------------------------------------ |
| Specifications and context     | speckit alignment (ADR-0009 L1), AGENTS.md / .cursorrules inheritance                      |
| Agents that translate specs    | `AgentProvider` Protocol (`src/requiem/agent.py`) + `.team(...)` (ADR-0003)                |
| Tests + quality gates          | discriminated outcomes + receipts + 200+ resume-fidelity tests                             |
| Feedback loops                 | `NeedsHuman` gates + bounded retry budgets + escalation (ADR-0027)                         |
| Guardrails                     | `process.yaml` tier policy (ADR-0010), preflight (ADR-0017), `INV-NO-CORRUPT-FORWARD`      |

The factory model isn't *aspirational* for Requiem — it's the built shape. The README literally pitches *"You aren't supervising the engine. The engine is doing the boring orchestration so you can spend attention on the few decisions that actually want a human."*

### 3. Conductor vs Orchestrator (paper pp. 31–34)

Paper: conductor = real-time IDE pair-programming (Copilot, Cursor); orchestrator = async, multi-agent delegation (Jules, Copilot agent mode, Claude Code).

Requiem is **firmly orchestrator**, by design:

- The Hermes delivery fleet (ADR-0017) is a kanban-task-dispatching multi-agent fleet. Workers run async in containers; Requiem polls and adjudicates.
- The web dashboard (ADR-0019) + opt-in auto-resume implements *"checks in periodically, reviews output, provides course corrections"* (paper p. 33).
- The squad pattern used to *build* Requiem (Mahler, Wagner, Boulez, Ravel, …) is itself the orchestrator pattern. ADR-0003 made `parallel_fork` + `.team(...)` first-class so the composition is available to user workflows too.

The paper's worry that conductor mode *"becomes a bottleneck — if the developer is personally directing every keystroke"* (p. 33) is the exact failure Requiem exists to escape from. Requiem is on the right side of that distinction by construction.

### 4. The harness (paper pp. 26–31)

Paper's six harness components vs Requiem:

| Paper                       | Requiem                                                                                                                                  |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| Instructions / rule files   | Doctrine (ADR-0016 legible accumulation), `requiem-*` profile SOULs (ADR-0017), AGENTS.md inheritance                                    |
| Tools                       | `Toolbelt` (`GhClient`, `AdoClient`, `TwigClient`, …) + `RepoPlatform` Protocol (ADR-0024)                                               |
| Sandboxes / exec env        | Containerized hermetic fleet image (ADR-0017 §2 — image hash + Hermes version snapshotted into the event log)                            |
| Orchestration logic         | The kernel (`kernel.py`, ~1 KLOC)                                                                                                        |
| Guardrails / hooks          | Receipts pattern + `process_config` + `INV-NO-CORRUPT-FORWARD` + preflight fail-closed (`fleet_preflight.py`)                            |
| Observability               | Event log (`run.events.jsonl`) + `requiem events`/`--follow`/`--raw` + dashboard (ADR-0019)                                              |

The paper's punchline on p. 30: *"Most agent failures, examined honestly, are configuration failures."* Requiem agrees structurally — every gap between *what the AI says happened* and *what really happened* is funneled to `NeedsHuman` rather than auto-retried.

ADR-0017's *"a kanban `done` is evidence, not authority. A worker can mark a leaf done with a bad PR. Requiem's verifier/close_out adjudicates and reconciles ADO"* is the same observation applied recursively at the meta-orchestration layer.

### 5. Tests + Evals (paper pp. 14, 22, 44)

Paper splits verification into:

- **Tests** = deterministic checks (function input → output).
- **Evals** = non-deterministic checks: trajectory compliance, tool-use quality, hallucination, final-response quality — scored by labelled datasets, rubrics, LM judges.

Requiem has tests in abundance (~1 KLOC `tests/`, including the resume-fidelity matrix that pins INV-RESTART). **It does not have evals** in the paper's sense. The event log IS the trajectory data format — exactly the artifact you'd score — but there's no rubric, no scoring run, no benchmark suite, no LM judge over agent output.

The squad pattern's *"Boulez + Ravel are mandatory reviewers on every Phase A seam decision"* (roadmap.md) is essentially an LM-judge eval, just not formalized as one. **This is the paper's strongest live recommendation and Requiem's clearest gap.** Promoted to ADR-0029.

### 6. The 80% problem (paper p. 34)

Paper: AI generates 80% fast; the 20% (edge cases, integration, subtle correctness) demands deep context. *"The developers who navigate this challenge most effectively reserve their own attention for what AI struggles with."*

Requiem's response is the right one: structurally route the hard 20% to `NeedsHuman` and let the operator make the call. ADR-0027 (reviewer escalation handling) and commit `657532a` (*"accept needs_human terminal verdict from --on-escalate=accept-last"*) are literally the engineering of this boundary. The 2026-06-17 dogfood post-mortem in ADR-0025 (*"reviewer escalation cascade kills runs at the leaf level"*) is the operator's pre-paper diagnosis of the same dynamic.

### 7. Economics: CapEx/OpEx and token routing (paper pp. 39–42)

Paper: vibe coding = low CapEx, high OpEx (token burn, maintenance tax, security remediation). Agentic engineering = high CapEx, low OpEx. Recommends **intelligent model routing**: cheap models for deterministic work (test gen, code review, CI/CD), frontier models for complex (requirements, architecture, hard impl).

Requiem has the field — `AgentSpec.model: str = "fake"` — and **token counts already flow through receipts** via `requiem.providers._common.make_receipt(model, input_tokens, output_tokens, latency_ms, request_id)`. What's missing is:

1. A **routing policy** (every agent in a workflow currently uses the same provider via `default_provider()`'s environment-based pick).
2. **Per-run aggregation** of the token data already in receipts (no rollup, no dashboard surface, no per-role cost breakdown).

Promoted to ADR-0030.

### 8. Context engineering (paper pp. 15–18)

Paper splits context into six types (Instructions, Knowledge, Memory, Examples, Tools, Guardrails) and into static (always loaded) vs dynamic (loaded on task match) — with Agent Skills as the dynamic-context vehicle.

Requiem has **half** of this right and **half** of it absent:

| Type         | Static or dynamic | Status                                                                                                                              |
| ------------ | ----------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| Tools        | static            | ✅ Typed `Toolbelt`, not free-form text                                                                                              |
| Guardrails   | static            | ✅ `INV-NO-CORRUPT-FORWARD` + `process_config` + preflight                                                                           |
| Instructions | static            | ✅ Doctrine + profile SOULs                                                                                                          |
| Examples     | dynamic           | ⚠️ Implicit only (in agent charters)                                                                                                |
| Knowledge    | dynamic           | ❌ Per-leaf implementer receives the work-item title/body but no curated knowledge slice (planner's rationale, neighbouring files, doctrine excerpt) |
| Memory       | both              | ⚠️ No cross-run agent memory (ADR-0003: *"Persistent agent identity across runs… Deferred"*); event log is the only persistent memory |

The hole that matters for production work: **per-leaf static context for the implementer's coding agent**. The planner already has the rationale, the doctrine slice, the expected files, the acceptance criteria — but today they only flow into the event log, not into the implementer's prompt. Promoted to ADR-0030.

### 9. Open standards: MCP + A2A (paper pp. 38, 45)

Paper recommends MCP for tool access and A2A for cross-agent delegation as *"the connective tissue of multi-agent systems."*

Requiem deliberately doesn't speak either. `Toolbelt` + `RepoPlatform` Protocol + the handoff wire contract (ADR-0017 §4) are **closed equivalents**. This is the right v0 call (single-operator engine, no third-party verb registry — explicitly excluded in north-star §5). Adoption would only matter if Requiem ever needs to **be extensible by other developers**, which is a post-v0 question.

---

## Recommendations from the paper to explicitly decline

Each is good advice for the paper's audience (engineering leaders adopting AI broadly across an organization) but wrong for Requiem v0 (single power-user, single 4K monitor, 8+ hr/day, north-star §5).

| Paper recommendation                                                  | Decline because                                                                                                                       |
| --------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| "Adopt MCP + A2A as open standards"                                   | v0 is a closed-world engine. Toolbelt + RepoPlatform Protocol are the right closed contracts. Reconsider post-v0 if Requiem ever ships as a library others extend. |
| "Reframe hiring around judgment"                                      | Daniel **is** the team. The squad is the substitute for hiring.                                                                       |
| "Re-shape code review for AI-generated code" / "Train reviewers"      | One operator, already trained. The code-review changes that matter live inside Requiem's PR-review skills, not org policy.            |
| "Plan for hybrid teams of humans and agents, clear handoff protocols" | Already done at a deeper level than the paper imagines — ADR-0017 §4 handoff wire contract is untrusted, schema-versioned, golden-fixture tested. |
| "Distinguish prototyping work from production work in team norms"     | Single operator; the boundary is `--dry-run` vs `--live --commit` and is already explicit.                                            |
| Cross-run analytics dashboards                                        | Explicitly excluded in north-star §5 (*"Cross-run analytics"*).                                                                       |
| Workflow-authoring GUI                                                | Explicitly excluded in north-star §5.                                                                                                 |
| Multi-operator collaboration                                          | Explicitly excluded in north-star §5.                                                                                                 |

---

## Two framings the paper sharpens for free (no code)

These are worth absorbing into the existing docs even with no code change:

1. **ADR-0017 preflight is "configuration failure prevention" (paper p. 30):** *"Most agent failures, examined honestly, are configuration failures."* ADR-0017's preflight fail-closed (missing profile, orchestration not Manual, version mismatch, profile-home escape) is exactly this. Naming it that way in the ADR's TL;DR makes the design legible to anyone who has read the paper.
2. **The dashboard is the orchestrator-mode UI (paper pp. 33–34):** ADR-0019's dashboard implements the orchestrator-mode interaction model. Saying that phrase in the dashboard ADR connects it to a now-well-known mental model.

These can be folded in as small docstring/intro edits the next time those ADRs are touched, not as their own ADR.

---

## What this reference promotes to ADR

Two ADRs follow from this analysis:

- **ADR-0029** — Evals as a first-class discipline. The strongest live recommendation in the paper, and the only mechanism that will catch agent drift when models version up under us.
- **ADR-0030** — Context engineering: per-leaf AGENTS.md slice, model routing policy, token-cost rollup. All three are additive, no invariant changes.

Both are sequenced **after** ADR-0025's v0-critical-path gaps (Gap B implementation-workflow ADO refit, Gap C worker backend). See ADR-0030 §Sequencing for the precise dependency order.

---

## What the analysis explicitly did NOT find

For completeness — these are claims that would be worth flagging if true, and aren't:

- **No invariant violation.** The seven north-star §2 invariants survive the paper's framework intact. The paper's "factory" / "harness" / "orchestrator" lexicon is additive vocabulary, not a competing design.
- **No ADR contradiction.** Every existing ADR (0001-0028) survives the paper's framework. Some are sharpened by it (ADR-0017 preflight, ADR-0019 dashboard); none are challenged.
- **No "we should have built X" hindsight.** The two gaps surfaced (evals, context engineering) are *additive* discoveries from a paper Requiem predates, not retroactive failures to consider — both would have been Phase D / post-parity even if the paper had existed in May.

---

## Source

The PDF lives at `https://microsoft.sharepoint.com/teams/AzureDeveloperExperience-GeneralChatOpen/Shared Documents/SDLC with Vibe Coding.pdf` (SSO-gated; Microsoft corp tenant only). Pulled and OCR'd to plain text on 2026-06-22 against the Requiem `HEAD = 657532a`.
