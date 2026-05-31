# Variant C — record + replay (hybrid)

## Shape

Two-step workflow:

1. **Record** (`record_demos.py` or any Python script): run the
   workflow once with a real provider + real gate handler.
   `RecordingProvider` and `RecordingGateHandler` wrap them and
   capture every agent reply / gate decision. `EventLog.subscribe`
   captures every event. Everything writes to a single
   `recordings/<name>.yaml`.

2. **Replay** (`replayer.py` + `test_variant_c.py`): in CI, read the
   YAML, build a `FakeProvider` and a scripted gate handler from the
   recording, and run the engine. The replay asserts both the
   recorded terminal AND that the (type, node) event fingerprint
   matches the recording — a drift signal.

## Run

```powershell
cd prototypes/harness-contract/variant-c-record-replay
python record_demos.py                  # produce/refresh recordings/*.yaml
python replayer.py                      # replay all
python replayer.py recordings/01_tiny_happy_path.yaml
python -m pytest -q                     # 7 tests, <1s
```

## Strengths

- **Lowest authoring cost per scenario** — for happy-path coverage.
  An operator runs the workflow once; CI permanently asserts that
  trace. Zero scenario-specific code or YAML written by hand.
- **Strong drift signal.** Every PR that changes a workflow surfaces
  the diff as a YAML changeset — easy to code-review.
- **Excellent for refactor-safety.** Renaming a node makes every
  recording stale at once; the operator re-records, the YAML diff
  is the audit trail.

## Weaknesses

- **Failure-injection is awkward.** Recording assumes the operator
  actually saw the failure. Scenarios like "verb returns
  RetryableFailure twice" can't be reliably re-recorded from prod —
  they require either:
    (a) re-running the recorder with a deliberately broken provider
        (which is just Variant B in disguise), or
    (b) a separate test that drives chaos around a "goal trace"
        recording (see `test_inv_restart_with_recorded_goal_trace`).
- **INV-RESTART can't be captured as a single recording.** The kill
  point is intrinsic to the test scaffold, not the recording. The
  recording is the "goal trace" the post-restart run must converge
  on. This is a foundational limitation, not a polishing issue.
- **Recorded-but-not-understood scenarios.** A 200-event YAML for a
  realistic workflow is unreviewable by eye. Failures point at YAML
  line numbers that nobody can interpret without re-running the
  recorder locally. Easy to land "look correct" recordings that
  encode buggy behaviour.
- **Sub-workflow scoping is fragile.** Today the replayer routes
  every agent name to a single `FakeProvider` because parent/child
  agent names happen not to collide. Real Requiem will have
  workflows where a sub-workflow's `architect` is not the parent's
  `architect`. Recording will need an explicit `agent_scope` field
  per call.

## Coverage matrix (the 6 mandated demos)

| # | recording                                | invariants exercised             |
| - | ---------------------------------------- | -------------------------------- |
| 1 | `01_tiny_happy_path.yaml`                | INV-EVENT-LOG-AUTHORITATIVE      |
| 2 | `02_transient_failure_retry.yaml`        | INV-DISCRIMINATED-OUTCOMES       |
| 3 | `03_specific_event_emitted.yaml`         | INV-EVENT-LOG-AUTHORITATIVE      |
| 4 | `04_inv_restart_goal_trace.yaml` + `test_inv_restart_with_recorded_goal_trace` | INV-RESTART (test-driven chaos around recorded goal trace) |
| 5 | `05_human_gate_branches.yaml`            | INV-NO-ENGINE-ABANDONMENT        |
| 6 | `06_subworkflow.yaml`                    | sub-workflow agent scoping       |
