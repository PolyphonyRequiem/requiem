"""Pin tests for the dual-clock idle recovery (run-#29 follow-up).

Conductor's IdleRecoveryConfig pattern, ported to requiem.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from requiem.agent import AgentCall, AgentSpec


class _ToyResponse(BaseModel):
    x: int = 0


@dataclass
class _StubAuthStatus:
    isAuthenticated: bool = True


def test_dual_clock_default_values_match_design():
    """Pin the new defaults so they don't drift back to the
    aggressive single-bucket shape from run #28/#29."""
    from requiem.providers.copilot import (
        _DEFAULT_IDLE_TIMEOUT_S,
        _DEFAULT_MAX_SESSION_S,
        _DEFAULT_MAX_RECOVERY_ATTEMPTS,
    )
    assert _DEFAULT_IDLE_TIMEOUT_S == 120.0
    assert _DEFAULT_MAX_SESSION_S == 3600.0
    assert _DEFAULT_MAX_RECOVERY_ATTEMPTS == 3


def test_legacy_session_timeout_s_maps_to_max_session_seconds():
    """Back-compat: a caller that constructed
    CopilotProvider(session_timeout_s=N) (the pre-run-#29 API)
    gets max_session_seconds=N via post_init."""
    from requiem.providers.copilot import CopilotProvider, _DEFAULT_IDLE_TIMEOUT_S

    class _StubClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None

    p = CopilotProvider(client=_StubClient(), session_timeout_s=30.0)
    assert p.session_timeout_s == 30.0
    assert p.max_session_seconds == 30.0
    assert p.idle_timeout_s == _DEFAULT_IDLE_TIMEOUT_S


def _make_stub_client(send_handler):
    """Build a _StubClient whose session has the given send handler."""

    class _StubSession:
        session_id = "sess-stub"
        _cb = None

        def on(self, cb):
            self._cb = cb

        async def send(self, prompt):
            await send_handler(self, prompt)

    class _StubClient:
        async def get_auth_status(self): return _StubAuthStatus()
        async def create_session(self, **kwargs):
            return _StubSession()
        async def delete_session(self, _sid): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None

    return _StubClient()


def test_idle_timeout_does_not_fire_when_events_keep_arriving():
    """The dual-clock loop's key insight: when the idle clock fires
    but time_since_last_event < idle_timeout_s, EVENTS ARE FLOWING.
    Reset and keep waiting. Without this, run-#28/#29's slow-but-
    progressing leaves got killed at 600s."""
    from requiem.providers.copilot import CopilotProvider, DEFAULT_COPILOT_MODEL

    async def _send_handler(session, _prompt):
        async def _stream():
            class _Ev:
                type = "assistant.usage"

                class data:
                    input_tokens = 0
                    output_tokens = 0

            for _ in range(6):
                await asyncio.sleep(0.030)
                if session._cb:
                    session._cb(_Ev())

            class _Done:
                type = "session.idle"
                data = None

            if session._cb:
                session._cb(_Done())
        asyncio.create_task(_stream())

    provider = CopilotProvider(
        model=DEFAULT_COPILOT_MODEL,
        client=_make_stub_client(_send_handler),
        idle_timeout_s=0.100,
        max_session_seconds=5.0,
        max_recovery_attempts=0,
    )
    spec = AgentSpec(name="t", charter="c", response_model=_ToyResponse, model=DEFAULT_COPILOT_MODEL)
    call = AgentCall(spec=spec, user_message="hi")

    outcome = asyncio.run(provider.invoke(call))
    # Should NOT be RetryableFailure — events flowed faster than idle window.
    assert outcome.__class__.__name__ != "RetryableFailure", (
        "outcome should be success/parse failure, got %r; idle clock "
        "fired despite events flowing every 30ms" % (outcome,)
    )


def test_idle_timeout_fires_when_events_truly_stop():
    """No events for the full idle window AND max_recovery_attempts=0
    → fail-fast as RetryableFailure with accurate elapsed."""
    from requiem.providers.copilot import CopilotProvider, DEFAULT_COPILOT_MODEL

    async def _send_handler(_session, _prompt):
        await asyncio.sleep(0)

    provider = CopilotProvider(
        model=DEFAULT_COPILOT_MODEL,
        client=_make_stub_client(_send_handler),
        idle_timeout_s=0.05,
        max_session_seconds=2.0,
        max_recovery_attempts=0,
    )
    spec = AgentSpec(name="t", charter="c", response_model=_ToyResponse, model=DEFAULT_COPILOT_MODEL)
    call = AgentCall(spec=spec, user_message="hi")

    outcome = asyncio.run(provider.invoke(call))
    assert outcome.__class__.__name__ == "RetryableFailure", (
        "expected RetryableFailure, got %r" % (outcome,)
    )


def test_recovery_prompt_sent_on_idle_then_retries():
    """When idle clock fires AND max_recovery_attempts > 0, send a
    recovery prompt and wait again. Only fail after exhausting nudges."""
    from requiem.providers.copilot import CopilotProvider, DEFAULT_COPILOT_MODEL

    sends: list[str] = []

    async def _send_handler(_session, prompt):
        sends.append(prompt)

    provider = CopilotProvider(
        model=DEFAULT_COPILOT_MODEL,
        client=_make_stub_client(_send_handler),
        idle_timeout_s=0.03,
        max_session_seconds=2.0,
        max_recovery_attempts=2,
        recovery_prompt="please continue",
    )
    spec = AgentSpec(name="t", charter="c", response_model=_ToyResponse, model=DEFAULT_COPILOT_MODEL)
    call = AgentCall(spec=spec, user_message="initial prompt")

    asyncio.run(provider.invoke(call))
    # First send is the user prompt (wrapped with charter + schema by
    # the provider's _build_prompt). The exact text isn't pinned here —
    # what matters is that the FIRST send is NOT the recovery prompt
    # and the next two ARE.
    assert "initial prompt" in sends[0]
    assert sends[0] != "please continue"
    assert sends.count("please continue") == 2, (
        "expected exactly 2 recovery prompts, got %d (all sends: %r)"
        % (sends.count("please continue"), sends)
    )


def test_final_assistant_message_completes_without_idle_or_recovery():
    """A final assembled response is authoritative even when the CLI
    omits session.idle. Recovery must not overwrite that response."""
    from requiem.providers.copilot import CopilotProvider, DEFAULT_COPILOT_MODEL

    sends: list[str] = []

    async def _send_handler(session, prompt):
        sends.append(prompt)

        class _MsgEv:
            type = "assistant.message"

            class data:
                content = '{"x": 42}'

        if session._cb:
            session._cb(_MsgEv())

    provider = CopilotProvider(
        model=DEFAULT_COPILOT_MODEL,
        client=_make_stub_client(_send_handler),
        idle_timeout_s=0.01,
        max_session_seconds=1.0,
        max_recovery_attempts=2,
        recovery_prompt="please continue",
    )
    spec = AgentSpec(
        name="t",
        charter="c",
        response_model=_ToyResponse,
        model=DEFAULT_COPILOT_MODEL,
    )

    outcome = asyncio.run(provider.invoke(AgentCall(spec=spec, user_message="hi")))

    assert outcome.__class__.__name__ == "Success"
    assert sends.count("please continue") == 0


def test_max_session_seconds_caps_runaway_event_stream():
    """Wall-clock ceiling fires even when events keep arriving.
    A runaway session shouldn't run forever just because something
    is emitting bookkeeping noise."""
    from requiem.providers.copilot import CopilotProvider, DEFAULT_COPILOT_MODEL

    async def _send_handler(session, _prompt):
        async def _stream():
            class _Ev:
                type = "assistant.usage"

                class data:
                    input_tokens = 0
                    output_tokens = 0

            for _ in range(200):
                await asyncio.sleep(0.010)
                if session._cb:
                    session._cb(_Ev())
        asyncio.create_task(_stream())

    provider = CopilotProvider(
        model=DEFAULT_COPILOT_MODEL,
        client=_make_stub_client(_send_handler),
        idle_timeout_s=0.100,
        max_session_seconds=0.2,
        max_recovery_attempts=10,
    )
    spec = AgentSpec(name="t", charter="c", response_model=_ToyResponse, model=DEFAULT_COPILOT_MODEL)
    call = AgentCall(spec=spec, user_message="hi")

    outcome = asyncio.run(provider.invoke(call))
    assert outcome.__class__.__name__ == "RetryableFailure", (
        "expected wall-clock to fire and produce RetryableFailure, got %r" % (outcome,)
    )
    rcpt = (outcome.receipts or [{}])[0]
    assert rcpt.get("latency_ms", 0) > 100, (
        "expected latency_ms > 100 (real elapsed), got %r" % (rcpt.get("latency_ms"),)
    )


def test_bookkeeping_events_do_not_reset_idle_clock():
    """session.start, pending_messages.modified, session.info are
    bookkeeping noise. They MUST NOT reset the idle clock — a noisy
    SDK shouldn't mask a genuinely-stuck session."""
    from requiem.providers.copilot import CopilotProvider, DEFAULT_COPILOT_MODEL

    async def _send_handler(session, _prompt):
        async def _stream():
            class _Ev:
                type = "pending_messages.modified"
                data = None

            for _ in range(100):
                await asyncio.sleep(0.005)
                if session._cb:
                    session._cb(_Ev())
        asyncio.create_task(_stream())

    provider = CopilotProvider(
        model=DEFAULT_COPILOT_MODEL,
        client=_make_stub_client(_send_handler),
        idle_timeout_s=0.08,
        max_session_seconds=2.0,
        max_recovery_attempts=0,
    )
    spec = AgentSpec(name="t", charter="c", response_model=_ToyResponse, model=DEFAULT_COPILOT_MODEL)
    call = AgentCall(spec=spec, user_message="hi")

    outcome = asyncio.run(provider.invoke(call))
    assert outcome.__class__.__name__ == "RetryableFailure", (
        "idle clock must not be reset by bookkeeping events; got %r" % (outcome,)
    )


# ---- run-#30 follow-up: cumulative input-token cap --------------------
#
# Leaf 9 racked up 120K input tokens across multiple recovery prompts
# before its 44-minute wedge produced hallucinated output. Each
# `session.send(recovery_prompt)` appends to the session history;
# without a cap, a wedged leaf can blow past Copilot's own context
# window and emit bad_output rather than terminate cleanly. The cap
# is a fail-fast path that turns the runaway into a clean retryable
# failure before the model hallucinates.


def test_cumulative_input_token_cap_default_value_pinned():
    """80K is calibrated: successful CVAPI dogfood leaves use 10-30K,
    leaf-9 runaway hit 120K. 80K cleanly separates the cases.
    Bump only with the operator's explicit say-so."""
    from requiem.providers.copilot import _DEFAULT_MAX_CUMULATIVE_INPUT_TOKENS
    assert _DEFAULT_MAX_CUMULATIVE_INPUT_TOKENS == 80_000


def test_cumulative_input_token_cap_fires_retryable_failure():
    """When peak observed input_tokens exceeds
    max_cumulative_input_tokens, the loop terminates as
    RetryableFailure (so the kernel routes via the same code path as
    `network_timeout` / idle-exhausted — the verb's outcome edges
    handle it cleanly)."""
    from requiem.providers.copilot import CopilotProvider, DEFAULT_COPILOT_MODEL

    async def _send_handler(session, _prompt):
        async def _stream():
            class _UsageEv:
                type = "assistant.usage"

                class data:
                    input_tokens = 0  # rewritten per-emit below
                    output_tokens = 5

            # Three usage emits with monotonically-growing input_tokens —
            # mirrors the Copilot SDK's cumulative-per-turn shape on a
            # session that keeps receiving recovery prompts.
            for value in (30_000, 60_000, 120_000):
                _UsageEv.data.input_tokens = value
                await asyncio.sleep(0.01)
                if session._cb:
                    session._cb(_UsageEv())
            # Note: we never emit session.idle — the cap, not the
            # idle clock or wall-clock, must be the trigger.
        asyncio.create_task(_stream())

    provider = CopilotProvider(
        model=DEFAULT_COPILOT_MODEL,
        client=_make_stub_client(_send_handler),
        # Wide-open dual-clock so we can isolate the cap as the cause.
        idle_timeout_s=10.0,
        max_session_seconds=30.0,
        max_recovery_attempts=0,
        max_cumulative_input_tokens=80_000,
    )
    spec = AgentSpec(name="t", charter="c", response_model=_ToyResponse, model=DEFAULT_COPILOT_MODEL)
    call = AgentCall(spec=spec, user_message="hi")

    outcome = asyncio.run(provider.invoke(call))
    assert outcome.__class__.__name__ == "RetryableFailure", (
        "expected RetryableFailure when cumulative input tokens exceeded "
        "the cap, got %r" % (outcome,)
    )
    rcpt = (outcome.receipts or [{}])[0]
    # Receipt must reflect the partial input_tokens we observed — not
    # zero. The operator needs to see how big the session actually got.
    assert rcpt.get("input_tokens", 0) >= 120_000, (
        "receipt should report the peak input_tokens we observed "
        f"(>=120000), got {rcpt.get('input_tokens')!r}"
    )


def test_cumulative_input_token_cap_silent_when_well_under_limit():
    """Successful leaves emit usage events with input_tokens far below
    the cap. They must complete normally — the cap MUST NOT fire on
    a healthy session."""
    from requiem.providers.copilot import CopilotProvider, DEFAULT_COPILOT_MODEL

    async def _send_handler(session, _prompt):
        async def _stream():
            class _UsageEv:
                type = "assistant.usage"

                class data:
                    input_tokens = 25_000  # one successful turn
                    output_tokens = 500

            class _MsgEv:
                type = "assistant.message"

                class data:
                    content = '{"x": 42}'

            class _IdleEv:
                type = "session.idle"
                data = None

            await asyncio.sleep(0.01)
            if session._cb:
                session._cb(_UsageEv())
                session._cb(_MsgEv())
                session._cb(_IdleEv())
        asyncio.create_task(_stream())

    provider = CopilotProvider(
        model=DEFAULT_COPILOT_MODEL,
        client=_make_stub_client(_send_handler),
        idle_timeout_s=2.0,
        max_session_seconds=5.0,
        max_recovery_attempts=0,
        max_cumulative_input_tokens=80_000,
    )
    spec = AgentSpec(name="t", charter="c", response_model=_ToyResponse, model=DEFAULT_COPILOT_MODEL)
    call = AgentCall(spec=spec, user_message="hi")

    outcome = asyncio.run(provider.invoke(call))
    # Should NOT be RetryableFailure — under-cap session succeeds normally.
    assert outcome.__class__.__name__ != "RetryableFailure", (
        "cap must not fire on under-limit session; got %r" % (outcome,)
    )


def test_cumulative_input_token_cap_none_disables_check():
    """max_cumulative_input_tokens=None disables the cap entirely.
    Useful for non-production tuning runs that legitimately need
    large contexts (e.g. exploring how far a model degrades)."""
    from requiem.providers.copilot import CopilotProvider, DEFAULT_COPILOT_MODEL

    async def _send_handler(session, _prompt):
        async def _stream():
            class _UsageEv:
                type = "assistant.usage"

                class data:
                    input_tokens = 500_000  # WAY over any sane cap
                    output_tokens = 0

            class _MsgEv:
                type = "assistant.message"

                class data:
                    content = '{"x": 7}'

            class _IdleEv:
                type = "session.idle"
                data = None

            await asyncio.sleep(0.01)
            if session._cb:
                session._cb(_UsageEv())
                session._cb(_MsgEv())
                session._cb(_IdleEv())
        asyncio.create_task(_stream())

    provider = CopilotProvider(
        model=DEFAULT_COPILOT_MODEL,
        client=_make_stub_client(_send_handler),
        idle_timeout_s=2.0,
        max_session_seconds=5.0,
        max_recovery_attempts=0,
        max_cumulative_input_tokens=None,
    )
    spec = AgentSpec(name="t", charter="c", response_model=_ToyResponse, model=DEFAULT_COPILOT_MODEL)
    call = AgentCall(spec=spec, user_message="hi")

    outcome = asyncio.run(provider.invoke(call))
    assert outcome.__class__.__name__ != "RetryableFailure", (
        "max_cumulative_input_tokens=None must disable the cap; got %r" % (outcome,)
    )
