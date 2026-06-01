"""Shared helpers for real `AgentProvider` implementations.

Both `AnthropicProvider` and `OpenAIProvider` go through the same outcome-
mapping table (ADR 0002 Mahler row × ADR 0004 §4.2 closed enum). This
module factors out the bits that are SDK-shape-independent:

* `make_receipt()`           — canonical receipt dict (ADR 0004 §4.4)
* `*_with(...)` helpers      — construct outcomes that populate both the
                               peer ``receipts`` field (ADR 0004 §4.4) and
                               (where relevant) the legacy in-``value`` /
                               in-``message`` encoding for back-compat
* `validate_schema()`        — pydantic validation that maps failure to
                               `BadOutput` per the "BadOutput is NOT
                               retried" rule

The peer ``receipts`` field is now the canonical channel; the legacy
in-band encodings remain so a consumer reading ``out.value["receipts"]``
or grepping ``out.message`` for ``retry_after`` keeps working until they
migrate to ``out.receipts`` / ``out.after``.
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError

from requiem.outcomes import (
    BadOutput,
    NeedsHuman,
    PermanentFailure,
    RetryableFailure,
    Success,
)


def make_receipt(
    *,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    latency_ms: int = 0,
    request_id: str = "",
    error: str = "",
) -> dict[str, Any]:
    return {
        "kind": "llm_call",
        "model": model,
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "latency_ms": int(latency_ms),
        "request_id": request_id or "",
        "error": error,
    }


# ---- outcome construction with receipts -------------------------------


def success_with(parsed: Any, receipt: dict[str, Any], *, agent: str) -> Success:
    """`Success` whose `value` carries parsed output + receipt. Mirrors
    the shape `FakeProvider` already emits (`{"agent": ..., "parsed": ...}`).

    The receipt is *also* attached to the peer ``receipts`` field per
    ADR 0004 §4.4; the in-``value`` copy stays for backwards-compat with
    any callers reading ``out.value["receipts"]`` directly.
    """
    if isinstance(parsed, BaseModel):
        parsed_payload: Any = parsed.model_dump()
    else:
        parsed_payload = parsed
    return Success(
        value={
            "agent": agent,
            "parsed": parsed_payload,
            "receipts": [receipt],
        },
        receipts=(receipt,),
    )


def bad_output_with(
    *, raw: str, errors: tuple[str, ...], receipt: dict[str, Any]
) -> BadOutput:
    """`BadOutput` with receipt prepended to `validation_errors` under the
    ``__receipt__:`` marker for backwards-compat, AND attached to the peer
    ``receipts`` field per ADR 0004 §4.4. `raw_output` stays the literal
    LLM text.
    """
    receipt_entry = "__receipt__:" + json.dumps(receipt, separators=(",", ":"))
    return BadOutput(
        error_kind="schema_mismatch",
        validation_errors=(receipt_entry, *errors),
        raw_output=raw,
        receipts=(receipt,),
    )


def retryable_with(
    *,
    error_kind: str,
    message: str,
    retry_after_s: int,
    retry_key: str,
    attempt: int,
    receipt: dict[str, Any],
) -> RetryableFailure:
    """`RetryableFailure` whose typed ``after`` field carries the
    ``Retry-After`` hint, with the peer ``receipts`` field populated per
    ADR 0004 §4.4. The legacy ``retry_after=<N>s | receipt=...`` suffix
    on ``message`` is retained so forensics-by-grep still work for
    consumers that haven't migrated to the typed shape yet.
    """
    suffix = (
        f" | retry_after={int(retry_after_s)}s "
        f"| receipt={json.dumps(receipt, separators=(',', ':'))}"
    )
    return RetryableFailure(
        retry_key=retry_key,
        error_kind=error_kind,
        message=message + suffix,
        attempt=attempt,
        after=float(retry_after_s),
        receipts=(receipt,),
    )


def permanent_with(
    *, error_kind: str, message: str, receipt: dict[str, Any], **details: Any
) -> PermanentFailure:
    return PermanentFailure(
        error_kind=error_kind,
        message=message,
        details={"receipts": [receipt], **details},
        receipts=(receipt,),
    )


def needs_human_with(
    *, gate: str, prompt: str, receipt: dict[str, Any], **context: Any
) -> NeedsHuman:
    return NeedsHuman(
        gate=gate,
        prompt=prompt,
        options=("retry", "abort"),
        context={"receipts": [receipt], **context},
        receipts=(receipt,),
    )


# ---- schema validation ------------------------------------------------


def validate_schema(
    raw_value: Any,
    schema: type[BaseModel],
) -> tuple[BaseModel | None, tuple[str, ...]]:
    """Coerce `raw_value` into `schema`. Returns
    ``(model_instance, ())`` on success or ``(None, errors)`` on failure.

    Accepts either an already-parsed dict (Anthropic tool-use input) or a
    JSON string (OpenAI returns content as JSON text).
    """
    if isinstance(raw_value, str):
        try:
            payload = json.loads(raw_value)
        except json.JSONDecodeError as e:
            return None, (f"json decode: {e}",)
    else:
        payload = raw_value
    try:
        return schema.model_validate(payload), ()
    except ValidationError as ve:
        return None, tuple(_format_validation_errors(ve))


def _format_validation_errors(ve: ValidationError) -> list[str]:
    out: list[str] = []
    for err in ve.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        msg = err.get("msg", "")
        typ = err.get("type", "")
        out.append(f"{loc or '<root>'}: {msg} [{typ}]")
    return out


__all__ = [
    "make_receipt",
    "success_with",
    "bad_output_with",
    "retryable_with",
    "permanent_with",
    "needs_human_with",
    "validate_schema",
]
