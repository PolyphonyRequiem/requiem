"""Pytest plugin — registers default fixtures for the harness.

Activate by listing the plugin in your `conftest.py`::

    pytest_plugins = ["requiem.harness.pytest_plugin"]

or rely on the entry-point in `pyproject.toml` (`pytest11.requiem-harness`)
which auto-loads it for every pytest invocation in the same environment.

Three fixtures ship:

* ``harness``           — a :class:`Harness` bound to ``tmp_path``.
* ``scripted_agent``    — factory: ``scripted_agent({...}) -> FakeAgent``.
* ``scripted_toolbelt`` — factory: ``scripted_toolbelt({...}) -> Toolbelt``.

The two factory fixtures are convenience helpers for tests that want a
provider / toolbelt without constructing a whole Scenario.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest

from requiem.harness.fakes import FakeAgent, FakeToolbelt
from requiem.harness.harness import Harness
from requiem.toolbelt import Toolbelt


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    """Per-test `Harness` rooted at pytest's `tmp_path`."""
    return Harness(log_dir=tmp_path / "runs")


@pytest.fixture
def scripted_agent() -> Callable[[dict[str, Any]], FakeAgent]:
    """Factory: ``scripted_agent({"name": dict_or_list_of_dicts})``."""

    def _make(outputs: dict[str, Any]) -> FakeAgent:
        return FakeAgent.from_outputs(outputs)

    return _make


@pytest.fixture
def scripted_toolbelt() -> Callable[[dict[tuple, Any]], Toolbelt]:
    """Factory: ``scripted_toolbelt({(tool, method, *args): value})``."""

    def _make(scripts: dict[tuple, Any]) -> Toolbelt:
        return FakeToolbelt(dict(scripts)).build()  # type: ignore[return-value]

    return _make
