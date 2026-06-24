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
class TypeConfig:
    """Per work-item-type configuration (ADR-0026).

    Each ADO work-item type in the repo's process template can carry
    one or more *facets* describing what requiem may do with items of
    that type:

    * ``plannable``     — the planner is invoked; child decomposition allowed.
    * ``implementable`` — workflow may implement the item directly (no
      decomposition required).
    * ``actionable``    — an executor backend (``actionable_executor``) picks
      this up for the actual action.

    A type can carry multiple facets. Polyphony's ``Feature``/``Issue`` is
    both ``plannable`` AND ``implementable``: the planner is invoked, and
    if the planner returns ``decomposable=false`` the workflow treats it
    as a leaf.

    ``decomposition_guidance`` is free-form prompt text injected into the
    planner prompt for items of this type — the most powerful lever for
    steering decomposition shape (e.g. "Decompose Scenarios into Features.
    NEVER directly into Tasks.").

    ``max_nesting_depth`` is an optional per-type recursion cap. ``1``
    means "decompose once, then children must be implementable leaves";
    ``None`` means no per-type cap (the global ``max_depth`` in
    ``build_engine`` still applies).

    ``actionable_executor`` names the executor backend for actionable
    items (today only ``requiem`` is implemented; future: kanban worker,
    ado-pipeline, etc.).
    """

    facets: tuple[str, ...] = ()
    decomposition_guidance: str | None = None
    max_nesting_depth: int | None = None
    actionable_executor: str | None = None

    def to_snapshot(self) -> dict[str, Any]:
        out: dict[str, Any] = {"facets": list(self.facets)}
        if self.decomposition_guidance is not None:
            out["decomposition_guidance"] = self.decomposition_guidance
        if self.max_nesting_depth is not None:
            out["max_nesting_depth"] = self.max_nesting_depth
        if self.actionable_executor is not None:
            out["actionable_executor"] = self.actionable_executor
        return out

    @classmethod
    def from_snapshot(cls, snap: Mapping[str, Any]) -> "TypeConfig":
        return cls(
            facets=tuple(snap.get("facets") or ()),
            decomposition_guidance=snap.get("decomposition_guidance"),
            max_nesting_depth=snap.get("max_nesting_depth"),
            actionable_executor=snap.get("actionable_executor"),
        )


VALID_FACETS: frozenset[str] = frozenset({"plannable", "implementable", "actionable"})
"""Allowed facet values. Unknown facets fail loud at parse time per
INV-NO-CORRUPT-FORWARD — silently ignoring an unrecognized facet would
let a typo (``implementible``) become a routing bug."""


@dataclass(frozen=True, slots=True)
class ProcessConfig:
    """The type-routing facts a workflow needs, loaded from ``process.yaml``.

    ``root_parent_types`` drives root classification (root_dispatch) and
    ``decomposable_types``/``implementable_types`` drive the planning tier
    decision (see :meth:`tier_for_type`); ``type_aliases`` normalize type
    names on both. ``roles`` map Requiem roles to the Hermes fleet.
    ``models`` maps Requiem roles to LLM provider/model bindings (ADR-0030
    §2 — sibling of ``roles`` but on a different axis: ``roles`` answers
    "which Hermes profile picks up this work", ``models`` answers "which
    LLM does the agent inside any workflow use").
    ``source``/``sha256`` carry the provenance of the effective config so it
    can be snapshotted into the run.
    """

    root_parent_types: frozenset[str] = DEFAULT_ROOT_PARENT_TYPES
    type_aliases: Mapping[str, str] = field(default_factory=dict)
    decomposable_types: frozenset[str] = frozenset()
    implementable_types: frozenset[str] = frozenset()
    types: Mapping[str, TypeConfig] = field(default_factory=dict)
    roles: Mapping[str, RoleBinding] = field(default_factory=dict)
    models: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    source: Path | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        # ADR-0026: `types` is the new authoritative source. If both
        # `types` and the legacy flat sets are provided, the source of
        # truth is ambiguous — fail loud (INV-NO-CORRUPT-FORWARD).
        # Empty flat sets are fine because that's the back-compat shape
        # used when `types` IS the source.
        if self.types and (self.decomposable_types or self.implementable_types):
            raise ProcessConfigError(
                "process config is ambiguous: both `types` (new per-type "
                "schema, ADR-0026) and legacy `decomposable_types`/"
                "`implementable_types` are provided. Use `types` only.",
                path=self.source,
            )

        # Two paths to populate flat sets + types map, both ending with
        # both shapes available to consumers:
        #
        #  (a) types is provided → derive flat sets from facets;
        #  (b) only flat sets are provided → synthesise a minimal types
        #      map so downstream code that reads `types` doesn't need a
        #      special case for legacy configs.
        if self.types:
            derived_dec, derived_impl = self._derive_flat_sets_from_types()
            # Bypass frozen via object.__setattr__ — the derived sets are
            # part of the config's invariant, not an override of an
            # operator-provided value.
            object.__setattr__(self, "decomposable_types", derived_dec)
            object.__setattr__(self, "implementable_types", derived_impl)
        elif self.decomposable_types or self.implementable_types:
            synth: dict[str, TypeConfig] = {}
            for t in self.decomposable_types:
                synth[t] = TypeConfig(facets=("plannable",))
            for t in self.implementable_types:
                synth[t] = TypeConfig(facets=("implementable", "actionable"))
            object.__setattr__(self, "types", synth)

        # A type cannot be declared both decomposable and implementable — that
        # is a contradictory tier policy. Catch it loud at construction (covers
        # parsing, ``from_snapshot``, and direct construction) rather than
        # letting it resolve inconsistently at routing time (INV-NO-CORRUPT-
        # FORWARD). The check is alias-normalized so an indirect contradiction
        # (e.g. ``Bug -> Task`` with ``Bug`` decomposable and ``Task``
        # implementable) is also rejected.
        overlap = self._normalize_set(self.decomposable_types) & self._normalize_set(
            self.implementable_types
        )
        if overlap:
            raise ProcessConfigError(
                "process config declares type(s) as both decomposable and "
                f"implementable (after alias normalization): {sorted(overlap)}",
                path=self.source,
            )

    def _derive_flat_sets_from_types(self) -> tuple[frozenset[str], frozenset[str]]:
        """Project the per-type facets onto the legacy flat sets.

        Rule (ADR-0026):

        * ``plannable`` facet → decomposable (planner is invoked)
        * ``implementable`` facet WITHOUT ``plannable`` → implementable leaf
        * BOTH facets → decomposable (planner is invoked, may emit
          ``decomposable=false`` and the workflow accepts that as a leaf —
          this avoids double-classifying Feature-style types)
        """
        dec: set[str] = set()
        impl: set[str] = set()
        for type_name, tc in self.types.items():
            if "plannable" in tc.facets:
                dec.add(type_name)
            elif "implementable" in tc.facets:
                impl.add(type_name)
        return frozenset(dec), frozenset(impl)

    def normalize_type(self, work_item_type: str | None) -> str | None:
        """Resolve a work-item type through the configured aliases."""
        if work_item_type is None:
            return None
        return self.type_aliases.get(work_item_type, work_item_type)

    def _normalize_set(self, types: frozenset[str]) -> frozenset[str]:
        """Alias-resolve every member of a tier set."""
        return frozenset(self.type_aliases.get(t, t) for t in types)

    def is_root_parent_type(self, work_item_type: str | None) -> bool:
        """True if a parent of ``work_item_type`` keeps its child at root tier."""
        return self.normalize_type(work_item_type) in self.root_parent_types

    def has_tier_policy(self) -> bool:
        """True if any decompose/implement tier constraint is configured."""
        return bool(self.decomposable_types or self.implementable_types)

    def tier_for_type(
        self, work_item_type: str | None
    ) -> str:
        """Classify a work-item type against the configured tier sets.

        Returns one of:

        * ``"implementable"`` — the type must be a leaf (never decomposed);
        * ``"decomposable"``  — the type must be broken down into children;
        * ``"unspecified"``   — no config opinion; the planner's judgment stands.

        Alias normalization is applied to both the input type and the
        configured sets, so ``type_aliases`` are honored on both sides. A
        ``None``/blank type is always ``"unspecified"`` here; callers that have
        a tier policy configured should fail closed on a missing type rather
        than relying on this method (see the planning workflow).
        """
        norm = self.normalize_type(work_item_type)
        if norm is None:
            return "unspecified"
        if norm in self._normalize_set(self.implementable_types):
            return "implementable"
        if norm in self._normalize_set(self.decomposable_types):
            return "decomposable"
        return "unspecified"

    def role(self, name: str) -> RoleBinding | None:
        """The fleet binding for a role, or ``None`` when none is configured."""
        return self.roles.get(name)

    # ---- ADR-0026 per-type helpers ----------------------------------

    def decomposition_guidance_for(self, work_item_type: str | None) -> str | None:
        """Per-type planner guidance text, or None when none is configured.

        Used by ``planning.py``'s ``_planner_prompt`` to inject domain
        rules ("Decompose Scenarios into Features. NEVER directly into
        Tasks.") into the planner's prompt for items of this type.
        Returns ``None`` for types without ``decomposition_guidance``,
        for unknown types, and for ``None`` input — callers fall back
        to the generic prompt in all three cases.
        """
        norm = self.normalize_type(work_item_type)
        if norm is None:
            return None
        tc = self.types.get(norm)
        if tc is None:
            return None
        return tc.decomposition_guidance

    def max_nesting_depth_for(self, work_item_type: str | None) -> int | None:
        """Per-type recursion cap, or None when none is configured.

        Used by ``planning.py``'s ``branch_decomposable`` to cap
        recursion below the global ``max_depth``. ``None`` means no
        per-type cap (the global cap still applies).
        """
        norm = self.normalize_type(work_item_type)
        if norm is None:
            return None
        tc = self.types.get(norm)
        if tc is None:
            return None
        return tc.max_nesting_depth

    def has_facet(self, work_item_type: str | None, facet: str) -> bool:
        """True if the type's TypeConfig declares ``facet``.

        Used by ``branch_decomposable`` to distinguish types that are
        BOTH plannable AND implementable (e.g. Feature in the CVAPI
        config — the planner is invoked, but a leaf verdict is OK
        because the type can also be implemented directly) from
        types that are plannable-only (where a leaf verdict from the
        planner is a policy violation).
        """
        norm = self.normalize_type(work_item_type)
        if norm is None:
            return False
        tc = self.types.get(norm)
        if tc is None:
            return False
        return facet in tc.facets

    def to_snapshot(self) -> dict[str, Any]:
        """A JSON-safe, order-stable snapshot for the event log / manifest."""
        return {
            "root_parent_types": sorted(self.root_parent_types),
            "type_aliases": dict(self.type_aliases),
            "decomposable_types": sorted(self.decomposable_types),
            "implementable_types": sorted(self.implementable_types),
            "types": {k: self.types[k].to_snapshot() for k in sorted(self.types)},
            "roles": {k: self.roles[k].to_snapshot() for k in sorted(self.roles)},
            "models": {k: dict(self.models[k]) for k in sorted(self.models)},
            "source": str(self.source) if self.source is not None else None,
            "sha256": self.sha256,
        }

    @classmethod
    def from_snapshot(cls, snap: Mapping[str, Any]) -> "ProcessConfig":
        """Reconstruct a config from a :meth:`to_snapshot` payload."""
        src = snap.get("source")
        types_snap = snap.get("types") or {}
        types = {
            k: TypeConfig.from_snapshot(v) for k, v in types_snap.items()
        }
        # When `types` is present, the flat sets in the snapshot are
        # the derived values from a prior __post_init__. Don't re-pass
        # them — __post_init__ will re-derive (and ambiguous-check would
        # fire otherwise).
        if types:
            return cls(
                root_parent_types=frozenset(snap.get("root_parent_types", ())),
                type_aliases=dict(snap.get("type_aliases", {})),
                types=types,
                roles={
                    k: RoleBinding.from_snapshot(v)
                    for k, v in (snap.get("roles") or {}).items()
                },
                source=Path(src) if src else None,
                sha256=snap.get("sha256"),
            )
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


def _models_map(
    data: Mapping[str, Any], key: str, path: Path | None
) -> dict[str, dict[str, Any]]:
    """Parse the ``models`` block (ADR-0030 §2 role→model routing).

    Returns ``dict[role_name, dict[field, value]]``. Shape validation
    only — the per-entry field validation (provider/model/max_tokens
    presence + types) lives in
    :func:`requiem.model_routing._validate_entry` and runs lazily on
    each ``resolve_model_for_role()`` call. This keeps the loader
    simple (no schema knowledge of the routing-entry shape, which
    may grow over time without a process_config schema bump) and
    matches the precedent set by the ADR-0030 §2 unit tests in
    ``tests/test_model_routing.py``, which build ``ProcessConfig``
    instances directly with raw dict values for ``models``.

    Forward-compat: an unrecognised field inside a role entry passes
    through to ``_validate_entry``, which only rejects shapes that
    would prevent routing (it doesn't reject extra keys — the routing
    layer is supposed to ignore what it doesn't understand so new
    knobs like ``reasoning_effort`` can be added to the YAML without
    breaking older requiem builds).
    """
    raw = data.get(key)
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ProcessConfigError(
            f"'{key}' must be a mapping of role name to model binding, "
            f"got {type(raw).__name__}",
            path=path,
        )
    out: dict[str, dict[str, Any]] = {}
    for role_name, binding in raw.items():
        if not isinstance(role_name, str):
            raise ProcessConfigError(
                f"'{key}' role names must be strings, got {type(role_name).__name__}",
                path=path,
            )
        if not isinstance(binding, Mapping):
            raise ProcessConfigError(
                f"'{key}.{role_name}' must be a mapping (with at least "
                f"'provider' + 'model'), got {type(binding).__name__}",
                path=path,
            )
        out[role_name] = dict(binding)
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


def _types_map(
    data: Mapping[str, Any], key: str, path: Path | None
) -> dict[str, TypeConfig]:
    """Parse the ADR-0026 `types: dict[str, TypeConfig]` field."""
    raw = data.get(key)
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ProcessConfigError(
            f"'{key}' must be a mapping of type-name to per-type config, "
            f"got {type(raw).__name__}",
            path=path,
        )
    out: dict[str, TypeConfig] = {}
    for type_name, body in raw.items():
        if not isinstance(type_name, str):
            raise ProcessConfigError(
                f"'{key}' keys must be type-name strings, got "
                f"{type(type_name).__name__}",
                path=path,
            )
        if not isinstance(body, Mapping):
            raise ProcessConfigError(
                f"'{key}.{type_name}' must be a mapping with at least "
                f"'facets', got {type(body).__name__}",
                path=path,
            )

        # facets — required, must be a list of valid facet strings.
        facets_raw = body.get("facets")
        if facets_raw is None:
            raise ProcessConfigError(
                f"'{key}.{type_name}' is missing required field 'facets'",
                path=path,
            )
        if isinstance(facets_raw, str) or not isinstance(facets_raw, (list, tuple)):
            raise ProcessConfigError(
                f"'{key}.{type_name}.facets' must be a list of strings, "
                f"got {type(facets_raw).__name__}",
                path=path,
            )
        facets: list[str] = []
        for f in facets_raw:
            if not isinstance(f, str):
                raise ProcessConfigError(
                    f"'{key}.{type_name}.facets' entries must be strings, "
                    f"got {type(f).__name__}",
                    path=path,
                )
            if f not in VALID_FACETS:
                raise ProcessConfigError(
                    f"'{key}.{type_name}.facets' contains unknown facet "
                    f"{f!r}; valid facets are {sorted(VALID_FACETS)}",
                    path=path,
                )
            facets.append(f)

        # decomposition_guidance — optional, must be a string when present.
        guidance = body.get("decomposition_guidance")
        if guidance is not None and not isinstance(guidance, str):
            raise ProcessConfigError(
                f"'{key}.{type_name}.decomposition_guidance' must be a "
                f"string, got {type(guidance).__name__}",
                path=path,
            )

        # max_nesting_depth — optional, must be a non-negative int.
        depth = body.get("max_nesting_depth")
        if depth is not None:
            if not isinstance(depth, int) or isinstance(depth, bool):
                raise ProcessConfigError(
                    f"'{key}.{type_name}.max_nesting_depth' must be a "
                    f"non-negative integer, got {type(depth).__name__}",
                    path=path,
                )
            if depth < 0:
                raise ProcessConfigError(
                    f"'{key}.{type_name}.max_nesting_depth' must be "
                    f"non-negative, got {depth}",
                    path=path,
                )

        # actionable_executor — optional, must be a string when present.
        executor = body.get("actionable_executor")
        if executor is not None and not isinstance(executor, str):
            raise ProcessConfigError(
                f"'{key}.{type_name}.actionable_executor' must be a "
                f"string, got {type(executor).__name__}",
                path=path,
            )

        out[type_name] = TypeConfig(
            facets=tuple(facets),
            decomposition_guidance=guidance,
            max_nesting_depth=depth,
            actionable_executor=executor,
        )
    return out


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
        types=_types_map(data, "types", source),
        roles=_roles_map(data, "roles", source),
        models=_models_map(data, "models", source),
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
