"""Real LLM `AgentProvider` implementations.

Mahler A (ADR 0002) defined `AgentProvider` as a structural Protocol with
one method, `async def invoke(self, call: AgentCall) -> Outcome`. Phase A
shipped `FakeProvider` only; this package lands the wire-real providers
the kernel needs to actually run a workflow.

Two providers live here today:

* `AnthropicProvider` — wraps the `anthropic` SDK; structured output via
  forced `tool_use`.
* `OpenAIProvider` — wraps the `openai` SDK; structured output via the
  `response_format={"type": "json_schema"}` channel.

Both translate provider-side conditions into the discriminated outcomes
defined in `requiem.outcomes` per the table in
`docs/decisions/0002-phase-a-integrated-design.md` (Mahler row) and the
`error_kind` enum guidance in `docs/decisions/0004-cross-cutting-defaults.md`
§4.2.

The receipt shape (ADR 0004 §4.4) is a plain dict — providers do not
introduce a new outcome variant. Receipts ride on the existing dict-shaped
fields of each outcome (`Success.value["receipts"]`,
`PermanentFailure.details["receipts"]`, `NeedsHuman.context["receipts"]`)
and as a JSON suffix on `RetryableFailure.message` and the first entry of
`BadOutput.validation_errors`. See `make_receipt()` in `_common`.

Adding a peer `receipts` field to every outcome variant in `outcomes.py`
is desirable per ADR 0004 §4.4 but was deferred — see Open Qs in the PR.
"""
from __future__ import annotations

import os
from typing import Any

from requiem.providers._common import make_receipt
from requiem.providers.anthropic import (
    DEFAULT_ANTHROPIC_MODEL,
    AnthropicProvider,
)
from requiem.providers.copilot import (
    DEFAULT_COPILOT_MODEL,
    CopilotProvider,
    _copilot_token_present,
)
from requiem.providers.openai import DEFAULT_OPENAI_MODEL, OpenAIProvider


def default_provider(**kw: Any):
    """Pick a provider based on environment.

    Resolution order (first match wins):

    1. ``COPILOT_GITHUB_TOKEN`` / ``GH_TOKEN`` / ``GITHUB_TOKEN`` set, or
       interactive ``copilot login`` has been run → ``CopilotProvider``.
       Copilot is preferred when present because it is the operator's
       sanctioned-by-default model surface in v0 (no API-key billing,
       no per-call cost worry).
    2. ``ANTHROPIC_API_KEY`` set → ``AnthropicProvider``.
    3. ``OPENAI_API_KEY`` set → ``OpenAIProvider``.
    4. None of the above → ``RuntimeError`` (callers should fall back to
       ``FakeProvider`` themselves in tests / dev).

    Extra kwargs are forwarded to the chosen provider's constructor.
    """
    if _copilot_token_present():
        return CopilotProvider(**kw)
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicProvider(**kw)
    if os.environ.get("OPENAI_API_KEY"):
        return OpenAIProvider(**kw)
    raise RuntimeError(
        "default_provider(): no provider credentials in env. Set one of:\n"
        "  - COPILOT_GITHUB_TOKEN / GH_TOKEN / GITHUB_TOKEN (CopilotProvider)\n"
        "  - ANTHROPIC_API_KEY (AnthropicProvider)\n"
        "  - OPENAI_API_KEY (OpenAIProvider)\n"
        "Or pass a concrete provider explicitly (`AnthropicProvider(api_key=...)`, "
        "`CopilotProvider()`, etc.), or use `FakeProvider` for tests."
    )


__all__ = [
    "AnthropicProvider",
    "CopilotProvider",
    "OpenAIProvider",
    "DEFAULT_ANTHROPIC_MODEL",
    "DEFAULT_COPILOT_MODEL",
    "DEFAULT_OPENAI_MODEL",
    "default_provider",
    "make_receipt",
]
