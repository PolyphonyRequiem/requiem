"""Shared evidence helpers for provider cumulative-input exhaustion."""
from __future__ import annotations

from typing import Any, Mapping


def token_failure_evidence(outcome: Mapping[str, Any]) -> dict[str, Any]:
    """Return the compact forensic fields needed by bounded recovery branches."""
    receipts = list(outcome.get("receipts") or [])
    input_tokens = max(
        (int(receipt.get("input_tokens") or 0) for receipt in receipts),
        default=0,
    )
    return {
        "error_kind": outcome.get("error_kind"),
        "message": outcome.get("message"),
        "input_tokens": input_tokens,
        "receipts": receipts,
    }


def is_cumulative_input_exhaustion(outcome: Mapping[str, Any]) -> bool:
    """Whether a retryable timeout was caused by the provider's input cap."""
    if (
        outcome.get("kind") != "retryable_failure"
        or outcome.get("error_kind") != "network_timeout"
    ):
        return False
    evidence = token_failure_evidence(outcome)
    messages = [str(evidence.get("message") or "")]
    messages.extend(
        str(receipt.get("error") or "") for receipt in evidence["receipts"]
    )
    return any(
        "session input tokens" in message
        and "max_cumulative_input_tokens=" in message
        for message in messages
    )
