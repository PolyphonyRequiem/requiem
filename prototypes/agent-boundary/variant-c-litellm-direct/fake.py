"""Variant C — ``FakeCompletionFn`` mimicking ``litellm.completion``.

The seam is a function reference, not a class. The scripted entries are
LiteLLM-shaped ``ChatCompletion`` responses (we build minimal duck-types
so we don't need litellm to construct them). This is exactly the shape
the agent function consumes, so the test path and the prod path are
indistinguishable at the seam.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any


@dataclass
class FakeToolCall:
    name: str
    args: dict[str, Any]


@dataclass
class FakeTurn:
    """One scripted ``completion`` response.

    * ``content`` + no ``tool_calls`` → final assistant message.
    * non-empty ``tool_calls`` → model demands tool execution; the *next*
      ``FakeTurn`` is consumed after the tool results come back.
    * ``raise_exc`` → simulate a transport error (used for 429 cases).
    """

    content: str | None = None
    tool_calls: list[FakeToolCall] = field(default_factory=list)
    raise_exc: Exception | None = None


class FakeProviderError(RuntimeError):
    pass


@dataclass
class FakeCompletionFn:
    """Drop-in replacement for ``litellm.completion``.

    Use ``invoke=fake_fn`` or pass via ``completion_fn=fake_fn``.
    """

    turns: list[FakeTurn]
    cursor: int = 0
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append({k: v for k, v in kwargs.items() if k in {"model", "messages"}})
        if self.cursor >= len(self.turns):
            raise FakeProviderError(
                f"FakeCompletionFn exhausted: {self.cursor} consumed, "
                f"{len(self.turns)} scripted"
            )
        turn = self.turns[self.cursor]
        self.cursor += 1
        if turn.raise_exc is not None:
            raise turn.raise_exc
        message = SimpleNamespace(
            content=turn.content or "",
            tool_calls=[
                SimpleNamespace(
                    id=f"fake-{self.cursor}-{tc.name}",
                    function=SimpleNamespace(
                        name=tc.name, arguments=json.dumps(tc.args)
                    ),
                )
                for tc in turn.tool_calls
            ]
            or None,
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


async def slow_completion(delay_s: float, **_: Any) -> Any:
    """An awaitable, cancellable stand-in for a slow LLM call."""
    await asyncio.sleep(delay_s)
    raise RuntimeError("should have been cancelled")
