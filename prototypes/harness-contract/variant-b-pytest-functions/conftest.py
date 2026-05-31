"""conftest.py — pytest fixtures for Variant B.

Scenarios are plain pytest functions. Fixtures expose every seam the
engine has: provider, event_log, gate handler, chaos, sub-workflow
provider. Asserts are plain Python.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _engine import (  # noqa: E402
    ChaosHook,
    EventLog,
    FakeProvider,
    KillRequested,
    WorkflowEngine,
)


@pytest.fixture
def event_log(tmp_path: Path) -> EventLog:
    """A fresh JSONL-backed log per test (unless the test reuses tmp_path)."""
    return EventLog(tmp_path / "run.events.jsonl")


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def make_engine(event_log, fake_provider) -> Callable[..., WorkflowEngine]:
    """Factory that lets the test customise every seam without losing the
    defaults provided by the other fixtures."""

    def _make(
        workflow,
        *,
        provider=None,
        gate_handler: Callable | None = None,
        subworkflow_provider_for: Callable | None = None,
        chaos: ChaosHook | None = None,
        log: EventLog | None = None,
    ) -> WorkflowEngine:
        return WorkflowEngine(
            workflow=workflow,
            provider=provider or fake_provider,
            event_log=log or event_log,
            gate_handler=gate_handler,
            subworkflow_provider_for=subworkflow_provider_for,
            chaos=chaos or ChaosHook(),
        )

    return _make


@pytest.fixture
def kill_after():
    """Return a ChaosHook factory: `kill_after('NodeCompleted', node='load')`."""

    def _factory(event_type: str, *, node: str | None = None) -> ChaosHook:
        def on_event(evt):
            if evt.type == event_type and (node is None or evt.node == node):
                raise KillRequested(f"chaos: kill after {event_type} node={node}")
        return ChaosHook(on_event=on_event)

    return _factory


@pytest.fixture
def gate_answer():
    """Return a gate_handler factory: `gate_answer(gate='gate', value='abort', reason='x')`."""

    def _factory(**by_gate: Any):
        # by_gate looks like `gate=dict(value='abort', reason='x')` OR `gate='abort'`
        def handler(name: str, options: list[str]):
            spec = by_gate.get(name)
            if spec is None:
                raise RuntimeError(f"No scripted answer for gate {name!r}")
            if isinstance(spec, str):
                return spec, {}
            value = spec.pop("value")
            return value, spec
        return handler

    return _factory
