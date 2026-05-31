"""Pattern #9 — `.team(...)` sugar over the `parallel_fork` primitive.

The TeamNode itself lives in `dsl.py` (it has to, so the kernel sees it
as data). This module exists as the documented surface for the team
pattern and as a place for the small `TeamBranch` author-facing alias.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TeamBranch:
    """Author-facing shape: one arm of a `.team(...)` call.

    The builder accepts plain tuples for ergonomics, but verbs that
    construct teams dynamically (e.g. recursive planning) should use
    this dataclass to keep the call site readable.
    """
    agent: str
    prompt_verb: str

    def as_tuple(self) -> tuple[str, str]:
        return (self.agent, self.prompt_verb)
