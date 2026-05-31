"""Variant B — ``FakeProvider`` as a custom ``pydantic_ai.models.Model``.

pydantic-ai already ships ``FunctionModel`` for exactly this use case;
the harness gets *real* tool-call dispatch, real schema validation,
real ``ModelRetry`` semantics — for free. The cost is binding our
fake's contract to pydantic-ai internals (``ModelResponse``,
``ToolCallPart``, ``TextPart``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Union

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel


@dataclass
class ScriptedToolCall:
    name: str
    args: dict[str, Any]


@dataclass
class ScriptedTurn:
    """One turn of the fake's response:
    * ``text`` → final assistant text (typically JSON for output_type).
    * ``tool_calls`` → trigger one or more tool calls; the next ``ScriptedTurn``
      in the list is consumed after the tool results come back.
    """

    text: str | None = None
    tool_calls: list[ScriptedToolCall] = field(default_factory=list)


class FakeProviderError(RuntimeError):
    pass


@dataclass
class FakeProviderState:
    """Per-agent cursor + call audit log."""

    turns: list[ScriptedTurn]
    cursor: int = 0
    invocations: int = 0


def make_fake_model(
    *,
    name: str,
    turns: list[ScriptedTurn],
    state_sink: dict[str, FakeProviderState] | None = None,
) -> FunctionModel:
    """Build a ``FunctionModel`` that replays scripted turns.

    The ``state_sink`` lets tests inspect call counts.
    """

    state = FakeProviderState(turns=turns)
    if state_sink is not None:
        state_sink[name] = state

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        state.invocations += 1
        if state.cursor >= len(state.turns):
            raise FakeProviderError(
                f"FakeProvider for agent {name!r} exhausted: "
                f"{state.invocations} invocations, {len(state.turns)} turns scripted"
            )
        turn = state.turns[state.cursor]
        state.cursor += 1
        parts: list[Any] = []
        for tc in turn.tool_calls:
            parts.append(
                ToolCallPart(
                    tool_name=tc.name,
                    args=json.dumps(tc.args),
                    tool_call_id=f"fake-{state.invocations}-{tc.name}",
                )
            )
        if turn.text is not None:
            parts.append(TextPart(content=turn.text))
        if not parts:
            raise FakeProviderError(
                f"FakeProvider turn for {name!r} has neither text nor tool_calls"
            )
        return ModelResponse(parts=parts)

    return FunctionModel(fn, model_name=f"fake:{name}")
