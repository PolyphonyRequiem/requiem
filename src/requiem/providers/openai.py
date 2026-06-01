"""`OpenAIProvider` — wraps the official `openai` SDK.

Structured output uses the **`response_format={"type": "json_schema"}`**
channel on Chat Completions. We set `strict: True` so the model is held
to the schema at decode time; we still validate with pydantic on receipt
to catch the cases where strict mode wasn't honoured (older models,
unsupported schema features).

----------------------------------------------------------------------
SDK-error → outcome mapping (ADR 0002 Mahler row × ADR 0004 §4.2)
----------------------------------------------------------------------
| SDK condition                            | Outcome                                              |
|------------------------------------------|------------------------------------------------------|
| 200 OK + content + schema valid          | Success                                              |
| 200 OK + content fails schema            | BadOutput  (NOT retried — Mahler-A invariant)        |
| 200 OK + content is None (refusal)       | BadOutput  (refusal text in `raw_output`)            |
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


DEFAULT_OPENAI_MODEL: Final[str] = "gpt-5.4"
_RATE_LIMIT_AFTER_S: Final[int] = 60
_SERVER_ERROR_AFTER_S: Final[int] = 30
_TIMEOUT_AFTER_S: Final[int] = 15


@dataclass
class OpenAIProvider:
    """`AgentProvider` backed by OpenAI's Chat Completions API.

    Constructor knobs match `AnthropicProvider`:

    * ``api_key``       — overrides ``OPENAI_API_KEY``.
    * ``model``         — default model; per-call override via
                          ``AgentSpec.model``.
    * ``client``        — inject a pre-built ``AsyncOpenAI`` for tests.
    """

    api_key: str | None = None
    model: str = DEFAULT_OPENAI_MODEL
    client: Any = None  # AsyncOpenAI | None

    def __post_init__(self) -> None:
        if self.client is None:
            from openai import AsyncOpenAI

            api_key = self.api_key or os.environ.get("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "OpenAIProvider: api_key not provided and "
                    "OPENAI_API_KEY is not set."
                )
            self.client = AsyncOpenAI(api_key=api_key, max_retries=0)

    async def invoke(self, call: AgentCall) -> Outcome:
        import openai as _openai

        spec = call.spec
        model = spec.model if spec.model and spec.model != "fake" else self.model

        if call.cancel is not None and call.cancel.is_set():
            return Cancelled(cause="operator", at_step=spec.name)

        if call.event_callback:
            call.event_callback(
                "prompt",
                {"agent": spec.name, "provider": "openai", "model": model},
            )

        schema = getattr(spec, "response_model", None)
        request_kwargs = _build_request(
            model=model,
            charter=spec.charter,
            user_message=call.user_message,
            schema=schema,
        )

        t0 = time.perf_counter()
        try:
            completion = await self.client.chat.completions.create(**request_kwargs)
        except _openai.RateLimitError as e:
            return _on_rate_limit(spec.name, call, e, model)
        except _openai.AuthenticationError as e:
            return _on_auth(call, e, model)
        except _openai.PermissionDeniedError as e:
            return _on_auth(call, e, model)
        except _openai.BadRequestError as e:
            return _on_bad_request(e, model)
        except _openai.InternalServerError as e:
            return _on_server_error(spec.name, call, e, model)
        except (_openai.APITimeoutError, _openai.APIConnectionError) as e:
            return _on_network(spec.name, call, e, model)
        # asyncio.CancelledError is intentionally NOT caught — it
        # propagates per INV-CANCEL-SHORT-CIRCUITS-RETRY.
        except _openai.APIStatusError as e:
            return _on_unknown(call, e, model, status=getattr(e, "status_code", None))
        except _openai.OpenAIError as e:
            return _on_unknown(call, e, model)

        latency_ms = int((time.perf_counter() - t0) * 1000)
        return _interpret_completion(
            completion,
            schema=schema,
            model=model,
            latency_ms=latency_ms,
            agent=spec.name,
        )


# ---- request shaping --------------------------------------------------


def _build_request(
    *,
    model: str,
    charter: str,
    user_message: str,
    schema: type[BaseModel] | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": charter},
            {"role": "user", "content": user_message},
        ],
    }
    if schema is not None:
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": _schema_name_for(schema),
                "strict": True,
                "schema": _coerce_to_openai_json_schema(schema.model_json_schema()),
            },
        }
    return kwargs


def _schema_name_for(schema: type[BaseModel]) -> str:
    """OpenAI requires schema `name` to match `^[a-zA-Z0-9_-]+$`."""
    raw = schema.__name__ or "Response"
    safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in raw)
    return safe or "Response"


def _coerce_to_openai_json_schema(schema_dict: dict[str, Any]) -> dict[str, Any]:
    """OpenAI strict-mode JSON Schema requires:

    * ``additionalProperties: false`` on every object
    * every property listed in ``required``

    pydantic's default `model_json_schema()` doesn't add either. We
    recursively patch object nodes here so callers don't have to think
    about it. If the model uses features strict mode rejects (anyOf
    across object/non-object, unsupported types, etc.) the SDK will 400
    and we map to `PermanentFailure(invalid_request)`, which is the
    correct outcome — the schema is the bug.
    """
    return _strictify(schema_dict)


def _strictify(node: Any) -> Any:
    if isinstance(node, dict):
        out = {k: _strictify(v) for k, v in node.items()}
        if out.get("type") == "object" and "properties" in out:
            out.setdefault("additionalProperties", False)
            out["required"] = sorted(out["properties"].keys())
        return out
    if isinstance(node, list):
        return [_strictify(v) for v in node]
    return node


# ---- response interpretation -----------------------------------------


def _interpret_completion(
    completion: Any,
    *,
    schema: type[BaseModel] | None,
    model: str,
    latency_ms: int,
    agent: str,
) -> Outcome:
    usage = getattr(completion, "usage", None)
    receipt = make_receipt(
        model=model,
        input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
        output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
        latency_ms=latency_ms,
        request_id=getattr(completion, "id", "") or "",
    )
    choices = list(getattr(completion, "choices", []) or [])
    if not choices:
        return bad_output_with(
            raw="", errors=("response had no choices",), receipt=receipt
        )

    message = getattr(choices[0], "message", None)
    refusal = getattr(message, "refusal", None) if message is not None else None
    if refusal:
        # The model declined; that's a non-retryable schema-shaped
        # outcome from the workflow's POV.
        return bad_output_with(
            raw=str(refusal),
            errors=("model refused to comply with schema",),
            receipt=receipt,
        )

    content = getattr(message, "content", None) if message is not None else None
    if content is None:
        return bad_output_with(
            raw="",
            errors=("response message had no content",),
            receipt=receipt,
        )

    if schema is None:
        return success_with({"text": content}, receipt, agent=agent)

    parsed, errors = validate_schema(content, schema)
    if parsed is None:
        return bad_output_with(raw=content, errors=errors, receipt=receipt)
    return success_with(parsed, receipt, agent=agent)


# ---- error-path mapping ----------------------------------------------


def _on_rate_limit(agent: str, call: AgentCall, e: Exception, model: str) -> Outcome:
    after = _retry_after_from(e, default_s=_RATE_LIMIT_AFTER_S)
    receipt = make_receipt(model=model, error=f"rate_limited: {e}")
    return retryable_with(
        error_kind="rate_limited",
        message=f"openai rate-limited: {e}",
        retry_after_s=after,
        retry_key=call.retry_key or f"openai:{agent}",
        attempt=1,
        receipt=receipt,
    )


def _on_server_error(agent: str, call: AgentCall, e: Exception, model: str) -> Outcome:
    receipt = make_receipt(model=model, error=f"provider_unavailable: {e}")
    return retryable_with(
        error_kind="provider_unavailable",
        message=f"openai 5xx: {e}",
        retry_after_s=_SERVER_ERROR_AFTER_S,
        retry_key=call.retry_key or f"openai:{agent}",
        attempt=1,
        receipt=receipt,
    )


def _on_network(agent: str, call: AgentCall, e: Exception, model: str) -> Outcome:
    receipt = make_receipt(model=model, error=f"network_timeout: {e}")
    return retryable_with(
        error_kind="network_timeout",
        message=f"openai network/timeout: {e}",
        retry_after_s=_TIMEOUT_AFTER_S,
        retry_key=call.retry_key or f"openai:{agent}",
        attempt=1,
        receipt=receipt,
    )


def _on_auth(call: AgentCall, e: Exception, model: str) -> Outcome:
    receipt = make_receipt(model=model, error=f"auth: {e}")
    return needs_human_with(
        gate="provider_auth",
        prompt="OpenAI API key invalid or missing required scopes.",
        receipt=receipt,
        error_message=str(e),
        agent=call.spec.name,
    )


def _on_bad_request(e: Exception, model: str) -> Outcome:
    receipt = make_receipt(model=model, error=f"invalid_request: {e}")
    return permanent_with(
        error_kind="invalid_request",
        message=f"openai 400 invalid request: {e}",
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
            f"Unknown OpenAI provider error"
            + (f" (HTTP {status})" if status is not None else "")
            + f": {e}"
        ),
        receipt=receipt,
        error_message=str(e),
        agent=call.spec.name,
    )


def _retry_after_from(e: Exception, *, default_s: int) -> int:
    resp = getattr(e, "response", None)
    if resp is None:
        return default_s
    headers = getattr(resp, "headers", None) or {}
    try:
        value = headers.get("retry-after") if hasattr(headers, "get") else None
    except Exception:
        value = None
    if not value:
        return default_s
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return default_s


__all__ = [
    "OpenAIProvider",
    "DEFAULT_OPENAI_MODEL",
]
