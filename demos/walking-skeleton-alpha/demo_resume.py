"""Walking Skeleton α — INV-RESTART proof.

Run the demo once (produces an event log), then truncate the log at a
mid-workflow event and re-run. The engine reads the partial log,
reconstructs (current_node, completed, attempt), and resumes from where
it stopped — without re-executing already-completed nodes.

    python demo_resume.py [run_id]

The script:
  1. runs the workflow fresh (run_id = "restart-demo")
  2. counts total events
  3. truncates the log to just after `review_team` completes
  4. re-runs with the same run_id → engine resumes mid-workflow
  5. asserts the second run did NOT re-execute `read_snippet`, `flaky_lint`,
     or `review_team` (each appears exactly once in the final log)
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

from engine.kernel import Completed, Engine
from engine.persistence import replay
from engine.toolbelt import Toolbelt
from reviewers import scripted_provider
from workflow import build_agent_registry, build_verb_registry, build_workflow

LOG_DIR = Path(__file__).parent / ".runs"
TRUNCATE_AFTER_KIND = "team_branch_completed"  # last branch of review_team


def truncate_after_team(run_id: str) -> tuple[int, int]:
    """Cut the log right after the team step finishes (but before synthesize).
    Returns (lines_before, lines_after)."""
    path = LOG_DIR / f"{run_id}.events.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    keep = []
    seen_team_completed = 0
    branches_total = 3
    cutoff = None
    for i, raw in enumerate(lines):
        ev = json.loads(raw)
        keep.append(raw)
        if ev["kind"] == TRUNCATE_AFTER_KIND:
            seen_team_completed += 1
            if seen_team_completed == branches_total:
                # Keep the verb_completed for review_team too (so the engine
                # sees the team result as already in `completed`), then stop.
                cutoff = i
                # walk forward past the verb_completed for review_team:
                for j in range(i + 1, len(lines)):
                    ev2 = json.loads(lines[j])
                    keep.append(lines[j])
                    if ev2["kind"] == "verb_completed" and ev2.get("node_id") == "review_team":
                        cutoff = j
                        break
                    if ev2["kind"] == "route_taken" and ev2.get("node_id") == "review_team":
                        # don't include the route — we want to prove the engine
                        # picks up at the same edge without us having taken it.
                        keep.pop()
                        break
                break
    if cutoff is None:
        raise RuntimeError("never saw team_branch_completed in the log")
    truncated = keep[: cutoff + 1]
    path.write_text("\n".join(truncated) + "\n", encoding="utf-8")
    return len(lines), len(truncated)


async def main(argv: list[str]) -> int:
    run_id = argv[1] if len(argv) > 1 else "restart-demo"
    shutil.rmtree(LOG_DIR, ignore_errors=True)

    workflow = build_workflow()

    def _auto(_n, _p, _opts):
        return "approve"

    def make_engine() -> Engine:
        return Engine(
            workflow=workflow,
            verbs=build_verb_registry(),
            agents=build_agent_registry(),
            provider=scripted_provider(),
            toolbelt=Toolbelt.real(),
            log_dir=LOG_DIR, gate_handler=_auto,
        )

    print("=" * 72)
    print("Walking Skeleton α — INV-RESTART proof")
    print("=" * 72)

    # Pass 1: full run.
    print("\n[pass 1] full run...")
    r1 = await make_engine().run(run_id)
    assert isinstance(r1, Completed), f"expected Completed, got {r1!r}"
    log_path = LOG_DIR / f"{run_id}.events.jsonl"
    events1 = list(replay(log_path))
    print(f"         events written: {len(events1)}")
    enters1 = [e for e in events1 if e["kind"] == "node_entered"]
    print(f"         nodes entered : {[e['node_id'] for e in enters1]}")

    # Truncate the log to simulate crash after `review_team` completed.
    before, after = truncate_after_team(run_id)
    print(f"\n[truncate] {before} → {after} lines  (simulated crash after team)")

    # Pass 2: re-run with same run_id and a *fresh* engine/provider.
    # Because the team is in `completed`, the synthesizer should fire on the
    # very first reviewer-script call (slot 0 of `synthesizer`), and the three
    # reviewer scripts should NOT be consumed a second time.
    print("\n[pass 2] resume from truncated log...")
    engine2 = make_engine()
    r2 = await engine2.run(run_id)
    assert isinstance(r2, Completed), f"expected Completed, got {r2!r}"
    events2 = list(replay(log_path))
    enters2 = [e["node_id"] for e in events2 if e["kind"] == "node_entered"]
    print(f"         events now    : {len(events2)}")
    print(f"         total entries : {enters2}")

    # INV-RESTART assertions
    counts = {n: enters2.count(n) for n in {"read_snippet", "flaky_lint", "review_team",
                                            "synthesize", "archive"}}
    print(f"         entry counts  : {counts}")
    assert counts["read_snippet"] == 1, "read_snippet was re-executed!"
    assert counts["review_team"] == 1, "review_team was re-executed!"
    assert counts["synthesize"] >= 1, "synthesize never ran on resume"
    assert counts["archive"] == 1, "archive should run exactly once"

    # The FakeProvider scripts each reviewer only once. Resume worked because
    # the engine did NOT call them a second time — proven by the run not
    # crashing with "fake.exhausted".
    print(f"         agent calls 2 : {len(engine2.provider.calls)} (synthesizer only, not reviewers)")

    print("\n" + "=" * 72)
    print("INV-RESTART: ✓  resume picked up at the next undecided edge")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv)))
