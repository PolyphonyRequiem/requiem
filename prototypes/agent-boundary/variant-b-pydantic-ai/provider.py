"""Variant B — pydantic-ai ``Agent`` + a ``FunctionModel``-backed fake.

The agent boundary is whatever ``pydantic_ai.Agent`` exposes. We hide it
behind one engine-facing function ``run_agent(agent, prompt, *, model,
cancel, …) -> AgentOutcome`` so the discriminated-outcome contract is
preserved even though pydantic-ai itself raises exceptions on failure.

Mahler's note: this variant trades dependency surface (pydantic-ai pulls
~20 transitive packages including anthropic, openai, mistralai, cohere)
for ergonomic gains — typed output, ``@agent.tool`` decorators, and
streaming/tool-calling/retries are all library-provided.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.exceptions import (
    ModelHTTPError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
    UserError,
)

from outcomes import AgentOutcome, BadOutput, Cancelled, Permanent, Success, Transient, Usage


# ---- output schema ---------------------------------------------------------


class ReviewFinding(BaseModel):
    severity: str = Field(pattern="^(blocking|nit|info)$")
    line: int = Field(ge=1)
    message: str


class ReviewVerdict(BaseModel):
    summary: str
    findings: list[ReviewFinding]
    recommend_merge: bool


# ---- agent declaration -----------------------------------------------------

_VIRTUAL_FILES: dict[str, str] = {
    "src/auth.py": "def login(u, p):\n    return True  # TODO: real auth\n",
}


def build_code_reviewer(model: Any | str = "test") -> Agent[None, ReviewVerdict]:
    """Build the same code-reviewer agent. ``model`` is what pydantic-ai expects:
    a string ID ("anthropic:claude-haiku-4-5"), a ``Model`` instance, or "test".
    """

    agent: Agent[None, ReviewVerdict] = Agent(
        model,
        output_type=ReviewVerdict,
        system_prompt=(
            "You review small Python diffs. Use the tools to inspect the file. "
            "Return JSON matching the ReviewVerdict schema."
        ),
    )

    @agent.tool
    def read_file(ctx: RunContext[None], path: str) -> str:
        """Return the contents of a repo-relative path."""
        return _VIRTUAL_FILES.get(path, f"<no such file: {path}>")

    @agent.tool
    def count_lines(ctx: RunContext[None], path: str) -> int:
        """Return the line count of a repo-relative path."""
        return len(_VIRTUAL_FILES.get(path, "").splitlines())

    return agent


# ---- engine-facing wrapper -------------------------------------------------


@dataclass
class AgentRunResult:
    """What the engine cares about — pure data, library-free."""

    tool_calls: tuple[str, ...] = ()


_TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}


async def run_agent(
    agent: Agent[Any, Any],
    user_message: str,
    *,
    retry_key: str = "",
    cancel: asyncio.Event | None = None,
) -> AgentOutcome:
    """Run a pydantic-ai agent and project the result onto AgentOutcome."""

    if cancel is not None and cancel.is_set():
        return Cancelled(reason="cancelled before dispatch")

    # Race pydantic-ai's run against the cancel event.
    run_task = asyncio.create_task(agent.run(user_message))
    waiters: list[asyncio.Task] = [run_task]
    cancel_task: asyncio.Task | None = None
    if cancel is not None:
        cancel_task = asyncio.create_task(cancel.wait())
        waiters.append(cancel_task)

    done, _pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)

    if cancel_task is not None and cancel_task in done and not run_task.done():
        run_task.cancel()
        try:
            await run_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        return Cancelled(reason="cancel observed mid-flight")

    if cancel_task is not None and not cancel_task.done():
        cancel_task.cancel()

    try:
        result = run_task.result()
    except asyncio.CancelledError:
        return Cancelled(reason="run task cancelled")
    except ModelHTTPError as exc:
        if exc.status_code in _TRANSIENT_HTTP or exc.status_code in {401, 403}:
            return Transient(reason=f"HTTP {exc.status_code}: {exc}")
        return Permanent(reason=f"HTTP {exc.status_code}: {exc}")
    except UnexpectedModelBehavior as exc:
        # pydantic-ai raises this when the model couldn't satisfy the
        # output schema after its own internal retries. That's BadOutput
        # in our discriminated taxonomy.
        return BadOutput(raw=str(exc), errors=(repr(exc),))
    except (UsageLimitExceeded, UserError) as exc:
        return Permanent(reason=str(exc))
    except Exception as exc:  # noqa: BLE001
        return Permanent(reason=f"unrecognised: {exc!r}")

    # Inventory tool calls from the message history for observability.
    tool_calls: list[str] = []
    for msg in result.all_messages():
        for part in getattr(msg, "parts", []):
            if part.__class__.__name__ == "ToolCallPart":
                tool_calls.append(part.tool_name)

    usage = result.usage
    if callable(usage):  # back-compat for older pydantic-ai
        usage = usage()
    return Success(
        value=result.output,
        usage=Usage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        ),
        tool_calls=tuple(tool_calls),
    )


def have_live_credentials(model_str: str) -> bool:
    if model_str.startswith(("anthropic", "claude")):
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    if model_str.startswith(("openai", "gpt")):
        return bool(os.environ.get("OPENAI_API_KEY"))
    return False
