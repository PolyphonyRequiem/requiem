"""The requiem-* delivery fleet distributions are a *contract artifact*, not
inert docs: each profile's `handoff-receipt` skill is the emit side of the wire
contract whose consume side is `requiem.handoff.parse_handoff`. These tests keep
the two in lockstep and guard against the contract drifting between fleet
members.

What they prove:
  * every distribution.yaml parses and names its profile (`name` is the one
    required field) matching its directory;
  * the canonical receipt example documented in each skill is *provably*
    contract-valid — it round-trips through the real `parse_handoff`;
  * the documented example matches the golden fixture's identity fields, so the
    skill and `tests/fixtures/handoff_v1_golden.json` cannot diverge silently;
  * the receipt skill is byte-identical across all three profiles (single-source
    wire contract — a fleet member cannot quietly speak a different dialect);
  * config.yaml ships Manual orchestration (`auto_decompose: false`) so Hermes'
    auto-decomposer never fans out behind requiem's back.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from requiem.handoff import HANDOFF_SCHEMA_VERSION, parse_handoff

_FLEET = Path(__file__).resolve().parent.parent / "fleet"
_PROFILES = ("requiem-implementer", "requiem-reviewer", "requiem-closer")
_GOLDEN = Path(__file__).resolve().parent / "fixtures" / "handoff_v1_golden.json"


def _skill_text(profile: str) -> str:
    return (_FLEET / profile / "skills" / "handoff-receipt" / "SKILL.md").read_text(
        encoding="utf-8"
    )


def _first_json_block(markdown: str) -> dict:
    """Extract the first ```json fenced block from a markdown document."""
    m = re.search(r"```json\s*\n(.*?)\n```", markdown, re.DOTALL)
    assert m is not None, "skill must document a ```json receipt example"
    return json.loads(m.group(1))


def test_every_profile_directory_exists():
    for profile in _PROFILES:
        assert (_FLEET / profile).is_dir(), f"missing fleet profile {profile}"


def test_distribution_manifest_parses_and_names_profile():
    for profile in _PROFILES:
        manifest = yaml.safe_load(
            (_FLEET / profile / "distribution.yaml").read_text(encoding="utf-8")
        )
        # `name` is the only required field and becomes the installed profile
        # name; it must match the directory so the role->profile mapping resolves.
        assert manifest["name"] == profile
        assert isinstance(manifest.get("description"), str) and manifest["description"]


def test_documented_receipt_is_contract_valid():
    """The receipt example each skill documents must parse cleanly through the
    REAL consumer — the profile's promise and requiem's parser cannot diverge."""
    for profile in _PROFILES:
        example = _first_json_block(_skill_text(profile))
        meta = parse_handoff(example)
        assert meta.schema_version == HANDOFF_SCHEMA_VERSION
        assert meta.leaf_id and meta.root_item and meta.plan_hash
        assert meta.worker_profile


def test_documented_receipt_matches_golden_identity():
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    example = _first_json_block(_skill_text("requiem-implementer"))
    for field in ("schema_version", "leaf_id", "root_item", "plan_hash"):
        assert example[field] == golden[field], f"{field} drifted from golden"


def test_receipt_skill_is_byte_identical_across_fleet():
    """The wire contract is single-sourced: a divergent copy is drift waiting to
    happen, so the receipt skill must be identical in every profile."""
    texts = {p: _skill_text(p) for p in _PROFILES}
    canonical = texts["requiem-implementer"]
    for profile, text in texts.items():
        assert text == canonical, f"{profile} receipt skill drifted from canonical"


def test_config_ships_manual_orchestration():
    for profile in _PROFILES:
        cfg = yaml.safe_load(
            (_FLEET / profile / "config.yaml").read_text(encoding="utf-8")
        )
        # requiem is the ONLY decomposition authority — the Hermes auto-decomposer
        # must be off so a triage task is never fanned out behind requiem's back.
        assert cfg["kanban"]["auto_decompose"] is False
