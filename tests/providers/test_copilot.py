"""Tests for `CopilotProvider`.

Unlike the live smoke tests, these are hermetic: we inject a `_FakeCopilotClient`
that mimics the surface area `CopilotProvider` actually touches, without
spawning the `copilot` CLI subprocess. The live integration is exercised by
the end-to-end driver tests (and proven manually before this provider
shipped).

The fake covers four scripted behaviours so the per-outcome branches in
the provider are pinned:

* Success path        — assistant.message → session.idle → JSON parses
* BadOutput (parse)   — assistant.message with malformed JSON
* BadOutput (empty)   — session.idle with no assistant.message
* RetryableFailure    — session.error event before idle
* NeedsHuman (auth)   — get_auth_status returns isAuthenticated=False
* RetryableFailure    — done.wait times out
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel

from requiem.agent import AgentCall, AgentSpec
from requiem.outcomes import (
    BadOutput,
    NeedsHuman,
    RetryableFailure,
    Success,
)
from requiem.providers.copilot import (
    CopilotProvider,
    _build_prompt,
    _copilot_token_present,
)


# ---- fake SDK plumbing -------------------------------------------------


@dataclass
class _FakeEventType:
    """Mimics ``copilot.session.SessionEventType`` enum entries."""
    value: str


@dataclass
class _FakeEvent:
    type: _FakeEventType
    data: Any


@dataclass
class _FakeAssistantMessageData:
    content: str


@dataclass
class _FakeAssistantUsageData:
    input_tokens: int
    output_tokens: int


@dataclass
class _FakeSessionErrorData:
    message: str


@dataclass
class _FakeAuthStatus:
    isAuthenticated: bool
    authType: str | None = None


class _FakeSession:
    """A scripted Copilot SDK session.

    On ``send()``, fires the configured script of events through the
    registered callback (in order) then sets the done-equivalent.
    """

    def __init__(self, session_id: str, script: list[_FakeEvent]) -> None:
        self.session_id = session_id
        self._script = script
        self._handlers: list[Any] = []

    def on(self, handler: Any) -> None:
        self._handlers.append(handler)

    async def send(self, prompt: str) -> str:
        # Fire the scripted events on the next event-loop tick so that
        # `await session.send(...)` returns first and the provider can
        # then `await done.wait()` — mirrors the real SDK's ordering.
        async def _fire():
            await asyncio.sleep(0)
            for event in self._script:
                for h in self._handlers:
                    h(event)
        asyncio.create_task(_fire())
        return "msg-id-fake"


class _FakeCopilotClient:
    """The minimal surface CopilotProvider touches.

    Implements: __aenter__/__aexit__, get_auth_status, create_session,
    delete_session.
    """

    def __init__(
        self,
        *,
        authenticated: bool = True,
        script: list[_FakeEvent] | None = None,
        raise_on_create: Exception | None = None,
    ) -> None:
        self._authenticated = authenticated
        self._script = script or []
        self._raise_on_create = raise_on_create
        self.created_sessions: list[str] = []
        self.deleted_sessions: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get_auth_status(self) -> _FakeAuthStatus:
        return _FakeAuthStatus(
            isAuthenticated=self._authenticated, authType="env",
        )

    async def create_session(self, **kw: Any) -> _FakeSession:
        if self._raise_on_create is not None:
            raise self._raise_on_create
        sid = f"sess-{len(self.created_sessions)}"
        self.created_sessions.append(sid)
        return _FakeSession(sid, self._script)

    async def delete_session(self, session_id: str) -> None:
        self.deleted_sessions.append(session_id)


def _event(evt_type: str, data: Any = None) -> _FakeEvent:
    return _FakeEvent(type=_FakeEventType(evt_type), data=data)


def _success_script(content: str) -> list[_FakeEvent]:
    """The shape of events a normal Copilot success run emits."""
    return [
        _event("assistant.message", _FakeAssistantMessageData(content=content)),
        _event("assistant.usage", _FakeAssistantUsageData(
            input_tokens=42, output_tokens=7,
        )),
        _event("session.idle"),
    ]


def _make_spec(**kw: Any) -> AgentSpec:
    """Build an AgentSpec with the schema below by default."""
    defaults = dict(
        name="test-agent",
        charter="Be a helpful JSON-emitting test agent.",
        response_model=_TinyOut,
        model="fake",   # → provider uses its DEFAULT_COPILOT_MODEL
    )
    defaults.update(kw)
    return AgentSpec(**defaults)


def _make_call(spec: AgentSpec, *, user_message: str = "hello") -> AgentCall:
    return AgentCall(spec=spec, user_message=user_message)


class _TinyOut(BaseModel):
    answer: str


# Make sure token-present helper doesn't depend on real env during tests.
@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for k in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    # Set just one so __post_init__'s token-presence check passes without
    # any of the real-env values leaking in.
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "fake-token-for-tests")


# ---- success ------------------------------------------------------------


async def test_success_path_parses_json_into_schema():
    fake = _FakeCopilotClient(
        script=_success_script('{"answer": "ok"}'),
    )
    provider = CopilotProvider(client=fake)
    spec = _make_spec()
    outcome = await provider.invoke(_make_call(spec))
    await provider.aclose()

    assert isinstance(outcome, Success)
    assert outcome.value["parsed"] == {"answer": "ok"}
    assert outcome.value["agent"] == "test-agent"
    # Receipts both on the typed .receipts field AND in .value (back-compat).
    assert len(outcome.receipts) == 1
    r = outcome.receipts[0]
    assert r["model"] == "claude-sonnet-4.5"   # DEFAULT_COPILOT_MODEL
    assert r["input_tokens"] == 42
    assert r["output_tokens"] == 7
    # Session was created exactly once AND deleted (no leak).
    assert len(fake.created_sessions) == 1
    assert fake.deleted_sessions == fake.created_sessions


async def test_success_passes_through_per_call_model_override():
    """An AgentSpec with a real model name should override the provider default."""
    fake = _FakeCopilotClient(
        script=_success_script('{"answer": "ok"}'),
    )
    provider = CopilotProvider(client=fake)
    spec = _make_spec(model="gpt-5.2")
    outcome = await provider.invoke(_make_call(spec))
    await provider.aclose()

    assert isinstance(outcome, Success)
    assert outcome.receipts[0]["model"] == "gpt-5.2"


async def test_no_schema_path_returns_text_only():
    """When AgentSpec.response_model is None, we should pass the raw text
    through without trying to parse it as JSON."""
    fake = _FakeCopilotClient(
        script=_success_script("hello there, not JSON"),
    )
    provider = CopilotProvider(client=fake)
    # Build a spec with no response_model. AgentSpec requires response_model,
    # so the only way to NOT have a schema is to mutate the dataclass field.
    # That's not realistic in production; we test it by directly invoking the
    # provider's path that's reached when getattr(spec, "response_model", None)
    # returns None.
    class _NoSchemaSpec:
        name = "no-schema"
        charter = "char"
        model = "fake"
        response_model = None
    call = AgentCall(spec=_NoSchemaSpec(), user_message="hi")  # type: ignore[arg-type]
    outcome = await provider.invoke(call)
    await provider.aclose()

    assert isinstance(outcome, Success)
    assert outcome.value["parsed"] == {"text": "hello there, not JSON"}


# ---- bad output ---------------------------------------------------------


async def test_malformed_json_returns_bad_output_not_retryable():
    """Mahler-A: BadOutput is NEVER retried. The provider must emit a
    BadOutput, not a RetryableFailure, on parse failure."""
    fake = _FakeCopilotClient(
        script=_success_script("this is not JSON at all"),
    )
    provider = CopilotProvider(client=fake)
    outcome = await provider.invoke(_make_call(_make_spec()))
    await provider.aclose()

    assert isinstance(outcome, BadOutput)
    assert outcome.raw_output == "this is not JSON at all"
    assert any("json decode" in e for e in outcome.validation_errors)


async def test_schema_violation_returns_bad_output():
    """Valid JSON but wrong shape → BadOutput with pydantic errors."""
    fake = _FakeCopilotClient(
        script=_success_script('{"wrong_field": "value"}'),
    )
    provider = CopilotProvider(client=fake)
    outcome = await provider.invoke(_make_call(_make_spec()))
    await provider.aclose()

    assert isinstance(outcome, BadOutput)
    # _TinyOut requires `answer` — pydantic should flag it missing.
    assert any("answer" in e for e in outcome.validation_errors)


async def test_empty_response_returns_bad_output():
    """session.idle with no assistant.message in the script → BadOutput."""
    fake = _FakeCopilotClient(script=[_event("session.idle")])
    provider = CopilotProvider(client=fake)
    outcome = await provider.invoke(_make_call(_make_spec()))
    await provider.aclose()

    assert isinstance(outcome, BadOutput)
    assert outcome.raw_output == ""
    assert any("no assistant.message" in e for e in outcome.validation_errors)


# ---- retryable + auth ---------------------------------------------------


async def test_session_error_event_returns_retryable_failure():
    """An `error` / `session.error` event before idle → RetryableFailure
    with the provider's default retry-after."""
    fake = _FakeCopilotClient(
        script=[
            _event("session.error", _FakeSessionErrorData(
                message="upstream provider 503",
            )),
        ],
    )
    provider = CopilotProvider(client=fake)
    outcome = await provider.invoke(_make_call(_make_spec()))
    await provider.aclose()

    assert isinstance(outcome, RetryableFailure)
    assert outcome.error_kind == "provider_unavailable"
    assert "upstream provider 503" in outcome.message
    assert outcome.after > 0


async def test_unauthenticated_returns_needs_human_provider_auth():
    """If get_auth_status reports isAuthenticated=False, we shortcut to
    NeedsHuman with gate=provider_auth — no point trying create_session."""
    fake = _FakeCopilotClient(authenticated=False)
    provider = CopilotProvider(client=fake)
    outcome = await provider.invoke(_make_call(_make_spec()))
    await provider.aclose()

    assert isinstance(outcome, NeedsHuman)
    assert outcome.gate == "provider_auth"
    # No session should have been created on the auth-failed path.
    assert fake.created_sessions == []


# ---- token presence helper ----------------------------------------------


def test_copilot_token_present_with_env(monkeypatch):
    """Token-presence helper honours all three env vars."""
    for k in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert _copilot_token_present() is False
    monkeypatch.setenv("GH_TOKEN", "gho_xxx")
    assert _copilot_token_present() is True


def test_copilot_provider_rejects_missing_token(monkeypatch):
    """Constructing CopilotProvider with no client AND no token in env
    should fail with a precise error, not a wrapped SDK exception."""
    for k in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError) as exc:
        CopilotProvider()
    assert "COPILOT_GITHUB_TOKEN" in str(exc.value)


# ---- prompt-shaping unit tests ------------------------------------------


def test_build_prompt_no_schema_concatenates_charter_and_user():
    out = _build_prompt(
        charter="Be helpful.", user_message="Hello!", schema=None,
    )
    assert "Be helpful." in out
    assert "Hello!" in out
    assert "Schema:" not in out
    assert "JSON object" not in out


def test_build_prompt_with_schema_appends_schema_instruction():
    out = _build_prompt(
        charter="C.", user_message="U.", schema=_TinyOut,
    )
    assert "C." in out
    assert "U." in out
    assert "Respond with ONLY a single JSON object" in out
    assert "Schema:" in out
    assert "answer" in out  # _TinyOut's field name


# ---- default_provider integration --------------------------------------


def test_default_provider_prefers_copilot_when_token_set(monkeypatch):
    """COPILOT_GITHUB_TOKEN > ANTHROPIC_API_KEY > OPENAI_API_KEY."""
    from requiem.providers import default_provider

    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "gho_x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-x")

    # CopilotProvider needs its client constructed lazily — bypass that
    # by injecting a stub at the client kwarg.
    # We can't pass kwargs through default_provider easily, so the
    # cleanest test is: assert the chosen TYPE.
    # default_provider() forwards **kw to the constructor, and our
    # constructor accepts `client=...` for tests.
    fake = _FakeCopilotClient(script=_success_script('{"answer": "x"}'))
    chosen = default_provider(client=fake)
    assert type(chosen).__name__ == "CopilotProvider"
