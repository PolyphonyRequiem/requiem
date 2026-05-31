"""Retry loop wrapper for the agent boundary.

The provider itself is one-shot. Retry is a separate concern, applied by
the engine. This wrapper encodes Requiem's hard constraints:

* Hardcoded ceiling of 3 attempts on network/auth (north-star §4).
* ``Cancelled`` short-circuits the loop immediately (INV-CANCEL-SHORT-CIRCUITS-RETRY).
* ``retry_key`` is stamped on each attempt so the journal can correlate
  retries against a single logical call (error-handling deep-dive §2.3).
* ``BadOutput`` is *not* network-retried here — it has its own remediation
  path (re-prompt with the validation error, owned by the workflow), and
  silently burning the network budget on a malformed JSON is exactly the
  failure mode INV-NO-CORRUPT-FORWARD forbids.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Awaitable, Callable

from outcomes import AgentOutcome, Cancelled, Permanent, Transient
from provider import AgentCall

log = logging.getLogger("requiem.agent.retry")

MAX_ATTEMPTS = 3  # north-star §4 — not configurable.


async def with_retry(
    provider_call: Callable[[AgentCall], Awaitable[AgentOutcome]],
    call: AgentCall,
    *,
    max_attempts: int = MAX_ATTEMPTS,
    backoff_base_s: float = 0.05,
) -> AgentOutcome:
    """Wrap one provider invocation in the standard retry policy."""

    if max_attempts > MAX_ATTEMPTS:
        raise ValueError(
            f"max_attempts={max_attempts} exceeds north-star hard cap of {MAX_ATTEMPTS}"
        )

    last: AgentOutcome | None = None
    for attempt in range(1, max_attempts + 1):
        if call.cancel is not None and call.cancel.is_set():
            return Cancelled(reason=f"cancel observed before attempt {attempt}")

        # Restamp retry_key with the attempt number — keeps it stable as
        # the *logical* key while letting the journal distinguish attempts.
        attempt_call = replace(call, retry_key=f"{call.retry_key}#{attempt}")
        outcome = await provider_call(attempt_call)
        last = outcome

        if outcome.type == "success":
            if attempt > 1:
                log.info("agent %s recovered on attempt %d", call.spec.name, attempt)
            return outcome
        if outcome.type == "cancelled":
            return outcome
        if outcome.type == "permanent":
            return outcome
        if outcome.type == "bad_output":
            # Owned by the workflow, not the network retry layer.
            return outcome

        # Transient — back off and try again.
        wait = outcome.retry_after_s or backoff_base_s * (2 ** (attempt - 1))
        log.info(
            "agent %s transient on attempt %d (%s); sleeping %.3fs",
            call.spec.name, attempt, outcome.reason, wait,
        )
        # Cancellable sleep: a cancel during backoff aborts immediately.
        if call.cancel is not None:
            try:
                await asyncio.wait_for(_wait_for_cancel(call.cancel), timeout=wait)
                return Cancelled(reason="cancel observed during backoff")
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(wait)

    assert last is not None
    if last.type == "transient":
        return Permanent(reason=f"retry budget exhausted: {last.reason}")
    return last


async def _wait_for_cancel(ev: asyncio.Event) -> None:
    await ev.wait()
