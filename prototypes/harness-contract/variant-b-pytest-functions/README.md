# Variant B — pytest functions with fixtures

## Shape

Each scenario is a plain pytest function. Fixtures (`conftest.py`)
expose every engine seam:

| fixture            | gives the scenario                                |
| ------------------ | ------------------------------------------------- |
| `event_log`        | a fresh JSONL-backed `EventLog` in `tmp_path`     |
| `fake_provider`    | an empty `FakeProvider` to `.script(...)` on      |
| `make_engine`      | factory: `make_engine(workflow, gate_handler=..., chaos=..., subworkflow_provider_for=...)` |
| `kill_after`       | factory: `kill_after('NodeCompleted', node='load')` returns a `ChaosHook` |
| `gate_answer`      | factory: `gate_answer(gate={'value': 'abort', 'reason': 'x'})` returns a handler |

The full set of demos is in `test_scenarios.py` (~140 LOC for all 6
scenarios). Authoring cost per scenario is 5-20 lines.

## Run

```powershell
cd prototypes/harness-contract/variant-b-pytest-functions
python -m pytest -q                       # 7 tests (gate is parametrised), <1s
python -m pytest -k transient -v          # one scenario, verbose
python -m pytest --pdb                    # drop into pdb on failure
```

## Strengths

- **Full Python expressiveness.** Need a regex on payload? A floating-
  point tolerance? A snapshot diff? Just write the assertion.
- **Best debuggability.** `--pdb` lands in the scenario; breakpoints
  work; the call stack is the scenario, not a driver.
- **Pytest ecosystem in one move.** `-k`, `-x`, `--lf`, parametrize,
  xdist parallelism, hypothesis fuzzing, coverage — all free.
- **Refactor-resilient.** Renaming a workflow node forces a code edit
  the type-checker / test run catches.
- **Sub-workflow scripting is natural.** A child fake is just a second
  `FakeProvider` instance passed through a lambda — no nested keys.
- **Fault injection is open-ended.** A `ChaosHook` is a callable; the
  scenario can express "fail every 3rd architect call" trivially.

## Weaknesses

- **Highest barrier to entry** for non-Python authors. Polyphony-
  shaped operators will see a function-and-fixture wall.
- **Per-scenario fixture management** is easy to get wrong (e.g.,
  the restart scenario must construct a second `EventLog` against the
  same path — a foot-gun for first-time authors).
- **Refactor-resilience is a double-edged sword.** A change to engine
  fixture signatures breaks every scenario at once. Mitigated by
  keeping the fixture surface narrow.
- **Schema-validation of scenarios isn't possible** — scenarios are
  code, so you can only catch shape problems at execution time.

## Coverage matrix (the 6 mandated demos)

All six are in `test_scenarios.py`:

| # | test                                          | invariants exercised               |
| - | --------------------------------------------- | ---------------------------------- |
| 1 | `test_tiny_happy_path`                        | INV-EVENT-LOG-AUTHORITATIVE        |
| 2 | `test_transient_failure_retries_twice`        | INV-DISCRIMINATED-OUTCOMES, retry cap |
| 3 | `test_run_started_event_emitted`              | INV-EVENT-LOG-AUTHORITATIVE        |
| 4 | `test_inv_restart_resumes_after_kill`         | INV-RESTART, INV-EVENT-LOG-AUTHORITATIVE |
| 5 | `test_human_gate_branches` (×2, parametrize)  | INV-NO-ENGINE-ABANDONMENT          |
| 6 | `test_subworkflow_scripts_per_child`          | sub-workflow agent scoping         |
