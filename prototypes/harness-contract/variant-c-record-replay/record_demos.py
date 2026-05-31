"""Record fresh YAML recordings for the 6 mandated demos.

Run this once whenever the example workflows change. The output goes
to `recordings/*.yaml` and is checked into git. CI runs `replayer.py`
against the committed recordings (no recorder needed in CI).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _engine import (  # noqa: E402
    ChaosHook,
    EventLog,
    FakeProvider,
    KillRequested,
    WorkflowEngine,
    gated_workflow,
    parent_with_subworkflow,
    tiny_three_node,
    transient_failure_workflow,
)
from recorder import RecordingGateHandler, RecordingProvider, record_run  # noqa: E402

OUT = Path(__file__).resolve().parent / "recordings"
OUT.mkdir(exist_ok=True)


def _tmp_log() -> Path:
    return Path(tempfile.mkdtemp()) / "events.jsonl"


def record_tiny_happy_path():
    provider = FakeProvider().script("architect", {"plan": "ok"})
    record_run(
        workflow_name="tiny_three_node",
        workflow=tiny_three_node(),
        provider=provider,
        run_id="tiny-happy",
        recording_path=OUT / "01_tiny_happy_path.yaml",
        log_path=_tmp_log(),
    )


def record_transient_failure():
    workflow, _attempts = transient_failure_workflow(fail_times=2)
    record_run(
        workflow_name="transient_failure_workflow",
        workflow=workflow,
        provider=FakeProvider(),
        run_id="flaky-retry",
        recording_path=OUT / "02_transient_failure_retry.yaml",
        log_path=_tmp_log(),
    )


def record_specific_event():
    provider = FakeProvider().script("architect", {"plan": "ok"})
    record_run(
        workflow_name="tiny_three_node",
        workflow=tiny_three_node(),
        provider=provider,
        inputs={"work_item_id": 7},
        run_id="event-assert-7",
        recording_path=OUT / "03_specific_event_emitted.yaml",
        log_path=_tmp_log(),
    )


def record_restart():
    """The recorder runs the workflow to completion with no chaos — the
    replay test exercises kill-and-resume separately in test_variant_c.
    INV-RESTART can't be captured by a recording per se; instead, the
    recorder produces an "expected good" trace that the resume engine
    must converge on. This is one of the harder edges of record/replay."""
    provider = FakeProvider().script("architect", {"plan": "after-resume"})
    record_run(
        workflow_name="tiny_three_node",
        workflow=tiny_three_node(),
        provider=provider,
        run_id="restart-demo",
        recording_path=OUT / "04_inv_restart_goal_trace.yaml",
        log_path=_tmp_log(),
    )


def record_gate_abort():
    provider = FakeProvider().script("architect", {"plan": "needs-review"})

    def real_gate(name, options):
        # The "operator" answered abort. In a real recording this would
        # be the operator clicking the UI; here we simulate.
        return "abort", {"reason": "operator-decision"}

    record_run(
        workflow_name="gated_workflow",
        workflow=gated_workflow(),
        provider=provider,
        gate_handler=real_gate,
        run_id="gate-abort",
        recording_path=OUT / "05_human_gate_branches.yaml",
        log_path=_tmp_log(),
    )


def record_subworkflow():
    parent = FakeProvider().script("architect", {"plan": "parent-plan"})
    child = FakeProvider().script("reviewer", {"verdict": "approve"})

    record_run(
        workflow_name="parent_with_subworkflow",
        workflow=parent_with_subworkflow(),
        provider=parent,
        subworkflow_provider_for=lambda name: child if name == "child" else parent,
        run_id="parent-with-child",
        recording_path=OUT / "06_subworkflow.yaml",
        log_path=_tmp_log(),
    )


if __name__ == "__main__":
    record_tiny_happy_path()
    record_transient_failure()
    record_specific_event()
    record_restart()
    record_gate_abort()
    record_subworkflow()
    for f in sorted(OUT.glob("*.yaml")):
        print(f"recorded: {f.name} ({f.stat().st_size} bytes)")
