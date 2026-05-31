# Variant A — YAML scenarios (polyphony-shaped, evolved)

## Shape

A scenario is a `scenarios/*.yaml` file declared against the
shared stub engine. The driver (`driver.py`, ~150 LOC) loads the YAML,
constructs a `WorkflowEngine` with a `FakeProvider` + gate handler +
chaos hook, runs it, and asserts a declarative `expect:` block.

Top-level keys:

| key                        | required | purpose                                 |
| -------------------------- | -------- | --------------------------------------- |
| `workflow`                 | yes      | name of an example workflow             |
| `run_id`                   | no       | string; shared run_id = shared log      |
| `agent_scripts`            | no       | `{agent: [reply, …]}` FIFO              |
| `subworkflow_agent_scripts`| no       | `{child_wf: {agent: [reply, …]}}`       |
| `gates`                    | no       | `{gate: option}` or `{gate: {value, additional_input}}` |
| `chaos`                    | no       | `{kill_after_event: {type, node}}`      |
| `resume`                   | no       | `true` ⇒ reuse the existing event log   |
| `expect`                   | yes      | declarative assertions                  |

`expect:` supports `terminal`, `crashed`, ordered-subsequence
`events`, exact `agents_invoked` counts, and per-node `retries`.

## Run

```powershell
cd prototypes/harness-contract/variant-a-yaml-scenarios
python driver.py                          # run all
python driver.py scenarios/01_tiny_happy_path.yaml   # run one
pytest test_variant_a.py                  # CI mode
```

## Coverage matrix (the 6 mandated demos)

| # | scenario                              | invariants exercised               |
| - | ------------------------------------- | ---------------------------------- |
| 1 | `01_tiny_happy_path`                  | INV-EVENT-LOG-AUTHORITATIVE        |
| 2 | `02_transient_failure_retry`          | INV-DISCRIMINATED-OUTCOMES, hard-3-retry cap |
| 3 | `03_specific_event_emitted`           | INV-EVENT-LOG-AUTHORITATIVE        |
| 4 | `04a_inv_restart_kill` + `04b_inv_restart_resume` | INV-RESTART, INV-EVENT-LOG-AUTHORITATIVE |
| 5 | `05_human_gate_branches`              | INV-NO-ENGINE-ABANDONMENT (gate routes) |
| 6 | `06_subworkflow`                      | sub-workflow agent-scripting story |

## Strengths

- **Cheap to author.** Six scenarios fit on one screen of YAML each.
  No Python to write per scenario.
- **Easy migration story** for polyphony harness authors — the
  vocabulary (`agent_scripts`, `cli_scripts`-shaped sections, `gates`,
  `expected_trace`) is recognisable.
- **Schema-validatable.** Every scenario can be checked against a
  pydantic model before any engine runs.
- **Refactor-resilient.** Renaming a workflow node breaks scenarios
  loudly (assertion fails with the bad node name); a CI grep can
  enumerate every scenario that touches a renamed node.

## Weaknesses

- **Assertion expressiveness ceiling.** Every new kind of assertion
  (regex on payload, "this happened within 100ms", "agent prompt
  mentioned X") forces a YAML schema extension and driver code.
  Each extension is a tax on the driver's complexity budget.
- **Chaos vocabulary is tiny.** Today: `kill_after_event`. Adding
  "fail every 3rd call to agent X" requires designing a new YAML
  schema fragment.
- **Sub-workflow scripting is verbose.** The child workflow's name
  is duplicated between the workflow definition and the
  `subworkflow_agent_scripts:` key — a refactoring trap.
- **Debuggability of failures is mediocre.** When an assertion fails,
  the operator sees an `AssertionError`; setting a breakpoint inside
  the failing scenario means stepping through the driver, not the
  scenario itself.
