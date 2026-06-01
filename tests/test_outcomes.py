from requiem.outcomes import (
    BadOutput,
    Cancelled,
    NeedsHuman,
    PermanentFailure,
    RetryableFailure,
    Success,
    outcome_from_dict,
    outcome_kind,
    outcome_to_dict,
)


def test_six_variants_tagged():
    cases = [
        (Success(), "success"),
        (RetryableFailure(retry_key="k", error_kind="t", message="m"), "retryable_failure"),
        (PermanentFailure(error_kind="x", message="y"), "permanent_failure"),
        (BadOutput(error_kind="schema_mismatch", validation_errors=("e1",)), "bad_output"),
        (NeedsHuman(gate="g", prompt="p", options=("a", "b")), "needs_human"),
        (Cancelled(cause="operator", at_step="s"), "cancelled"),
    ]
    for o, expected in cases:
        assert outcome_kind(o) == expected
        assert outcome_to_dict(o)["kind"] == expected


def test_roundtrip_preserves_tuple_fields():
    o = Success(value={"k": 1}, inspected_artifacts=("file:a", "file:b"))
    d = outcome_to_dict(o)
    r = outcome_from_dict(d)
    assert isinstance(r, Success)
    assert r.inspected_artifacts == ("file:a", "file:b")


def test_bad_output_roundtrip():
    o = BadOutput(
        error_kind="schema_mismatch",
        validation_errors=("missing field x", "wrong type for y"),
        raw_output='{"bad": true}',
    )
    d = outcome_to_dict(o)
    assert d["kind"] == "bad_output"
    r = outcome_from_dict(d)
    assert isinstance(r, BadOutput)
    assert r.validation_errors == ("missing field x", "wrong type for y")
    assert r.raw_output == '{"bad": true}'


def test_unknown_kind_raises():
    import pytest
    with pytest.raises(ValueError, match="unknown outcome kind"):
        outcome_from_dict({"kind": "not_a_kind"})
