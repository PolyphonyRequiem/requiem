"""Fleet preflight — the requiem-side, fail-closed gate (ADR-0017 §2).

These exercise the *pure* verdict logic without a live Hermes: the gate must
fail closed on an unbound role, a missing profile, non-Manual orchestration, a
disabled gateway dispatcher, a home that escapes the run root, unauthorized
writable memory, and any pinned-but-unverifiable version/hash. They also cover
the schema-first identity snapshot (repo-local artifact hashing).
"""

from __future__ import annotations

from pathlib import Path

from requiem.fleet_preflight import (
    BASELINE_ROLES,
    ExpectedFleet,
    ExpectedProfile,
    FleetInventory,
    ProfileInventory,
    evaluate_fleet,
    fleet_identity_snapshot,
    profile_artifact_hashes,
    required_profiles,
)
from requiem.process_config import ProcessConfig, RoleBinding

_FLEET = Path(__file__).resolve().parent.parent / "fleet"


def _full_config() -> ProcessConfig:
    return ProcessConfig(
        roles={
            "implementer": RoleBinding(profile="requiem-implementer"),
            "reviewer": RoleBinding(profile="requiem-reviewer"),
            "closer": RoleBinding(profile="requiem-closer"),
        }
    )


def _healthy_profile(name: str, *, home_root: str = "/runs/r1") -> ProfileInventory:
    return ProfileInventory(
        name=name,
        version="0.1.0",
        auto_decompose=False,
        dispatch_in_gateway=True,
        home_path=f"{home_root}/{name}",
        writable_memory_paths=(f"{home_root}/{name}/memories",),
    )


def _healthy_inventory() -> FleetInventory:
    return FleetInventory(
        hermes_version="0.15.1",
        image_digest="sha256:abc",
        run_home_root="/runs/r1",
        profiles={
            "requiem-implementer": _healthy_profile("requiem-implementer"),
            "requiem-reviewer": _healthy_profile("requiem-reviewer"),
            "requiem-closer": _healthy_profile("requiem-closer"),
        },
    )


def _codes(verdict) -> set[str]:
    return {f.code for f in verdict.findings}


def test_healthy_fleet_passes():
    verdict = evaluate_fleet(config=_full_config(), inventory=_healthy_inventory())
    assert verdict.ok, verdict.summary()


def test_unbound_role_fails_closed_even_with_no_roles():
    """An empty roles map must NOT degrade into 'no fleet required'."""
    resolved, findings = required_profiles(ProcessConfig(roles={}))
    assert resolved == {}
    assert {f.code for f in findings} == {"role.unbound"}
    assert len(findings) == len(BASELINE_ROLES)

    verdict = evaluate_fleet(config=ProcessConfig(roles={}),
                             inventory=_healthy_inventory())
    assert not verdict.ok
    assert "role.unbound" in _codes(verdict)


def test_missing_profile_fails_closed():
    inv = _healthy_inventory()
    profiles = dict(inv.profiles)
    del profiles["requiem-closer"]
    inv = FleetInventory(hermes_version=inv.hermes_version,
                         image_digest=inv.image_digest,
                         run_home_root=inv.run_home_root, profiles=profiles)
    verdict = evaluate_fleet(config=_full_config(), inventory=inv)
    assert not verdict.ok
    assert "profile.missing" in _codes(verdict)


def test_non_manual_orchestration_fails_closed():
    inv = _healthy_inventory()
    profiles = dict(inv.profiles)
    bad = profiles["requiem-implementer"]
    profiles["requiem-implementer"] = ProfileInventory(
        name=bad.name, version=bad.version, auto_decompose=True,
        dispatch_in_gateway=True, home_path=bad.home_path,
        writable_memory_paths=bad.writable_memory_paths,
    )
    inv = FleetInventory(run_home_root=inv.run_home_root, profiles=profiles)
    verdict = evaluate_fleet(config=_full_config(), inventory=inv)
    assert "orchestration.not_manual" in _codes(verdict)


def test_dispatch_disabled_fails_closed():
    inv = _healthy_inventory()
    profiles = dict(inv.profiles)
    bad = profiles["requiem-reviewer"]
    profiles["requiem-reviewer"] = ProfileInventory(
        name=bad.name, version=bad.version, auto_decompose=False,
        dispatch_in_gateway=False, home_path=bad.home_path,
        writable_memory_paths=bad.writable_memory_paths,
    )
    inv = FleetInventory(run_home_root=inv.run_home_root, profiles=profiles)
    verdict = evaluate_fleet(config=_full_config(), inventory=inv)
    assert "orchestration.dispatch_disabled" in _codes(verdict)


def test_home_not_run_scoped_fails_closed():
    inv = _healthy_inventory()
    profiles = dict(inv.profiles)
    bad = profiles["requiem-closer"]
    profiles["requiem-closer"] = ProfileInventory(
        name=bad.name, version=bad.version, auto_decompose=False,
        dispatch_in_gateway=True, home_path="/var/global/requiem-closer",
        writable_memory_paths=(),
    )
    inv = FleetInventory(run_home_root="/runs/r1", profiles=profiles)
    verdict = evaluate_fleet(config=_full_config(), inventory=inv)
    assert "home.not_run_scoped" in _codes(verdict)


def test_unauthorized_writable_memory_fails_closed():
    inv = _healthy_inventory()
    profiles = dict(inv.profiles)
    bad = profiles["requiem-implementer"]
    profiles["requiem-implementer"] = ProfileInventory(
        name=bad.name, version=bad.version, auto_decompose=False,
        dispatch_in_gateway=True, home_path=bad.home_path,
        writable_memory_paths=("/var/global/shared-memory",),
    )
    inv = FleetInventory(run_home_root="/runs/r1", profiles=profiles)
    verdict = evaluate_fleet(config=_full_config(), inventory=inv)
    assert "memory.unauthorized" in _codes(verdict)


def test_pinned_version_mismatch_fails_closed():
    expected = ExpectedFleet(profiles={
        "requiem-implementer": ExpectedProfile(version="9.9.9"),
    })
    verdict = evaluate_fleet(config=_full_config(),
                             inventory=_healthy_inventory(), expected=expected)
    assert "profile.version_mismatch" in _codes(verdict)


def test_pinned_but_unreported_is_unverifiable_failure():
    """A pinned hash the inventory cannot report is a failure, not a skip."""
    inv = _healthy_inventory()  # profiles carry no distribution_sha256
    expected = ExpectedFleet(profiles={
        "requiem-reviewer": ExpectedProfile(distribution_sha256="deadbeef"),
    })
    verdict = evaluate_fleet(config=_full_config(), inventory=inv,
                             expected=expected)
    findings = [f for f in verdict.findings
                if f.code == "profile.distribution_mismatch"]
    assert findings and "unverifiable" in findings[0].detail


def test_pinned_hermes_version_and_image_mismatch():
    expected = ExpectedFleet(hermes_version="0.99.0", image_digest="sha256:zzz")
    verdict = evaluate_fleet(config=_full_config(),
                             inventory=_healthy_inventory(), expected=expected)
    assert {"hermes.version_mismatch", "image.digest_mismatch"} <= _codes(verdict)


# ---- identity snapshot ------------------------------------------------


def test_profile_artifact_hashes_cover_distribution_owned_files():
    hashes = profile_artifact_hashes(_FLEET / "requiem-implementer")
    assert "distribution.yaml" in hashes
    assert "SOUL.md" in hashes
    assert "skills/handoff-receipt/SKILL.md" in hashes
    assert all(len(h) == 64 for h in hashes.values())


def test_fleet_identity_snapshot_is_stable_and_complete():
    snap1 = fleet_identity_snapshot(_FLEET, hermes_version="0.15.1")
    snap2 = fleet_identity_snapshot(_FLEET, hermes_version="0.15.1")
    assert snap1 == snap2  # order-stable, deterministic
    assert set(snap1["profiles"]) == {
        "requiem-implementer", "requiem-reviewer", "requiem-closer"
    }
    assert snap1["hermes_version"] == "0.15.1"
    # the receipt skill is byte-identical across the fleet, so its hash must be
    # equal in every profile's snapshot — a cheap drift guard on the snapshot.
    skill_hashes = {
        snap1["profiles"][p]["skills/handoff-receipt/SKILL.md"]
        for p in snap1["profiles"]
    }
    assert len(skill_hashes) == 1
