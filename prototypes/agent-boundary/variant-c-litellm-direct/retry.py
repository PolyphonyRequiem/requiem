"""Variant C — retry wrapper (same shape as A and B)."""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from outcomes import AgentOutcome, Cancelled, Permanent

log = logging.getLogger("requiem.agent.retry")
MAX_ATTEMPTS = 3


async def with_retry(
    do_call: Callable[[str], Awaitable[AgentOutcome]],
    retry_key: str,
    *,
    cancel: asyncio.Event | None = None,
    max_attempts: int = MAX_ATTEMPTS,
    backoff_base_s: float = 0.02,
) -> AgentOutcome:
    if max_attempts > MAX_ATTEMPTS:
        raise ValueError(f"exceeds north-star cap of {MAX_ATTEMPTS}")
    last: AgentOutcome | None = None
    for attempt in range(1, max_attempts + 1):
        if cancel is not None and cancel.is_set():
            return Cancelled(reason=f"cancel observed before attempt {attempt}")
        last = await do_call(f"{retry_key}#{attempt}")
        if last.type in {"success", "permanent", "bad_output", "cancelled"}:
            return last
        wait = last.retry_after_s or backoff_base_s * (2 ** (attempt - 1))
        log.info("attempt %d transient (%s); sleeping %.3fs", attempt, last.reason, wait)
        if cancel is not None:
            try:
                await asyncio.wait_for(cancel.wait(), timeout=wait)
                return Cancelled(reason="cancel observed during backoff")
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(wait)
    assert last is not None
    if last.type == "transient":
        return Permanent(reason=f"retry budget exhausted: {last.reason}")
    return last
