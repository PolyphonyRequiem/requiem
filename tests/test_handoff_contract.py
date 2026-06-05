"""Handoff wire-contract tests (ADR-0017 §4).

This is the cross-process contract test co-owned by the kanban-delivery and
profile-distribution tracks. The golden fixture is the canonical shape a
``requiem-*`` worker emits; if either side changes the wire shape, the fixture
AND these assertions move together — that is the guard against the
Tchaikovsky-class drift the two-specialist split could otherwise reintroduce.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from requiem.handoff import (
    HANDOFF_SCHEMA_VERSION,
    HandoffError,
    HandoffMetadata,
    extract_handoff,
    parse_handoff,
)

GOLDEN = Path(__file__).parent / "fixtures" / "handoff_v1_golden.json"


def _golden() -> dict:
    raw = json.loads(GOLDEN.read_text(encoding="utf-8"))
    raw.pop("_comment", None)
    return raw


# ---- golden fixture ---------------------------------------------------


def test_golden_fixture_parses_to_expected_object():
    h = parse_handoff(_golden())

    assert h.schema_version == HANDOFF_SCHEMA_VERSION
    assert h.leaf_id == "22002"
    assert h.root_item == "880"
    assert h.plan_hash.startswith("a1b2c3")
    assert h.worker_profile == "requiem-implementer"
    assert h.branch == "impl/880-22002"
    assert h.commit_sha.startswith("9f8e7d6c")
    assert h.pr_url.endswith("/pull/417")
    assert h.changed_files[0] == "src/widgets/auth/reset.py"
    assert len(h.changed_files) == 3
    assert len(h.tests_run) == 2
    assert h.worker_profile_version == "1.4.2"


def test_golden_round_trips_through_to_dict():
    h = parse_handoff(_golden())
    again = parse_handoff(h.to_dict())
    assert again == h


# ---- required identity fields (strict) --------------------------------


@pytest.mark.parametrize("field", ["leaf_id", "root_item", "plan_hash", "worker_profile"])
def test_missing_required_identity_field_fails_closed(field):
    raw = _golden()
    del raw[field]
    with pytest.raises(HandoffError) as exc:
        parse_handoff(raw)
    assert exc.value.field == field


@pytest.mark.parametrize("field", ["leaf_id", "root_item", "plan_hash", "worker_profile"])
def test_empty_required_identity_field_fails_closed(field):
    raw = _golden()
    raw[field] = "   "
    with pytest.raises(HandoffError) as exc:
        parse_handoff(raw)
    assert exc.value.field == field


# ---- schema_version (fail closed) -------------------------------------


def test_missing_schema_version_fails_closed():
    raw = _golden()
    del raw["schema_version"]
    with pytest.raises(HandoffError) as exc:
        parse_handoff(raw)
    assert exc.value.field == "schema_version"


def test_future_schema_version_fails_closed():
    raw = _golden()
    raw["schema_version"] = HANDOFF_SCHEMA_VERSION + 1
    with pytest.raises(HandoffError) as exc:
        parse_handoff(raw)
    assert exc.value.field == "schema_version"


def test_bool_schema_version_rejected():
    raw = _golden()
    raw["schema_version"] = True  # bool is an int subclass — must be rejected
    with pytest.raises(HandoffError):
        parse_handoff(raw)


# ---- optional evidence fields (lenient) -------------------------------


def test_evidence_fields_may_be_absent():
    minimal = {
        "schema_version": 1,
        "leaf_id": "1",
        "root_item": "2",
        "plan_hash": "h",
        "worker_profile": "requiem-implementer",
    }
    h = parse_handoff(minimal)
    assert h.branch is None
    assert h.pr_url is None
    assert h.changed_files == ()
    assert h.tests_run == ()


def test_changed_files_must_be_list_of_strings():
    raw = _golden()
    raw["changed_files"] = "src/one.py"  # a bare string is not a file list
    with pytest.raises(HandoffError) as exc:
        parse_handoff(raw)
    assert exc.value.field == "changed_files"


def test_unknown_extra_keys_tolerated():
    raw = _golden()
    raw["future_field"] = {"anything": 1}
    h = parse_handoff(raw)
    assert h.leaf_id == "22002"


# ---- extraction from a run row ----------------------------------------


def test_extract_handoff_from_run_raw():
    run_raw = {"metadata": _golden(), "other": "ignored"}
    h = extract_handoff(run_raw)
    assert isinstance(h, HandoffMetadata)
    assert h.leaf_id == "22002"


def test_extract_handoff_absent_metadata_returns_none():
    assert extract_handoff({"summary": "did stuff, no metadata"}) is None


def test_extract_handoff_propagates_contract_violation():
    # A present-but-broken metadata blob is a contract violation, not a
    # silent "no evidence" — must raise, not return None.
    with pytest.raises(HandoffError):
        extract_handoff({"metadata": {"schema_version": 99}})


def test_non_mapping_payload_rejected():
    with pytest.raises(HandoffError):
        parse_handoff(["not", "a", "mapping"])  # type: ignore[arg-type]
