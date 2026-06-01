"""Pathological event-log shapes — Rachmaninov, Phase B.

Distinct from the truncate-at-every-event matrix (`test_resume_fidelity.py`,
`test_resume_fidelity_matrix.py`), these tests probe what the kernel does
when the log is **deformed in shapes that the engine itself cannot have
produced** — bytes from a different process, hand-edited tampering,
filesystem races, or external corruption.

The umbrella invariant being defended is **INV-NO-CORRUPT-FORWARD**: when
the kernel cannot verify its prerequisites it refuses to act, never
"best-efforts" past an unverified precondition.

Each test pins the kernel's *current* behaviour and ties to one of:

* **INV-PARTIAL-LINE-DROP** *(proposed in north-star "Invariant candidates
  from Rachmaninov")* — a partial trailing JSON line: the kernel raises
  ``CorruptLogError`` (strict-stop behaviour; the brief's "drop and
  continue" framing is documented but not adopted, see north-star).
* **INV-FIRST-EVENT-WINS** *(proposed)* — duplicate envelopes with the
  same ``event_id`` are not flagged today; the fold processes both and
  picks the latter. Pinned as documented behaviour with a TODO marker.
* **INV-MONOTONIC-EVENT-ID** *(proposed)* — out-of-order ``event_id``
  values are not detected today; the fold processes the log positionally
  and ignores ``event_id``. Pinned with a TODO marker.
* **INV-RUN-ID-FILTER** *(proposed)* — runs each live in their own
  ``{run_id}.events.jsonl`` file, so cross-contamination by ``run_id`` is
  impossible at the path layer. Pinned by demonstrating that two runs in
  the same directory write to separate files.
* **INV-EMPTY-LOG-IS-FRESH** *(implicit)* — an empty file (touched but
  no events) is treated as a fresh run.
* **INV-MISSING-LOG-IS-FRESH** *(implicit)* — a missing log file for a
  given ``run_id`` is treated as a fresh run; the kernel does NOT raise
  "no log for run X" as the brief speculated. Pinned as the actual
  behaviour (and a candidate invariant flip if a future ``requiem
  resume`` verb wants stricter semantics).
* **INV-PARTIAL-RUN-RESUMES-FROM-FIRST-NODE** *(implicit)* — a log
  containing only ``run_started`` (no ``node_entered``) resumes at the
  entry node, identical to a fresh run.

Out of scope (documented limitations of this test file):
  * No real disk-corruption simulation (truncation/tampering is the proxy).
  * No cross-process resume (the engine is single-process at v0; see
    INV-SINGLE-PROCESS).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from requiem.kernel import Completed, Engine
from requiem.persistence import CorruptLogError, replay

from tests.fixtures import gate_workflow


# ---- helpers --------------------------------------------------------


def _write_events(log_path: Path, events: list[dict[str, Any]]) -> None:
    log_path.write_text(
        "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in events),
        encoding="utf-8",
    )


async def _canonical_gate_events(tmp_path: Path) -> list[dict[str, Any]]:
    """Capture a clean canonical log from the gate-in-middle workflow."""
    d = tmp_path / "canonical"
    d.mkdir()
    result = await gate_workflow.build_engine(d).run("canonical")
    assert isinstance(result, Completed)
    return list(replay(d / "canonical.events.jsonl"))


# ---- 1. Truncated mid-line ------------------------------------------


async def test_truncated_mid_line_refuses_to_resume(tmp_path: Path) -> None:
    """Partial JSON line at end of log — the kernel raises
    ``CorruptLogError`` rather than silently dropping the partial line.

    This pins the *strict* interpretation of INV-NO-CORRUPT-FORWARD: an
    unparseable byte sequence cannot be silently elided because the kernel
    has no way to know whether the dropped bytes encode a state-changing
    event (``verb_completed``) that the resume must honour.

    The Rachmaninov brief speculated INV-PARTIAL-LINE-DROP would be the
    desired invariant. After examining the trade-off in light of
    INV-NO-CORRUPT-FORWARD, strict-stop wins: silent recovery from a
    partial line could re-execute a state-mutating verb whose outcome was
    already partially durable, violating INV-RESTART idempotency. See
    docs/north-star.md "Invariant candidates from Rachmaninov" for the
    rationale.
    """
    canonical = await _canonical_gate_events(tmp_path)
    d = tmp_path / "midline"
    d.mkdir()
    log_path = d / "run.events.jsonl"

    # First 4 events intact + half of the 5th (no trailing newline).
    intact = "".join(
        json.dumps(e, separators=(",", ":")) + "\n" for e in canonical[:4]
    )
    half = json.dumps(canonical[4], separators=(",", ":"))[: len(json.dumps(canonical[4])) // 2]
    log_path.write_text(intact + half, encoding="utf-8")

    with pytest.raises(CorruptLogError):
        await gate_workflow.build_engine(d).run("run")


# ---- 2. Duplicate events --------------------------------------------


async def test_duplicate_event_ids_documented_behaviour(tmp_path: Path) -> None:
    """Two envelopes with the same ``event_id`` — the kernel currently does
    NOT raise. ``_reconstruct`` is a positional fold; it processes both
    events in order, and (because they have the same ``node_id`` and shape)
    the second simply overwrites the first's state.

    Pinned as documented behaviour. Strict ``INV-FIRST-EVENT-WINS`` (or
    equivalently ``INV-NO-DUPLICATE-EVENT-IDS``) is proposed in
    docs/north-star.md as a candidate invariant; this test is the
    regression hook that will catch the flip when the kernel adds
    duplicate detection.
    """
    canonical = await _canonical_gate_events(tmp_path)
    d = tmp_path / "dup"
    d.mkdir()
    log_path = d / "run.events.jsonl"

    # Duplicate every ``verb_completed`` envelope verbatim. event_id stays
    # the same on the duplicate — the EventStore would never produce this,
    # but a malicious hand-edit could.
    fattened: list[dict[str, Any]] = []
    for ev in canonical:
        fattened.append(ev)
        if ev["kind"] == "verb_completed":
            fattened.append(dict(ev))
    _write_events(log_path, fattened)

    # The kernel processes the log; the second resume should reach a
    # terminal state without raising. We pin the *fact* of no-raise so
    # that a future stricter check (raise on duplicate event_id) is a
    # deliberate, visible change.
    result = await gate_workflow.build_engine(d).run("run")
    assert isinstance(result, Completed), result
    # Disposition parity holds because every duplicated verb_completed
    # carries the same outcome — the fold's "latter wins" overwrite is
    # idempotent for this fixture.
    assert result.disposition == "completed"


# ---- 3. Out-of-order events ------------------------------------------


async def test_out_of_order_event_ids_documented_behaviour(tmp_path: Path) -> None:
    """``event_id`` values out of monotonic order — the kernel currently
    folds the log positionally and ignores ``event_id`` ordering. Pinned
    as documented behaviour; strict ``INV-MONOTONIC-EVENT-ID`` is
    proposed in docs/north-star.md.

    Because the fold is positional, the kernel will *not* silently
    advance past an inconsistent sequence — it will follow the file order
    and produce whatever cursor that yields. The risk this test pins is
    therefore not "kernel silently advances" but "kernel cannot tell the
    log was reordered".
    """
    canonical = await _canonical_gate_events(tmp_path)
    d = tmp_path / "ooo"
    d.mkdir()
    log_path = d / "run.events.jsonl"

    # Reverse the event_id field on the first two events without
    # reordering them in the file. The kernel reads them in file order.
    tampered = [dict(e) for e in canonical]
    tampered[0]["event_id"], tampered[1]["event_id"] = (
        tampered[1]["event_id"], tampered[0]["event_id"],
    )
    _write_events(log_path, tampered)

    result = await gate_workflow.build_engine(d).run("run")
    # The kernel must NOT crash; it must reach the same terminal — because
    # `event_id` is not consulted by the fold today.
    assert isinstance(result, Completed), result
    assert result.disposition == "completed"


# ---- 4. Different run_id contamination ------------------------------


async def test_run_id_isolation_at_path_layer(tmp_path: Path) -> None:
    """INV-RUN-ID-FILTER (path-layer): two runs in the same log_dir write
    to *separate* ``{run_id}.events.jsonl`` files. Cross-contamination by
    ``run_id`` cannot happen at the path layer — the kernel never reads a
    log file belonging to a different run.

    This is a structural pin. A future change (e.g. shared-log mode) that
    breaks the one-file-per-run invariant must update this test.
    """
    d = tmp_path / "shared"
    d.mkdir()

    r1 = await gate_workflow.build_engine(d).run("run_one")
    r2 = await gate_workflow.build_engine(d).run("run_two")
    assert isinstance(r1, Completed) and isinstance(r2, Completed)

    log_one = d / "run_one.events.jsonl"
    log_two = d / "run_two.events.jsonl"
    assert log_one.exists() and log_two.exists(), list(d.iterdir())

    events_one = list(replay(log_one))
    events_two = list(replay(log_two))
    # No envelope in log_one carries run_id="run_two" or vice versa.
    assert {e["run_id"] for e in events_one} == {"run_one"}, events_one
    assert {e["run_id"] for e in events_two} == {"run_two"}, events_two


# ---- 5. Empty log file (touched but no events) ----------------------


async def test_empty_log_starts_fresh(tmp_path: Path) -> None:
    """An empty ``{run_id}.events.jsonl`` (zero bytes) — the kernel must
    treat the run as fresh: emit ``run_started`` and execute from the
    entry node. INV-EMPTY-LOG-IS-FRESH."""
    d = tmp_path / "empty"
    d.mkdir()
    (d / "run.events.jsonl").write_text("", encoding="utf-8")

    result = await gate_workflow.build_engine(d).run("run")
    assert isinstance(result, Completed)
    assert result.disposition == "completed"
    events = list(replay(d / "run.events.jsonl"))
    assert events[0]["kind"] == "run_started", events[0]


# ---- 6. Missing log file ---------------------------------------------


async def test_missing_log_starts_fresh(tmp_path: Path) -> None:
    """No log file at all for the given ``run_id``. The kernel currently
    treats this as a fresh run (creates the file, emits ``run_started``,
    executes the workflow).

    The Rachmaninov brief speculated the kernel should raise "no log for
    run X". The kernel does NOT do this — it cannot distinguish "fresh
    run named X" from "resume of X whose log was deleted" without an
    external signal (a verb on the CLI, an explicit ``Engine.resume`` vs
    ``Engine.run`` API split, etc.). Pinned as the actual behaviour. If a
    future ``Engine.resume(run_id)`` distinct from ``Engine.run(run_id)``
    is added, the resume variant should flip to strict-raise.
    """
    d = tmp_path / "missing"
    d.mkdir()
    assert not (d / "run.events.jsonl").exists()

    result = await gate_workflow.build_engine(d).run("run")
    assert isinstance(result, Completed)
    assert result.disposition == "completed"
    events = list(replay(d / "run.events.jsonl"))
    assert events[0]["kind"] == "run_started", events[0]


# ---- 7. Log with only run_started, no node_entered ------------------


async def test_run_started_only_resumes_from_entry_node(tmp_path: Path) -> None:
    """A log containing ``run_started`` but no ``node_entered`` — the
    cursor must default to ``_AtNode(workflow.entry, 1)`` and the resume
    must execute the entry node identically to a fresh run.

    INV-PARTIAL-RUN-RESUMES-FROM-FIRST-NODE: a partially-started log is
    indistinguishable from a fresh run from the entry node's perspective.
    """
    canonical = await _canonical_gate_events(tmp_path)
    only_started = [e for e in canonical if e["kind"] == "run_started"]
    assert len(only_started) == 1, only_started

    d = tmp_path / "started_only"
    d.mkdir()
    _write_events(d / "run.events.jsonl", only_started)

    result = await gate_workflow.build_engine(d).run("run")
    assert isinstance(result, Completed)
    assert result.disposition == "completed"

    events = list(replay(d / "run.events.jsonl"))
    # Exactly one ``run_started`` — the kernel must not re-emit it.
    starts = [e for e in events if e["kind"] == "run_started"]
    assert len(starts) == 1, starts
    # Entry node ``start`` must be entered after the original run_started.
    first_entered = next(
        i for i, e in enumerate(events) if e["kind"] == "node_entered"
    )
    assert events[first_entered]["node_id"] == "start", events[first_entered]
