"""pytest harness: runs every YAML in scenarios/ through the driver."""

from __future__ import annotations

from pathlib import Path

import pytest

from driver import run_scenario

SCENARIOS = sorted((Path(__file__).resolve().parent / "scenarios").glob("*.yaml"))


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda p: p.stem)
def test_scenario(scenario: Path, tmp_path: Path):
    # Restart scenarios must share their event log across runs.
    if scenario.stem.startswith("04"):
        log_path = tmp_path.parent / "restart-shared.events.jsonl"
        # 04a creates fresh log; 04b reuses what 04a wrote.
        if scenario.stem.endswith("kill") and log_path.exists():
            log_path.unlink()
    else:
        log_path = tmp_path / f"{scenario.stem}.events.jsonl"
    run_scenario(scenario, log_path=log_path)
