"""requiem.projections — read-only projections of authoritative state.

R4 ships ``work_state``: a tree projection of an ADO work-item hierarchy
that combines raw work-item state with the artifact linkage (impl branch,
leaf PR number/state) requiem already owns. R3 (computed roll-up) will
build on top — this module deliberately ships RAW state only, no
derivation, so R3 can layer derivation in a separate ADR without
re-plumbing the data.

See ``work_state.py`` for the WorkItemNode + WorkStateProjection dataclasses
and the ``compute_work_state`` driver.
"""
from __future__ import annotations

from requiem.projections.work_state import (
    WorkItemNode,
    WorkStateProjection,
    compute_work_state,
)

__all__ = [
    "WorkItemNode",
    "WorkStateProjection",
    "compute_work_state",
]
