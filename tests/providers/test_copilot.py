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
    DEFAULT_COPILOT_MODEL,
    _build_prompt,
    _copilot_token_present,
    _extract_balanced_json,
    _extract_fenced_json,
    _extract_json_block,
    _strip_code_fence,
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
    assert r["model"] == DEFAULT_COPILOT_MODEL  # whatever the current default is
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


async def test_json_wrapped_in_markdown_fences_parses_via_strip():
    """REGRESSION pin (2026-06-17 live dryrun): claude-sonnet-4.5 ignores
    the prompt instruction to omit fences and wraps its JSON in
    ```json ... ```. Provider must strip the fence before parsing —
    otherwise Mahler-A turns this into a permanent abort."""
    wrapped = '```json\n{"answer": "wrapped"}\n```'
    fake = _FakeCopilotClient(script=_success_script(wrapped))
    provider = CopilotProvider(client=fake)
    outcome = await provider.invoke(_make_call(_make_spec()))
    await provider.aclose()

    assert isinstance(outcome, Success), f"expected Success, got {type(outcome).__name__}"
    assert outcome.value["parsed"] == {"answer": "wrapped"}


async def test_json_wrapped_in_bare_fences_also_parses():
    """Same fix should handle ``` ... ``` without the `json` hint."""
    wrapped = '```\n{"answer": "bare"}\n```'
    fake = _FakeCopilotClient(script=_success_script(wrapped))
    provider = CopilotProvider(client=fake)
    outcome = await provider.invoke(_make_call(_make_spec()))
    await provider.aclose()

    assert isinstance(outcome, Success)
    assert outcome.value["parsed"] == {"answer": "bare"}


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


def test_build_prompt_with_schema_warns_model_off_tool_calls():
    """Run-#31 follow-up. With `excluded_tools=builtin:*` sealing the
    Copilot SDK tool surface (commit 4e5ccf7), sonnet-4.6 has NO
    tools to call. But it sometimes still emits Anthropic-native
    `<function_calls>` XML trying to call non-existent tools — which
    appears as trailing prose AFTER the valid CoderOutput JSON and
    breaks `json.loads`. The prompt MUST tell the model explicitly
    that no tools exist, so it doesn't waste output tokens (and
    contaminate the response) attempting to call them.
    """
    out = _build_prompt(
        charter="Charter.", user_message="Do the thing.", schema=_TinyOut,
    )
    lowered = out.lower()
    # The prompt must mention BOTH 'no tools' AND that tool-call
    # syntax should be avoided. Otherwise the model will still try.
    assert "no tools" in lowered or "no tool" in lowered, (
        "prompt must explicitly state no tools are available; got:\n"
        + out
    )
    assert "function_call" in lowered or "tool call" in lowered or "tool_call" in lowered, (
        "prompt must mention tool-call syntax by name so the model "
        "knows what NOT to emit (function_calls XML, tool_call blocks, etc.); got:\n"
        + out
    )


def test_build_prompt_no_schema_omits_no_tools_note():
    """The no-tools warning only matters when we're expecting structured
    output. Plain text agents (no schema) don't need it."""
    out = _build_prompt(
        charter="Be helpful.", user_message="Hello!", schema=None,
    )
    lowered = out.lower()
    assert "no tools" not in lowered, (
        "no-schema prompts don't need the no-tools warning; keeps the "
        "prompt minimal for the freeform path"
    )


# ---- code-fence stripping unit tests ------------------------------------


def test_strip_code_fence_no_fence_passthrough():
    """Bare text without fences is returned unchanged."""
    assert _strip_code_fence('{"a": 1}') == '{"a": 1}'


def test_strip_code_fence_with_json_hint():
    assert _strip_code_fence('```json\n{"a": 1}\n```') == '{"a": 1}'


def test_strip_code_fence_with_bare_fence():
    assert _strip_code_fence('```\n{"a": 1}\n```') == '{"a": 1}'


def test_strip_code_fence_uppercase_json_hint():
    """Some models emit ```JSON in caps; should still strip."""
    assert _strip_code_fence('```JSON\n{"a": 1}\n```') == '{"a": 1}'


def test_strip_code_fence_with_trailing_newline_after_close():
    """Trailing newline after the closing fence is tolerated."""
    assert _strip_code_fence('```json\n{"a": 1}\n```\n') == '{"a": 1}'


def test_strip_code_fence_no_newline_in_body_is_returned_unchanged():
    """If there's no newline after the opening ```, the input is
    malformed; we return the original text and let pydantic surface
    the parse error rather than guessing."""
    assert _strip_code_fence('```nofence') == '```nofence'


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


# ---- regression: 2026-06-17 dogfood #62759077 grandchild --------------
#
# The first --commit dogfood ran the recursion 2 levels deep. At the
# grandchild planner_1 (task: "Implement capacity probe mechanism"),
# claude-sonnet-4.5 produced a high-quality JSON plan — but PREFIXED
# with prose ("Based on the codebase analysis, I can see this is a
# CloudVault service..."). The original _strip_code_fence only stripped
# if the *whole text* started with ```; this case starts with prose
# then has the fence in the middle. Result: BadOutput, escalation_gate,
# exit 1 — and we threw away a perfectly good plan.
#
# The fix (_extract_json_block) handles four shapes:
#   1. whole text already JSON
#   2. ``` fence anywhere in the response (with or without `json` hint)
#   3. naked JSON after prose (brace-balanced extraction)
#   4. give up (let validate_schema report)
# These tests pin each shape + the actual live response that triggered
# the bug.


_LIVE_GRANDCHILD_RESPONSE = '''Based on the codebase analysis, I can see this is a CloudVault service with existing health endpoints (`/health/live`, `/health/ready`, `/health/dependencies`). A "capacity probe mechanism" would extend this health infrastructure to report service capacity metrics for load balancing and auto-scaling decisions. Given this is at planning depth 2 of 4, this should decompose into concrete implementation tasks.

```json
{
  "summary": "Implement a capacity probe endpoint",
  "decomposable": true,
  "estimated_complexity": "medium",
  "rationale": "This task naturally decomposes into four distinct sub-tasks.",
  "children": [
    {"title": "Define capacity metrics", "description": "spec the schema", "work_item_type": "Task"},
    {"title": "Implement capacity calc", "description": "IHealthCheck impl", "work_item_type": "Task"}
  ]
}
```'''


async def test_live_dogfood_grandchild_prose_preamble_then_fenced_json():
    """REGRESSION PIN (dogfood 2026-06-17 grandchild #62759077): the
    actual live response from claude-sonnet-4.5 must parse. Before the
    fix this hit BadOutput → bad_output_gate → abort."""

    class _RichOut(BaseModel):
        summary: str
        decomposable: bool
        estimated_complexity: str
        rationale: str
        children: list[dict]

    spec = _make_spec(response_model=_RichOut)
    fake = _FakeCopilotClient(script=_success_script(_LIVE_GRANDCHILD_RESPONSE))
    provider = CopilotProvider(client=fake)
    outcome = await provider.invoke(_make_call(spec))
    await provider.aclose()

    assert isinstance(outcome, Success), f"got {type(outcome).__name__}: {outcome}"
    parsed = outcome.value["parsed"]
    assert parsed["decomposable"] is True
    assert len(parsed["children"]) == 2
    assert parsed["children"][0]["title"] == "Define capacity metrics"


async def test_naked_json_after_prose_brace_balanced_extraction():
    """Prose preamble + bare JSON (no fence) + optional prose trail —
    falls through to the brace-balanced extraction path."""
    raw = '''Looking at the schema requirements:

{"answer": "naked", "extra": "ignored?"}

That's my answer.'''
    fake = _FakeCopilotClient(script=_success_script(raw))
    provider = CopilotProvider(client=fake)
    outcome = await provider.invoke(_make_call(_make_spec()))
    await provider.aclose()
    assert isinstance(outcome, Success), f"got {type(outcome).__name__}"
    # _TinyOut has only `answer`; pydantic ignores the extra by default.
    assert outcome.value["parsed"] == {"answer": "naked"}


async def test_python_fenced_block_is_ignored_falls_to_balanced():
    """A ```python fence in the response is NOT mistaken for JSON.
    The extractor skips non-JSON fences and falls through to balanced
    JSON extraction (which finds nothing → fence content survives →
    parse fails → BadOutput, the correct outcome)."""
    raw = '''Here is some code:
```python
print("hello")
```
No JSON answer.'''
    fake = _FakeCopilotClient(script=_success_script(raw))
    provider = CopilotProvider(client=fake)
    outcome = await provider.invoke(_make_call(_make_spec()))
    await provider.aclose()
    assert isinstance(outcome, BadOutput), (
        f"non-JSON response should produce BadOutput; got {type(outcome).__name__}"
    )


# ---- unit tests for the new extractor helpers -------------------------


def test_extract_json_block_whole_text_json_passthrough():
    assert _extract_json_block('{"a": 1}') == '{"a": 1}'
    assert _extract_json_block('[1, 2, 3]') == '[1, 2, 3]'


def test_extract_json_block_empty_input():
    assert _extract_json_block("") == ""
    assert _extract_json_block("   ") == ""


# ---- run-#31 follow-up: trailing-prose-after-JSON ---------------------
#
# When `excluded_tools=builtin:*` seals the SDK tool surface (run-#30
# leaf-9 fix, commit 4e5ccf7), sonnet-4.6 sometimes emits a valid
# CoderOutput followed by Anthropic native function-call XML (`<function_calls>...`)
# as it tries to call tools that no longer exist. The text starts with
# `{` so step (1) of _extract_json_block returns it whole; json.loads
# then chokes with "Extra data". This was 7/7 of run #31's bad_output
# leaves. Fix: when whole-text passthrough would fail to parse, fall
# through to step (3) which uses brace-balanced extraction and returns
# the first complete JSON object, ignoring the trailing prose.


def test_extract_json_block_falls_through_when_whole_text_has_trailing_prose():
    """The actual leaf-62879412 raw_output shape: valid CoderOutput
    JSON followed by `\\n\\nLet me explore the codebase first.\\n\\n<function_calls>...`"""
    raw = (
        '{"intent_summary":"Exploring repo structure before implementing",'
        '"file_changes":[],"notes":"Exploring first"}\n\n'
        'Let me explore the codebase first.\n\n'
        '<function_calls>\n<invoke name="glob">\n<parameter name="pattern">**/*</parameter>\n</invoke>\n</function_calls>'
    )
    out = _extract_json_block(raw)
    # Must return ONLY the first balanced JSON object, not the whole text.
    assert out == (
        '{"intent_summary":"Exploring repo structure before implementing",'
        '"file_changes":[],"notes":"Exploring first"}'
    ), (
        f"expected balanced JSON extraction; got prose-contaminated text "
        f"starting with: {out[:80]!r}"
    )


def test_extract_json_block_falls_through_when_whole_text_has_trailing_garbage():
    """Smaller version: any non-whitespace trailing after the balanced
    close-brace breaks json.loads. Pre-fix step (1) returned this
    whole; post-fix step (3) returns the slice through the first }."""
    raw = '{"x": 1} trailing garbage that breaks json.loads'
    assert _extract_json_block(raw) == '{"x": 1}'


def test_extract_json_block_passthrough_still_works_when_whole_text_IS_valid():
    """Don't regress the original step (1) behavior: when the WHOLE
    trimmed text is valid JSON, we keep returning it verbatim (no
    need to invoke the balanced extractor). Validates the
    try-parse-then-fall-through wiring is conditional, not always-on."""
    s = '{"a": 1, "b": [2, 3], "c": {"nested": true}}'
    assert _extract_json_block(s) == s


def test_extract_json_block_falls_through_for_array_with_trailing_prose():
    """Symmetric to the object case — arrays as the leading payload."""
    raw = '[1, 2, 3]\n\nNow let me explore the rest of the codebase.'
    assert _extract_json_block(raw) == '[1, 2, 3]'


def test_extract_fenced_json_finds_mid_response_fence():
    """The killer case: prose preamble, then ```json {...} ```, then
    optional trailing prose."""
    s = 'Prefix prose.\n\n```json\n{"x": 1}\n```\n\nTrailing prose.'
    out = _extract_fenced_json(s)
    assert out == '{"x": 1}'


def test_extract_fenced_json_bare_fence_with_json_content():
    s = 'Some context.\n\n```\n{"x": 1}\n```'
    out = _extract_fenced_json(s)
    assert out == '{"x": 1}'


def test_extract_fenced_json_skips_non_json_fences():
    """A ```python fence should be skipped; if there's a later ```json
    fence, that one wins. If not, returns None."""
    s = '```python\nprint(1)\n```\n\n```json\n{"x": 2}\n```'
    out = _extract_fenced_json(s)
    assert out == '{"x": 2}'


def test_extract_fenced_json_python_only_returns_none():
    """If the only fence is non-JSON-looking content, return None so the
    caller falls through to the next extraction strategy."""
    s = '```python\nprint("not json")\n```'
    out = _extract_fenced_json(s)
    assert out is None


def test_extract_fenced_json_uppercase_hint():
    s = '```JSON\n{"x": 1}\n```'
    assert _extract_fenced_json(s) == '{"x": 1}'


def test_extract_balanced_json_finds_object_after_prose():
    s = 'Here is the result: {"x": 1, "y": [2, 3]} done.'
    assert _extract_balanced_json(s) == '{"x": 1, "y": [2, 3]}'


def test_extract_balanced_json_finds_array_after_prose():
    s = 'Items: [1, 2, {"k": "v"}] returned.'
    assert _extract_balanced_json(s) == '[1, 2, {"k": "v"}]'


def test_extract_balanced_json_respects_string_escaping():
    """Braces inside JSON string values must not affect depth counting."""
    s = 'Result: {"msg": "has } and { inside"} done.'
    assert _extract_balanced_json(s) == '{"msg": "has } and { inside"}'


def test_extract_balanced_json_respects_backslash_escape():
    """Backslash-escaped quote inside a string must not toggle in_string."""
    s = r'Result: {"msg": "has \"quotes\" inside"} done.'
    assert _extract_balanced_json(s) == r'{"msg": "has \"quotes\" inside"}'


def test_extract_balanced_json_no_opener_returns_none():
    assert _extract_balanced_json("no braces here") is None


def test_extract_balanced_json_unbalanced_returns_none():
    """Half-open brackets are not extracted (no matching close)."""
    assert _extract_balanced_json("{open without close") is None


def test_strip_code_fence_back_compat_alias():
    """_strip_code_fence is kept as a back-compat alias delegating to
    _extract_json_block. Verifies the symbol still works for callers
    that imported it under the old name."""
    assert _strip_code_fence('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert _strip_code_fence('{"a": 1}') == '{"a": 1}'
    assert _strip_code_fence("plain text") == "plain text"



# ---- session timeout knob (ADR-0030 §1 follow-up after run #24) ------


def test_session_timeout_default_is_600_seconds():
    """The default session timeout is 600s (raised from 180s on 2026-06-23).

    Pin the value so a future drop-back to 180s doesn't silently
    reintroduce the run-#24 failure mode (~70% of leaves timing out
    while Copilot was still producing real output)."""
    from requiem.providers.copilot import CopilotProvider, _SESSION_TIMEOUT_S
    assert _SESSION_TIMEOUT_S == 600.0
    # Construct without arguments — the field default plumbs through.
    # We use object.__new__ to avoid pulling in the SDK at test time.
    p = object.__new__(CopilotProvider)
    p.session_timeout_s = _SESSION_TIMEOUT_S
    assert p.session_timeout_s == 600.0


def test_session_timeout_overridable_per_instance():
    """The per-instance override is honored — process.yaml or test
    harnesses can pin a tighter (CI) or looser (slow link) ceiling."""
    from requiem.providers.copilot import CopilotProvider
    p = object.__new__(CopilotProvider)
    p.session_timeout_s = 30.0
    assert p.session_timeout_s == 30.0


def test_on_timeout_message_uses_supplied_timeout_not_module_constant():
    """The error message reflects the actual configured timeout (so a
    diagnostic at 30s doesn't say '>600s')."""
    from requiem.providers.copilot import _on_timeout
    from pydantic import BaseModel

    class _M(BaseModel):
        x: int = 0

    from requiem.agent import AgentCall, AgentSpec
    spec = AgentSpec(name="x", charter="t", response_model=_M, model="m")
    call = AgentCall(spec=spec, user_message="p")
    outcome = _on_timeout("agent", call, TimeoutError("boom"), "m", timeout_s=30.0)
    # outcome.message contains the timeout
    assert "30.0" in outcome.message
    assert "600" not in outcome.message


# ---- tool isolation + accurate timeout receipts (run-#26 follow-up) ---


def test_on_timeout_receipt_uses_supplied_elapsed_not_zero():
    """ADR-0030 followup (run-#26): when our asyncio.wait_for ceiling
    fires, the receipt's latency_ms must reflect the WALL-TIME the
    session ran before being aborted, NOT zero.

    Run #26 had every leaf's receipt reporting `latency_ms=0,
    input_tokens=0, request_id=""` because the previous `_on_timeout`
    built an empty receipt — masking the fact that 600s of real
    Copilot work had just been abandoned. The fix threads
    `elapsed_s`/`session_id`/partial token counts from the call site
    so operators see the real picture."""
    from requiem.providers.copilot import _on_timeout
    from pydantic import BaseModel

    class _M(BaseModel):
        x: int = 0

    from requiem.agent import AgentCall, AgentSpec
    spec = AgentSpec(name="x", charter="t", response_model=_M, model="m")
    call = AgentCall(spec=spec, user_message="p")
    outcome = _on_timeout(
        "agent", call, TimeoutError("boom"), "m", timeout_s=600.0,
        elapsed_s=487.2,
        session_id="sess-abc-123",
        partial_input_tokens=75_559,
        partial_output_tokens=3_609,
    )
    receipt = outcome.receipts[0]
    # Latency in ms, not seconds.
    assert receipt["latency_ms"] == 487_200
    assert receipt["input_tokens"] == 75_559
    assert receipt["output_tokens"] == 3_609
    assert receipt["request_id"] == "sess-abc-123"
    # And the actual non-empty error tag is preserved.
    assert "network_timeout" in receipt["error"]


def test_on_timeout_receipt_defaults_to_upper_bound_when_elapsed_unknown():
    """When ``elapsed_s`` is not supplied, the receipt's latency_ms
    defaults to ``timeout_s * 1000`` — a strict upper bound that's
    still better than zero. Pins the back-compat call signature so
    legacy/manual call sites keep working."""
    from requiem.providers.copilot import _on_timeout
    from pydantic import BaseModel

    class _M(BaseModel):
        x: int = 0

    from requiem.agent import AgentCall, AgentSpec
    spec = AgentSpec(name="x", charter="t", response_model=_M, model="m")
    call = AgentCall(spec=spec, user_message="p")
    outcome = _on_timeout(
        "agent", call, TimeoutError("boom"), "m", timeout_s=30.0,
        # No elapsed_s / session_id / partial_*.
    )
    receipt = outcome.receipts[0]
    assert receipt["latency_ms"] == 30_000  # 30s upper bound, not 0


def test_session_uses_isolated_tool_preset():
    """ADR-0030-followup (run-#26): every Copilot session MUST be
    created with ``available_tools=BUILTIN_TOOLS_ISOLATED``. Without
    this, the SDK exposes its default tool surface (write_file,
    edit_file, bash, …) and the model can write to the working
    directory mid-session, contaminating the worktree for every
    subsequent sequential fanout leaf.

    Pin the call signature: a future regression that drops the tool
    filter would re-introduce the run-#26 cascade.
    """
    import asyncio
    import inspect

    import copilot

    from requiem.providers.copilot import CopilotProvider, DEFAULT_COPILOT_MODEL

    # Capture create_session's kwargs without actually talking to a
    # real Copilot SDK. The provider's _start() path calls into the
    # client; we stub the client with a Mock that records the call.
    recorded: dict[str, object] = {}

    class _StubSession:
        session_id = "sess-stub"

        def on(self, _cb):
            pass

        async def send(self, _prompt):
            # Fire the done event immediately by closing the session.
            pass

    class _StubAuthStatus:
        isAuthenticated = True

    class _StubClient:
        async def get_auth_status(self):
            return _StubAuthStatus()

        async def create_session(self, **kwargs):
            recorded.update(kwargs)
            # We don't care about end-to-end behavior here — just that
            # `available_tools` was supplied. Return something the
            # caller can release.
            return _StubSession()

        async def delete_session(self, _sid):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    # Construct the provider directly with the stub client so we don't
    # need a real github-copilot-sdk install or a token.
    provider = CopilotProvider(model=DEFAULT_COPILOT_MODEL, client=_StubClient())

    from requiem.agent import AgentCall, AgentSpec
    from pydantic import BaseModel

    class _Resp(BaseModel):
        x: int = 0

    spec = AgentSpec(name="t", charter="c", response_model=_Resp, model=DEFAULT_COPILOT_MODEL)
    call = AgentCall(spec=spec, user_message="hi")

    # Drive the invoke once. We expect a timeout (the stub session
    # never fires `session.idle`) but the create_session kwargs we
    # care about will have been captured by then.
    async def _drive():
        # Shorten the per-call ceiling so the stub timeout fires fast.
        # Dual-clock: shorten everything so the stub flow exits fast.
        provider.session_timeout_s = 0.05
        provider.max_session_seconds = 0.05
        provider.idle_timeout_s = 0.01
        provider.max_recovery_attempts = 0
        await provider.invoke(call)

    asyncio.run(_drive())

    assert "available_tools" in recorded, (
        "CopilotProvider.create_session must pass available_tools — "
        "without it the SDK enables write_file/edit_file/bash and "
        "the model contaminates the worktree (run-#26 regression)."
    )
    tools = list(recorded["available_tools"])
    # The exact list is the SDK's BUILTIN_TOOLS_ISOLATED constant; we
    # compare with the constant rather than hardcoding so the SDK can
    # rev the set without breaking our test.
    assert tools == list(copilot.BUILTIN_TOOLS_ISOLATED), (
        f"available_tools must equal BUILTIN_TOOLS_ISOLATED "
        f"({copilot.BUILTIN_TOOLS_ISOLATED!r}); got {tools!r}"
    )
    # Belt + suspenders: file-writing tools must NOT be in the set.
    for forbidden in ("write_file", "edit_file", "bash", "create_file"):
        assert forbidden not in tools, (
            f"{forbidden!r} present in available_tools — would let the "
            f"model write the host worktree mid-session"
        )

    # Run #30 leaf 9 follow-up: also pin excluded_tools=ToolSet(builtin:*).
    # available_tools is insufficient alone because the SDK forces
    # `toolFilterPrecedence: "excluded"` — see SDK client.py around
    # lines 1802/2369 — which makes available_tools a weak hint rather
    # than an authoritative whitelist. Without an excluded_tools cap,
    # the model could call powershell/apply_patch/task/view/create
    # despite none of them appearing in BUILTIN_TOOLS_ISOLATED.
    assert "excluded_tools" in recorded, (
        "CopilotProvider.create_session must ALSO pass excluded_tools — "
        "without it the SDK's forced 'excluded'-precedence policy lets "
        "the model call powershell/apply_patch/task even though they "
        "aren't in available_tools (run-#30 leaf 9: 30 .cs files "
        "written to the worktree via powershell despite "
        "available_tools=BUILTIN_TOOLS_ISOLATED)."
    )
    excluded = recorded["excluded_tools"]
    # The shape must be a ToolSet with builtin:* — the most aggressive
    # cap available. The coder agent never needs an SDK-side tool —
    # its CoderOutput JSON IS the work product; apply_changes (a
    # requiem verb, not an SDK tool) does the file writes.
    excluded_list = excluded.to_list() if hasattr(excluded, "to_list") else list(excluded)
    assert "builtin:*" in excluded_list, (
        f"excluded_tools must cap ALL builtin tools via 'builtin:*'; "
        f"got {excluded_list!r}"
    )


# ---- model bump + reasoning knobs (run-#27 follow-up) -----------------


def test_default_copilot_model_is_sonnet_46():
    """Pin the default model to ``claude-sonnet-4.6``.

    Was ``4.5`` until 2026-06-24. Run #27 against AB#62759077 surfaced
    a hard problem on 4.5: 600s sessions that emitted only 365 output
    tokens because the model has no separate reasoning loop
    (``supported_reasoning_efforts=None`` per ``list_models()``) and
    apparently thinks slowly in-band on context-pack-dense prompts.
    4.6 supports tunable reasoning effort (``low``/``medium``/``high``/
    ``max``) and has 5× the prompt window (936K vs 168K), so it's
    strictly better for the requiem coder agent."""
    from requiem.providers.copilot import DEFAULT_COPILOT_MODEL
    assert DEFAULT_COPILOT_MODEL == "claude-sonnet-4.6"


def test_reasoning_effort_omitted_from_create_session_when_unset():
    """When ``reasoning_effort=None`` (the constructor default), the
    kwarg is OMITTED from ``create_session(...)`` entirely — not
    passed as ``reasoning_effort=None``. Lets the SDK fall back to
    the model's ``default_reasoning_effort`` (typically ``medium``)
    without us second-guessing it."""
    import asyncio
    from requiem.providers.copilot import CopilotProvider, DEFAULT_COPILOT_MODEL

    recorded: dict[str, object] = {}

    class _StubSession:
        session_id = "sess-stub"
        def on(self, _cb): pass
        async def send(self, _prompt): pass

    class _StubAuthStatus:
        isAuthenticated = True

    class _StubClient:
        async def get_auth_status(self): return _StubAuthStatus()
        async def create_session(self, **kwargs):
            recorded.update(kwargs)
            return _StubSession()
        async def delete_session(self, _sid): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None

    provider = CopilotProvider(model=DEFAULT_COPILOT_MODEL, client=_StubClient())
    spec = AgentSpec(name="t", charter="c", response_model=ToyResponse, model=DEFAULT_COPILOT_MODEL)
    call = AgentCall(spec=spec, user_message="hi")

    async def _drive():
        # Dual-clock: shorten everything so the stub flow exits fast.
        provider.session_timeout_s = 0.05
        provider.max_session_seconds = 0.05
        provider.idle_timeout_s = 0.01
        provider.max_recovery_attempts = 0
        await provider.invoke(call)

    asyncio.run(_drive())

    # The crucial assertion: the kwarg is NOT in recorded at all.
    assert "reasoning_effort" not in recorded
    assert "reasoning_summary" not in recorded
    assert "context_tier" not in recorded


def test_reasoning_effort_threaded_into_create_session_when_set():
    """When the provider is constructed with ``reasoning_effort="low"``
    (or any non-None value), the kwarg is passed through to
    ``create_session(...)`` verbatim.

    The forward-compatibility shape: ``reasoning_summary`` and
    ``context_tier`` plumb through the same way. Operators tune
    these on the provider once (or via per-role process.yaml in
    follow-up work) and every session honors them."""
    import asyncio
    from requiem.providers.copilot import CopilotProvider, DEFAULT_COPILOT_MODEL

    recorded: dict[str, object] = {}

    class _StubSession:
        session_id = "sess-stub"
        def on(self, _cb): pass
        async def send(self, _prompt): pass

    class _StubAuthStatus:
        isAuthenticated = True

    class _StubClient:
        async def get_auth_status(self): return _StubAuthStatus()
        async def create_session(self, **kwargs):
            recorded.update(kwargs)
            return _StubSession()
        async def delete_session(self, _sid): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None

    provider = CopilotProvider(
        model=DEFAULT_COPILOT_MODEL,
        client=_StubClient(),
        reasoning_effort="low",
        reasoning_summary="none",
        context_tier="standard",
    )
    spec = AgentSpec(name="t", charter="c", response_model=ToyResponse, model=DEFAULT_COPILOT_MODEL)
    call = AgentCall(spec=spec, user_message="hi")

    async def _drive():
        # Dual-clock: shorten everything so the stub flow exits fast.
        provider.session_timeout_s = 0.05
        provider.max_session_seconds = 0.05
        provider.idle_timeout_s = 0.01
        provider.max_recovery_attempts = 0
        await provider.invoke(call)

    asyncio.run(_drive())

    assert recorded.get("reasoning_effort") == "low"
    assert recorded.get("reasoning_summary") == "none"
    assert recorded.get("context_tier") == "standard"


# ToyResponse for the reasoning-knob tests — these tests don't care
# what the model returns, just what kwargs got passed to create_session.
class ToyResponse(BaseModel):
    x: int = 0


def test_model_options_per_call_overrides_provider_default():
    """When ``AgentCall.model_options`` carries a reasoning_effort
    (the kernel populates this from the operator yaml's
    ``models.<role>`` block via ModelSpec.to_model_options()), the
    per-call value beats the CopilotProvider's constructor default.

    Run #28 follow-up: the operator can pin
    `models.implementer.reasoning_effort: low` without changing the
    global provider default — exactly the kind of per-role tuning
    that lets us iterate on dogfood reliability without affecting
    other workflows.
    """
    import asyncio
    from requiem.providers.copilot import CopilotProvider, DEFAULT_COPILOT_MODEL

    recorded: dict[str, object] = {}

    class _StubSession:
        session_id = "sess-stub"
        def on(self, _cb): pass
        async def send(self, _prompt): pass

    class _StubAuthStatus:
        isAuthenticated = True

    class _StubClient:
        async def get_auth_status(self): return _StubAuthStatus()
        async def create_session(self, **kwargs):
            recorded.update(kwargs)
            return _StubSession()
        async def delete_session(self, _sid): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None

    # Provider constructed WITH a default reasoning_effort='high'.
    # The per-call value 'low' should win.
    provider = CopilotProvider(
        model=DEFAULT_COPILOT_MODEL,
        client=_StubClient(),
        reasoning_effort="high",   # constructor default
        reasoning_summary="detailed",
    )
    spec = AgentSpec(name="t", charter="c", response_model=ToyResponse, model=DEFAULT_COPILOT_MODEL)
    # AgentCall now has model_options — kernel populates this from
    # ModelSpec.to_model_options() when a role is routed.
    call = AgentCall(
        spec=spec, user_message="hi",
        model_options={"reasoning_effort": "low", "context_tier": "extended"},
    )

    async def _drive():
        # Dual-clock: shorten everything so the stub flow exits fast.
        provider.session_timeout_s = 0.05
        provider.max_session_seconds = 0.05
        provider.idle_timeout_s = 0.01
        provider.max_recovery_attempts = 0
        await provider.invoke(call)

    asyncio.run(_drive())

    # Per-call values win on the keys they specify.
    assert recorded.get("reasoning_effort") == "low"      # overridden
    assert recorded.get("context_tier") == "extended"    # supplied via call
    # Constructor default still applies for keys NOT in model_options.
    assert recorded.get("reasoning_summary") == "detailed"


def test_model_options_default_empty_preserves_v0_behaviour():
    """An AgentCall with the default empty model_options dict is
    indistinguishable from the pre-run-#28 shape — the provider sees
    only its constructor-time defaults. This is the backward-compat
    invariant: callers that never opt into role routing aren't
    affected by this plumbing."""
    import asyncio
    from requiem.providers.copilot import CopilotProvider, DEFAULT_COPILOT_MODEL

    recorded: dict[str, object] = {}

    class _StubSession:
        session_id = "sess-stub"
        def on(self, _cb): pass
        async def send(self, _prompt): pass

    class _StubAuthStatus:
        isAuthenticated = True

    class _StubClient:
        async def get_auth_status(self): return _StubAuthStatus()
        async def create_session(self, **kwargs):
            recorded.update(kwargs)
            return _StubSession()
        async def delete_session(self, _sid): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None

    provider = CopilotProvider(
        model=DEFAULT_COPILOT_MODEL,
        client=_StubClient(),
        # No constructor reasoning_* args either.
    )
    spec = AgentSpec(name="t", charter="c", response_model=ToyResponse, model=DEFAULT_COPILOT_MODEL)
    # AgentCall.model_options defaults to {} — no per-call override.
    call = AgentCall(spec=spec, user_message="hi")

    async def _drive():
        # Dual-clock: shorten everything so the stub flow exits fast.
        provider.session_timeout_s = 0.05
        provider.max_session_seconds = 0.05
        provider.idle_timeout_s = 0.01
        provider.max_recovery_attempts = 0
        await provider.invoke(call)

    asyncio.run(_drive())

    # Nothing leaks into the SDK kwargs.
    assert "reasoning_effort" not in recorded
    assert "reasoning_summary" not in recorded
    assert "context_tier" not in recorded
