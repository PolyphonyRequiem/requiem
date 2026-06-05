"""Process configuration — the type-agnostic routing seam (§9 non-negotiable #1).

Polyphony's tier model is *data*, not code: which work-item types sit above
the implementable tier (and therefore still qualify a child as a dispatchable
root), which types are decomposable, and how type names alias to one another
all live in a per-repo ``process.yaml`` rather than being hardcoded in the
engine. Requiem mirrors that: this module loads ``.requiem-config/process.yaml``
and exposes it as a frozen :class:`ProcessConfig` that workflows consult instead
of baking ADO type literals into their verbs.

Resolution order (deterministic, see :func:`resolve_process_config`):

1. an explicit :class:`ProcessConfig` passed by a programmatic caller / test;
2. ``.requiem-config/process.yaml`` discovered by walking up from the repo path;
3. :func:`default_process_config` — the polyphony-equivalent ``Epic``/``Feature``
   tier defaults, so a repo with no config file behaves exactly as before.

The *effective* config is snapshotted into the run's event log early (via the
``start_run`` verb) so that a resume re-reads the durable snapshot rather than
ambient disk state — a config file edited between a crash and a resume cannot
change a routing decision the run already recorded (INV-RESTART).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIRNAME = ".requiem-config"
CONFIG_FILENAME = "process.yaml"

DEFAULT_ROOT_PARENT_TYPES: frozenset[str] = frozenset({"Epic", "Feature"})
"""Parent work-item types that still qualify a child as a dispatchable root.

Polyphony's tier model puts ``Epic`` and ``Feature`` above the implementable
tier; a User Story / Task whose parent is one of these is a valid SDLC root.
This is the *default* only — a repo's ``process.yaml`` overrides it.
"""


@dataclass(frozen=True, slots=True)
class RoleBinding:
    """How a Requiem role is delivered by the Hermes fleet (ADR-0017 §1).

    A role (``implementer``, ``reviewer``, ``closer``, …) is *data* mapped to a
    fleet ``profile`` plus the ``skills`` it should run with and an optional
    per-role ``model`` override. This keeps "who does this kind of work" out of
    code — the same agnostic posture as the tier model.
    """

    profile: str
    skills: tuple[str, ...] = ()
    model: str | None = None

    def to_snapshot(self) -> dict[str, Any]:
        return {"profile": self.profile, "skills": list(self.skills), "model": self.model}

    @classmethod
    def from_snapshot(cls, snap: Mapping[str, Any]) -> "RoleBinding":
        return cls(
            profile=str(snap["profile"]),
            skills=tuple(snap.get("skills") or ()),
            model=snap.get("model"),
        )


class ProcessConfigError(Exception):
    """Raised when a process config file is present but cannot be parsed.

    A *missing* config file is never an error (callers fall back to
    :func:`default_process_config`); only a present-but-malformed file fails
    loud, per INV-NO-CORRUPT-FORWARD — we never silently guess at routing.
    """

    def __init__(self, message: str, *, path: Path | None = None) -> None:
        self.path = path
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ProcessConfig:
    """The type-routing facts a workflow needs, loaded from ``process.yaml``.

    Only ``root_parent_types`` is consumed today; the remaining fields are
    reserved so the on-disk schema can grow (decomposable/leaf hints, type
    aliases) without a breaking migration. ``source``/``sha256`` carry the
    provenance of the effective config so it can be snapshotted into the run.
    """

    root_parent_types: frozenset[str] = DEFAULT_ROOT_PARENT_TYPES
    type_aliases: Mapping[str, str] = field(default_factory=dict)
    decomposable_types: frozenset[str] = frozenset()
    implementable_types: frozenset[str] = frozenset()
    roles: Mapping[str, RoleBinding] = field(default_factory=dict)
    source: Path | None = None
    sha256: str | None = None

    def normalize_type(self, work_item_type: str | None) -> str | None:
        """Resolve a work-item type through the configured aliases."""
        if work_item_type is None:
            return None
        return self.type_aliases.get(work_item_type, work_item_type)

    def is_root_parent_type(self, work_item_type: str | None) -> bool:
        """True if a parent of ``work_item_type`` keeps its child at root tier."""
        return self.normalize_type(work_item_type) in self.root_parent_types

    def role(self, name: str) -> RoleBinding | None:
        """The fleet binding for a role, or ``None`` when none is configured."""
        return self.roles.get(name)

    def to_snapshot(self) -> dict[str, Any]:
        """A JSON-safe, order-stable snapshot for the event log / manifest."""
        return {
            "root_parent_types": sorted(self.root_parent_types),
            "type_aliases": dict(self.type_aliases),
            "decomposable_types": sorted(self.decomposable_types),
            "implementable_types": sorted(self.implementable_types),
            "roles": {k: self.roles[k].to_snapshot() for k in sorted(self.roles)},
            "source": str(self.source) if self.source is not None else None,
            "sha256": self.sha256,
        }

    @classmethod
    def from_snapshot(cls, snap: Mapping[str, Any]) -> "ProcessConfig":
        """Reconstruct a config from a :meth:`to_snapshot` payload."""
        src = snap.get("source")
        return cls(
            root_parent_types=frozenset(snap.get("root_parent_types", ())),
            type_aliases=dict(snap.get("type_aliases", {})),
            decomposable_types=frozenset(snap.get("decomposable_types", ())),
            implementable_types=frozenset(snap.get("implementable_types", ())),
            roles={
                k: RoleBinding.from_snapshot(v)
                for k, v in (snap.get("roles") or {}).items()
            },
            source=Path(src) if src else None,
            sha256=snap.get("sha256"),
        )


def default_process_config() -> ProcessConfig:
    """The polyphony-equivalent defaults used when no ``process.yaml`` exists."""
    return ProcessConfig(root_parent_types=DEFAULT_ROOT_PARENT_TYPES)


def _str_set(data: Mapping[str, Any], key: str, path: Path | None) -> frozenset[str]:
    raw = data.get(key)
    if raw is None:
        return frozenset()
    if isinstance(raw, str) or not isinstance(raw, (list, tuple, set, frozenset)):
        raise ProcessConfigError(
            f"'{key}' must be a list of strings, got {type(raw).__name__}",
            path=path,
        )
    out: list[str] = []
    for elem in raw:
        if not isinstance(elem, str):
            raise ProcessConfigError(
                f"'{key}' entries must be strings, got {type(elem).__name__}",
                path=path,
            )
        out.append(elem)
    return frozenset(out)


def _str_map(data: Mapping[str, Any], key: str, path: Path | None) -> dict[str, str]:
    raw = data.get(key)
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ProcessConfigError(
            f"'{key}' must be a mapping of string to string, "
            f"got {type(raw).__name__}",
            path=path,
        )
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ProcessConfigError(
                f"'{key}' must map strings to strings", path=path
            )
        out[k] = v
    return out


def _roles_map(
    data: Mapping[str, Any], key: str, path: Path | None
) -> dict[str, RoleBinding]:
    raw = data.get(key)
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ProcessConfigError(
            f"'{key}' must be a mapping of role to binding, "
            f"got {type(raw).__name__}",
            path=path,
        )
    out: dict[str, RoleBinding] = {}
    for role_name, binding in raw.items():
        if not isinstance(role_name, str):
            raise ProcessConfigError(f"'{key}' role names must be strings", path=path)
        if not isinstance(binding, Mapping):
            raise ProcessConfigError(
                f"'{key}.{role_name}' must be a mapping with a 'profile'", path=path
            )
        profile = binding.get("profile")
        if not isinstance(profile, str) or profile.strip() == "":
            raise ProcessConfigError(
                f"'{key}.{role_name}' requires a non-empty string 'profile'",
                path=path,
            )
        skills = _str_set_seq(binding, "skills", role_name, key, path)
        model = binding.get("model")
        if model is not None and not isinstance(model, str):
            raise ProcessConfigError(
                f"'{key}.{role_name}.model' must be a string", path=path
            )
        out[role_name] = RoleBinding(profile=profile, skills=skills, model=model)
    return out


def _str_set_seq(
    binding: Mapping[str, Any], key: str, role: str, parent: str, path: Path | None
) -> tuple[str, ...]:
    raw = binding.get(key)
    if raw is None:
        return ()
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        raise ProcessConfigError(
            f"'{parent}.{role}.{key}' must be a list of strings", path=path
        )
    for elem in raw:
        if not isinstance(elem, str):
            raise ProcessConfigError(
                f"'{parent}.{role}.{key}' entries must be strings", path=path
            )
    return tuple(raw)


def _build_from_mapping(
    data: Mapping[str, Any], *, source: Path | None, sha256: str | None
) -> ProcessConfig:
    root = _str_set(data, "root_parent_types", source)
    if not root:
        # An empty/omitted root set would route *every* parented item to a
        # human gate — almost certainly an authoring mistake. Fall back to
        # the documented defaults rather than silently disabling dispatch.
        root = DEFAULT_ROOT_PARENT_TYPES
    return ProcessConfig(
        root_parent_types=root,
        type_aliases=_str_map(data, "type_aliases", source),
        decomposable_types=_str_set(data, "decomposable_types", source),
        implementable_types=_str_set(data, "implementable_types", source),
        roles=_roles_map(data, "roles", source),
        source=source,
        sha256=sha256,
    )


def load_process_config(path: Path | str) -> ProcessConfig:
    """Load and validate a ``process.yaml`` from an explicit path.

    Raises :class:`ProcessConfigError` if the file cannot be read, is not
    valid YAML, or is not a mapping with well-typed known fields. Unknown
    keys are tolerated for forward compatibility.
    """
    p = Path(path)
    try:
        raw_text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProcessConfigError(
            f"cannot read process config {p}: {exc}", path=p
        ) from exc
    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ProcessConfigError(
            f"invalid YAML in process config {p}: {exc}", path=p
        ) from exc
    if data is None:
        data = {}
    if not isinstance(data, Mapping):
        raise ProcessConfigError(
            f"process config {p} must be a mapping, got {type(data).__name__}",
            path=p,
        )
    sha = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    return _build_from_mapping(data, source=p, sha256=sha)


def find_process_config_path(start_dir: Path | str) -> Path | None:
    """Walk up from ``start_dir`` for a ``.requiem-config/process.yaml`` file."""
    start = Path(start_dir).resolve()
    for directory in (start, *start.parents):
        candidate = directory / CONFIG_DIRNAME / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def discover_process_config(
    start_dir: Path | str, *, default: bool = True
) -> ProcessConfig | None:
    """Discover the nearest ``process.yaml`` at or above ``start_dir``.

    Returns the loaded config, or :func:`default_process_config` when none is
    found and ``default`` is True, else ``None``.
    """
    found = find_process_config_path(start_dir)
    if found is not None:
        return load_process_config(found)
    return default_process_config() if default else None


def resolve_process_config(
    explicit: ProcessConfig | None, repo_path: Path | str
) -> ProcessConfig:
    """Resolve the effective config: explicit > discovered > default.

    Discovery is anchored to ``repo_path`` (not ambient cwd) so a run never
    silently picks up an unrelated repo's config.
    """
    if explicit is not None:
        return explicit
    return discover_process_config(repo_path, default=True) or default_process_config()
