"""`CopilotProvider` — wraps the official GitHub Copilot SDK.

GitHub Copilot's only sanctioned programmatic surface is the
``github-copilot-sdk`` package (PyPI; ``import copilot``), which spawns
the ``copilot`` CLI as a subprocess and talks to it over JSON-RPC.
There is no published OpenAI-compatible REST endpoint or SDK we can
use directly without going through that subprocess. The CLI handles
the GitHub-token → Copilot-session-token exchange internally; we just
need to make sure the CLI is on ``$PATH`` and a usable GitHub token is
exported via one of ``COPILOT_GITHUB_TOKEN`` / ``GH_TOKEN`` /
``GITHUB_TOKEN`` (in that precedence order), or interactive
``copilot login`` has been run on the host.

How structured output works here
--------------------------------

The Copilot SDK has NO ``response_format`` / JSON-schema channel like
OpenAI's Chat Completions. We get structured output the same way
Anthropic Tool Use does it conceptually, but stripped down to its
essentials: the charter instructs the model to emit ONLY a JSON object
matching the schema, the response is parsed by :func:`validate_schema`,
and a parse failure → ``BadOutput`` (NOT retried — Mahler-A invariant).
The workflow author wires a ``bad_output`` remediation edge if they
want to re-prompt; that is the same contract as the other two
providers.

We send a one-shot, no-tool, no-MCP session per ``invoke()``: the
agent's user_message is the only message and the charter is prefixed
as a leading system-style instruction. We use ``streaming=True`` (the
CLI default) and ``allow-all`` permissions because no tools are
enabled — there is nothing to permission.

----------------------------------------------------------------------
SDK-error → outcome mapping (ADR 0002 Mahler row × ADR 0004 §4.2)
----------------------------------------------------------------------
| Condition                                            | Outcome                                              |
|------------------------------------------------------|------------------------------------------------------|
| ``session.idle`` + content + schema valid            | Success                                              |
| ``session.idle`` + content fails schema/JSON parse   | BadOutput  (NOT retried — Mahler-A invariant)        |
| ``session.idle`` + content empty                     | BadOutput  (raw is empty)                            |
| ``session.error`` / ``error`` event                  | RetryableFailure  (default; Copilot CLI's own retry  |
|                                                      | classification is opaque, so we retry once)          |
| Auth missing / ``get_auth_status`` returns False     | NeedsHuman(gate=\"provider_auth\")                     |
| ``asyncio.TimeoutError`` on done.wait               | RetryableFailure(error_kind=\"network_timeout\")       |
| ``asyncio.CancelledError``                           | re-raised (kernel converts to Cancelled)             |
| any other SDK exception                              | NeedsHuman(gate=\"provider_unknown\")                  |
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
from dataclasses import dataclass, field
from typing import Any, Final

from pydantic import BaseModel

from requiem.agent import AgentCall
from requiem.outcomes import Cancelled, Outcome
from requiem.providers._common import (
    bad_output_with,
    make_receipt,
    needs_human_with,
    retryable_with,
    success_with,
    validate_schema,
)


DEFAULT_COPILOT_MODEL: Final[str] = "claude-sonnet-4.5"
_SESSION_TIMEOUT_S: Final[float] = 180.0
_SERVER_ERROR_AFTER_S: Final[int] = 30
_TIMEOUT_AFTER_S: Final[int] = 15


def _copilot_token_present() -> bool:
    """Whether any of the env vars Copilot CLI honours is set."""
    return any(
        os.environ.get(name)
        for name in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
    )


@dataclass
class CopilotProvider:
    """`AgentProvider` backed by the GitHub Copilot SDK.

    Constructor knobs:

    * ``model``    — default model (per-call override via ``AgentSpec.model``).
      Copilot exposes its own set of models (``claude-sonnet-4.5``,
      ``gpt-5.x``, etc.); see ``copilot --help model`` on the host CLI.
    * ``client``   — pre-built ``copilot.CopilotClient`` for tests.
    * ``working_directory`` — passed to ``create_session``; defaults to
      ``os.getcwd()``. Copilot only writes inside this directory when
      tools are enabled (we enable none), so the value is mostly a
      formality — but the SDK rejects ``None``.
    """

    model: str = DEFAULT_COPILOT_MODEL
    client: Any = None  # copilot.CopilotClient | None — lazy
    working_directory: str | None = None
    # Internal: the CopilotClient may be entered as a context manager once;
    # we keep a flag so close() is idempotent.
    _started: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.client is None:
            # Lazy import — only pay the cost if a CopilotProvider is
            # actually constructed. Keeps Anthropic-only / OpenAI-only
            # users from being forced to install the SDK.
            try:
                from copilot import CopilotClient
            except ImportError as e:
                raise RuntimeError(
                    "CopilotProvider: the `github-copilot-sdk` package is "
                    "not installed. `pip install github-copilot-sdk` (and "
                    "ensure the `copilot` CLI is on $PATH)."
                ) from e
            # Sanity-check the token shape before we go any further so the
            # caller gets a precise error message rather than an opaque
            # SDK failure deep in the subprocess.
            if not _copilot_token_present():
                raise RuntimeError(
                    "CopilotProvider: no Copilot GitHub token in env. Set "
                    "COPILOT_GITHUB_TOKEN (or GH_TOKEN / GITHUB_TOKEN) to a "
                    "GitHub OAuth token (gho_* from `gh auth login` works), "
                    "OR run `copilot login` interactively on the host."
                )
            self.client = CopilotClient()

    async def _ensure_started(self) -> None:
        """Enter the CopilotClient context manager once per provider lifetime."""
        if self._started:
            return
        # CopilotClient is an async context manager; calling __aenter__
        # spawns the CLI subprocess. We do this on first use to avoid the
        # spawn cost on import.
        await self.client.__aenter__()
        self._started = True

    async def aclose(self) -> None:
        """Tear down the CopilotClient + subprocess. Idempotent."""
        if not self._started:
            return
        with contextlib.suppress(Exception):
            await self.client.__aexit__(None, None, None)
        self._started = False

    async def invoke(self, call: AgentCall) -> Outcome:
        spec = call.spec
        model = spec.model if spec.model and spec.model != "fake" else self.model

        if call.cancel is not None and call.cancel.is_set():
            return Cancelled(cause="operator", at_step=spec.name)

        if call.event_callback:
            call.event_callback(
                "prompt",
                {"agent": spec.name, "provider": "copilot", "model": model},
            )

        # Spin up the subprocess on first use.
        try:
            await self._ensure_started()
        except Exception as e:  # noqa: BLE001
            return _on_unknown(call, e, model)

        # Sanity-check auth status up front. We could let create_session
        # fail, but a clean NeedsHuman with a specific gate is more
        # actionable than a wrapped SDK exception.
        try:
            status = await self.client.get_auth_status()
            if not status.isAuthenticated:
                return _on_auth(
                    call,
                    RuntimeError("Copilot SDK reports not authenticated"),
                    model,
                )
        except Exception as e:  # noqa: BLE001
            return _on_unknown(call, e, model)

        schema = getattr(spec, "response_model", None)
        # Build a single-message prompt that prefixes the charter as a
        # system-style preamble. The Copilot SDK doesn't expose a
        # role-aware message API; this is the cleanest way to inject
        # the charter into a one-shot session.
        prompt = _build_prompt(
            charter=spec.charter,
            user_message=call.user_message,
            schema=schema,
        )

        t0 = time.perf_counter()
        try:
            session = await self.client.create_session(
                on_permission_request=_allow_all_permissions,
                working_directory=self.working_directory or os.getcwd(),
                streaming=True,
                model=model,
            )
        except Exception as e:  # noqa: BLE001
            return _on_unknown(call, e, model)

        # Mutable refs (closure-captured by the event callback).
        response_text = ""
        error_message: str | None = None
        usage_in = 0
        usage_out = 0
        done = asyncio.Event()

        def on_event(event: Any) -> None:
            nonlocal response_text, error_message, usage_in, usage_out
            evt = (
                event.type.value if hasattr(event.type, "value") else str(event.type)
            )
            if evt == "assistant.message":
                # The final assembled assistant message — this is what we keep.
                response_text = getattr(event.data, "content", "") or ""
            elif evt == "assistant.usage":
                # Token accounting (floats sometimes; coerce).
                ein = getattr(event.data, "input_tokens", 0) or 0
                eout = getattr(event.data, "output_tokens", 0) or 0
                usage_in = int(ein)
                usage_out = int(eout)
            elif evt == "session.idle":
                done.set()
            elif evt in ("error", "session.error"):
                error_message = getattr(
                    event.data, "message", str(event.data),
                )
                done.set()

        session.on(on_event)

        try:
            await session.send(prompt)
            await asyncio.wait_for(done.wait(), timeout=_SESSION_TIMEOUT_S)
        except asyncio.CancelledError:
            # INV-CANCEL-SHORT-CIRCUITS-RETRY: do not swallow.
            raise
        except asyncio.TimeoutError as e:
            return _on_timeout(spec.name, call, e, model)
        except Exception as e:  # noqa: BLE001
            return _on_unknown(call, e, model)
        finally:
            # Best-effort: close the session so we don't leak. The SDK
            # garbage-collects abandoned sessions but explicit is better.
            with contextlib.suppress(Exception):
                await self.client.delete_session(session.session_id)

        latency_ms = int((time.perf_counter() - t0) * 1000)
        receipt = make_receipt(
            model=model,
            input_tokens=usage_in,
            output_tokens=usage_out,
            latency_ms=latency_ms,
            request_id=str(session.session_id),
            error=error_message or "",
        )

        if error_message:
            return _on_session_error(
                spec.name, call, error_message, model, receipt=receipt,
            )

        if not response_text:
            return bad_output_with(
                raw="",
                errors=("copilot returned no assistant.message before idle",),
                receipt=receipt,
            )

        if schema is None:
            return success_with({"text": response_text}, receipt, agent=spec.name)

        parsed, errors = validate_schema(response_text, schema)
        if parsed is None:
            return bad_output_with(
                raw=response_text, errors=errors, receipt=receipt,
            )
        return success_with(parsed, receipt, agent=spec.name)


# ---- prompt shaping ---------------------------------------------------


def _build_prompt(
    *, charter: str, user_message: str, schema: type[BaseModel] | None,
) -> str:
    """Combine charter + user message + (optional) JSON-only instruction.

    Copilot has no role-aware message API on this one-shot codepath, so we
    fold everything into a single user prompt. When a schema is set, we
    append a strong "respond with ONLY a JSON object matching this schema"
    instruction; parse failures become ``BadOutput`` and the workflow
    author is responsible for wiring a remediation edge.
    """
    parts = [charter.strip(), "", user_message.strip()]
    if schema is not None:
        schema_json = schema.model_json_schema()
        parts.extend(
            [
                "",
                "Respond with ONLY a single JSON object matching the schema "
                "below. No prose, no markdown fences, no commentary — JUST "
                "the JSON object.",
                "",
                "Schema:",
                _stringify_schema(schema_json),
            ]
        )
    return "\n".join(parts)


def _stringify_schema(schema_dict: dict[str, Any]) -> str:
    import json
    return json.dumps(schema_dict, indent=2, sort_keys=True)


# ---- permission handler ------------------------------------------------


def _allow_all_permissions(request: Any, invocation: dict[str, str]) -> Any:
    """The Copilot SDK requires a permission callback on create_session.

    We never enable any tools (no MCP servers, no built-in tool subsets),
    so this callback should never actually fire — but the SDK enforces
    the signature, so we provide one that approves anything just in case.
    """
    from copilot.session import PermissionHandler
    return PermissionHandler.approve_all(request, invocation)


# ---- error-path mapping ----------------------------------------------


def _on_session_error(
    agent: str,
    call: AgentCall,
    message: str,
    model: str,
    *,
    receipt: dict[str, Any],
) -> Outcome:
    """The Copilot CLI emitted an `error` / `session.error` event.

    We treat these as transient by default because the CLI's own error
    taxonomy is opaque (the message text varies by underlying provider).
    A persistent failure surfaces as a NeedsHuman after the kernel's
    retry policy exhausts. A more precise classifier could be wired in
    once we see real failure patterns from the live runs.
    """
    return retryable_with(
        error_kind="provider_unavailable",
        message=f"copilot session error: {message}",
        retry_after_s=_SERVER_ERROR_AFTER_S,
        retry_key=call.retry_key or f"copilot:{agent}",
        attempt=1,
        receipt=receipt,
    )


def _on_timeout(
    agent: str, call: AgentCall, e: Exception, model: str,
) -> Outcome:
    receipt = make_receipt(model=model, error=f"network_timeout: {e}")
    return retryable_with(
        error_kind="network_timeout",
        message=f"copilot session timeout (>{_SESSION_TIMEOUT_S}s): {e}",
        retry_after_s=_TIMEOUT_AFTER_S,
        retry_key=call.retry_key or f"copilot:{agent}",
        attempt=1,
        receipt=receipt,
    )


def _on_auth(call: AgentCall, e: Exception, model: str) -> Outcome:
    receipt = make_receipt(model=model, error=f"auth: {e}")
    return needs_human_with(
        gate="provider_auth",
        prompt=(
            "Copilot SDK reports unauthenticated. Set COPILOT_GITHUB_TOKEN "
            "(or GH_TOKEN / GITHUB_TOKEN) to a valid gho_* token, or run "
            "`copilot login` on the host."
        ),
        receipt=receipt,
        error_message=str(e),
        agent=call.spec.name,
    )


def _on_unknown(call: AgentCall, e: Exception, model: str) -> Outcome:
    receipt = make_receipt(model=model, error=f"unknown: {e}")
    return needs_human_with(
        gate="provider_unknown",
        prompt=f"Unknown Copilot SDK error: {type(e).__name__}: {e}",
        receipt=receipt,
        error_message=str(e),
        agent=call.spec.name,
    )


__all__ = [
    "CopilotProvider",
    "DEFAULT_COPILOT_MODEL",
]
