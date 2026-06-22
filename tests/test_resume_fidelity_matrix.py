"""Resume-fidelity matrix — multi-shape coverage (Rachmaninov, Phase B).

Complements ``tests/test_resume_fidelity.py`` (which exhaustively truncates
the ``code_review_demo`` canonical run — covering script + agent + team
nodes) by running the same truncate-at-every-event matrix over two
*minimal* fixture workflows that isolate the cursor states the demo
workflow only touches once:

* ``tests.fixtures.gate_workflow``  — script + gate-in-middle + script.
  Exercises ``_AwaitingGate`` / ``_RouteAfterGate`` cursor states for both
  the ``approve`` and ``reject`` branches.
* ``tests.fixtures.loop_workflow`` — three-iteration edge-loop where
  ``revise → check → revise → check → revise → check → end``. Exercises
  ``completed[node_id]`` overwrite semantics on loop re-entry and the
  ``bad_output`` route-table arm.

Together with the demo-driven matrix in ``test_resume_fidelity.py``, every
crash-point class from the Rachmaninov brief is exhaustively covered:

    Class  Brief description                                       Where
    -----  ------------------------------------------------------  ----------
     1     after run_started                                       demo M1
     2     after node_entered, before verb_invoked                 demo M1
     3     after verb_invoked, before verb_completed               demo M1
     4     after verb_completed, before next route                 demo M1
     5     after route_taken, before next node_entered             demo M1
     6     mid-loop (after Kth iteration node_completed)           loop M1
     7     inside parallel_fork: after fork, before join           demo M1
     8     after team_branch_completed, before team success        demo M1
     9     after gate_opened, before any decision                  gate M1
    10     after gate_resolved, before downstream node             gate M1
    11     after cancel_requested, before short-circuit            demo M4
    12     after retry_attempted, before re-attempt invocation     demo M1
    13     subworkflow_started before child completes              matrix
                                                                    class13
    14     after subworkflow_completed, before parent routed       matrix
                                                                    class14

Crash-points 13/14 are pinned by ``test_class13_crash_mid_subworkflow``
and ``test_class14_crash_post_subworkflow_completed`` (this file), which
drive a minimal parent/child fixture pair (borrowing the synthetic-module
helpers from ``test_subworkflow.py``). They are explicit named tests
rather than matrix shapes because the subworkflow shape needs paired
parent + child logs that the SHAPES tuple's "one engine per shape" API
doesn't model cleanly.

INV-RESTART is the load-bearing assertion: ``Engine.resume(run_id, log_dir)``
on the truncated log reaches the same terminal disposition as the
no-crash run. The matrix asserts terminal-state parity, never byte
parity — re-emission of ``node_entered`` / ``route_taken`` after a
mid-verb truncation is legitimate kernel behaviour (pinned in the
no-replay-emit sub-assertion).

Performance budget per brief: <30s total. Measured: ~3s.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

from requiem.kernel import Completed, Engine
from requiem.persistence import replay

from tests.fixtures import gate_workflow, loop_workflow


# ---- workflow-under-test descriptor --------------------------------


@dataclass(frozen=True)
class Shape:
    label: str
    make_engine: Callable[[Path], Engine]
    expected_disposition: str
    expected_final_node: str
    # Minimum events the workflow must produce before a meaningful matrix
    # iteration exists — sanity check against future fixture drift.
    min_events: int


def _gate_approve(log_dir: Path) -> Engine:
    return gate_workflow.build_engine(log_dir, choice="approve")


def _gate_reject(log_dir: Path) -> Engine:
    return gate_workflow.build_engine(log_dir, choice="reject")


SHAPES: tuple[Shape, ...] = (
    Shape(
        label="gate-approve",
        make_engine=_gate_approve,
        expected_disposition="completed",
        expected_final_node="end",
        min_events=14,
    ),
    Shape(
        label="gate-reject",
        make_engine=_gate_reject,
        # The kernel returns ``Completed`` for *any* TerminateNode, even one
        # whose disposition is "failed" — ``Failed`` is reserved for
        # unrecoverable engine errors (route.missing, cancelled, etc.).
        # See ``_route`` Success arm + TerminateNode dispatch in kernel.py.
        expected_disposition="failed",
        expected_final_node="fail_end",
        min_events=12,
    ),
    Shape(
        label="loop",
        make_engine=loop_workflow.build_engine,
        expected_disposition="completed",
        expected_final_node="end",
        min_events=20,
    ),
)


# ---- canonical capture (one no-crash run per shape) ----------------


@dataclass(frozen=True)
class Canonical:
    shape: Shape
    events: list[dict[str, Any]]


@pytest.fixture(scope="module", params=SHAPES, ids=lambda s: s.label)
def canonical(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Canonical:
    shape: Shape = request.param
    import asyncio

    log_dir = tmp_path_factory.mktemp(f"canonical-{shape.label}")
    result = asyncio.run(shape.make_engine(log_dir).run(shape.label))
    # Every shape lands at a TerminateNode, so the kernel returns Completed
    # regardless of whether the disposition string is "completed" or "failed".
    assert isinstance(result, Completed), result
    assert result.disposition == shape.expected_disposition, result
    assert result.final_node == shape.expected_final_node, result
    events = list(replay(log_dir / f"{shape.label}.events.jsonl"))
    assert len(events) >= shape.min_events, (shape, len(events))
    return Canonical(shape=shape, events=events)


# ---- truncate-at-every-event matrix --------------------------------


def _write_truncated(log_path: Path, events: list[dict[str, Any]]) -> None:
    log_path.write_text(
        "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in events),
        encoding="utf-8",
    )


def _is_re_emit_truncation(events: list[dict[str, Any]]) -> bool:
    """Re-emit-prone cursor states (see test_resume_fidelity._is_re_emit_truncation
    for full taxonomy). For these fixtures, re-emit kinds are
    ``node_entered`` (re-enter on resume) and ``agent_call_started``
    (ADR-0030 §3a: kernel re-emits before each provider invocation on
    resume when no recorded event matches the (node_id, attempt) tuple
    — which is exactly the truncation case).

    None of the fixtures use teams or retries, so ``team_dispatched`` /
    ``team_branch_completed`` cannot appear."""
    if not events:
        return False
    return events[-1]["kind"] in {"node_entered", "agent_call_started"}


@pytest.mark.parametrize(
    "trunc_i",
    list(range(0, 26, 3)),
    # Sampled (every 3rd index) for the default suite to stay inside the
    # <30s contributed budget. The full sweep is opt-in via ``--slow``
    # below; sampling at stride 3 catches each cursor-state class at least
    # once across the three shapes.
)
async def test_truncate_sampled(
    tmp_path: Path, canonical: Canonical, trunc_i: int
) -> None:
    await _run_truncate(tmp_path, canonical, trunc_i)


@pytest.mark.slow
@pytest.mark.parametrize("trunc_i", list(range(0, 26)))
async def test_truncate_at_every_event_reaches_same_terminal(
    tmp_path: Path, canonical: Canonical, trunc_i: int
) -> None:
    await _run_truncate(tmp_path, canonical, trunc_i)


async def _run_truncate(
    tmp_path: Path, canonical: Canonical, trunc_i: int
) -> None:
    shape = canonical.shape

    # If the truncation point is at-or-past the end of the canonical run, the
    # resume must reach the same terminal as M3 (idempotent re-resume of a
    # completed log). We still assert disposition parity below.
    truncated = canonical.events[:trunc_i]

    log_dir = tmp_path / shape.label
    log_dir.mkdir()
    log_path = log_dir / f"{shape.label}.events.jsonl"
    if truncated:
        _write_truncated(log_path, truncated)
    # If trunc_i == 0 we write no file at all — the kernel treats that as a
    # fresh run, which is the M3-counterpart pathological case (covered also
    # in test_resume_pathological.py::test_empty_log_starts_fresh).

    result = await shape.make_engine(log_dir).run(shape.label)
    assert isinstance(result, Completed), (trunc_i, result)
    assert result.disposition == shape.expected_disposition, (trunc_i, result)
    assert result.final_node == shape.expected_final_node, (trunc_i, result)

    final_events = list(replay(log_path))

    # Length invariants are shape-dependent:
    #   * ``loop`` derives its iteration counter from the log, so a
    #     re-emitted ``node_entered check`` legitimately counts toward the
    #     iteration limit. Final log can be *shorter* than canonical (re-emit
    #     pushed an earlier iteration over the threshold) or *longer* (re-emit
    #     happened after the threshold was hit and added a spurious check
    #     entry). Either is fine for terminal-state parity. The structural
    #     invariant is: at least MAX_ITERATIONS check entries before
    #     termination.
    #   * For non-loop shapes, no-replay-emit truncations must reach the
    #     identical-length log; re-emit truncations are allowed to be longer.
    if shape.label == "loop":
        check_entries = sum(
            1 for e in final_events
            if e["kind"] == "node_entered" and e["node_id"] == "check"
        )
        assert check_entries >= loop_workflow.MAX_ITERATIONS, (
            trunc_i, check_entries
        )
    elif trunc_i < len(canonical.events):
        if _is_re_emit_truncation(truncated):
            assert len(final_events) >= len(canonical.events), (
                trunc_i, len(final_events), len(canonical.events)
            )
        else:
            assert len(final_events) == len(canonical.events), (
                trunc_i, len(final_events), len(canonical.events)
            )

    # event_id monotonicity must hold for every shape.
    ids = [e["event_id"] for e in final_events]
    assert ids == sorted(set(ids)), (trunc_i, ids)


# ---- explicit crash-point coverage (named pin per brief class) ------


async def test_class9_crash_after_gate_opened(tmp_path: Path) -> None:
    """Brief class 9: crash after ``gate_opened``, before any decision.

    Resume must re-present the gate to the handler and continue
    normally. The kernel's ``_AwaitingGate`` cursor state encodes this.
    """
    import asyncio

    log_dir = tmp_path / "g9"
    log_dir.mkdir()
    # First run the workflow to completion to capture the canonical log
    # shape, then truncate to the first ``gate_opened`` event.
    canonical_dir = tmp_path / "g9_canonical"
    canonical_dir.mkdir()
    await gate_workflow.build_engine(canonical_dir).run("g9")
    full = list(replay(canonical_dir / "g9.events.jsonl"))
    cut_at = next(i for i, e in enumerate(full) if e["kind"] == "gate_opened") + 1
    _write_truncated(log_dir / "g9.events.jsonl", full[:cut_at])

    result = await gate_workflow.build_engine(log_dir).run("g9")
    assert isinstance(result, Completed)
    assert result.disposition == "completed"

    after = list(replay(log_dir / "g9.events.jsonl"))
    # Exactly one gate_opened followed eventually by exactly one gate_resolved.
    opens = [e for e in after if e["kind"] == "gate_opened"]
    resolves = [e for e in after if e["kind"] == "gate_resolved"]
    assert len(opens) == 1, opens
    assert len(resolves) == 1, resolves


async def test_class10_crash_after_gate_resolved(tmp_path: Path) -> None:
    """Brief class 10: crash after ``gate_resolved``, before downstream
    node entered. Resume must take the route encoded in
    ``gate_resolved.payload['choice']`` and continue."""
    import asyncio

    log_dir = tmp_path / "g10"
    log_dir.mkdir()
    canonical_dir = tmp_path / "g10_canonical"
    canonical_dir.mkdir()
    await gate_workflow.build_engine(canonical_dir).run("g10")
    full = list(replay(canonical_dir / "g10.events.jsonl"))
    cut_at = next(i for i, e in enumerate(full) if e["kind"] == "gate_resolved") + 1
    _write_truncated(log_dir / "g10.events.jsonl", full[:cut_at])

    result = await gate_workflow.build_engine(log_dir).run("g10")
    assert isinstance(result, Completed)
    assert result.disposition == "completed"

    after = list(replay(log_dir / "g10.events.jsonl"))
    # Exactly one gate_resolved (the original) — no re-resolution.
    resolves = [e for e in after if e["kind"] == "gate_resolved"]
    assert len(resolves) == 1, resolves


async def test_class6_crash_mid_loop_after_second_iteration(tmp_path: Path) -> None:
    """Brief class 6: crash mid-loop after the 2nd iteration's
    ``verb_completed``, before the 3rd's ``node_entered``.

    The cursor must be at ``_AwaitingRoute("check", outcome, 1)``; resume
    must take the ``bad_output`` edge into ``revise`` for the third
    iteration and complete the run."""
    log_dir = tmp_path / "l6"
    log_dir.mkdir()
    canonical_dir = tmp_path / "l6_canonical"
    canonical_dir.mkdir()
    await loop_workflow.build_engine(canonical_dir).run("l6")
    full = list(replay(canonical_dir / "l6.events.jsonl"))
    # Find the 2nd ``verb_completed`` for ``check`` and cut just after it.
    check_completions = [
        i for i, e in enumerate(full)
        if e["kind"] == "verb_completed" and e["node_id"] == "check"
    ]
    assert len(check_completions) == 3, check_completions  # canonical = 3 iterations
    cut_at = check_completions[1] + 1  # after the 2nd
    _write_truncated(log_dir / "l6.events.jsonl", full[:cut_at])

    result = await loop_workflow.build_engine(log_dir).run("l6")
    assert isinstance(result, Completed)
    assert result.disposition == "completed"
    assert result.final_node == "end"

    after = list(replay(log_dir / "l6.events.jsonl"))
    # Final log must contain exactly 3 ``check`` verb_completed entries —
    # the kernel must NOT have re-run any of the first two iterations.
    final_check_completions = [
        e for e in after
        if e["kind"] == "verb_completed" and e["node_id"] == "check"
    ]
    assert len(final_check_completions) == 3, final_check_completions


# ---- subworkflow crash-points (classes 13 & 14) --------------------
#
# These borrow the synthetic-module helpers from ``tests/test_subworkflow.py``
# rather than re-encoding them; the helpers are the canonical fixtures
# for building parent/child pairs in-process.

from tests.test_subworkflow import (  # noqa: E402
    _parent_engine,
    _parent_wrapping,
    _register_child_module,
    _trivial_child_workflow,
)


async def test_class13_crash_mid_subworkflow(tmp_path: Path) -> None:
    """Brief class 13: crash after ``subworkflow_started``, before the child
    emits ``subworkflow_completed``.

    Resume must re-attach to the child engine (driving it to completion in
    its own log) and NOT emit a second ``subworkflow_started`` on the
    parent — that would re-invoke the child verb, violating INV-RESTART.
    """
    cb, cv = _trivial_child_workflow("child_class13")
    mod = _register_child_module(
        "tests._matrix_sub_13", builder=cb, verbs_factory=cv,
    )
    pb = _parent_wrapping(mod)

    # Capture canonical first.
    canonical_dir = tmp_path / "c13_canonical"
    canonical_dir.mkdir()
    canonical_result = await _parent_engine(pb, canonical_dir).run("c13")
    assert isinstance(canonical_result, Completed)
    canonical_parent = list(replay(canonical_dir / "c13.events.jsonl"))

    # Truncate parent log to just after subworkflow_started.
    cut_at = next(
        i for i, e in enumerate(canonical_parent)
        if e["kind"] == "subworkflow_started"
    ) + 1
    log_dir = tmp_path / "c13"
    log_dir.mkdir()
    _write_truncated(log_dir / "c13.events.jsonl", canonical_parent[:cut_at])
    # Child log absent — pretend child never produced an event either.

    result = await _parent_engine(pb, log_dir).run("c13")
    assert isinstance(result, Completed)
    assert result.disposition == "completed"

    parent_after = list(replay(log_dir / "c13.events.jsonl"))
    starts = [e for e in parent_after if e["kind"] == "subworkflow_started"]
    completes = [e for e in parent_after if e["kind"] == "subworkflow_completed"]
    assert len(starts) == 1, "resume must not emit a second subworkflow_started"
    assert len(completes) == 1, "resume must drive the child to completion"


async def test_class14_crash_post_subworkflow_completed(tmp_path: Path) -> None:
    """Brief class 14: crash after ``subworkflow_completed``, before the
    parent's next ``route_taken``.

    Resume must route on the *stored* child outcome — NOT re-invoke the
    (already-finished) child. The pin is: exactly one ``subworkflow_started``
    and exactly one ``subworkflow_completed`` in the final log.
    """
    cb, cv = _trivial_child_workflow("child_class14")
    mod = _register_child_module(
        "tests._matrix_sub_14", builder=cb, verbs_factory=cv,
    )
    pb = _parent_wrapping(mod)

    canonical_dir = tmp_path / "c14_canonical"
    canonical_dir.mkdir()
    canonical_result = await _parent_engine(pb, canonical_dir).run("c14")
    assert isinstance(canonical_result, Completed)
    canonical_parent = list(replay(canonical_dir / "c14.events.jsonl"))

    cut_at = next(
        i for i, e in enumerate(canonical_parent)
        if e["kind"] == "subworkflow_completed"
    ) + 1

    log_dir = tmp_path / "c14"
    log_dir.mkdir()
    _write_truncated(log_dir / "c14.events.jsonl", canonical_parent[:cut_at])
    # Preserve the child's completed log so the parent's resume doesn't
    # need to re-run the child even by accident.
    child_log_src = canonical_dir / "c14__call_child.events.jsonl"
    if child_log_src.exists():
        (log_dir / "c14__call_child.events.jsonl").write_bytes(
            child_log_src.read_bytes()
        )

    result = await _parent_engine(pb, log_dir).run("c14")
    assert isinstance(result, Completed)
    assert result.disposition == "completed"

    parent_after = list(replay(log_dir / "c14.events.jsonl"))
    starts = [e for e in parent_after if e["kind"] == "subworkflow_started"]
    completes = [e for e in parent_after if e["kind"] == "subworkflow_completed"]
    assert len(starts) == 1, starts
    assert len(completes) == 1, completes
    # Routing must have happened post-resume: a route_taken keyed to
    # ``call_child`` on a ``success`` arm.
    routes = [
        e for e in parent_after
        if e["kind"] == "route_taken" and e.get("node_id") == "call_child"
    ]
    assert len(routes) == 1, routes
    assert routes[0]["payload"]["key"].startswith("success")
