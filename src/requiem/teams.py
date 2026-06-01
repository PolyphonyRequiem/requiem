"""Agent-team sugar — `.team(...)` over the `parallel_fork` primitive.

The `TeamNode` itself lives in `dsl.py` (the kernel sees nodes as pure
data). This module exists as the documented author-facing surface and as
a place for the small `TeamBranch` value class for verbs that construct
teams dynamically (e.g. recursive planning).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TeamBranch:
    """One arm of a `.team(...)` call.

    The builder accepts plain tuples for ergonomics, but verbs that
    construct teams dynamically should use this dataclass to keep the
    call site readable.
    """

    agent: str
    prompt_verb: str

    def as_tuple(self) -> tuple[str, str]:
        return (self.agent, self.prompt_verb)
