# Phase A — Agent Boundary + FakeProvider seam

> **Seam #5.** Every LLM call in Requiem crosses this boundary. Production calls real models; tests replay a `FakeProvider`. The contract decides how a workflow declares an agent, binds structured output, executes tools, and survives transient failure and cancellation.

**Status:** prototype gallery. No production code. Three runnable variants for Daniel to drive.

---

## What this seam owns

Every interaction with an LLM. Specifically:

- **Declaration** — how a workflow author describes an agent (system prompt, response schema, tools, model).
- **Invocation** — how the engine calls the model and gets back a typed result.
- **Structured output binding** — model bytes → pydantic model, with validation failure as a distinct outcome.
- **Tool / function-call round-trip** — model invokes a registered Python function; engine dispatches and feeds results back.
- **Transient error handling** — 429 / 503 / 5xx / auth, retried under a fixed budget.
- **Cancellation** — a deadline cancel must abort an in-flight call cleanly without one more retry attempt.
- **Test injection** — the harness substitutes a `FakeProvider` at the boundary; no monkey-patching in tests.

## Invariants this seam must respect

| Invariant | How each variant honours it |
|---|---|
| **INV-DISCRIMINATED-OUTCOMES** | Every variant returns `Success \| BadOutput \| Transient \| Permanent \| Cancelled`. The variant *tag* is the contract. |
| **INV-RESTART** | `retry_key` is stamped on every call and restamped per attempt (`{key}#N`) so the journal can correlate retries with one logical call. |
| **INV-CANCEL-SHORT-CIRCUITS-RETRY** | All retry wrappers check `cancel.is_set()` before *and* during backoff; a tripped cancel returns `Cancelled` and never starts another attempt. |
| **INV-EVENT-LOG-AUTHORITATIVE** | Every variant accepts an `event_callback`. The engine wires this to `run.events.jsonl`. The seam emits at minimum `prompt`, `tool_call`, `response`. |
| **3-retry hard cap (north-star §4)** | `MAX_ATTEMPTS = 3`. Each `with_retry` raises if a caller asks for more. |
| **INV-NO-CORRUPT-FORWARD** | `BadOutput` is its own variant — it is *not* network-retried. Schema-validation failure is a workflow-domain concern (re-prompt with the error, or surrender), not a transport concern. |

`BadOutput` getting its own tag is the load-bearing call. Conductor today conflates schema failure with generic "agent failed", which means the retry budget gets eaten by malformed JSON. We refuse that.

---

## File layout

```
prototypes/agent-boundary/
├── README.md                          ← you are here
├── requirements.txt
├── run_all.py                         ← runs all 3 demos in sequence
├── variant-a-protocol-provider/       ← Protocol seam, bring-your-own client
│   ├── outcomes.py   provider.py   fake.py   retry.py   agents.py   demo.py
├── variant-b-pydantic-ai/             ← pydantic-ai Agent + FunctionModel fake
│   ├── outcomes.py   provider.py   fake.py   retry.py   demo.py
└── variant-c-litellm-direct/          ← direct litellm.completion + validators
    ├── outcomes.py   agents.py   fake.py   retry.py   demo.py
```

Run any demo directly: `python prototypes/agent-boundary/variant-X-…/demo.py`. Or `python prototypes/agent-boundary/run_all.py` for all three. Each demo prints sections 1–7 mapped to the task contract.

The dirs use dashes (not underscores) and are not Python packages — each demo adds its own dir to `sys.path` and imports siblings flat. This is intentional: it keeps each variant a self-contained spike with no cross-variant coupling.

---

## Variant comparison

| Axis | A — Protocol provider | B — pydantic-ai | C — LiteLLM direct |
|---|---|---|---|
| **Dependency surface** | Engine: pydantic only. Live provider uses litellm (~30 transitive). Provider is replaceable. | pydantic-ai (~50 transitive: anthropic, openai, mistralai, cohere, mcp, opentelemetry, …). Heavy. | litellm (~30 transitive). Pydantic for output. |
| **Ergonomics for agent author** | One `AgentSpec` dataclass. Explicit `Tool` declaration. Boilerplate but obvious. | Best: `@agent.tool` decorator, typed `output_type`, dependency injection via `RunContext`. | Most explicit: agent is a dataclass + an `invoke()` function that you compose. No magic. |
| **Structured output** | Pydantic `model_validate_json` at the seam; `BadOutput` on `ValidationError`. | Library-native via `output_type=`; pydantic-ai internally re-prompts up to `output_retries`. Our seam projects its `UnexpectedModelBehavior` into `BadOutput`. | Manual: JSON parse → schema validator → optional domain validators. Most flexibility, most code. |
| **Tool-call story** | Engine round-trips the tool loop. Demo's live path documents the contract; full live impl needs one more `completion` after each batch. | Free: pydantic-ai handles the loop, schema generation, and `ModelRetry`. | Manual: agent loops over `tool_calls`, dispatches via a `TOOL_REGISTRY`, appends `tool` messages back. ~60 LOC. |
| **FakeProvider cost** | ~100 LOC: a class with a `scripts` dict and `ToolRoundTrip` helper. Same shape as today's polyphony harness fake. | ~50 LOC: a `FunctionModel` wrapping a scripted `(messages, info) → ModelResponse`. *But* the script must speak pydantic-ai's `ToolCallPart` / `TextPart` taxonomy — coupling to library internals. | ~50 LOC: `FakeCompletionFn` returns a `SimpleNamespace` shaped like a `ChatCompletion`. No library coupling — the fake mimics the LiteLLM response shape, which is OpenAI-canonical. |
| **Transient handling** | Classification at the seam by exception inspection (string match). Explicit and engine-controlled. | Library raises typed `ModelHTTPError(status_code=N)` — clean classification by status code. | Same approach as A; LiteLLM normalises some exceptions but not all. |
| **Cancellation** | Cooperative: provider checks `cancel.is_set()` at entry and during work; retry wrapper races `cancel.wait()` against backoff sleeps. Demo proves it. | Library has no cancel primitive; we wrap `agent.run()` in `asyncio.wait(FIRST_COMPLETED)` against `cancel.wait()`. Works, but adds a layer. | Cooperative; the agent loop checks `cancel.is_set()` at every iteration of the tool loop. |
| **Lock-in risk** | Low. The Protocol is one method; swapping providers is replacing one class. | High. Agent definitions are pydantic-ai objects; tool functions use `RunContext`. Migrating away later means rewriting every agent. | Low. LiteLLM is itself a multi-provider shim; ditching it is replacing one call site. |
| **Observability** | Excellent: every callable knows nothing about logging, the engine wires `event_callback` to `run.events.jsonl`. Three event kinds today: `prompt`, `tool_call`, `response`. | Library has its own opentelemetry/logfire emission; we'd have to either disable it or bridge it into our event log. Friction. | Excellent: same `event_callback` pattern as A; LiteLLM has good logging hooks if we want them. |
| **Time-to-implement v0** | Medium. Have to write the live provider, tool round-trip, FakeProvider, retry. ~400 LOC. | Smallest. ~200 LOC of glue around pydantic-ai. | Largest. ~500 LOC: agent class + tool dispatch + validator framework + retry + fake. |
| **What it looks like in 18 months** | Stable. The Protocol absorbs new providers without changing the engine. | Couples our engine release cadence to pydantic-ai's. Their 1.0 → 2.0 will be our problem. | Stable. Each agent is plain Python; refactoring stays local. |

---

## Recommendation

**Variant A (Protocol-based AgentProvider).** The seam is one method. The fake is small. The engine doesn't import any LLM client — `litellm` lives behind the live provider so it can be swapped or removed. Observability and cancel ride on the same Protocol with no library coupling. The cost is that we write the tool round-trip ourselves (`~60 LOC`), but we already write the event-log integration and the retry wrapper, so we are not really avoiding that work in any variant.

**Variant B is tempting** — pydantic-ai's `Agent` is genuinely nicer to author, `FunctionModel` is a real piece of test infrastructure, and `@agent.tool` is the cleanest tool API of the three. But the dependency surface is enormous (anthropic + openai + mistralai + cohere + mcp + opentelemetry transitively), the FakeProvider couples to internal message-part taxonomy that will change across minor versions, and the library has its own retry loop that runs *inside* our retry loop — two budgets to reason about. The cancel story is also retrofitted, not native.

**Variant C is worth keeping in mind** for one specific situation: if we decide later that agents should be *just functions* with the engine providing no abstraction, this is the shape. Today it costs the most code and offers the least cross-agent reuse.

**Adopt A. Steal one idea each from B and C:**
- From B: the `@tool` decorator ergonomic. Build a tiny one in our `Tool` dataclass so agent authors write `@agent.tool` instead of constructing `Tool(...)` explicitly.
- From C: the explicit *validator pipeline* (schema validator + domain validators). Our agents will routinely want consistency checks ("if any blocking finding, recommend_merge must be False") that the response schema cannot express. Validators are first-class in C; they would slot cleanly into A's `AgentSpec`.

---

## Open questions for Daniel

1. **Which models for v0?** Polyphony-parity calls for at least Claude (haiku/sonnet) and one Copilot/GPT line. The live provider in A is LiteLLM-backed because that gives us "Anthropic + OpenAI + Azure + Bedrock + Vertex + Copilot" for one dependency. Are we OK with that, or do we want direct Anthropic + Copilot SDKs?
2. **Multi-provider failover.** When `Transient` exhausts on Claude, do we fail over to GPT (different vendor, different rate-limit pool) before declaring `Permanent`? The deep-dive's `provider.max_attempts=3` × `route.max=2` math (= 9 LLM calls) assumes one provider. Variant A has the seam for failover — `LiveProvider` could wrap a list — but we haven't designed the policy.
3. **How does the harness script agents?** Three options, all viable in A's `FakeProvider`:
   - **By agent name** (current implementation, matches today's polyphony harness): `scripts["code_reviewer"] = [response1, response2, ...]`.
   - **By role + agent name** (matches conversational agents with multiple personas).
   - **By a deterministic content hash** of the rendered prompt (catches "this scenario went down a different prompt path than expected" — but couples scenarios to prompt wording).
   The polyphony harness fake we lifted from uses option 1 and Mahler-2 (the polyphony engineer) has been happy with it. Default: keep option 1.
4. **`BadOutput` remediation policy.** The seam exposes `BadOutput` cleanly, but who handles it? Options: (a) re-prompt automatically with the validation error injected as a system message (pydantic-ai-style, max N times); (b) surrender to the human gate immediately (no auto re-prompt — strictest INV-NO-CORRUPT-FORWARD reading); (c) the workflow author chooses per agent. Pydantic-ai default is (a) with N=1. Daniel's earlier `INV-NO-CORRUPT-FORWARD` framing suggests (b) or (c).
5. **Auth failures: transient or permanent?** Variant A treats `401/403` as `Transient` (under the 3-retry cap, on the theory that a token refresh elsewhere may have repaired it). This matches the error-handling deep-dive's §F (auth shares the 3-retry ceiling with network). But Boulez might argue auth is always `Permanent` and route directly to human. Confirm before locking the classifier.
6. **Streaming.** None of the three demos exercises streamed responses. Polyphony-conductor has streaming for the agent dashboard. Do we need streaming at the seam *in v0*, or can the v0 UI render only completed messages?
7. **Tool-call ceiling.** Each variant caps tool round-trips at 8. Reasonable? Higher? Per-agent override?

---

## What the demos prove (the 7 capability checks)

Each `demo.py` executes seven labelled sections:

1. **Declaration** — prints the agent's name, model, response model, tools.
2. **Live invocation** — runs against a real model when API keys are present; degrades to a clean "skipped" notice when not. (CI runs dry.)
3. **Same agent under FakeProvider** — scripted JSON → parsed pydantic model → `Success`.
4. **Schema validation failure → BadOutput** — model returns bytes that don't match `response_model`; we get a `BadOutput` variant with the pydantic errors attached. *Not* a `Permanent` or `Transient`.
5. **Tool round-trip** — model "calls" `read_file` and `count_lines`; the engine dispatches them, feeds results back, model returns the final structured payload.
6. **Transient retry** — fake returns 429 once, then success; `with_retry` recovers on attempt 2 with restamped `retry_key`. Followed by a "ceiling" sub-test that proves we stop at exactly 3 attempts and convert to `Permanent`.
7. **Cancellation mid-call** — `cancel.set()` fires while the agent is in flight; the call returns `Cancelled` and the retry loop never starts another attempt.

Every demo passes today on a machine with no API keys. With `ANTHROPIC_API_KEY` set, section 2 in variants B and C does a real one-shot round-trip; variant A's live path is similarly wired (but its demo defaults to the credential probe).
