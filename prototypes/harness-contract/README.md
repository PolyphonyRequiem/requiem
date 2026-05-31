# Phase A — Harness scenario contract (seam #9)

> Three runnable prototype variants for what a Requiem harness scenario
> looks like. Sits next to the polyphony harness for direct comparison.
> All three drive the same `_engine/` stub: a ~250-LOC single-process
> Python engine that exercises every load-bearing invariant.

## What this seam is

The harness is the substrate every Requiem workflow ships with — by
hard constraint, **the test surface ships with v0** (north-star §4).
This seam decides:

- the file shape of a scenario,
- the assertion vocabulary,
- the chaos / failure-injection primitives,
- the sub-workflow scripting story,
- how INV-RESTART is exercised (which is the harness's hardest job).

## Which invariants the harness must exercise

Every Requiem invariant must be *cheaply expressible* in at least one
scenario:

| invariant                          | exercise mechanism in this seam               |
| ---------------------------------- | --------------------------------------------- |
| INV-SINGLE-PROCESS                 | the engine is a Python `WorkflowEngine`; fakes inject at function-call boundary |
| INV-RESTART                        | `ChaosHook` kills mid-run; restart resumes from JSONL log |
| INV-NO-CORRUPT-FORWARD             | scenarios assert `permanent_failure` terminals & gate routing |
| INV-EVENT-LOG-AUTHORITATIVE        | every assertion reads `EventLog`; nothing else |
| INV-DISCRIMINATED-OUTCOMES         | `Success / RetryableFailure / PermanentFailure / NeedsHuman / Cancelled` are first-class |
| INV-CANCEL-SHORT-CIRCUITS-RETRY    | `Cancelled` outcome terminates immediately, no retries |
| INV-NO-ENGINE-ABANDONMENT          | `NeedsHuman` routes through scripted gates, never to auto-retry |

## Layout

```
prototypes/harness-contract/
├── _engine/                     # SHARED stub engine (~250 LOC)
│   ├── outcomes.py              # discriminated outcome union
│   ├── events.py                # append-only JSONL EventLog
│   ├── workflow.py              # Node / Workflow primitives
│   ├── provider.py              # AgentProvider + FakeProvider
│   ├── engine.py                # WorkflowEngine: run, retry, resume, chaos
│   └── examples.py              # 4 example workflows used by all variants
├── variant-a-yaml-scenarios/    # declarative YAML
├── variant-b-pytest-functions/  # pytest functions + fixtures
└── variant-c-record-replay/     # record once, replay forever
```

## Run everything

```powershell
cd prototypes/harness-contract
python -m pytest -q              # 21 tests, ~0.7s wall time
```

Each variant also has its own README with a self-contained run guide.

## Variant comparison table

> All three drive the *same* stub engine and cover the *same* 6 mandated
> demos. The differences below are about authoring shape and ceilings,
> not about engine coverage.

| axis                                      | A — YAML scenarios            | B — pytest functions          | C — record + replay              |
| ----------------------------------------- | ----------------------------- | ----------------------------- | -------------------------------- |
| **authoring cost / new happy-path**       | low — ~15 lines of YAML       | medium — ~10 lines of Python  | **lowest — operator runs it once** |
| **authoring cost / new chaos scenario**   | medium — extends YAML schema  | **low — `ChaosHook(...)` is just a callable** | high — must hand-edit recording or fall back to B-style test |
| **assertion expressiveness**              | bounded by YAML schema        | **unbounded (Python)**         | bounded by recording fingerprint match |
| **debuggability of a failed scenario**    | poor — AssertionError in driver | **best — `--pdb` lands in the scenario** | poor — points at YAML line numbers |
| **refactor resilience** (workflow change → scenarios break) | many scenarios break loudly w/ readable diffs | scenarios break loudly; mass-rewrite needed | all recordings stale; one-shot re-record fixes |
| **CI integration**                        | pytest wrapper (1 file)       | **native pytest, all features**    | native pytest                    |
| **parallelism**                           | per-scenario via pytest-xdist | per-scenario via pytest-xdist | per-recording via pytest-xdist   |
| **learning curve**                        | **lowest** (polyphony migrators recognise it) | medium (pytest fluency required) | low to record, medium to debug |
| **covers all 7 invariants?**              | yes (4a+4b for INV-RESTART)   | **yes (all in one file)**     | **partial** — INV-RESTART needs Python scaffold around recording |
| **risk of "looks-correct but-wrong" scenarios** | low — YAML reviewable    | low — code reviewable          | **high** — large recordings unreviewable by eye |
| **bytes per scenario (typical)**          | 400-800 B                     | 200-500 B                     | 1-2.5 KB                         |
| **schema-validatable?**                   | **yes** (pydantic over YAML)  | no (code is the spec)          | yes (recording schema)           |

## Recommendation

**Adopt Variant B as the primary contract; ship a thin Variant A
veneer for migrants and operator-authored happy paths.**

Rationale:

1. **Variant B's debuggability is the deciding factor.** Requiem's
   value prop is *trustable operator-driven SDLC* — when a scenario
   fails on Daniel's machine he needs to land inside the failure
   with full Python. Today's polyphony harness loses ~30 min/incident
   to AssertionError-in-driver friction.

2. **B is the only variant where INV-RESTART is a one-file scenario.**
   `kill_after('NodeCompleted', node='load')` is a 3-line fixture
   call; A needs a two-file 4a/4b pair; C needs hand-written Python
   scaffold around the recording. Given INV-RESTART is *the* load-
   bearing invariant, the contract that makes it cheapest wins.

3. **B's chaos vocabulary is unbounded by construction** — Brahms-2's
   Patches A+B+C+D (FaultMode, delay_ms, no_cli_calls_after, two-phase
   resume — see error-handling-deep-dive R11) collapse into "the
   scenario passes whatever callable it wants." A would need YAML
   schema extensions for each; C can't express them at all.

4. **Variant A still earns its keep** as a *thin* migration layer
   (~150 LOC driver) so polyphony harness authors aren't blocked.
   Limit it to scenarios that fit the YAML schema; anything fancier
   gets a Variant B test.

5. **Variant C is recommended for `close-out` and other one-LLM-call
   workflows only.** Recordings of complex workflows are unreviewable;
   the failure mode (an unreviewable green CI) is exactly the
   "best-efforts past a state-drift signal" trap INV-NO-CORRUPT-
   FORWARD exists to prevent. Use C for narrow happy-path drift
   detection on simple workflows, not as a general-purpose contract.

The combined posture: **B is the contract; A is a courtesy; C is a
specialised tool.**

## What changes vs the polyphony harness?

### Better

- **Single-process engine ⇒ in-process assertions.** No Pester
  wrapper, no JSON-stdout-then-parse round-trip, no shim binary on
  PATH. `python -m pytest -q` runs the whole suite in under a second.
- **Event-log assertions are first-class** (per INV-EVENT-LOG-
  AUTHORITATIVE). All three variants assert against an in-memory
  reader rather than re-parsing a written JSONL.
- **Chaos primitives are real.** `ChaosHook` can kill the engine,
  inject delays, force outcomes — none of which polyphony supports
  today (see error-handling-deep-dive §1 table-cell (g)).
- **INV-RESTART is testable.** Polyphony's harness has no mechanism
  to kill-then-resume; Brahms-2 Patch D (two-phase) was withdrawn
  from polyphony as too invasive. In Requiem it's a 3-line fixture.
- **Discriminated outcomes** mean assertions key off `outcome.kind`,
  not exit codes or `error` fields.
- **Sub-workflow scripting is real** (polyphony harness has it
  explicitly listed as "future" in `tests/harness/README.md`).

### Worse

- **No real workflow YAML.** The stub engine uses Python-defined
  workflows; polyphony harness loads real `.conductor/registry/
  workflows/*.yaml`. Requiem's workflow DSL is undecided (seam #7);
  whichever variant of that seam wins, the harness must wire to it.
  Decision item, not a blocker.
- **Variant A's schema is narrower than polyphony's.** Polyphony has
  `expected_trace.scripts_executed`, `gates_resolved`, etc.; Variant
  A starts with a leaner schema and extends as scenarios demand.
- **Lock-in to Python everywhere.** Polyphony's .NET shim let
  scenarios assert exit-code behaviour of the real CLI; in Requiem
  the verbs are Python functions, so any "the CLI handles bad input"
  test must live outside this harness.

### New

- **`ChaosHook` seam** — programmable failure injection at every
  event boundary. No analogue in polyphony harness.
- **Resume scenarios** — `04a` + `04b` (Variant A), `kill_after`
  fixture (B), goal-trace test (C). Polyphony has no kill-then-
  resume scenario today.
- **Recording mode (Variant C)** — operator runs once, CI replays.
  Polyphony has no recording infrastructure.
- **Sub-workflow provider scoping** — `subworkflow_provider_for`
  callback. Polyphony harness has no story for this yet (its README
  flags it as future work).
- **Multi-`run_id` event log filtering** — required so a sub-workflow
  doesn't think it should resume from the parent's last completed
  node. New invariant: events are always scoped by `run_id`.

## Open questions for Daniel

1. **Should `_engine/` shapes leak into the real engine?** This stub
   is intentionally simple. The real engine will have richer types
   (e.g., `Outcome.payload` typed per verb). Recommendation: keep
   the *seam shape* (5-variant discriminated union, JSONL event log
   subscribable in-process) and re-derive the internal types from
   the verb-outcome ADR.
2. **Variant A's `chaos:` block — extend YAML schema or punt to
   Variant B?** Today A supports only `kill_after_event`. Adding
   "fail every Nth call" via YAML is a slippery slope toward
   re-inventing pytest. Recommendation: cap A's chaos vocabulary
   at `kill_after_event` + `inject_outcome`; anything richer is a
   B scenario.
3. **Variant C — should recordings carry an `agent_scope` per call?**
   Today the replayer routes by agent name only. Real workflows
   will collide (parent's `architect` ≠ child's `architect`).
   Recommendation: yes — make `agent_scope: parent` / `child::child`
   part of the recording format from day one.
4. **Where does the workflow DSL live?** This seam stubs workflows
   in Python (`_engine/examples.py`). Seam #7 decides the real DSL.
   The harness scenario contract must be DSL-agnostic — confirm with
   Daniel that the variants here read as DSL-agnostic before locking
   #9.
5. **Should the harness assert against `domain signals` too?** North-
   star §3.2 introduces domain signals as a separate stream. Today
   only execution events are first-class in this seam. Recommendation:
   add a `notifications.find(...)` API to the engine before locking
   the assertion vocabulary — cheaper to bake in than retrofit.
6. **Does Variant B's fixture surface need to be locked as the
   harness API?** Once authors depend on `make_engine`, `kill_after`,
   `gate_answer`, they're load-bearing. The ADR should pin them and
   any addition should go through an ADR.

---

*Brahms (harness hat) — Phase A, Seam #9 — 2026-05-31*
