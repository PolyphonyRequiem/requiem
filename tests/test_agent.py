import pytest
from pydantic import BaseModel

from requiem.agent import AgentCall, AgentSpec, FakeProvider
from requiem.outcomes import BadOutput, PermanentFailure, RetryableFailure, Success


class Reply(BaseModel):
    ok: bool
    msg: str


SPEC = AgentSpec(name="t", charter="test", response_model=Reply)


async def test_happy_path_returns_success():
    p = FakeProvider(scripts={"t": [{"ok": True, "msg": "hi"}]})
    out = await p.invoke(AgentCall(spec=SPEC, user_message="x"))
    assert isinstance(out, Success)
    assert out.value["parsed"] == {"ok": True, "msg": "hi"}


async def test_schema_mismatch_returns_bad_output():
    """The Mahler-A fix: validation failures → BadOutput, not PermanentFailure."""
    p = FakeProvider(scripts={"t": [{"ok": "not a bool"}]})
    out = await p.invoke(AgentCall(spec=SPEC, user_message="x"))
    assert isinstance(out, BadOutput)
    assert out.error_kind == "schema_mismatch"
    assert len(out.validation_errors) >= 1


async def test_unscripted_agent_returns_permanent_failure():
    p = FakeProvider(scripts={})
    out = await p.invoke(AgentCall(spec=SPEC, user_message="x"))
    assert isinstance(out, PermanentFailure)
    assert out.error_kind == "fake.unscripted"


async def test_scripted_outcome_passed_through():
    out_in = RetryableFailure(retry_key="k", error_kind="t", message="fail")
    p = FakeProvider(scripts={"t": [out_in]})
    out = await p.invoke(AgentCall(spec=SPEC, user_message="x"))
    assert out is out_in


async def test_exhausted_script():
    p = FakeProvider(scripts={"t": [{"ok": True, "msg": "hi"}]})
    await p.invoke(AgentCall(spec=SPEC, user_message="x"))
    second = await p.invoke(AgentCall(spec=SPEC, user_message="x"))
    assert isinstance(second, PermanentFailure)
    assert second.error_kind == "fake.exhausted"
