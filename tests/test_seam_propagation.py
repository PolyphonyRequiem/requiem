"""Tests for in-process child-seam propagation (ADR-0020 / ADR-0013 blocker B1).

The defect: the kernel reconstructs a dispatched sub-workflow child from JSON-flat
recorded inputs, dropping the non-serialisable provider/toolbelt/gate_handler
seams; a child build_engine that treats missing seams as "synthesize a demo" then
silently runs fakes over real git and reports success. The fix: the kernel
installs the parent engine's seams into contextvars before building the child, and
a child build_engine resolves seams as explicit arg -> active seam -> demo.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from requiem import seam
from requiem.workflows import implementation as impl


@pytest.fixture(autouse=True)
def _clear_seams():
    """Each test starts and ends with no installed seam (contextvar hygiene)."""
    with seam.install(provider=None, toolbelt=None, gate_handler=None):
        pass
    # Reset to defaults explicitly (install with all-None is a no-op).
    seam.active_provider.set(None)
    seam.active_toolbelt.set(None)
    seam.active_gate_handler.set(None)
    yield
    seam.active_provider.set(None)
    seam.active_toolbelt.set(None)
    seam.active_gate_handler.set(None)


class _SentinelProvider:
    name = "SENTINEL_PROVIDER"


class _ExplicitProvider:
    name = "EXPLICIT_PROVIDER"


_SENTINEL_TOOLBELT = object()


# ---- the seam module itself ---------------------------------------------


def test_get_returns_none_when_unset():
    assert seam.get_provider() is None
    assert seam.get_toolbelt() is None
    assert seam.get_gate_handler() is None


def test_set_seams_only_overwrites_non_none():
    seam.set_seams(provider=_SentinelProvider())
    seam.set_seams(toolbelt=_SENTINEL_TOOLBELT)  # must NOT wipe the provider
    assert seam.get_provider().name == "SENTINEL_PROVIDER"
    assert seam.get_toolbelt() is _SENTINEL_TOOLBELT


def test_install_restores_prior_tokens_on_exit():
    seam.set_seams(provider=_SentinelProvider())
    with seam.install(provider=_ExplicitProvider()):
        assert seam.get_provider().name == "EXPLICIT_PROVIDER"
    # restored
    assert seam.get_provider().name == "SENTINEL_PROVIDER"


# ---- implementation.build_engine seam resolution ------------------------


def test_no_seam_no_explicit_falls_back_to_demo(tmp_path: Path):
    """The pre-B1 behaviour: a bare build gets the canned demo FakeProvider."""
    engine = impl.build_engine(tmp_path)
    assert type(engine.provider).__name__ == "FakeProvider"


def test_installed_seam_is_inherited(tmp_path: Path):
    """B1 footgun closed: a child inherits the parent's installed seam."""
    seam.set_seams(provider=_SentinelProvider(), toolbelt=_SENTINEL_TOOLBELT)
    engine = impl.build_engine(tmp_path)
    assert engine.provider.name == "SENTINEL_PROVIDER"
    assert engine.toolbelt is _SENTINEL_TOOLBELT


def test_explicit_arg_wins_over_seam(tmp_path: Path):
    """Back-compat invariant: an explicit provider beats an installed seam."""
    seam.set_seams(provider=_SentinelProvider())
    engine = impl.build_engine(tmp_path, provider=_ExplicitProvider())
    assert engine.provider.name == "EXPLICIT_PROVIDER"


def test_demo_flag_forces_demo_ignoring_seam(tmp_path: Path):
    """demo=True keeps a hermetic demo even under an installed real seam."""
    seam.set_seams(provider=_SentinelProvider(), toolbelt=_SENTINEL_TOOLBELT)
    engine = impl.build_engine(tmp_path, demo=True)
    assert type(engine.provider).__name__ == "FakeProvider"
    assert engine.toolbelt is not _SENTINEL_TOOLBELT


# ---- the kernel installs the parent's seams before building a child -----


async def test_kernel_propagates_parent_seam_to_dispatched_child(tmp_path: Path):
    """End-to-end: a parent engine carrying a sentinel provider dispatches a
    child whose build_engine reads the seam — proving the kernel installs it.

    The child factory records which provider it received."""
    from requiem.dsl import AgentRegistry, VerbRegistry, WorkflowBuilder
    from requiem.kernel import Engine
    from requiem.outcomes import Success
    from requiem.toolbelt import Toolbelt

    seen = {}

    # A child module exposing build_engine that resolves provider via the seam.
    import sys
    import types
    child_mod = types.ModuleType("requiem.workflows._b1_probe_child")

    def child_build_engine(log_dir, **_):
        provider = seam.get_provider()
        seen["child_provider"] = getattr(provider, "name", None)
        wf = (WorkflowBuilder("probe-child").entry("only")
              .script("only", verb="ok").edge("only", on="success", to="end")
              .terminate("end", disposition="completed").build())
        verbs = VerbRegistry()
        verbs.register("ok")(lambda ctx: Success(value={}))
        # The child still needs SOME provider/toolbelt to construct.
        return Engine(workflow=wf, verbs=verbs, agents=AgentRegistry(),
                      provider=provider or _ExplicitProvider(),
                      toolbelt=Toolbelt.real(), log_dir=log_dir)

    child_mod.build_engine = child_build_engine
    sys.modules["requiem.workflows._b1_probe_child"] = child_mod
    try:
        # Parent workflow: one subworkflow node pointing at the probe child.
        parent_wf = (WorkflowBuilder("probe-parent").entry("call")
                     .subworkflow("call",
                                  workflow="requiem.workflows._b1_probe_child")
                     .edge("call", on="success", to="end")
                     .terminate("end", disposition="completed").build())
        parent = Engine(
            workflow=parent_wf, verbs=VerbRegistry(), agents=AgentRegistry(),
            provider=_SentinelProvider(), toolbelt=Toolbelt.real(),
            log_dir=tmp_path, gate_handler=None,
        )
        await parent.run("probe-run")
        # The child saw the parent's sentinel provider via the seam.
        assert seen["child_provider"] == "SENTINEL_PROVIDER", seen
    finally:
        del sys.modules["requiem.workflows._b1_probe_child"]
