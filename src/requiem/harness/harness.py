"""`Harness` — multi-run object with truncate-log + resume helpers.

Where `run_scenario(scn)` is the one-shot ergonomic entry, `Harness` is
the long-lived object you reach for when you want to:

* Run the same scenario multiple times against a shared `log_dir`.
* Truncate a log mid-run to test INV-RESTART (the kernel resumes from
  the truncated tail; this is the cheap-resume contract the north star
  promises).
* Cancel a run mid-flight via `cancel_after_event` without rebuilding
  the scenario.

Fixture defaults in :mod:`requiem.harness.pytest_plugin` register a
`harness` fixture bound to pytest's `tmp_path`, so most tests never
construct a `Harness` directly.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from requiem.harness.scenario import (
    Scenario,
    ScenarioResult,
    run_scenario,
)


@dataclass
class Harness:
    """Multi-run harness bound to a single `log_dir`.

    Reuse one `Harness` across a test to keep `run_id`s and event-log
    paths consistent; the truncate/resume contract relies on the same
    log path being read twice.
    """

    log_dir: Path
    _runs: dict[str, ScenarioResult] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.log_dir = Path(self.log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    # ---- run / resume ------------------------------------------------

    def run(
        self,
        scn: Scenario,
        *,
        run_id: str | None = None,
        cancel_after_event: int | None = None,
    ) -> ScenarioResult:
        """Run `scn` and cache the result by run_id for later resume."""
        rid = run_id or scn.run_id
        result = run_scenario(
            scn,
            log_dir=self.log_dir,
            run_id=rid,
            cancel_after_event=cancel_after_event,
        )
        self._runs[rid] = result
        return result

    def resume(
        self,
        scn: Scenario,
        log_path: Path | None = None,
        *,
        run_id: str | None = None,
    ) -> ScenarioResult:
        """Resume `scn` from an existing (possibly truncated) log.

        The kernel reads the tail of ``log_path`` via `_reconstruct`,
        derives a `_Cursor`, and continues. The harness simply re-runs
        the scenario with the same `run_id`; the engine's `replay`
        does the resume.
        """
        rid = run_id or scn.run_id
        if log_path is not None:
            # If the caller passed a log path that isn't ours, copy /
            # symlink semantics get hairy; instead require the path is
            # already located at ``log_dir / f"{run_id}.events.jsonl"``.
            expected = self.log_dir / f"{rid}.events.jsonl"
            if log_path.resolve() != expected.resolve():
                raise ValueError(
                    f"resume log_path {log_path!r} must equal {expected!r}; "
                    f"truncate the canonical log in place rather than passing "
                    f"a different path"
                )
        return self.run(scn, run_id=rid)

    # ---- log surgery -------------------------------------------------

    def truncate_log(self, run_id: str, *, after_event: int) -> Path:
        """Truncate the run log to keep events with ``event_id <= after_event``.

        Returns the (same) log path. Raises `ValueError` if `run_id`'s
        log is missing or if `after_event` is out of range.

        ``after_event`` is the LAST event_id to retain (inclusive). To
        keep the first 10 events (event_ids 0..9) pass ``after_event=9``.
        """
        if after_event < 0:
            raise ValueError(f"after_event must be >= 0, got {after_event!r}")
        log_path = self.log_dir / f"{run_id}.events.jsonl"
        if not log_path.exists():
            raise ValueError(
                f"no log for run_id={run_id!r} at {log_path!r}; "
                f"existing runs: {sorted(self._runs)!r}"
            )
        lines = log_path.read_text(encoding="utf-8").splitlines()
        # Each line is one event; parse to find max event_id and validate.
        decoded: list[tuple[int, str]] = []
        max_id = -1
        for raw in lines:
            if not raw.strip():
                continue
            try:
                env = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"corrupt event in {log_path}: {e}"
                ) from e
            eid = int(env.get("event_id", -1))
            decoded.append((eid, raw))
            max_id = max(max_id, eid)
        if after_event > max_id:
            raise ValueError(
                f"after_event={after_event} exceeds max event_id={max_id} "
                f"in {log_path}"
            )
        kept = [raw for eid, raw in decoded if eid <= after_event]
        log_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        return log_path

    # ---- bookkeeping -------------------------------------------------

    def get(self, run_id: str) -> ScenarioResult:
        return self._runs[run_id]

    def log_path(self, run_id: str) -> Path:
        return self.log_dir / f"{run_id}.events.jsonl"

    def __iter__(self):  # iterate over cached results
        return iter(self._runs.values())
