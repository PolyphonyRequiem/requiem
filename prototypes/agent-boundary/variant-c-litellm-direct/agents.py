"""Variant C — direct LiteLLM calls + explicit validator pipeline.

There is no AgentProvider abstraction. An "agent" is just a function
that builds messages, calls ``litellm.completion``, runs validators,
and returns ``AgentOutcome``. The fake seam is a ``completion_fn``
parameter that defaults to ``litellm.completion`` — the harness passes
a scripted ``FakeCompletionFn`` instead. No subclassing, no Protocols.
This is the *least magic* variant — and the one that exposes the most
machinery at the call site.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from pydantic import BaseModel, Field, ValidationError

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


# ---- tools (plain Python; the agent function dispatches them) --------------


_VIRTUAL_FILES: dict[str, str] = {
    "src/auth.py": "def login(u, p):\n    return True  # TODO: real auth\n",
}


def read_file(path: str) -> str:
    return _VIRTUAL_FILES.get(path, f"<no such file: {path}>")


def count_lines(path: str) -> int:
    return len(_VIRTUAL_FILES.get(path, "").splitlines())


TOOL_REGISTRY: dict[str, Callable[..., Any]] = {
    "read_file": read_file,
    "count_lines": count_lines,
}


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Return file contents.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "count_lines",
            "description": "Return file line count.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
]


# ---- the seam --------------------------------------------------------------


CompletionFn = Callable[..., Any]


def default_completion_fn(**kwargs: Any) -> Any:
    import litellm  # local import — keeps the seam testable without litellm
    return litellm.completion(**kwargs)


# ---- validator pipeline ----------------------------------------------------


@dataclass
class ValidatorResult:
    ok: bool
    error: str | None = None


Validator = Callable[[Any], ValidatorResult]


def schema_validator(model: type[BaseModel]) -> Validator:
    def _v(value: Any) -> ValidatorResult:
        try:
            model.model_validate(value)
            return ValidatorResult(ok=True)
        except ValidationError as ve:
            return ValidatorResult(ok=False, error=str(ve))
    return _v


def recommend_merge_consistency(value: Any) -> ValidatorResult:
    """Domain validator: if any blocking finding, recommend_merge must be False."""
    findings = value.get("findings") or []
    has_blocker = any(f.get("severity") == "blocking" for f in findings)
    if has_blocker and value.get("recommend_merge"):
        return ValidatorResult(ok=False, error="recommend_merge=True with blocking findings")
    return ValidatorResult(ok=True)


# ---- agent function --------------------------------------------------------


@dataclass
class CodeReviewerAgent:
    """The agent: plain dataclass + a callable. No framework."""

    model: str = "claude-haiku-4-5"
    system: str = (
        "You review small Python diffs. Use the tools to inspect the file. "
        "Return JSON matching the ReviewVerdict schema."
    )
    response_model: type[BaseModel] = ReviewVerdict
    validators: tuple[Validator, ...] = field(
        default_factory=lambda: (
            schema_validator(ReviewVerdict),
            recommend_merge_consistency,
        )
    )

    async def invoke(
        self,
        user_message: str,
        *,
        retry_key: str = "",
        cancel: asyncio.Event | None = None,
        completion_fn: CompletionFn = default_completion_fn,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> AgentOutcome:
        if cancel is not None and cancel.is_set():
            return Cancelled(reason="cancelled before dispatch")
        if event_callback:
            event_callback("prompt", {"retry_key": retry_key, "model": self.model})

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system},
            {"role": "user", "content": user_message},
        ]
        tool_log: list[str] = []

        for _ in range(8):  # tool-call ceiling
            if cancel is not None and cancel.is_set():
                return Cancelled(reason="cancelled mid-tool-loop")
            try:
                raw = await _maybe_async(
                    completion_fn,
                    model=self.model,
                    messages=messages,
                    tools=TOOL_SCHEMAS,
                    response_format={"type": "json_object"},
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                return _classify(exc)

            choice = raw.choices[0]
            tool_calls = getattr(choice.message, "tool_calls", None) or []
            if tool_calls:
                # append assistant turn + tool results, then loop
                messages.append(
                    {
                        "role": "assistant",
                        "content": choice.message.content,
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments,
                                },
                            }
                            for tc in tool_calls
                        ],
                    }
                )
                for tc in tool_calls:
                    name = tc.function.name
                    args = json.loads(tc.function.arguments or "{}")
                    fn = TOOL_REGISTRY.get(name)
                    if fn is None:
                        return Permanent(reason=f"unknown tool {name!r}")
                    result = fn(**args)
                    tool_log.append(name)
                    if event_callback:
                        event_callback("tool_call", {"name": name, "args": args})
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": name,
                            "content": json.dumps(result),
                        }
                    )
                continue

            text = choice.message.content or ""
            if event_callback:
                event_callback("response", {"len": len(text)})
            return self._validate(text, tool_log)

        return Permanent(reason="exceeded tool-call ceiling")

    def _validate(self, text: str, tool_log: list[str]) -> AgentOutcome:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            return BadOutput(raw=text, errors=(f"json: {exc}",))
        errors: list[str] = []
        for v in self.validators:
            r = v(parsed)
            if not r.ok and r.error is not None:
                errors.append(r.error)
        if errors:
            return BadOutput(raw=text, errors=tuple(errors))
        return Success(
            value=self.response_model.model_validate(parsed),
            tool_calls=tuple(tool_log),
            usage=Usage(model=self.model),
        )


# ---- helpers ---------------------------------------------------------------


_TRANSIENT_MARKERS = ("rate_limit", "429", "503", "504", "timeout", "overloaded")
_AUTH_MARKERS = ("401", "403", "invalid_api_key", "authentication")


def _classify(exc: Exception) -> AgentOutcome:
    msg = str(exc).lower()
    if any(m in msg for m in _AUTH_MARKERS):
        return Transient(reason=f"auth: {exc}")
    if any(m in msg for m in _TRANSIENT_MARKERS):
        return Transient(reason=str(exc))
    return Permanent(reason=f"unrecognised: {exc}")


async def _maybe_async(fn: CompletionFn, **kwargs: Any) -> Any:
    """Allow ``completion_fn`` to be either sync or async."""
    if inspect.iscoroutinefunction(fn):
        return await fn(**kwargs)
    return await asyncio.to_thread(fn, **kwargs)


def have_live_credentials(model: str) -> bool:
    if model.startswith(("claude", "anthropic")):
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    if model.startswith(("gpt", "o1", "o4")):
        return bool(os.environ.get("OPENAI_API_KEY"))
    return False
