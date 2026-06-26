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


DEFAULT_COPILOT_MODEL: Final[str] = "claude-sonnet-4.6"
"""Default model for the Copilot-backed provider. Was ``claude-sonnet-4.5``
until 2026-06-24; bumped to ``4.6`` after run #27 showed Claude 4.5
hitting the 600s session timeout while emitting only ~365 output tokens
in 10 minutes (~0.6 tok/s) on context-pack-dense prompts.

Key capability deltas (queried via ``CopilotClient.list_models()``):

  * 4.5: ``supported_reasoning_efforts=None`` (no explicit
    reasoning loop; no knobs to tune; thinks in-band, apparently
    slowly on dense prompts), ``max_prompt_tokens=168000``,
    ``max_context_window_tokens=200000``.
  * 4.6: ``supported_reasoning_efforts=['low', 'medium', 'high',
    'max']`` (separate reasoning loop with tunable effort;
    ``default_reasoning_effort='medium'``), ``max_prompt_tokens=
    936000``, ``max_context_window_tokens=1000000``.

Operator override path is unchanged: pass ``model=<id>`` to
``CopilotProvider(...)`` or set per-role policy via
``process.yaml`` (ADR-0030 §2).
"""
_SESSION_TIMEOUT_S: Final[float] = 600.0
"""DEPRECATED alias for the old single-bucket session ceiling.

Run #29 against AB#62759077 (2026-06-24) made it clear that "kill
the session after N seconds total" is the wrong shape for a coder
agent: leaves that legitimately took 15 minutes of actively-emitting
work got cut off the same as leaves that genuinely went silent.

The new shape mirrors conductor's prior art (see
``~/projects/conductor/src/conductor/providers/copilot.py`` — the
:class:`IdleRecoveryConfig` pattern): a **wall-clock ceiling** that
fires only on truly runaway sessions (default 1 hour), plus a
short **idle ceiling** that detects "no SDK events for N seconds"
and either nudges the agent with a recovery prompt or fails fast
(default 120s + 3 recovery attempts).

Kept as a constant so legacy callers that pass
``session_timeout_s=X`` still work — the value is then plumbed in
as the wall-clock ceiling (preserving "you said max 600s, you got
max 600s" semantics for old callers)."""

_DEFAULT_IDLE_TIMEOUT_S: Final[float] = 120.0
"""Time without ANY meaningful SDK event before we consider the
Copilot session genuinely stuck and either send a recovery prompt
or fail.

Lower than conductor's 90s default because requiem's coder workflow
prompts are bigger (~10-40K input tokens with ADR-0030 §1 context
packs) and we want some headroom for the first ``assistant.usage``
event to land before nudging. Bookkeeping/lifecycle events
(``session.start``, ``pending_messages.modified``) are explicitly
excluded — they don't indicate real agent progress, so they don't
reset the idle clock."""

_DEFAULT_MAX_SESSION_S: Final[float] = 3600.0
"""Hard wall-clock ceiling. Even with a still-flowing event stream,
no single ``invoke`` may run longer than this — prevents runaway
sessions from chewing minutes of operator wall-clock while the
model loops on tool calls or repeatedly emits delta events without
ever reaching ``session.idle``.

1 hour is generous for the requiem coder workflow (the biggest run
#29 success was 8.5 minutes); operators can tighten via
``CopilotProvider(max_session_seconds=N)``."""

_DEFAULT_MAX_RECOVERY_ATTEMPTS: Final[int] = 3
"""Number of recovery prompts to send before declaring the session
stuck and returning ``retryable_failure``. Set to 0 to disable
recovery (fail on first idle)."""

_DEFAULT_RECOVERY_PROMPT: Final[str] = (
    "It appears your previous response stalled or did not complete. "
    "Please continue producing the structured response from where you "
    "left off. Remember: the final output MUST be a single JSON object "
    "matching the CoderOutput schema, returned as the assistant's final "
    "message — not as a tool call."
)
"""Sent to the SDK session when the idle clock fires but
``max_recovery_attempts`` hasn't been exhausted. Tuned for the
requiem coder contract (structured JSON output as final message,
not tool calls). Operators can override via
``CopilotProvider(recovery_prompt=…)``."""

# Events that DON'T reset the idle clock — bookkeeping / lifecycle
# noise that doesn't indicate the agent is actively working.
# Matches conductor's `_IDLE_IGNORED_EVENTS` list.
_IDLE_IGNORED_EVENTS: Final[frozenset[str]] = frozenset({
    "pending_messages.modified",
    "session.start",
    "session.info",
})

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
      Copilot exposes its own set of models (``claude-sonnet-4.6``,
      ``gpt-5.x``, etc.); see ``copilot --help model`` on the host CLI.
    * ``client``   — pre-built ``copilot.CopilotClient`` for tests.
    * ``working_directory`` — passed to ``create_session``; defaults to
      ``os.getcwd()``. Copilot only writes inside this directory when
      tools are enabled (we enable none), so the value is mostly a
      formality — but the SDK rejects ``None``.
    * ``session_timeout_s`` — DEPRECATED alias kept for back-compat.
      When set (non-default), reinterpreted as ``max_session_seconds``
      so callers that pinned a single ceiling continue to honor it.
      New code should use ``max_session_seconds`` + ``idle_timeout_s``
      directly.
    * ``max_session_seconds`` — hard wall-clock ceiling (default 1
      hour). Only fires if the session is still running after this
      long, regardless of event activity. Catches genuinely-runaway
      sessions (stuck MCP, infinite tool loop, etc.) without
      penalizing slow-but-progressing real work. Mirrors conductor's
      ``IdleRecoveryConfig.max_session_seconds``.
    * ``idle_timeout_s`` — time without any meaningful SDK event
      before we consider the session stuck and send a recovery
      prompt (default 120s). Bookkeeping/lifecycle events
      (``session.start``, ``pending_messages.modified``,
      ``session.info``) are excluded — they don't indicate the agent
      is actively working. Run #29's failures were almost all
      "model went silent for the full 600s" — far longer than any
      genuine progress gap.
    * ``max_recovery_attempts`` — number of recovery prompts to send
      before failing (default 3). Set to 0 to fail on first idle.
    * ``recovery_prompt`` — template sent to the SDK session when
      the idle clock fires. Default is tuned for the requiem coder
      contract; override for other workflows.
    * ``reasoning_effort`` — passed through to ``create_session``
      verbatim. Honored only by reasoning-capable models (e.g.
      ``claude-sonnet-4.6`` accepts ``"low"`` / ``"medium"`` / ``"high"``
      / ``"max"``; query supported values via
      ``CopilotClient.list_models()`` → ``ModelInfo.supported_reasoning_efforts``).
      Ignored by older models that don't support a separate reasoning
      loop. ``None`` (default) → SDK falls back to the model's
      ``default_reasoning_effort`` (typically ``"medium"``). Set to
      ``"low"`` to force quicker turnaround at the cost of less
      thinking — useful for the requiem coder agent on prompts that
      don't need deep reasoning.
    * ``reasoning_summary`` — passed through. Controls whether/how
      Copilot returns the reasoning trace alongside the final answer.
      Use ``"none"`` to suppress reasoning output for non-reasoning
      models, or for reasoning-capable models when you don't need
      the chain-of-thought. Saves output tokens and latency.
    * ``context_tier`` — passed through. Controls Copilot's context-
      window tier for supported models. Typically left ``None`` (use
      model defaults).
    """

    model: str = DEFAULT_COPILOT_MODEL
    client: Any = None  # copilot.CopilotClient | None — lazy
    working_directory: str | None = None
    session_timeout_s: float = _SESSION_TIMEOUT_S
    """DEPRECATED — kept for back-compat. When non-default, used as
    ``max_session_seconds``. New code should set the two clocks
    directly."""
    max_session_seconds: float = _DEFAULT_MAX_SESSION_S
    idle_timeout_s: float = _DEFAULT_IDLE_TIMEOUT_S
    max_recovery_attempts: int = _DEFAULT_MAX_RECOVERY_ATTEMPTS
    recovery_prompt: str = _DEFAULT_RECOVERY_PROMPT
    reasoning_effort: str | None = None
    reasoning_summary: str | None = None
    context_tier: str | None = None
    # Internal: the CopilotClient may be entered as a context manager once;
    # we keep a flag so close() is idempotent.
    _started: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        # Back-compat: a caller that explicitly passes session_timeout_s
        # (i.e. a value != the default sentinel) wants to pin the wall-
        # clock ceiling to that value. Honor it so the deprecation is
        # silent for existing callers.
        if self.session_timeout_s != _SESSION_TIMEOUT_S:
            # Caller intent: "cap the total time at session_timeout_s."
            # Plumb into max_session_seconds. Idle timeout stays at the
            # default — none of the legacy callers reasoned about idle.
            object.__setattr__(self, "max_session_seconds", self.session_timeout_s)
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
        # Lazy import — like CopilotClient, keep this in the call path so
        # Anthropic-only / OpenAI-only users aren't forced to install the
        # Copilot SDK. The constant is part of the SDK's public API
        # (re-exported from copilot/__init__.py).
        from copilot import BUILTIN_TOOLS_ISOLATED
        # ADR-0030-followup (run-#26): restrict the session's tool
        # surface to the SDK's BUILTIN_TOOLS_ISOLATED preset
        # (ask_user, task_complete, exit_plan_mode, task, read_agent,
        # write_agent, list_agents, send_inbox, context_board, skill).
        # NONE of these tools can read or write the host filesystem
        # or open network connections (the SDK's own contract — see
        # copilot._mode docstring "no access outside the session,
        # no cross-session state, no host environment access, no
        # network").
        #
        # Without this argument the SDK defaults to its FULL tool
        # surface (write_file, edit_file, bash, web_fetch, …) and
        # the model can — and did, in run #26 — write files to
        # the working_directory mid-session. Those writes survive
        # the session even when we return `bad_output` or
        # `network_timeout`, contaminating the worktree for every
        # subsequent leaf in a sequential fanout (each leaf's
        # `assert_clean_workspace` then bails with `workspace.dirty`,
        # turning one stray Copilot tool call into a fanout-wide
        # cascade).
        #
        # Our requiem coder contract is "parse the assistant
        # message as CoderOutput JSON and apply file_changes via
        # the implementation workflow's apply_changes verb"; we
        # never want the SDK to do filesystem IO on our behalf.
        # The isolated set is exactly the right level of
        # capability for that contract.
        #
        # Build the create_session kwargs. The reasoning_* knobs are
        # only included when set so the SDK sees its own ``=None``
        # defaults — keeps the wire shape clean and makes the test
        # stub's recorded-kwargs intent unambiguous (only what the
        # caller actually requested shows up).
        # Run #30 leaf 9 revealed that BUILTIN_TOOLS_ISOLATED alone is
        # INSUFFICIENT. Even with available_tools set to the "isolated"
        # builtin preset, the SDK's `toolFilterPrecedence: "excluded"`
        # (always set by the SDK — see client.py around lines 1802/2369)
        # means available_tools acts as a weak hint, not an authoritative
        # whitelist. The model can still call dangerous builtins like
        # `powershell`/`bash`/`apply_patch`/`task` if they aren't in the
        # excluded list.
        #
        # Repro (June 26): a thin prompt asking the model to write a file
        # caused these tool calls under all four tested configurations:
        #   available_tools=[ask_user,…,skill]            → calls powershell,task,create
        #   available_tools=ToolSet().add_builtin(…)      → calls powershell,apply_patch,view
        #   available_tools=[…] + excluded=[bash,…,task]  → blocked (skill only)
        #   excluded_tools=ToolSet().add_builtin("*")     → blocked all
        #
        # Production proof: run #30 leaf 9 wrote 30 .cs files to the
        # worktree during a 44-min recovery-prompt loop despite our
        # `available_tools=BUILTIN_TOOLS_ISOLATED` setting — the model
        # used `powershell` and `task` tools that aren't in that list.
        #
        # Fix: pass excluded_tools with a wildcard against ALL builtins
        # (`ToolSet().add_builtin("*")`). The requiem coder agent doesn't
        # need any builtin tools — the JSON CoderOutput it returns IS the
        # work product; apply_changes (a requiem verb, not an SDK tool)
        # handles the file writes. Keeping available_tools=
        # BUILTIN_TOOLS_ISOLATED as belt-and-braces in case the SDK ever
        # changes precedence semantics.
        from copilot import ToolSet
        excluded = ToolSet().add_builtin("*")
        session_kwargs: dict[str, Any] = {
            "on_permission_request": _allow_all_permissions,
            "working_directory": self.working_directory or os.getcwd(),
            "streaming": True,
            "model": model,
            "available_tools": list(BUILTIN_TOOLS_ISOLATED),
            "excluded_tools": excluded,
        }
        # Per-call provider-specific knobs from AgentCall.model_options
        # take precedence over the provider's constructor defaults so
        # the operator's process.yaml `models.<role>` block (which
        # populates model_options via ADR-0030 §2 / model_routing) can
        # tune any specific call without changing the global default.
        # The constructor defaults still apply when a key is absent
        # from model_options.
        call_options = getattr(call, "model_options", None) or {}
        effort = call_options.get("reasoning_effort", self.reasoning_effort)
        summary = call_options.get("reasoning_summary", self.reasoning_summary)
        tier = call_options.get("context_tier", self.context_tier)
        if effort is not None:
            session_kwargs["reasoning_effort"] = effort
        if summary is not None:
            session_kwargs["reasoning_summary"] = summary
        if tier is not None:
            session_kwargs["context_tier"] = tier
        try:
            session = await self.client.create_session(**session_kwargs)
        except Exception as e:  # noqa: BLE001
            return _on_unknown(call, e, model)

        # Mutable refs (closure-captured by the event callback). The
        # idle clock tracks the most-recent NON-bookkeeping event so the
        # dual-clock loop below knows whether "no done.set() yet" means
        # the agent is actively working (events flowing) or genuinely
        # stuck (no events at all).
        response_text = ""
        error_message: str | None = None
        usage_in = 0
        usage_out = 0
        done = asyncio.Event()
        last_activity_at = time.monotonic()
        last_activity_event = "<initial>"

        def on_event(event: Any) -> None:
            nonlocal response_text, error_message, usage_in, usage_out
            nonlocal last_activity_at, last_activity_event
            evt = (
                event.type.value if hasattr(event.type, "value") else str(event.type)
            )
            # Update idle clock for every event EXCEPT the bookkeeping
            # / lifecycle noise that doesn't indicate real agent work.
            # See `_IDLE_IGNORED_EVENTS` for the full list + rationale.
            if evt not in _IDLE_IGNORED_EVENTS:
                last_activity_event = evt
                last_activity_at = time.monotonic()
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
            # Dual-clock loop (run #29 follow-up; port of conductor's
            # IdleRecoveryConfig pattern). Two ceilings:
            #
            #   1. max_session_seconds — hard wall-clock ceiling on the
            #      whole `invoke` call. Only fires if STILL running after
            #      this long (default 1 hour); catches runaway sessions.
            #
            #   2. idle_timeout_s — short clock that fires when no
            #      meaningful SDK event has arrived for this long
            #      (default 120s). Doesn't immediately fail — first
            #      sends a recovery prompt, then waits again, up to
            #      max_recovery_attempts times.
            #
            # The key insight from conductor: when `wait_for(done, idle)`
            # fires but `time_since_last_event < idle`, EVENTS ARE STILL
            # FLOWING — the agent is actively working, just hasn't
            # reached `session.idle` yet. Reset and keep waiting. This is
            # what unblocks "slow but progressing" Copilot sessions that
            # the run-#28/#29 single-bucket ceiling killed indiscriminately.
            recovery_attempts = 0
            session_start = time.monotonic()
            while not done.is_set():
                elapsed = time.monotonic() - session_start
                if elapsed > self.max_session_seconds:
                    # Hard wall-clock — fail with full context.
                    raise asyncio.TimeoutError(
                        f"session exceeded max_session_seconds="
                        f"{self.max_session_seconds:.0f}s "
                        f"(last activity: {last_activity_event} "
                        f"{time.monotonic() - last_activity_at:.0f}s ago)"
                    )
                try:
                    await asyncio.wait_for(
                        done.wait(),
                        timeout=self.idle_timeout_s,
                    )
                    break  # done.set() — exit the loop
                except asyncio.TimeoutError:
                    # The idle clock fired — but check if events ARE
                    # flowing (agent is working, just not done yet).
                    time_since_last = time.monotonic() - last_activity_at
                    if time_since_last < self.idle_timeout_s:
                        # Events are still arriving — keep waiting.
                        # Don't increment recovery_attempts; this is
                        # normal progress, not a stuck session.
                        continue
                    # Genuinely idle — no events for the full window.
                    recovery_attempts += 1
                    if recovery_attempts > self.max_recovery_attempts:
                        # Exhausted recovery — declare stuck. Use the
                        # same asyncio.TimeoutError path that legacy
                        # callers handled, with a more informative
                        # message.
                        raise asyncio.TimeoutError(
                            f"session idle after "
                            f"{recovery_attempts - 1} recovery attempts "
                            f"(idle_timeout_s={self.idle_timeout_s:.0f}s; "
                            f"last activity: {last_activity_event} "
                            f"{time_since_last:.0f}s ago)"
                        )
                    # Send a recovery nudge and loop back. The SDK
                    # may emit more events once the model responds.
                    # If `done` got set between the wait_for and here
                    # (race), the loop's while-condition will exit.
                    if not done.is_set():
                        await session.send(self.recovery_prompt)
        except asyncio.CancelledError:
            # INV-CANCEL-SHORT-CIRCUITS-RETRY: do not swallow.
            raise
        except asyncio.TimeoutError as e:
            # The dual-clock loop above raises this with a detailed
            # message (which clock fired, last-activity context).
            # Capture full receipt state so the run analyzer can see
            # whether time was burned in wall-clock-runaway vs
            # genuine-stuck patterns.
            return _on_timeout(
                spec.name, call, e, model, self.max_session_seconds,
                elapsed_s=time.perf_counter() - t0,
                session_id=str(getattr(session, "session_id", "") or ""),
                partial_input_tokens=usage_in,
                partial_output_tokens=usage_out,
            )
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

        # Extract JSON from the response — Copilot models (especially
        # claude-sonnet-4.5) routinely add prose preamble + wrap JSON in
        # ```json ... ``` fences even when the prompt forbids it. They
        # also sometimes emit prose AFTER the JSON. OpenAI's strict
        # json_schema mode and Anthropic's tool-use API both sidestep
        # this at the API layer; with Copilot we have to post-process
        # defensively. See _extract_json_block for the strategy.
        clean = _extract_json_block(response_text)

        parsed, errors = validate_schema(clean, schema)
        if parsed is None:
            return bad_output_with(
                raw=response_text, errors=errors, receipt=receipt,
            )
        return success_with(parsed, receipt, agent=spec.name)


def _extract_json_block(text: str) -> str:
    """Pull a JSON candidate out of a possibly-prosey LLM response.

    Strategy (try each in order, return the first that produces a
    plausible JSON candidate):

    1. **Whole-text already-JSON**: if the trimmed text starts with
       ``{`` or ``[``, return it. validate_schema handles the parsing.
    2. **Fenced block anywhere**: scan for the first ``\\u0060\\u0060\\u0060`` opening
       fence (with or without a ``json``/``JSON`` language hint); return
       the content up to the next ``\\u0060\\u0060\\u0060``. This handles the common
       "prose preamble + ```json {...} ``` + optional trailing prose"
       shape claude-sonnet-4.5 produces under codebase-grounding tasks.
    3. **Brace-balanced extraction**: find the first ``{`` or ``[`` in
       the text, then walk forward maintaining brace depth (respecting
       JSON string escaping) until the matching close. Return that
       slice. This is the fallback for the un-fenced "prose then naked
       JSON" shape.
    4. **Give up**: return the trimmed text and let validate_schema
       report a parse error. (Caller turns this into BadOutput.)

    No JSON parsing here — that's validate_schema's job. We just narrow
    the candidate to maximise its chance of parsing cleanly.
    """
    s = text.strip()
    if not s:
        return s

    # (1) Already-JSON whole text.
    if s.startswith("{") or s.startswith("["):
        return s

    # (2) Fenced block anywhere in the response.
    fenced = _extract_fenced_json(s)
    if fenced is not None:
        return fenced

    # (3) Brace-balanced extraction from first { or [.
    balanced = _extract_balanced_json(s)
    if balanced is not None:
        return balanced

    # (4) Last resort — return as-is and let the parser surface the error.
    return s


def _extract_fenced_json(s: str) -> str | None:
    """Find the first ```...``` block whose opening line is bare or
    ``json``/``JSON``. Returns the inner content (no fence markers), or
    None if no plausible fence was found.

    The fence may appear anywhere in the input (start, middle, end);
    prose before and after is ignored. Non-JSON fences (```python,
    ```bash, etc.) are skipped — including their closing ``` — so the
    scan continues past them looking for a JSON-typed fence later in
    the response.
    """
    # Walk through ``` openings. For each, the opening line ends at the
    # next \n; the content is everything up to the next ``` on its own
    # line (or end-of-string).
    cursor = 0
    while True:
        open_idx = s.find("```", cursor)
        if open_idx == -1:
            return None
        # The "language hint" is whatever's on the opening line after ```
        line_end = s.find("\n", open_idx + 3)
        if line_end == -1:
            # ``` with no newline — malformed, skip.
            return None
        hint = s[open_idx + 3 : line_end].strip().lower()
        body_start = line_end + 1
        # Find the closing ```; tolerate optional whitespace before/after.
        close_idx = s.find("```", body_start)
        # Accept bare fence ("") or json/JSON hint. Skip non-JSON fences
        # like ```python, ```bash etc. — they won't contain our payload.
        # Critical: when skipping, advance cursor PAST the close fence
        # (close_idx + 3) so we don't re-find the same close as the next
        # "opening" ```.
        if hint not in ("", "json"):
            cursor = (close_idx + 3) if close_idx != -1 else (line_end + 1)
            continue
        if close_idx == -1:
            # Open fence but no close — take everything to end of string.
            body = s[body_start:].strip()
            if body and (body[0] == "{" or body[0] == "["):
                return body
            return None
        body = s[body_start:close_idx].strip()
        # Only return this fenced block if its content looks like JSON;
        # otherwise keep scanning for a later fence (advance past close).
        if body and (body[0] == "{" or body[0] == "["):
            return body
        cursor = close_idx + 3


def _extract_balanced_json(s: str) -> str | None:
    """Find the first { or [ and return the slice through its matching
    close bracket, respecting JSON string escaping. Returns None if no
    opener is found or the bracket structure never balances.

    This is the "naked JSON after prose" fallback when no fence was used.
    """
    # Find the first opener.
    for i, ch in enumerate(s):
        if ch in "{[":
            opener = ch
            closer = "}" if opener == "{" else "]"
            break
    else:
        return None

    depth = 0
    in_string = False
    escape_next = False
    for j in range(i, len(s)):
        ch = s[j]
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return s[i : j + 1]
    return None


# Back-compat: older callers / tests import _strip_code_fence directly.
# Keep the symbol as a thin wrapper over the new extractor so existing
# imports continue to work. The new logic is a strict superset of the
# old behaviour (every input the old function returned cleanly the new
# one returns the same cleaned result, plus more).
def _strip_code_fence(text: str) -> str:
    """Deprecated alias for :func:`_extract_json_block`. Kept for tests
    and external callers that imported the old name."""
    return _extract_json_block(text)


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
    timeout_s: float = _SESSION_TIMEOUT_S,
    *,
    elapsed_s: float | None = None,
    session_id: str = "",
    partial_input_tokens: int = 0,
    partial_output_tokens: int = 0,
) -> Outcome:
    """Build a retryable-failure outcome for an asyncio.wait_for timeout.

    ``elapsed_s`` is the wall-time the session ran BEFORE the timeout
    fired (we know that — we wrapped it). Reported as ``latency_ms`` on
    the receipt so operators don't see ``latency_ms=0`` as in run #26,
    which was systematically wrong (the SDK call was running for the
    full ``timeout_s``, not zero seconds). When unknown, latency_ms
    defaults to ``timeout_s * 1000`` (a strict upper bound).

    ``session_id`` and ``partial_input_tokens`` / ``partial_output_tokens``
    are the live session's state at the moment of timeout — useful when
    Copilot streamed some usage updates before stalling.
    """
    latency_ms = (
        int(elapsed_s * 1000) if elapsed_s is not None
        else int(timeout_s * 1000)
    )
    receipt = make_receipt(
        model=model,
        input_tokens=partial_input_tokens,
        output_tokens=partial_output_tokens,
        latency_ms=latency_ms,
        request_id=session_id,
        error=f"network_timeout: {e}",
    )
    return retryable_with(
        error_kind="network_timeout",
        message=f"copilot session timeout (>{timeout_s}s): {e}",
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
