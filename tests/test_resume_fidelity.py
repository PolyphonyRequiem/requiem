"""Resume-fidelity matrix for INV-RESTART.

Phase B / Rachmaninov: exhaustive crash-test matrix that pins the engine's
restart-from-log behaviour against any future regression that would silently
violate "restart from the event log reaches identical terminal state."

The matrix runs entirely against the existing ``code_review_demo`` workflow.
No new workflows, no external dependencies. Determinism comes from the
scripted ``FakeProvider`` and the data-driven kernel.

Five matrices:

* **M1 — Truncate-at-every-event.** For every event_id in the canonical run,
  cut the log to that length, resume in a fresh process-shape, and assert
  the run reaches the same terminal state and never re-invokes an agent
  whose ``verb_completed`` is already durable in the truncated log.
* **M2 — Kill-at-every-tick.** Same shape, but instead of post-hoc truncation
  the engine is killed mid-tick: the ``on_event`` observer raises
  ``asyncio.CancelledError`` after the Kth event is durably written. This
  exercises the *real* path a SIGTERM would take (the event is appended
  before the abort can propagate, exactly because ``EventStore.append``
  flushes inside the lock before observers fire).
* **M3 — Resume-from-completed.** Resume an already-completed run; must be
  idempotent (no new events, ``Completed`` returned, sub-50ms).
* **M4 — Cancel mid-flight.** Kill mid-run, inject a ``cancel_requested``
  event into the log (the exact shape ``requiem cancel`` writes), then
  resume; must terminate at ``Cancelled`` per
  INV-CANCEL-SHORT-CIRCUITS-RETRY, and re-resume must be terminal-stable.
* **M5 — Concurrent resumes (defensive).** Two engines on the same run_id
  in one process via ``asyncio.gather``. Pins current behaviour so any
  future change is intentional.

Performance budget per the brief: <1s per matrix iteration, <30s full file.
Measured on the author's box: ~3s, well inside budget.

Determinism note: every assertion is on the engine's monotonic ``event_id``
or on derived counts. No wall-clock thresholds beyond the M3 sub-50ms
sanity check (which uses ``time.perf_counter`` against a generous 250ms
ceiling so CI clock jitter is irrelevant).
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from requiem.kernel import Completed, Engine, Failed, _reconstruct
from requiem.persistence import CorruptLogError, EventStore, replay
from requiem.events import EventEmitter
from requiem.workflows.code_review_demo import build_engine


# ---- canonical-run fixture ------------------------------------------


@dataclass(frozen=True)
class CanonicalRun:
    """A reference run captured once per session.

    Subsequent tests truncate ``events`` and replay, then compare the
    *terminal* state against ``terminal`` — not the full event sequence,
    because re-emission after mid-verb truncation is expected (see M1 below).
    """
    events: list[dict[str, Any]]
    terminal: "TerminalState"


@dataclass(frozen=True)
class TerminalState:
    """The cross-resume invariant: what must be identical regardless of where
    the original run was cut.

    Pulled entirely from the engine result + the event log — never from
    side-effect artefacts like the summary file, because those were written
    by the original process and are not guaranteed to exist in a fresh
    resume sandbox.

    Specifically excluded from the invariant: ``total_events`` (re-emission
    of node_entered / team_dispatched / team_branch_completed after mid-verb
    truncation legitimately lengthens the log) and the ``nodes_entered``
    list (same reason). Both are pinned by the *no-replay-emit* sub-class
    of M1 separately.
    """
    disposition: str
    final_node: str
    terminal: str  # projection["terminal"] — should mirror disposition
    recommend_merge: bool
    top_finding: str
    severity_counts: tuple[tuple[str, int], ...]


def _silent_approve(_node_id: str, _prompt: str, _opts: tuple[str, ...]) -> str:
    return "approve"


def _engine(log_dir: Path) -> Engine:
    return build_engine(log_dir, gate_handler=_silent_approve)


def _terminal_of(result: Completed, events: list[dict[str, Any]]) -> TerminalState:
    """Derive the cross-resume terminal state from the result + event log.

    Pulls the verdict out of the synthesizer's ``verb_completed`` payload
    (which is durable in the log) rather than reading the archive verb's
    side-effect markdown file (which is *not* re-written on a resume that
    skips the archive node)."""
    synth_outcome = next(
        e["payload"]["outcome"] for e in events
        if e["kind"] == "verb_completed" and e["node_id"] == "synthesize"
    )
    parsed = synth_outcome["value"]["parsed"]
    sev_seen = parsed.get("severity_seen") or []
    counts: dict[str, int] = {}
    for s in sev_seen:
        counts[s] = counts.get(s, 0) + 1
    return TerminalState(
        disposition=result.disposition,
        final_node=result.final_node,
        terminal=result.projection["terminal"],
        recommend_merge=bool(parsed["recommend_merge"]),
        top_finding=str(parsed["top_finding"]),
        severity_counts=tuple(sorted(counts.items())),
    )


@pytest.fixture(scope="module")
def canonical(tmp_path_factory: pytest.TempPathFactory) -> CanonicalRun:
    """Run the demo once with run_id=``canonical`` and capture everything."""
    log_dir = tmp_path_factory.mktemp("canonical")
    engine = _engine(log_dir)
    result = asyncio.run(engine.run("canonical"))
    assert isinstance(result, Completed), result
    log_path = log_dir / "canonical.events.jsonl"
    events = list(replay(log_path))
    return CanonicalRun(events=events, terminal=_terminal_of(result, events))


# ---- helpers --------------------------------------------------------


def _is_re_emit_truncation(events: list[dict[str, Any]]) -> bool:
    """True iff resuming from this truncation will re-emit events.

    Cursor states that re-emit:
      * ``_AtNode`` ← last event was ``node_entered`` / ``retry_attempted``
        (retry re-emits node_entered — but the *original* run also emitted
        node_entered immediately after retry_attempted, so the resumed log
        is byte-equivalent to the original here; not a re-emit).
      * Mid-team: ``team_dispatched`` / ``team_branch_completed`` leave the
        cursor at the team node, so the whole team re-runs.

    ``node_entered`` truncation alone IS a re-emit, because the resumed
    loop will emit a second ``node_entered`` for the same node before
    running the verb.

    ``run_started`` truncation does NOT re-emit: the engine guards the
    ``emit_run_started`` call with ``if not replayed``, and the cursor
    defaults to ``_AtNode(entry, 1)`` exactly as a fresh run would.
    """
    if not events:
        return False
    last = events[-1]["kind"]
    return last in {"node_entered", "team_dispatched", "team_branch_completed"}


def _expected_agent_calls(events: list[dict[str, Any]]) -> list[str]:
    """Agent calls a fresh resume from this truncated log will record.

    Agent-invoking nodes in ``code_review_demo``: ``review_team`` (3 reviewers)
    and ``synthesize`` (1 synthesizer). If a node's ``verb_completed`` is in
    the truncated log, the engine restores it from the log; if not, the
    fresh provider re-invokes the agent(s).
    """
    completed_nodes = {
        e["node_id"] for e in events if e["kind"] == "verb_completed"
    }
    calls: list[str] = []
    if "review_team" not in completed_nodes:
        calls += ["style_reviewer", "correctness_reviewer", "performance_reviewer"]
    if "synthesize" not in completed_nodes:
        calls += ["synthesizer"]
    return calls


def _write_truncated(log_path: Path, events: list[dict[str, Any]]) -> None:
    log_path.write_text(
        "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in events),
        encoding="utf-8",
    )


def _make_resume_log_dir(
    tmp_path: Path, run_id: str, events: list[dict[str, Any]],
    *, snippet_text: str,
) -> Path:
    """Prepare a log_dir for a resume: write the truncated log AND the sample
    snippet that ``read_snippet`` will look for (``build_engine`` writes it
    on first construction, but we want the resume to re-read what the
    canonical run saw, not a fresh blob)."""
    d = tmp_path / "resume"
    d.mkdir(parents=True, exist_ok=True)
    (d / "sample_snippet.py").write_text(snippet_text, encoding="utf-8")
    _write_truncated(d / f"{run_id}.events.jsonl", events)
    return d


# ---- M1: truncate-at-every-event -----------------------------------


# Full sweep is opt-in via --slow (33 iterations × ~600ms each ≈ 20s).
# A sampled subset stays in the default suite so a regression surfaces
# without a slow-flag opt-in.
_M1_ALL = list(range(1, 34))
_M1_SAMPLED = _M1_ALL[::5]  # every 5th truncation point — 7 iterations


@pytest.mark.slow
@pytest.mark.parametrize("trunc_i", _M1_ALL)
async def test_m1_truncate_every_event(
    tmp_path: Path, canonical: CanonicalRun, trunc_i: int
) -> None:
    await _run_m1(tmp_path, canonical, trunc_i)


@pytest.mark.parametrize("trunc_i", _M1_SAMPLED)
async def test_m1_truncate_sampled(
    tmp_path: Path, canonical: CanonicalRun, trunc_i: int
) -> None:
    await _run_m1(tmp_path, canonical, trunc_i)


async def _run_m1(
    tmp_path: Path, canonical: CanonicalRun, trunc_i: int
) -> None:
    """Cut the log to ``trunc_i`` events; resume; assert terminal-state parity.

    ``trunc_i=N`` keeps events[0:N] and discards the rest.
    ``trunc_i=len(events)`` is M3 territory and is exercised there instead.
    """
    truncated = canonical.events[:trunc_i]
    snippet_text = _snippet_text()
    d = _make_resume_log_dir(
        tmp_path, "canonical", truncated, snippet_text=snippet_text
    )

    engine = _engine(d)
    result = await engine.run("canonical")
    assert isinstance(result, Completed), (trunc_i, result)

    final_events = list(replay(d / "canonical.events.jsonl"))
    terminal = _terminal_of(result, final_events)
    assert terminal == canonical.terminal, (trunc_i, terminal)

    # Provider re-invocation: only uncommitted agent verbs should re-run.
    expected = _expected_agent_calls(truncated)
    actual = [c["agent"] for c in engine.provider.calls]
    assert actual == expected, (trunc_i, actual, expected)

    # No-replay-emit truncations must produce a byte-equivalent log length.
    if not _is_re_emit_truncation(truncated):
        assert len(final_events) == len(canonical.events), (
            trunc_i, len(final_events)
        )
    else:
        # Re-emit truncations may legitimately produce a longer log; the
        # only hard invariant is "at least as long as the original".
        assert len(final_events) >= len(canonical.events), (
            trunc_i, len(final_events)
        )

    # event_id monotonicity must always hold.
    ids = [e["event_id"] for e in final_events]
    assert ids == sorted(set(ids)), (trunc_i, ids)


def _snippet_text() -> str:
    from requiem.workflows.code_review_demo import SAMPLE_SNIPPET
    return SAMPLE_SNIPPET


# ---- M1 corruption sub-case ----------------------------------------


async def test_m1_truncate_mid_line_refuses_to_resume(
    tmp_path: Path, canonical: CanonicalRun
) -> None:
    """Truncate inside an event line (mid-JSON). The engine must refuse to
    resume (raise ``CorruptLogError``), not silently guess a recovery."""
    d = tmp_path / "midline"
    d.mkdir()
    (d / "sample_snippet.py").write_text(_snippet_text(), encoding="utf-8")
    log_path = d / "canonical.events.jsonl"
    # Take the first 5 events as bytes, then add half of the next event's
    # line (no trailing newline). That last "line" will be invalid JSON.
    intact = "".join(
        json.dumps(e, separators=(",", ":")) + "\n"
        for e in canonical.events[:5]
    )
    next_line = json.dumps(canonical.events[5], separators=(",", ":"))
    log_path.write_text(intact + next_line[: len(next_line) // 2], encoding="utf-8")

    engine = _engine(d)
    with pytest.raises(CorruptLogError):
        await engine.run("canonical")


# ---- M2: kill-at-every-tick ----------------------------------------


class _KillAfter:
    """Observer that raises ``asyncio.CancelledError`` after the Kth event.

    The observer fires *after* ``EventStore.append`` flushes, so by the
    time the cancel propagates the event is already durable. This mimics
    SIGTERM after the syscall returns.
    """

    def __init__(self, k: int) -> None:
        self.k = k
        self.seen = 0

    def __call__(self, _envelope: dict[str, Any]) -> None:
        self.seen += 1
        if self.seen >= self.k:
            raise asyncio.CancelledError(f"kill after event {self.k}")


_M2_ALL = list(range(1, 35))
_M2_SAMPLED = _M2_ALL[::5]


@pytest.mark.slow
@pytest.mark.parametrize("kill_at", _M2_ALL)
async def test_m2_kill_at_every_tick(
    tmp_path: Path, canonical: CanonicalRun, kill_at: int
) -> None:
    await _run_m2(tmp_path, canonical, kill_at)


@pytest.mark.parametrize("kill_at", _M2_SAMPLED)
async def test_m2_kill_sampled(
    tmp_path: Path, canonical: CanonicalRun, kill_at: int
) -> None:
    await _run_m2(tmp_path, canonical, kill_at)


async def _run_m2(
    tmp_path: Path, canonical: CanonicalRun, kill_at: int
) -> None:
    """Kill the engine mid-tick after ``kill_at`` events; resume; assert
    terminal-state parity.

    The kernel does not currently swallow observer exceptions, so the
    CancelledError propagates out of ``Engine.run`` — exactly the shape
    a real process kill takes from the engine's perspective. We catch it
    here and construct a fresh engine to drive the resume.
    """
    d = tmp_path / "k2"
    d.mkdir()
    (d / "sample_snippet.py").write_text(_snippet_text(), encoding="utf-8")

    first = _engine(d)
    first.on_event = _KillAfter(kill_at)
    try:
        await first.run("k2run")
    except asyncio.CancelledError:
        pass  # expected — the kill hook fired

    log_path = d / "k2run.events.jsonl"
    pre_resume_events = list(replay(log_path))
    assert pre_resume_events, kill_at

    second = _engine(d)
    result = await second.run("k2run")
    assert isinstance(result, Completed), (kill_at, result)
    final_events = list(replay(d / "k2run.events.jsonl"))
    terminal = _terminal_of(result, final_events)
    assert terminal == canonical.terminal, (kill_at, terminal)

    # Provider re-invocation budget on the *second* engine alone.
    expected = _expected_agent_calls(pre_resume_events)
    actual = [c["agent"] for c in second.provider.calls]
    assert actual == expected, (kill_at, actual, expected)


# ---- M3: resume-from-completed -------------------------------------


async def test_m3_resume_from_completed_is_idempotent(tmp_path: Path) -> None:
    """Resume a Completed run; no new events, returns Completed quickly,
    projection unchanged."""
    d = tmp_path / "m3"
    d.mkdir()
    first = _engine(d)
    first_result = await first.run("m3run")
    assert isinstance(first_result, Completed)
    log_path = d / "m3run.events.jsonl"
    before = list(replay(log_path))

    second = _engine(d)
    t0 = time.perf_counter()
    second_result = await second.run("m3run")
    elapsed = time.perf_counter() - t0
    after = list(replay(log_path))

    assert isinstance(second_result, Completed)
    assert second_result.disposition == first_result.disposition
    assert second_result.final_node == first_result.final_node
    assert second_result.projection == first_result.projection
    assert after == before, "resume of completed run must append zero events"
    # Generous ceiling — well above any plausible CI jitter.
    assert elapsed < 0.25, f"idempotent resume took {elapsed*1000:.1f}ms"
    # And no agent calls — the engine should never reach _execute.
    assert second.provider.calls == []


# ---- M4: cancel mid-flight (cancel_requested injected) -------------


def _inject_cancel(log_path: Path, run_id: str, *, reason: str) -> None:
    """Write a ``cancel_requested`` event the way ``requiem cancel`` does:
    construct an ``EventStore`` against the same path (which scans the log
    for the next ``event_id``) and emit through an ``EventEmitter``."""
    store = EventStore(log_path)
    EventEmitter(run_id, store.append).emit_cancel_requested(
        reason=reason, requested_by="cli"
    )


@pytest.mark.parametrize("kill_at", [3, 8, 14, 20, 26])  # spread across the run
async def test_m4_cancel_mid_flight_short_circuits(
    tmp_path: Path, kill_at: int
) -> None:
    """Kill mid-flight, inject ``cancel_requested``, resume — must terminate
    at Cancelled per INV-CANCEL-SHORT-CIRCUITS-RETRY. Verify no further
    retries or verb executions occur after the cancel signal."""
    d = tmp_path / "m4"
    d.mkdir()
    first = _engine(d)
    first.on_event = _KillAfter(kill_at)
    try:
        await first.run("m4run")
    except asyncio.CancelledError:
        pass

    log_path = d / "m4run.events.jsonl"
    _inject_cancel(log_path, "m4run", reason="deadline-exceeded")

    second = _engine(d)
    result = await second.run("m4run")
    assert isinstance(result, Failed), (kill_at, result)
    assert result.error_kind == "cancelled"
    assert "deadline-exceeded" in result.message

    events_after = list(replay(log_path))
    # No retry_attempted may follow the cancel_requested marker.
    cancel_idx = next(
        i for i, e in enumerate(events_after) if e["kind"] == "cancel_requested"
    )
    post = events_after[cancel_idx + 1:]
    assert not [e for e in post if e["kind"] == "retry_attempted"], post
    # No new node_entered after the cancel marker either — the engine must
    # not start any new verb after observing the cancel.
    assert not [e for e in post if e["kind"] == "node_entered"], post
    # Exactly one run_completed("cancelled") must close out the log.
    completers = [e for e in post if e["kind"] == "run_completed"]
    assert len(completers) == 1, post
    assert completers[0]["payload"]["terminal"] == "cancelled"

    # Re-resume after cancel must be terminal-stable in *disposition*.
    #
    # ANOMALY (documented in docs/resume-fidelity-report.md, candidate
    # invariant INV-CANCEL-RESUME-IDEMPOTENT): each resume of a cancelled
    # run currently *re-emits* `run_completed("cancelled")`, because
    # `_pending_cancel()` scans for any `cancel_requested` in the log
    # without regard for whether it has already been honoured. So the log
    # grows by one `run_completed` per resume. Disposition stays "cancelled"
    # (the invariant the operator cares about), but the log is not byte-
    # idempotent. We pin disposition here and flag the log growth as a
    # bug-shaped regression hook.
    third = _engine(d)
    third_result = await third.run("m4run")
    third_disposition = (
        third_result.disposition if isinstance(third_result, Completed)
        else "cancelled"  # Failed always carries error_kind == cancelled here
    )
    if isinstance(third_result, Failed):
        assert third_result.error_kind == "cancelled"
    assert third_disposition == "cancelled"

    events_after_third = list(replay(log_path))
    # Pin the current (buggy) shape: exactly one extra run_completed.
    extra = len(events_after_third) - len(events_after)
    assert extra == 1, (
        "expected the documented +1 run_completed quirk on re-resume; "
        f"got {extra} extra events"
    )
    new_events = events_after_third[len(events_after):]
    assert all(e["kind"] == "run_completed" for e in new_events), new_events


async def test_m4_cancel_before_any_run_terminates_at_cancelled(
    tmp_path: Path,
) -> None:
    """Edge case: cancel arrives before the first run even starts.

    Mimics ``requiem cancel`` racing the first ``requiem resume``. The log
    has no events at the moment of cancel injection, so we must seed it
    first with a ``run_started`` (otherwise the engine treats the log as
    fresh and ignores the cancel)."""
    d = tmp_path / "m4pre"
    d.mkdir()
    # Seed run_started (the cheapest way: run with a kill-after-1 hook).
    engine0 = _engine(d)
    engine0.on_event = _KillAfter(1)
    try:
        await engine0.run("pre")
    except asyncio.CancelledError:
        pass
    log_path = d / "pre.events.jsonl"
    assert len(list(replay(log_path))) == 1

    _inject_cancel(log_path, "pre", reason="operator-aborted")

    engine1 = _engine(d)
    result = await engine1.run("pre")
    assert isinstance(result, Failed)
    assert result.error_kind == "cancelled"


# ---- M5: concurrent resumes (defensive) ----------------------------


async def test_m5_concurrent_resumes_pin_current_behaviour(
    tmp_path: Path, canonical: CanonicalRun
) -> None:
    """Race two engines on the same run_id. Document the current behaviour
    so any future change (e.g. adding a file lock) is intentional.

    Currently the engine has no inter-instance lock; both engines:
      * scan the log at construction and arrive at the same ``next_id``
      * race on writes, producing duplicate ``event_id`` values
      * both reach a terminal state (often Completed, sometimes Failed if
        a router decision sees a duplicated outcome dict)

    The pinned assertions below are deliberately weak: they would catch a
    silent regression where one engine crashes or hangs, but not the
    duplicate-ID condition itself (which is documented, not fixed, at v0).
    """
    d = tmp_path / "m5"
    d.mkdir()
    # Pre-populate the log to ~halfway so both engines genuinely race the
    # tail rather than both running the whole workflow from scratch.
    half = canonical.events[: len(canonical.events) // 2]
    _write_truncated(d / "m5run.events.jsonl", half)
    (d / "sample_snippet.py").write_text(_snippet_text(), encoding="utf-8")

    e1, e2 = _engine(d), _engine(d)
    r1, r2 = await asyncio.gather(
        e1.run("m5run"), e2.run("m5run"), return_exceptions=True
    )

    # Pin: both return a result (no hangs, no uncaught crashes other than
    # the documented races). If one raised, it must be an exception type
    # the engine is already known to surface (CorruptLogError from a
    # half-written line, or a verb-side KeyError if `completed` lacks an
    # entry the other engine hadn't reached yet).
    for r in (r1, r2):
        if isinstance(r, BaseException):
            assert isinstance(r, (CorruptLogError, KeyError, RuntimeError)), r
        else:
            assert isinstance(r, (Completed, Failed)), r

    # At least one engine must have reached a terminal state successfully —
    # the other is allowed to crash on the race.
    successes = [r for r in (r1, r2) if isinstance(r, Completed)]
    assert successes, (r1, r2)


# ---- M6: bonus — _reconstruct exhaustiveness ------------------------


def test_reconstruct_handles_every_event_kind_in_canonical_run(
    canonical: CanonicalRun,
) -> None:
    """Pin: every event kind emitted by the canonical run is handled by
    ``_reconstruct``. If a future event kind is added and ``_reconstruct``
    grows a new arm (or correctly ignores it), the fold must not raise.

    Conversely, if a future change removes an arm, this test catches it.
    """
    from requiem.events import EVENT_KINDS
    kinds_in_run = {e["kind"] for e in canonical.events}
    assert kinds_in_run <= EVENT_KINDS, kinds_in_run - EVENT_KINDS
    # Folding every prefix must not raise — this is the property the matrix
    # implicitly relies on. Cheap O(N^2) sanity check on 34 events.
    for i in range(len(canonical.events) + 1):
        _reconstruct(canonical.events[:i], entry="start")
