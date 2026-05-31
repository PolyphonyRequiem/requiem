"""Reviewer agent specs + FakeProvider scripts.

Each reviewer has a distinct charter and a typed `ReviewFinding` output.
The synthesizer reads all three findings and produces a verdict.

The FakeProvider is scripted by agent_name (Mahler open-question default:
matches today's polyphony harness).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from engine.agent import AgentSpec, FakeProvider


# ---- typed agent outputs --------------------------------------------


class ReviewFinding(BaseModel):
    severity: Literal["info", "warn", "blocking"]
    category: str
    summary: str
    line_hint: int | None = None


class Verdict(BaseModel):
    recommend_merge: bool
    rationale: str
    top_finding: str
    severity_seen: list[Literal["info", "warn", "blocking"]] = Field(default_factory=list)


# ---- agent specs (chartered reviewers + a synthesizer) --------------


STYLE = AgentSpec(
    name="style_reviewer",
    charter=(
        "You enforce house style. Look for naming, formatting, mutability, "
        "and clarity. You do NOT block on perf or correctness."
    ),
    response_model=ReviewFinding,
)

CORRECTNESS = AgentSpec(
    name="correctness_reviewer",
    charter=(
        "You hunt for bugs. Off-by-one, exception swallowing, mutable "
        "default args, missing error handling. Severity is `blocking` if "
        "the code is observably wrong."
    ),
    response_model=ReviewFinding,
)

PERFORMANCE = AgentSpec(
    name="performance_reviewer",
    charter=(
        "You look for O(n^2) loops, redundant I/O, missing async, and "
        "obvious allocations. Severity is `warn` unless catastrophic."
    ),
    response_model=ReviewFinding,
)

SYNTHESIZER = AgentSpec(
    name="synthesizer",
    charter=(
        "You read all reviewer findings and decide whether to recommend "
        "merge. Blocking findings veto. You synthesize one rationale."
    ),
    response_model=Verdict,
)

ALL_SPECS = [STYLE, CORRECTNESS, PERFORMANCE, SYNTHESIZER]


# ---- FakeProvider scripts -------------------------------------------


def scripted_provider() -> FakeProvider:
    """The canonical happy-path script for the walking skeleton."""
    return FakeProvider(scripts={
        "style_reviewer": [
            {"severity": "warn", "category": "style",
             "summary": "mutable default argument `cache={}` will leak state across calls",
             "line_hint": 3},
        ],
        "correctness_reviewer": [
            {"severity": "blocking", "category": "correctness",
             "summary": "`int(x)` raises ValueError on bad input; no handling",
             "line_hint": 5},
        ],
        "performance_reviewer": [
            {"severity": "info", "category": "performance",
             "summary": "linear scan of `cache.keys()` could be O(1) dict lookup",
             "line_hint": 7},
        ],
        "synthesizer": [
            {"recommend_merge": False,
             "rationale": "1 blocking + 1 warn; correctness reviewer's "
                          "unhandled ValueError must be fixed before merge.",
             "top_finding": "unhandled ValueError on int(x)",
             "severity_seen": ["warn", "blocking", "info"]},
        ],
    })
