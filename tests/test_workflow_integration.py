"""Cross-workflow integration tests — Wave 5 / Schoenberg.

These tests cover the seams between parent and child workflows that
single-workflow tests don't exercise. They exist because the v0 push
has already produced two escapes:

* **Tchaikovsky/Haydn (PR #36):** planning's ``TwigClientProto`` was
  tightened to require ``show_async``; root_dispatch's ``FakeTwigClient``
  (which is *handed across* to planning when root_dispatch spawns it as
  a sub-workflow) still only had sync ``show``. Each module's own tests
  passed; the failure surfaced only at the seam.
* **Verdi-3 (PR #37):** ``full_sdlc.py`` imported
  ``root_dispatch.DispatchInputs`` after Haydn renamed it to
  ``RootDispatchInputs``. Both workflows compiled in isolation; the
  shim invocation was the seam that broke.

The shared shape: **a parent workflow building a child engine via shim
(plus a fake passed across the seam) can silently rot when either side
evolves.** This file's job is to keep those seams honest.

Each test docstring cites the invariant (``INV-*``) or ADR it protects.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import inspect
import json
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pytest

from requiem.agent import FakeProvider
from requiem.clients.fs import FsNotFoundError
from requiem.clients.twig import TwigItem, TwigItemNotFoundError
from requiem.dsl import AgentRegistry, VerbRegistry, WorkflowBuilder
from requiem.events import EventEmitter
from requiem.kernel import Completed, Engine, Failed
from requiem.outcomes import PermanentFailure, Success
from requiem.persistence import EventStore, replay
from requiem.toolbelt import Toolbelt
from requiem.workflows import (
    close_out as _close_out,
    full_sdlc as _full_sdlc,
    implementation as _implementation,
    planning as _planning,
    pr_lifecycle as _pr_lifecycle,
    root_dispatch as _root_dispatch,
)


# ---- shared fixtures + helpers -------------------------------------


@pytest.fixture(autouse=True)
def _reset_full_sdlc_cells():
    """Each test gets a clean shim cell — no cross-test bleed."""
    _full_sdlc._CURRENT_INPUTS["inputs"] = None
    _full_sdlc._CROSS_STAGE["pr_number"] = None
    _full_sdlc._OBSERVER["obs"] = None
    yield
    _full_sdlc._CURRENT_INPUTS["inputs"] = None
    _full_sdlc._CROSS_STAGE["pr_number"] = None
    _full_sdlc._OBSERVER["obs"] = None


def _demo_full_sdlc_inputs(log_dir: Path, **overrides) -> _full_sdlc.FullSdlcInputs:
    """Self-contained full_sdlc inputs rooted under ``log_dir``."""
    base = dict(
        item_id=99999,
        repo="PolyphonyRequiem/requiem",
        repo_path=log_dir / "operator_repo",
        base_branch="main",
        dry_run=True,
        today=_dt.date(2026, 6, 1),
    )
    base.update(overrides)
    return _full_sdlc.FullSdlcInputs(**base)


def _seam_failures(parent_log: Path) -> list[dict[str, Any]]:
    """Return any seam-level PermanentFailures in ``parent_log``.

    These are the failure shapes the kernel's ``_run_subworkflow``
    synthesises when the parent → child handshake breaks
    (``subworkflow.import_failed``, ``subworkflow.no_build_engine``,
    ``subworkflow.build_failed``, ``subworkflow.run_crashed``,
    ``subworkflow.bad_factory``, ``subworkflow.inputs_crash``).
    """
    seam_kinds = {
        "subworkflow.import_failed",
        "subworkflow.no_build_engine",
        "subworkflow.build_failed",
        "subworkflow.run_crashed",
        "subworkflow.bad_factory",
        "subworkflow.inputs_crash",
    }
    out: list[dict[str, Any]] = []
    for ev in replay(parent_log):
        if ev.get("kind") != "subworkflow_completed":
            continue
        outcome = (ev.get("payload") or {}).get("outcome") or {}
        if outcome.get("kind") != "permanent_failure":
            continue
        if outcome.get("error_kind") in seam_kinds:
            out.append({"node_id": ev.get("node_id"), "outcome": outcome})
    return out


def _planning_provider_for_leaf() -> FakeProvider:
    """Planner + reviewer scripts that produce a single leaf plan."""
    return FakeProvider(
        scripts={
            "planner": [{
                "summary": "Atomic refactor; no children.",
                "decomposable": False,
                "children": [],
                "estimated_complexity": "small",
                "rationale": "Localised change.",
            }],
            "plan_reviewer": [{
                "verdict": "approve",
                "feedback": "Looks scoped correctly.",
            }],
        }
    )


def _proceed_handler(node_id: str, prompt: str,
                     options: tuple[str, ...]) -> str:
    return "proceed" if "proceed" in options else options[0]


_proceed_handler.__requiem_auto__ = True  # type: ignore[attr-defined]


# =====================================================================
# TestSubWorkflowProtocolStability
# =====================================================================


class TestSubWorkflowProtocolStability:
    """One test per (parent, child) shim pairing.

    Each test builds the parent engine with stock fakes, drives it
    through the shim layer, and asserts no seam-level PermanentFailure
    surfaces (no ``AttributeError`` / ``TypeError`` got swallowed into
    a ``subworkflow.run_crashed``). These are the exact failure shapes
    that would have caught PR #36 (Tchaikovsky/Haydn) and PR #37
    (Verdi-3).
    """

    async def test_full_sdlc_to_root_dispatch_shim(self, tmp_path: Path):
        """full_sdlc → root_dispatch: RootDispatchInputs field check.

        Regression: PR #37 / Verdi-3 — full_sdlc's dispatch shim
        referenced ``root_dispatch.DispatchInputs`` after Haydn renamed
        it to ``RootDispatchInputs``. The dispatch shim's
        ``build_engine`` is what would have raised ``AttributeError``;
        the kernel maps that to ``subworkflow.build_failed``.
        """
        inputs = _demo_full_sdlc_inputs(tmp_path)
        _full_sdlc.build_engine(tmp_path, inputs=inputs)  # seeds inputs cell

        shim = sys.modules[_full_sdlc.SHIM_DISPATCH]
        child = shim.build_engine(tmp_path)
        assert isinstance(child, Engine)
        result = await child.run("seam-dispatch")
        assert isinstance(result, Completed), (
            f"dispatch child failed to complete via shim: {result!r}"
        )

    async def test_full_sdlc_to_planning_shim(self, tmp_path: Path):
        """full_sdlc → planning: TwigClientProto + provider wiring.

        The planning shim hands the parent's twig/provider through to
        ``planning.build_engine``. INV-NO-CORRUPT-FORWARD via the
        seam: a protocol mismatch would surface as a verb crash
        inside the planning child.
        """
        inputs = _demo_full_sdlc_inputs(tmp_path)
        _full_sdlc.build_engine(tmp_path, inputs=inputs)

        shim = sys.modules[_full_sdlc.SHIM_PLANNING]
        child = shim.build_engine(tmp_path)
        assert isinstance(child, Engine)
        result = await child.run("seam-plan")
        assert isinstance(result, Completed), (
            f"planning child failed via shim: {result!r}"
        )

    async def test_full_sdlc_to_implementation_shim(self, tmp_path: Path):
        """full_sdlc → implementation: FilesystemClient/GhClient/TwigClient.

        Implementation needs all three clients on ``ctx.toolbelt``;
        the shim seeds a demo git repo under ``log_dir/demo_repo``.
        A protocol drift on any of those clients would crash the
        impl child's first verb (``fetch_plan`` / ``create_branch``).
        """
        inputs = _demo_full_sdlc_inputs(tmp_path)
        _full_sdlc.build_engine(tmp_path, inputs=inputs)

        shim = sys.modules[_full_sdlc.SHIM_IMPLEMENTATION]
        child = shim.build_engine(tmp_path)
        assert isinstance(child, Engine)
        result = await child.run("seam-impl")
        assert isinstance(result, Completed), (
            f"implementation child failed via shim: {result!r}"
        )

    async def test_full_sdlc_to_pr_lifecycle_shim(self, tmp_path: Path):
        """full_sdlc → pr_lifecycle: PrToolkit + GhClient.pr_view shape.

        The pr_lifecycle shim builds a child with the default
        ``FakePrToolkit``. A protocol drift on ``PrToolkit`` would
        surface here as a ``verb.crash`` inside ``fetch_pr`` /
        ``list_reviews``.
        """
        inputs = _demo_full_sdlc_inputs(tmp_path)
        _full_sdlc.build_engine(tmp_path, inputs=inputs)

        shim = sys.modules[_full_sdlc.SHIM_PR_LIFECYCLE]
        child = shim.build_engine(tmp_path)
        assert isinstance(child, Engine)
        result = await child.run("seam-pr")
        assert isinstance(result, Completed), (
            f"pr_lifecycle child failed via shim: {result!r}"
        )

    async def test_full_sdlc_to_close_out_shim(self, tmp_path: Path):
        """full_sdlc → close_out: TwigClient state transitions.

        close_out drives ``twig.set_state_async`` (the
        ``Closed`` transition). A protocol drift on the twig fake
        would crash ``close_item``. dry_run=True keeps the assertion
        focused on the seam, not on disk side-effects.
        """
        inputs = _demo_full_sdlc_inputs(tmp_path)
        _full_sdlc.build_engine(tmp_path, inputs=inputs)

        shim = sys.modules[_full_sdlc.SHIM_CLOSE_OUT]
        child = shim.build_engine(tmp_path)
        assert isinstance(child, Engine)
        result = await child.run("seam-close")
        assert isinstance(result, Completed), (
            f"close_out child failed via shim: {result!r}"
        )

    async def test_root_dispatch_to_planning_protocol_pr36(
        self, tmp_path: Path,
    ):
        """root_dispatch → planning: the PR #36 regression.

        Tchaikovsky added ``show_async`` to planning's
        ``TwigClientProto``. Haydn's ``root_dispatch.FakeTwigClient``
        only had sync ``show``. root_dispatch's planning shim hands
        that same fake to ``planning.build_engine``, so planning's
        ``fetch_item`` verb would call ``twig.show_async`` and raise
        ``AttributeError`` — which the kernel catches as a
        ``verb.crash`` inside the planning child.

        This test fails clearly if any future drift between
        root_dispatch's twig protocol and planning's twig protocol
        re-introduces that escape.
        """
        manifest_dir = tmp_path / ".runs"
        inputs = _root_dispatch.RootDispatchInputs(
            item_id=4242,
            repo="acme/widgets",
            repo_path=Path("."),
            base_branch="main",
            dry_run=False,
            auto_plan=True,
            manifest_dir=manifest_dir,
        )
        twig = _root_dispatch.FakeTwigClient(items={
            4242: TwigItem(
                id=4242,
                title="Refactor outcome dispatch in kernel",
                state="Active",
                area_path="PolyphonyRequiem\\v0",
                work_item_type="User Story",
                parent_id=None,
                raw={},
            ),
        })

        engine = _root_dispatch.build_engine(
            tmp_path,
            inputs=inputs,
            twig=twig,
            provider=_planning_provider_for_leaf(),
            gate_handler=_proceed_handler,
            today="2026-06-01",
        )
        run_id = "pr36-regression"
        result = await engine.run(run_id)

        # Seam-level signal first: no AttributeError leaked through the
        # shim into ``subworkflow.run_crashed``.
        parent_log = engine.log_path(run_id)
        seam_fails = _seam_failures(parent_log)
        assert not seam_fails, (
            "PR #36 regression: root_dispatch's twig fake failed to "
            "satisfy planning's TwigClientProto across the shim. "
            f"Seam failures: {seam_fails}"
        )
        assert isinstance(result, Completed), result
        assert result.final_node == "end_planned"

        # Child planning log exists and completed successfully.
        sw_started = [
            e for e in replay(parent_log)
            if e["kind"] == "subworkflow_started"
        ]
        assert len(sw_started) == 1
        sub_run_id = sw_started[0]["payload"]["sub_run_id"]
        child_log = tmp_path / f"{sub_run_id}.events.jsonl"
        assert child_log.exists()
        child_terminals = [
            e for e in replay(child_log) if e["kind"] == "run_completed"
        ]
        assert child_terminals
        assert child_terminals[0]["payload"]["terminal"] == "completed"


# =====================================================================
# TestSubWorkflowLogIsolation
# =====================================================================


class TestSubWorkflowLogIsolation:
    """Parent + child must not bleed into each other's logs.

    Anchors: INV-SUBWORKFLOW-LOG-ISOLATION (ADR 0005 §"Log isolation"),
    INV-RUN-ID-FILTER (the kernel filters foreign run_ids on replay).
    """

    async def test_full_sdlc_parent_log_has_only_subworkflow_markers(
        self, tmp_path: Path,
    ):
        """Parent log carries no child verb events.

        INV-SUBWORKFLOW-LOG-ISOLATION: the parent records
        ``subworkflow_started`` / ``subworkflow_completed`` for each
        child node; the child's per-verb events live exclusively in
        the child's own ``{sub_run_id}.events.jsonl``.
        """
        inputs = _demo_full_sdlc_inputs(tmp_path)
        engine = _full_sdlc.build_engine(tmp_path, inputs=inputs)
        run_id = "isolation-full"
        result = await engine.run(run_id)
        assert isinstance(result, Completed)

        parent_events = list(replay(tmp_path / f"{run_id}.events.jsonl"))

        # Allowed-in-parent kinds are exactly the kernel kinds that
        # describe parent-level state (no child-level verb_completed
        # for any node the parent doesn't own).
        parent_owns = {
            "dispatch", "plan", "implement", "capture_impl",
            "pr_lifecycle", "close_out",
            "end", "fail_end", "cancel_end",
            "paused_dispatch", "paused_plan", "paused_implement",
            "paused_pr", "paused_close",
        }
        child_owned_kinds = {"verb_invoked", "verb_completed"}
        for ev in parent_events:
            if ev["kind"] not in child_owned_kinds:
                continue
            nid = ev.get("node_id")
            assert nid in parent_owns, (
                f"INV-SUBWORKFLOW-LOG-ISOLATION violated: parent log "
                f"holds {ev['kind']!r} for node {nid!r} which the "
                f"parent does not own; full event: {ev}"
            )

    async def test_root_dispatch_planning_log_isolation(self, tmp_path: Path):
        """root_dispatch ↔ planning: child log is sub_run_id-scoped.

        INV-SUBWORKFLOW-LOG-ISOLATION + INV-RUN-ID-FILTER. Every event
        in the child log carries the sub_run_id; no event in either
        log appears in the other.
        """
        manifest_dir = tmp_path / ".runs"
        inputs = _root_dispatch.RootDispatchInputs(
            item_id=4242, repo_path=Path("."), auto_plan=True,
            manifest_dir=manifest_dir,
        )
        engine = _root_dispatch.build_engine(
            tmp_path,
            inputs=inputs,
            twig=_root_dispatch.FakeTwigClient(items={
                4242: TwigItem(
                    id=4242, title="leaf", state="Active",
                    area_path="PolyphonyRequiem\\v0",
                    work_item_type="User Story",
                    parent_id=None, raw={},
                ),
            }),
            provider=_planning_provider_for_leaf(),
            gate_handler=_proceed_handler,
            today="2026-06-02",
        )
        run_id = "isolation-rd"
        result = await engine.run(run_id)
        assert isinstance(result, Completed)

        parent_log = engine.log_path(run_id)
        parent_events = list(replay(parent_log))
        sub_run_id = next(
            e["payload"]["sub_run_id"]
            for e in parent_events
            if e["kind"] == "subworkflow_started"
        )
        child_log = tmp_path / f"{sub_run_id}.events.jsonl"
        child_events = list(replay(child_log))

        # Each log's run_id is uniform and distinct.
        assert {e.get("run_id") for e in parent_events} == {run_id}
        assert {e.get("run_id") for e in child_events} == {sub_run_id}
        assert run_id != sub_run_id

    async def test_no_event_id_overlap_between_parent_and_child(
        self, tmp_path: Path,
    ):
        """INV-RUN-ID-FILTER: event_ids are per-file monotonic; the
        two log files are independent counters but no observation
        should cross over.

        Practically this means: replaying parent vs child should yield
        two distinct lists of ``(run_id, ts, kind)`` tuples whose
        run_ids never appear in the other file.
        """
        inputs = _demo_full_sdlc_inputs(tmp_path)
        engine = _full_sdlc.build_engine(tmp_path, inputs=inputs)
        run_id = "overlap-check"
        result = await engine.run(run_id)
        assert isinstance(result, Completed)

        parent_log = tmp_path / f"{run_id}.events.jsonl"
        parent_events = list(replay(parent_log))
        parent_run_ids = {e.get("run_id") for e in parent_events}
        assert parent_run_ids == {run_id}

        # All five child logs.
        child_run_ids: set[str] = set()
        for ev in parent_events:
            if ev["kind"] != "subworkflow_started":
                continue
            sub_run_id = ev["payload"]["sub_run_id"]
            child_run_ids.add(sub_run_id)
            child_log = tmp_path / f"{sub_run_id}.events.jsonl"
            assert child_log.exists(), (
                f"missing child log for {sub_run_id}"
            )
            child_events = list(replay(child_log))
            crids = {e.get("run_id") for e in child_events}
            assert crids == {sub_run_id}, (
                f"INV-RUN-ID-FILTER violated: {child_log.name} "
                f"contains foreign run_ids: {crids - {sub_run_id}}"
            )
            assert run_id not in crids
        assert child_run_ids, "no subworkflow_started events recorded"
        assert run_id not in child_run_ids


# =====================================================================
# TestEndToEndCancellation
# =====================================================================


class TestEndToEndCancellation:
    """End-to-end cancel: parent + children must honour cancel atomically.

    INV-CANCEL-SHORT-CIRCUITS-RETRY: no retry events emitted after the
    cancel point.
    INV-CANCEL-RESUME-IDEMPOTENT: resuming the cancelled run produces
    the same terminal and does not append a second ``run_completed``.
    """

    async def test_external_cancel_terminates_full_sdlc(
        self, tmp_path: Path,
    ):
        """Pre-baked ``cancel_requested`` short-circuits the run.

        Mirrors the ``requiem cancel <run_id>`` flow: an external
        process writes a ``cancel_requested`` event into the log; the
        next engine invocation observes it at the top of ``run()`` and
        terminates with a ``Failed(error_kind="cancelled")``.
        """
        run_id = "cancel-pre"
        log_path = tmp_path / f"{run_id}.events.jsonl"
        emitter = EventEmitter(run_id, EventStore(log_path).append)
        emitter.emit_run_started("full-sdlc")
        emitter.emit_node_entered("dispatch", attempt=1)
        emitter.emit_cancel_requested(
            reason="operator pulled the plug", requested_by="cli",
        )

        inputs = _demo_full_sdlc_inputs(tmp_path)
        engine = _full_sdlc.build_engine(tmp_path, inputs=inputs)
        result = await engine.run(run_id)

        assert isinstance(result, Failed)
        assert result.error_kind == "cancelled"
        assert "operator pulled the plug" in result.message

        # INV-CANCEL-SHORT-CIRCUITS-RETRY: no retry events after cancel.
        events = list(replay(log_path))
        cancel_idx = next(
            i for i, e in enumerate(events) if e["kind"] == "cancel_requested"
        )
        after_cancel = events[cancel_idx + 1:]
        retries = [e for e in after_cancel if e["kind"] == "retry_attempted"]
        assert not retries, (
            f"INV-CANCEL-SHORT-CIRCUITS-RETRY violated: "
            f"{len(retries)} retry events after cancel"
        )
        # Exactly one terminal run_completed.
        terminals = [e for e in events if e["kind"] == "run_completed"]
        assert len(terminals) == 1
        assert terminals[0]["payload"]["terminal"] == "cancelled"

    async def test_cancel_resume_is_idempotent(self, tmp_path: Path):
        """INV-CANCEL-RESUME-IDEMPOTENT: re-running adds no events.

        After a cancelled run terminates, a fresh engine invoked on
        the same log_dir + run_id must produce the same terminal
        without appending duplicate ``run_completed`` markers.
        """
        run_id = "cancel-resume"
        log_path = tmp_path / f"{run_id}.events.jsonl"
        emitter = EventEmitter(run_id, EventStore(log_path).append)
        emitter.emit_run_started("full-sdlc")
        emitter.emit_node_entered("dispatch", attempt=1)
        emitter.emit_cancel_requested(reason="abort now", requested_by="cli")

        inputs = _demo_full_sdlc_inputs(tmp_path)
        e1 = _full_sdlc.build_engine(tmp_path, inputs=inputs)
        r1 = await e1.run(run_id)
        assert isinstance(r1, Failed)
        assert r1.error_kind == "cancelled"

        snapshot_after_first = log_path.read_bytes()

        e2 = _full_sdlc.build_engine(tmp_path, inputs=inputs)
        r2 = await e2.run(run_id)
        # Second run: kernel sees the existing run_completed event and
        # reconstructs into _Terminated, returning Completed("cancelled").
        # Whether the return type is Completed or Failed, the disposition
        # signal must be 'cancelled' both times.
        if isinstance(r2, Completed):
            assert r2.disposition == "cancelled"
        else:
            assert isinstance(r2, Failed)
            assert r2.error_kind == "cancelled"

        snapshot_after_second = log_path.read_bytes()
        assert snapshot_after_first == snapshot_after_second, (
            "INV-CANCEL-RESUME-IDEMPOTENT violated: resuming a "
            "cancelled run mutated the log"
        )

    async def test_cancel_propagates_to_active_subworkflow_child(
        self, tmp_path: Path,
    ):
        """Cancel at a SubWorkflowNode propagates into the child log.

        Forges a parent log with ``node_entered: dispatch`` followed by
        ``cancel_requested``. Parent observes the cancel, propagates a
        ``cancel_requested`` marker into the child's log so a future
        ``requiem resume <sub_run_id>`` also short-circuits, then
        terminates.
        """
        run_id = "cancel-propagate"
        sub_run_id = f"{run_id}__dispatch"

        log_path = tmp_path / f"{run_id}.events.jsonl"
        emitter = EventEmitter(run_id, EventStore(log_path).append)
        emitter.emit_run_started("full-sdlc")
        emitter.emit_node_entered("dispatch", attempt=1)
        emitter.emit_cancel_requested(
            reason="mid-flight abort", requested_by="cli",
        )

        inputs = _demo_full_sdlc_inputs(tmp_path)
        engine = _full_sdlc.build_engine(tmp_path, inputs=inputs)
        result = await engine.run(run_id)
        assert isinstance(result, Failed)
        assert result.error_kind == "cancelled"

        # Parent recorded a subworkflow_cancelled marker.
        parent_events = list(replay(log_path))
        sw_cancelled = [
            e for e in parent_events if e["kind"] == "subworkflow_cancelled"
        ]
        assert sw_cancelled, (
            "parent must record subworkflow_cancelled when cancel "
            "intercepts at a SubWorkflowNode"
        )

        # Child log got a cancel_requested marker authored by parent.
        child_log = tmp_path / f"{sub_run_id}.events.jsonl"
        assert child_log.exists()
        child_events = list(replay(child_log))
        child_cancels = [
            e for e in child_events if e["kind"] == "cancel_requested"
        ]
        assert child_cancels
        assert child_cancels[0]["payload"]["requested_by"] == "parent"


# =====================================================================
# TestEndToEndResume
# =====================================================================


class TestEndToEndResume:
    """Full-pipeline resume after parent-log truncation mid-child.

    INV-RESTART: every state-mutating verb is idempotent; resume on
    the same log_dir + run_id picks up where the parent crashed
    without losing the child's progress.
    """

    async def test_resume_after_parent_log_truncated_mid_implementation(
        self, tmp_path: Path,
    ):
        """Truncate parent's log mid-``implement``; resume cleanly.

        Strategy:
          1. Run full_sdlc once end-to-end; capture child sub_run_ids.
          2. Truncate the parent log after ``subworkflow_started:
             implement`` but before its ``subworkflow_completed``
             (simulates a parent crash mid-implementation).
          3. The child's log is intact (the implementation child ran
             to completion before the truncate).
          4. Resume in a fresh engine; the parent's cursor reconstructs
             to ``_AwaitingSubworkflow(node_id="implement", ...)``; the
             kernel re-attaches the child (which is idempotent on its
             own log) and the parent resumes to ``end``.

        Asserts: same terminal, same final_node, same sub_run_ids (no
        new sub_run was minted).
        """
        inputs = _demo_full_sdlc_inputs(tmp_path)
        e1 = _full_sdlc.build_engine(tmp_path, inputs=inputs)
        run_id = "resume-mid-impl"
        r1 = await e1.run(run_id)
        assert isinstance(r1, Completed)
        assert r1.final_node == "end"

        parent_log = tmp_path / f"{run_id}.events.jsonl"
        original_events = list(replay(parent_log))
        original_sub_runs = {
            e["node_id"]: e["payload"]["sub_run_id"]
            for e in original_events
            if e["kind"] == "subworkflow_started"
        }
        assert "implement" in original_sub_runs, (
            "test prerequisite: implement stage must have run"
        )

        # Truncate after `subworkflow_started: implement`.
        lines = parent_log.read_text(encoding="utf-8").splitlines()
        keep: list[str] = []
        found = False
        for raw in lines:
            ev = json.loads(raw)
            keep.append(raw)
            if (
                ev["kind"] == "subworkflow_started"
                and ev.get("node_id") == "implement"
            ):
                found = True
                break
        assert found, "test fixture: implement subworkflow_started missing"
        parent_log.write_text("\n".join(keep) + "\n", encoding="utf-8")

        # Resume in a fresh engine, same run_id.
        inputs2 = _demo_full_sdlc_inputs(tmp_path)
        e2 = _full_sdlc.build_engine(tmp_path, inputs=inputs2)
        r2 = await e2.run(run_id)
        assert isinstance(r2, Completed), r2
        assert r2.disposition == r1.disposition
        assert r2.final_node == r1.final_node

        # sub_run_ids preserved across resume: no new sub_run minted.
        resumed_events = list(replay(parent_log))
        resumed_sub_runs = {
            e["node_id"]: e["payload"]["sub_run_id"]
            for e in resumed_events
            if e["kind"] == "subworkflow_started"
        }
        for nid, srid in original_sub_runs.items():
            assert resumed_sub_runs.get(nid) == srid, (
                f"resume minted a new sub_run for {nid}: original={srid}, "
                f"resumed={resumed_sub_runs.get(nid)}"
            )


# =====================================================================
# TestProtocolDriftSentinel
# =====================================================================


def _protocol_method_names(proto_cls: type) -> set[str]:
    """Public method names declared on a ``typing.Protocol`` class.

    Catches both sync ``def`` and ``async def`` methods. Excludes
    dunder methods and ``_private`` helpers.
    """
    names: set[str] = set()
    for name in vars(proto_cls):
        if name.startswith("_"):
            continue
        attr = vars(proto_cls)[name]
        if inspect.isfunction(attr) or inspect.iscoroutinefunction(attr):
            names.add(name)
    return names


def _async_method_names(real_cls: type) -> set[str]:
    """Public ``async def`` method names declared on a concrete class.

    Used to derive the structural contract a fake must satisfy when no
    explicit ``Protocol`` exists (e.g., for ``GhClient`` / ``TwigClient``
    / ``FilesystemClient``).
    """
    names: set[str] = set()
    for name in vars(real_cls):
        if name.startswith("_"):
            continue
        attr = vars(real_cls)[name]
        if inspect.iscoroutinefunction(attr):
            names.add(name)
    return names


def _missing_methods(fake_cls: type, required: set[str]) -> set[str]:
    """Names in ``required`` that ``fake_cls`` (or its bases) lacks.

    Walks the MRO so subclassed fakes inherit their parents' methods.
    """
    have: set[str] = set()
    for klass in fake_cls.__mro__:
        have.update(name for name in vars(klass) if not name.startswith("_"))
    return required - have


class TestProtocolDriftSentinel:
    """Meta-tests: every protocol's known fakes must satisfy the protocol.

    Designed to FAIL clearly if a future PR adds a method to a
    ``*Proto`` or a real client without also extending the fakes. This
    is exactly the gap PR #36 fell through (planning's
    ``TwigClientProto`` grew ``show_async`` but ``root_dispatch.FakeTwigClient``
    was not updated until the integration broke).
    """

    def test_root_dispatch_twig_proto_methods_are_public_and_known(self):
        """Locks the root_dispatch TwigClientProto surface to ``{show}``.

        If this fails, a method was added to the protocol — extend the
        per-fake checks below (or simplify the protocol) and update
        the docstring of the regression test in
        ``TestSubWorkflowProtocolStability.test_root_dispatch_to_planning_protocol_pr36``.
        """
        proto_methods = _protocol_method_names(_root_dispatch.TwigClientProto)
        assert proto_methods == {"show"}, (
            f"root_dispatch.TwigClientProto surface changed; was "
            f"{{'show'}}, now {proto_methods}. Update the drift "
            f"sentinel and the PR #36 regression test."
        )

    def test_planning_twig_proto_methods_are_public_and_known(self):
        """Locks the planning TwigClientProto surface to ``{show_async}``."""
        proto_methods = _protocol_method_names(_planning.TwigClientProto)
        assert proto_methods == {"show_async"}, (
            f"planning.TwigClientProto surface changed; was "
            f"{{'show_async'}}, now {proto_methods}. Audit every fake "
            f"that crosses into planning."
        )

    def test_pr_toolkit_proto_surface_is_known(self):
        """Locks pr_lifecycle.PrToolkit surface so drift gets noticed."""
        proto_methods = _protocol_method_names(_pr_lifecycle.PrToolkit)
        expected = {
            "pr_view", "list_review_comments", "list_reviews",
            "request_review", "mergeability", "merge_pr", "git_push",
        }
        assert proto_methods == expected, (
            f"pr_lifecycle.PrToolkit surface changed; was {expected}, "
            f"now {proto_methods}. Update FakePrToolkit and the "
            f"per-pair seam tests."
        )

    @pytest.mark.parametrize(
        "fake_cls, proto_cls, why",
        [
            (
                _root_dispatch.FakeTwigClient,
                _root_dispatch.TwigClientProto,
                "root_dispatch uses its own fake for its own verbs",
            ),
            (
                _planning.FakeTwigClient,
                _planning.TwigClientProto,
                "planning uses its own fake for its own verbs",
            ),
            (
                _root_dispatch.FakeTwigClient,
                _planning.TwigClientProto,
                "PR #36: root_dispatch hands its fake across to planning",
            ),
            (
                _pr_lifecycle.FakePrToolkit,
                _pr_lifecycle.PrToolkit,
                "pr_lifecycle's fake must satisfy its own PrToolkit",
            ),
        ],
    )
    def test_fake_satisfies_protocol(
        self, fake_cls: type, proto_cls: type, why: str,
    ):
        """Each known fake must implement every protocol method.

        Parametrised so a future fake/protocol pair can be added with
        one line. The ``why`` column records the integration reason —
        most importantly, the PR #36 cross-pairing
        (root_dispatch.FakeTwigClient × planning.TwigClientProto).
        """
        required = _protocol_method_names(proto_cls)
        missing = _missing_methods(fake_cls, required)
        assert not missing, (
            f"{fake_cls.__module__}.{fake_cls.__name__} does not satisfy "
            f"{proto_cls.__module__}.{proto_cls.__name__} ({why}); "
            f"missing methods: {sorted(missing)}"
        )

    def test_shared_fakes_satisfy_known_protocols(self):
        """The shared fakes under ``tests/fakes/clients.py`` cover the
        same shapes as their per-workflow demo cousins.

        The shared ``FakeTwigClient`` is the broadest one (it implements
        the union of every workflow's twig needs). It must satisfy
        both root_dispatch's and planning's twig protocols.
        """
        from tests.fakes.clients import FakeTwigClient as SharedFakeTwig

        for proto in (
            _root_dispatch.TwigClientProto,
            _planning.TwigClientProto,
        ):
            required = _protocol_method_names(proto)
            missing = _missing_methods(SharedFakeTwig, required)
            assert not missing, (
                f"tests/fakes/clients.py:FakeTwigClient does not satisfy "
                f"{proto.__module__}.{proto.__name__}; "
                f"missing: {sorted(missing)}. Bring the shared fake in "
                f"line with the per-workflow protocol."
            )
