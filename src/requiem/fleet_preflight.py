"""Fleet preflight — the requiem-side, fail-closed gate before a live run
(ADR-0017 §2).

requiem refuses to dispatch into a Hermes fleet it cannot vouch for. This module
is the *pure* half of that gate: given what requiem **requires** (the baseline
delivery roles, resolved through ``process.yaml``), what it **expects** (a
committed fleet lock — versions and artifact hashes), and what the live fleet
**reports** (a :class:`FleetInventory` gathered by an operator-side adapter), it
returns a fail-closed :class:`FleetVerdict`.

The split is deliberate (and the rubber-duck's main correction):

* **Reproducibility comes from immutable inputs** — the ``fleet/`` distributions
  and the expected lock are repo-resident and hashable here, with no live Hermes.
* **Clean execution comes from fresh runtime homes** — whether each profile's
  home is actually run-scoped is a *live* fact, gathered into the inventory by
  the operator-side adapter, then judged by this pure logic.

So this module never shells out. It is the authority; the (future) live gatherer
is dumb. Everything here is unit-testable without a gateway, ADO, or worker.

Two load-bearing fail-closed postures, both learned from the critique:

* **"No roles configured" is NOT "no fleet required."** The baseline delivery
  roles are required regardless; an unbound role is a finding, never a pass.
* **"Cannot verify" is a failure, not a skip.** When the expected lock pins a
  version/hash but the inventory does not report it, that is an unverifiable
  claim — a finding — not a silently-tolerated gap.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from requiem.process_config import ProcessConfig

# The delivery roles requiem's executor needs to exist no matter what the repo's
# process.yaml does or doesn't say. Resolving these through ProcessConfig.roles
# is how "agnostic" routing stays agnostic without degrading into "nothing is
# required" when roles are omitted.
BASELINE_ROLES: tuple[str, ...] = ("implementer", "reviewer", "closer")


@dataclass(frozen=True, slots=True)
class ProfileInventory:
    """What the live fleet reports about one installed profile.

    Fields the operator-side gatherer fills from ``hermes profile`` /
    ``config.yaml`` / the container. ``None`` means "the gatherer could not
    observe this" — which, when the expected lock pins it, is treated as
    unverifiable (a finding), never as agreement.
    """

    name: str
    version: str | None = None
    distribution_sha256: str | None = None
    config_sha256: str | None = None
    auto_decompose: bool | None = None
    dispatch_in_gateway: bool | None = None
    home_path: str | None = None
    writable_memory_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FleetInventory:
    """A snapshot of the live fleet, gathered by the operator-side adapter."""

    hermes_version: str | None = None
    image_digest: str | None = None
    run_home_root: str | None = None
    profiles: Mapping[str, ProfileInventory] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExpectedProfile:
    """The committed expectation for one profile (a fleet-lock entry)."""

    version: str | None = None
    distribution_sha256: str | None = None
    config_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class ExpectedFleet:
    """The committed fleet lock the live inventory is judged against.

    Absent fields are simply not pinned (no enforcement for that dimension). A
    *pinned* field that the inventory cannot corroborate is a failure.
    """

    hermes_version: str | None = None
    image_digest: str | None = None
    profiles: Mapping[str, ExpectedProfile] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FleetFinding:
    """One reason the fleet is not fit to run. ``code`` is stable for tests."""

    code: str
    detail: str
    profile: str | None = None


@dataclass(frozen=True, slots=True)
class FleetVerdict:
    """The fail-closed result. ``ok`` is True only when there are no findings."""

    ok: bool
    findings: tuple[FleetFinding, ...] = ()

    def summary(self) -> str:
        if self.ok:
            return "fleet preflight OK"
        return "fleet preflight FAILED: " + "; ".join(
            f"[{f.code}]{(' ' + f.profile) if f.profile else ''} {f.detail}"
            for f in self.findings
        )


def _is_within(path: str, root: str) -> bool:
    """True when ``path`` is ``root`` or nested under it (lexical, normalized)."""
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def required_profiles(
    config: ProcessConfig, *, roles: Iterable[str] = BASELINE_ROLES
) -> tuple[dict[str, str], tuple[FleetFinding, ...]]:
    """Resolve the baseline roles to profile names through ``process.yaml``.

    Returns ``(role->profile, findings)``. A required role with no binding is a
    fail-closed finding, never a silent omission.
    """
    resolved: dict[str, str] = {}
    findings: list[FleetFinding] = []
    for role in roles:
        binding = config.role(role)
        if binding is None:
            findings.append(
                FleetFinding(
                    code="role.unbound",
                    detail=f"required role {role!r} has no profile binding in "
                    "process.yaml; cannot dispatch a fleet for it",
                )
            )
            continue
        resolved[role] = binding.profile
    return resolved, tuple(findings)


def evaluate_fleet(
    *,
    config: ProcessConfig,
    inventory: FleetInventory,
    expected: ExpectedFleet | None = None,
    roles: Iterable[str] = BASELINE_ROLES,
) -> FleetVerdict:
    """The fail-closed gate. Empty findings ⇒ the fleet is fit to run live."""
    findings: list[FleetFinding] = []

    resolved, role_findings = required_profiles(config, roles=roles)
    findings.extend(role_findings)

    for role, profile in resolved.items():
        inv = inventory.profiles.get(profile)
        if inv is None:
            findings.append(
                FleetFinding(
                    code="profile.missing",
                    detail=f"role {role!r} needs profile {profile!r}, which is "
                    "not installed in the fleet",
                    profile=profile,
                )
            )
            continue

        # Orchestration must be Manual — requiem is the only decomposition
        # authority; an auto-decomposer would fan out behind its back.
        if inv.auto_decompose is not False:
            findings.append(
                FleetFinding(
                    code="orchestration.not_manual",
                    detail=f"auto_decompose={inv.auto_decompose!r}; requiem "
                    "requires Manual orchestration (auto_decompose: false)",
                    profile=profile,
                )
            )
        if inv.dispatch_in_gateway is not True:
            findings.append(
                FleetFinding(
                    code="orchestration.dispatch_disabled",
                    detail=f"dispatch_in_gateway={inv.dispatch_in_gateway!r}; "
                    "the gateway-embedded dispatcher must be enabled",
                    profile=profile,
                )
            )

        # Clean per-run home: the profile's runtime home must live under the
        # run's HERMES_HOME root (no cross-run task-state leakage).
        if inventory.run_home_root is not None and inv.home_path is not None:
            if not _is_within(inv.home_path, inventory.run_home_root):
                findings.append(
                    FleetFinding(
                        code="home.not_run_scoped",
                        detail=f"profile home {inv.home_path!r} is not under the "
                        f"run home root {inventory.run_home_root!r}",
                        profile=profile,
                    )
                )
            for mem in inv.writable_memory_paths:
                if not _is_within(mem, inventory.run_home_root):
                    findings.append(
                        FleetFinding(
                            code="memory.unauthorized",
                            detail=f"writable memory {mem!r} escapes the run home "
                            f"root {inventory.run_home_root!r}",
                            profile=profile,
                        )
                    )

        if expected is not None:
            findings.extend(_check_expected_profile(profile, inv, expected))

    if expected is not None:
        findings.extend(_check_expected_global(inventory, expected))

    return FleetVerdict(ok=not findings, findings=tuple(findings))


def _pin_finding(
    code: str, profile: str | None, what: str, want: str, got: str | None
) -> FleetFinding:
    seen = "not reported (unverifiable)" if got is None else repr(got)
    return FleetFinding(
        code=code,
        detail=f"{what} expected {want!r} but inventory {seen}",
        profile=profile,
    )


def _check_expected_profile(
    profile: str, inv: ProfileInventory, expected: ExpectedFleet
) -> list[FleetFinding]:
    exp = expected.profiles.get(profile)
    if exp is None:
        return []
    out: list[FleetFinding] = []
    # A pinned dimension the inventory cannot corroborate is unverifiable —
    # a finding, not a tolerated gap (fail closed on "cannot verify").
    if exp.version is not None and inv.version != exp.version:
        out.append(_pin_finding("profile.version_mismatch", profile,
                                 "version", exp.version, inv.version))
    if exp.distribution_sha256 is not None \
            and inv.distribution_sha256 != exp.distribution_sha256:
        out.append(_pin_finding("profile.distribution_mismatch", profile,
                                 "distribution_sha256", exp.distribution_sha256,
                                 inv.distribution_sha256))
    if exp.config_sha256 is not None and inv.config_sha256 != exp.config_sha256:
        out.append(_pin_finding("profile.config_mismatch", profile,
                                 "config_sha256", exp.config_sha256,
                                 inv.config_sha256))
    return out


def _check_expected_global(
    inventory: FleetInventory, expected: ExpectedFleet
) -> list[FleetFinding]:
    out: list[FleetFinding] = []
    if expected.hermes_version is not None \
            and inventory.hermes_version != expected.hermes_version:
        out.append(_pin_finding("hermes.version_mismatch", None, "hermes_version",
                                 expected.hermes_version, inventory.hermes_version))
    if expected.image_digest is not None \
            and inventory.image_digest != expected.image_digest:
        out.append(_pin_finding("image.digest_mismatch", None, "image_digest",
                                 expected.image_digest, inventory.image_digest))
    return out


# ---- identity snapshot (schema-first, repo-local hashing) ------------


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def profile_artifact_hashes(profile_dir: Path | str) -> dict[str, str]:
    """sha256 the immutable, distribution-owned artifacts of one profile.

    These are the inputs reproducibility derives from (the things baked into the
    image), distinct from the mutable runtime home. Missing optional files are
    simply absent from the map; ``distribution.yaml`` is required.
    """
    base = Path(profile_dir)
    hashes: dict[str, str] = {}
    manifest = base / "distribution.yaml"
    if not manifest.is_file():
        raise FileNotFoundError(f"no distribution.yaml under {base}")
    hashes["distribution.yaml"] = _sha256_file(manifest)
    for rel in ("SOUL.md", "config.yaml",
                "skills/handoff-receipt/SKILL.md"):
        candidate = base / rel
        if candidate.is_file():
            hashes[rel] = _sha256_file(candidate)
    return hashes


def fleet_identity_snapshot(
    fleet_dir: Path | str,
    *,
    profiles: Iterable[str] = ("requiem-implementer", "requiem-reviewer",
                               "requiem-closer"),
    hermes_version: str | None = None,
    image_digest: str | None = None,
) -> dict[str, object]:
    """A JSON-safe, order-stable identity snapshot for the run event log.

    Mirrors the doctrine / process-config snapshot pattern (ADR-0015): the
    repo-local artifact hashes are computed here; live-only fields
    (``hermes_version``, ``image_digest``) are *inputs* gathered by the operator
    adapter, never faked. A resume reads this durable identity rather than
    re-hashing ambient disk.
    """
    base = Path(fleet_dir)
    profile_snaps: dict[str, dict[str, str]] = {}
    for name in sorted(profiles):
        profile_snaps[name] = profile_artifact_hashes(base / name)
    return {
        "fleet_dir": str(base),
        "hermes_version": hermes_version,
        "image_digest": image_digest,
        "profiles": profile_snaps,
    }
