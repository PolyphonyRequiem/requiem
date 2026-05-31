"""AgentProvider seam + a deterministic FakeProvider for scenarios."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ScriptedAgent:
    """A queue of scripted replies. `.fetch()` returns the next one and
    raises if the queue is empty (loud failure — INV-NO-CORRUPT-FORWARD
    style)."""

    name: str
    replies: list[dict[str, Any]] = field(default_factory=list)
    _cursor: int = 0
    call_count: int = 0

    def fetch(self) -> dict[str, Any]:
        if self._cursor >= len(self.replies):
            raise RuntimeError(
                f"FakeProvider: agent '{self.name}' invoked {self._cursor + 1} times "
                f"but only {len(self.replies)} replies were scripted."
            )
        reply = self.replies[self._cursor]
        self._cursor += 1
        self.call_count += 1
        return reply


class AgentProvider(Protocol):
    def invoke(self, agent_name: str, prompt_context: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class FakeProvider:
    """A scoped fake: agent name -> ScriptedAgent.

    Sub-workflows can be given their own FakeProvider, or the parent
    fake can be `scoped` with a prefix (variant-dependent — the engine
    just calls .invoke())."""

    agents: dict[str, ScriptedAgent] = field(default_factory=dict)

    def script(self, agent_name: str, *replies: dict[str, Any]) -> "FakeProvider":
        self.agents[agent_name] = ScriptedAgent(name=agent_name, replies=list(replies))
        return self

    def invoke(self, agent_name: str, prompt_context: dict[str, Any]) -> dict[str, Any]:
        if agent_name not in self.agents:
            raise RuntimeError(
                f"FakeProvider: workflow asked for agent '{agent_name}' but no "
                f"script was registered (have: {sorted(self.agents)})"
            )
        return self.agents[agent_name].fetch()

    def call_count(self, agent_name: str) -> int:
        return self.agents[agent_name].call_count if agent_name in self.agents else 0
