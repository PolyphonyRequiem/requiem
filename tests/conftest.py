"""Pytest plumbing — the ``--slow`` opt-in for matrix-heavy tests.

Resume-fidelity (Phase B / Rachmaninov) ships an exhaustive truncate-and-
resume matrix over the full ``code_review_demo`` workflow (~34 events
across two complementary matrices). Each iteration is well under 1s but
the combined volume exceeds the 30s default-suite budget on slower CI.

The bulk matrices are marked ``@pytest.mark.slow`` and excluded from the
default ``pytest`` run; a sampled subset (every Nth truncation point)
stays in the default suite to catch most regressions without cost.

Run the full matrix with ``pytest --slow``.
"""
from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--slow",
        action="store_true",
        default=False,
        help="run @pytest.mark.slow tests (Rachmaninov resume-fidelity full matrix)",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "slow: opt-in tests; run with --slow",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--slow"):
        return
    skip = pytest.mark.skip(reason="opt-in; run with --slow")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)
