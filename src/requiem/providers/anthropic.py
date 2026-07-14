"""`AnthropicProvider` — wraps the official `anthropic` SDK.

Structured output uses the **forced tool_use** pattern: we synthesize a
single tool whose `input_schema` is the caller's pydantic schema, force
the model to call it via `tool_choice={"type":"tool","name":...}`, and
parse the tool's `.input` dict against the schema. This is the supported
Anthropic recipe for "must conform to schema" output.

----------------------------------------------------------------------
SDK-error → outcome mapping (ADR 0002 Mahler row × ADR 0004 §4.2)
----------------------------------------------------------------------
| SDK condition                            | Outcome                                              |
|------------------------------------------|------------------------------------------------------|
| 200 OK + tool_use block + schema valid   | Success                                              |
| 200 OK + tool_use input fails schema     | BadOutput  (NOT retried — Mahler-A invariant)        |
| 200 OK + no tool_use block               | BadOutput  (model refused / disobeyed tool_choice)   |
| `RateLimitError` (HTTP 429)              | RetryableFailure(error_kind="rate_limited")          |
| `InternalServerError` (HTTP 5xx)         | RetryableFailure(error_kind="provider_unavailable")  |
| `APITimeoutError` / `APIConnectionError` | RetryableFailure(error_kind="network_timeout")       |
| `AuthenticationError` / `PermissionDeniedError` | NeedsHuman(gate="provider_auth")              |
| `BadRequestError` (400)                  | PermanentFailure(error_kind="invalid_request")       |
| any other SDK error                      | NeedsHuman(gate="provider_unknown")  (Ravel L-1)     |
| `asyncio.CancelledError`                 | re-raised (kernel converts to Cancelled)             |

The "BadOutput is not retried" rule is enforced *at the provider level*:
we return `BadOutput` and never loop. Re-prompting the model with a
different prompt is the workflow author's job (wire a `bad_output`
remediation edge).

----------------------------------------------------------------------
SDK retry posture
----------------------------------------------------------------------
We construct the SDK client with ``max_retries=0``. The kernel owns the
retry budget; an SDK-level retry would (a) hide a transient failure from
our `RetryableFailure` accounting and (b) make `RUN_LIVE_*` smoke tests
slow when keys are wrong.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Final

from pydantic import BaseModel

from requiem.agent import AgentCall
from requiem.outcomes import Cancelled, Outcome
from requiem.providers._common import (
    bad_output_with,
    make_receipt,
    needs_human_with,
    permanent_with,
    retryable_with,
    success_with,
    validate_schema,
)


DEFAULT_ANTHROPIC_MODEL: Final[str] = "claude-sonnet-5"
DEFAULT_MAX_TOKENS: Final[int] = 4096
_RATE_LIMIT_AFTER_S: Final[int] = 60
_SERVER_ERROR_AFTER_S: Final[int] = 30
_TIMEOUT_AFTER_S: Final[int] = 15
_FREEFORM_TOOL_NAME: Final[str] = "respond"


@dataclass
class AnthropicProvider:
    """`AgentProvider` backed by Anthropic's Messages API.

    Constructor knobs:

    * ``api_key``       — overrides ``ANTHROPIC_API_KEY``. If both are
                          unset the constructor raises (we want loud
                          failure at startup, not at first invoke).
    * ``model``         — default Claude model; per-call override via
                          ``AgentSpec.model`` ("fake" is treated as
                          "use the provider default", to ease migration
                          from `FakeProvider`-authored agents).
    * ``max_tokens``    — per-call `max_tokens`; defaults to 4096.
    * ``client``        — inject a pre-built ``AsyncAnthropic`` for
                          tests (HTTP-level mocking via
                          ``httpx.MockTransport``).
    """

    api_key: str | None = None
    model: str = DEFAULT_ANTHROPIC_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    client: Any = None  # AsyncAnthropic | None

    def __post_init__(self) -> None:
        # Resolve the client lazily but validate config eagerly so a
        # misconfigured deployment surfaces at construction, not on the
        # first agent invoke deep in a workflow run.
        if self.client is None:
            from anthropic import AsyncAnthropic

            api_key = self.api_key or os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "AnthropicProvider: api_key not provided and "
                    "ANTHROPIC_API_KEY is not set."
                )
            self.client = AsyncAnthropic(api_key=api_key, max_retries=0)

    async def invoke(self, call: AgentCall) -> Outcome:
        import anthropic as _anthropic

        spec = call.spec
        # "fake" is the sentinel default from `AgentSpec`; treat it as
        # "I haven't picked a model, use the provider's default".
        model = spec.model if spec.model and spec.model != "fake" else self.model

        if call.cancel is not None and call.cancel.is_set():
            return Cancelled(cause="operator", at_step=spec.name)

        if call.event_callback:
            call.event_callback(
                "prompt",
                {"agent": spec.name, "provider": "anthropic", "model": model},
            )

        schema = getattr(spec, "response_model", None)
        request_kwargs = _build_request(
            model=model,
            max_tokens=self.max_tokens,
            charter=spec.charter,
            user_message=call.user_message,
            schema=schema,
        )

        t0 = time.perf_counter()
        try:
            message = await self.client.messages.create(**request_kwargs)
        except _anthropic.RateLimitError as e:
            return _on_rate_limit(spec.name, call, e, model)
        except _anthropic.AuthenticationError as e:
            return _on_auth(call, e, model)
        except _anthropic.PermissionDeniedError as e:
            return _on_auth(call, e, model)
        except _anthropic.BadRequestError as e:
            return _on_bad_request(e, model)
        except _anthropic.InternalServerError as e:
            return _on_server_error(spec.name, call, e, model)
        except (_anthropic.APITimeoutError, _anthropic.APIConnectionError) as e:
            return _on_network(spec.name, call, e, model)
        # asyncio.CancelledError is intentionally NOT caught — it
        # propagates up so the kernel can mark the verb cancelled per
        # INV-CANCEL-SHORT-CIRCUITS-RETRY.
        except _anthropic.APIStatusError as e:
            # Catch-all for status errors we didn't enumerate above.
            return _on_unknown(call, e, model, status=getattr(e, "status_code", None))
        except _anthropic.AnthropicError as e:
            return _on_unknown(call, e, model)

        latency_ms = int((time.perf_counter() - t0) * 1000)
        return _interpret_message(
            message, schema=schema, model=model, latency_ms=latency_ms, agent=spec.name
        )


# ---- request shaping --------------------------------------------------


def _build_request(
    *,
    model: str,
    max_tokens: int,
    charter: str,
    user_message: str,
    schema: type[BaseModel] | None,
) -> dict[str, Any]:
    """Compose the kwargs for `messages.create`.

    With a schema → forced tool_use. Without → plain text response.
    """
    messages = [{"role": "user", "content": user_message}]
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": charter,
        "messages": messages,
    }
    if schema is not None:
        json_schema = _coerce_to_anthropic_input_schema(schema.model_json_schema())
        tool_name = _tool_name_for(schema)
        kwargs["tools"] = [
            {
                "name": tool_name,
                "description": (
                    f"Respond by calling this tool exactly once with the "
                    f"required `{schema.__name__}` shape."
                ),
                "input_schema": json_schema,
            }
        ]
        kwargs["tool_choice"] = {"type": "tool", "name": tool_name}
    return kwargs


def _tool_name_for(schema: type[BaseModel]) -> str:
    """Anthropic tool names must be `^[a-zA-Z0-9_-]{1,64}$`."""
    raw = schema.__name__ or _FREEFORM_TOOL_NAME
    safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in raw)
    return safe[:64] or _FREEFORM_TOOL_NAME


def _coerce_to_anthropic_input_schema(schema_dict: dict[str, Any]) -> dict[str, Any]:
    """Anthropic requires `input_schema` to be a JSON-Schema *object*
    with `type: "object"`. pydantic's `model_json_schema()` already
    produces that shape, but root models can yield other top-level
    types. Guard against that with a clear error rather than letting
    the SDK 400 us.
    """
    if schema_dict.get("type") != "object":
        raise ValueError(
            "AnthropicProvider requires a pydantic BaseModel whose "
            "JSON schema is an object (got type="
            f"{schema_dict.get('type')!r}). RootModels are not supported."
        )
    return schema_dict


# ---- response interpretation -----------------------------------------


def _interpret_message(
    message: Any,
    *,
    schema: type[BaseModel] | None,
    model: str,
    latency_ms: int,
    agent: str,
) -> Outcome:
    """Turn a 200-OK `Message` into an `Outcome`.

    With a schema we expect a `tool_use` block; without, we accept any
    `text` block.
    """
    usage = getattr(message, "usage", None)
    receipt = make_receipt(
        model=model,
        input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
        output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
        latency_ms=latency_ms,
        request_id=getattr(message, "id", "") or "",
    )

    content = list(getattr(message, "content", []) or [])

    if schema is None:
        text = _extract_text(content)
        if text is None:
            return bad_output_with(
                raw=_repr_content(content),
                errors=("no text block in response",),
                receipt=receipt,
            )
        return success_with({"text": text}, receipt, agent=agent)

    tool_block = _extract_tool_use(content)
    if tool_block is None:
        text_or_repr = _extract_text(content) or _repr_content(content)
        return bad_output_with(
            raw=text_or_repr,
            errors=("model returned no tool_use block; forced tool_choice ignored",),
            receipt=receipt,
        )

    parsed, errors = validate_schema(getattr(tool_block, "input", {}), schema)
    if parsed is None:
        raw_text = _safe_json(getattr(tool_block, "input", {}))
        return bad_output_with(raw=raw_text, errors=errors, receipt=receipt)
    return success_with(parsed, receipt, agent=agent)


def _extract_text(content: list[Any]) -> str | None:
    chunks = [getattr(b, "text", "") for b in content if getattr(b, "type", "") == "text"]
    if not chunks:
        return None
    return "\n".join(c for c in chunks if c)


def _extract_tool_use(content: list[Any]) -> Any | None:
    for b in content:
        if getattr(b, "type", "") == "tool_use":
            return b
    return None


def _repr_content(content: list[Any]) -> str:
    return "[" + ", ".join(getattr(b, "type", "?") for b in content) + "]"


def _safe_json(value: Any) -> str:
    import json

    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return repr(value)


# ---- error-path mapping ----------------------------------------------


def _on_rate_limit(agent: str, call: AgentCall, e: Exception, model: str) -> Outcome:
    after = _retry_after_from(e, default_s=_RATE_LIMIT_AFTER_S)
    receipt = make_receipt(model=model, error=f"rate_limited: {e}")
    return retryable_with(
        error_kind="rate_limited",
        message=f"anthropic rate-limited: {e}",
        retry_after_s=after,
        retry_key=call.retry_key or f"anthropic:{agent}",
        attempt=1,
        receipt=receipt,
    )


def _on_server_error(agent: str, call: AgentCall, e: Exception, model: str) -> Outcome:
    receipt = make_receipt(model=model, error=f"provider_unavailable: {e}")
    return retryable_with(
        error_kind="provider_unavailable",
        message=f"anthropic 5xx: {e}",
        retry_after_s=_SERVER_ERROR_AFTER_S,
        retry_key=call.retry_key or f"anthropic:{agent}",
        attempt=1,
        receipt=receipt,
    )


def _on_network(agent: str, call: AgentCall, e: Exception, model: str) -> Outcome:
    receipt = make_receipt(model=model, error=f"network_timeout: {e}")
    return retryable_with(
        error_kind="network_timeout",
        message=f"anthropic network/timeout: {e}",
        retry_after_s=_TIMEOUT_AFTER_S,
        retry_key=call.retry_key or f"anthropic:{agent}",
        attempt=1,
        receipt=receipt,
    )


def _on_auth(call: AgentCall, e: Exception, model: str) -> Outcome:
    receipt = make_receipt(model=model, error=f"auth: {e}")
    return needs_human_with(
        gate="provider_auth",
        prompt="Anthropic API key invalid or missing required scopes.",
        receipt=receipt,
        error_message=str(e),
        agent=call.spec.name,
    )


def _on_bad_request(e: Exception, model: str) -> Outcome:
    receipt = make_receipt(model=model, error=f"invalid_request: {e}")
    return permanent_with(
        error_kind="invalid_request",
        message=f"anthropic 400 invalid request: {e}",
        receipt=receipt,
        error_text=str(e),
    )


def _on_unknown(
    call: AgentCall, e: Exception, model: str, *, status: int | None = None
) -> Outcome:
    receipt = make_receipt(model=model, error=f"unknown: {e}")
    return needs_human_with(
        gate="provider_unknown",
        prompt=(
            f"Unknown Anthropic provider error"
            + (f" (HTTP {status})" if status is not None else "")
            + f": {e}"
        ),
        receipt=receipt,
        error_message=str(e),
        agent=call.spec.name,
    )


def _retry_after_from(e: Exception, *, default_s: int) -> int:
    """Pull `retry-after` from a `RateLimitError`'s response if present."""
    resp = getattr(e, "response", None)
    if resp is None:
        return default_s
    headers = getattr(resp, "headers", None) or {}
    try:
        # httpx `Headers` is case-insensitive; raw dict path also works.
        value = headers.get("retry-after") if hasattr(headers, "get") else None
    except Exception:
        value = None
    if not value:
        return default_s
    try:
        # `Retry-After` may be seconds OR an HTTP-date; we only parse the
        # numeric form. HTTP-date is uncommon for 429 and Anthropic ships
        # seconds. Fall back to default on parse failure.
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return default_s


__all__ = [
    "AnthropicProvider",
    "DEFAULT_ANTHROPIC_MODEL",
]
