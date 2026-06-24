"""Process-config loader tests — the type-agnostic routing seam (#1).

Covers: default behaviour with no file, YAML load + validation, discovery
walking up the tree, explicit > discovered > default resolution, snapshot
round-trip, and fail-closed behaviour on malformed input.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from requiem.process_config import (
    CONFIG_DIRNAME,
    CONFIG_FILENAME,
    DEFAULT_ROOT_PARENT_TYPES,
    ProcessConfig,
    ProcessConfigError,
    RoleBinding,
    TypeConfig,
    default_process_config,
    discover_process_config,
    find_process_config_path,
    load_process_config,
    resolve_process_config,
)


def _write_config(root: Path, body: str) -> Path:
    cfg_dir = root / CONFIG_DIRNAME
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / CONFIG_FILENAME
    path.write_text(body, encoding="utf-8")
    return path


# ---- defaults ---------------------------------------------------------


def test_default_config_matches_polyphony_tier_model():
    cfg = default_process_config()
    assert cfg.root_parent_types == DEFAULT_ROOT_PARENT_TYPES
    assert cfg.is_root_parent_type("Epic")
    assert cfg.is_root_parent_type("Feature")
    assert not cfg.is_root_parent_type("Task")
    assert not cfg.is_root_parent_type(None)


# ---- load + validate --------------------------------------------------


# ---- role -> profile routing (ADR-0017 §1) ----------------------------


def test_default_config_has_no_roles():
    assert default_process_config().roles == {}
    assert default_process_config().role("implementer") is None


def test_roles_parse_full_binding(tmp_path: Path):
    _write_config(
        tmp_path,
        """
roles:
  implementer:
    profile: requiem-implementer
    skills: [coder, lsp]
    model: anthropic/claude-sonnet-4
  closer:
    profile: requiem-closer
""",
    )
    cfg = load_process_config(tmp_path / CONFIG_DIRNAME / CONFIG_FILENAME)

    impl = cfg.role("implementer")
    assert impl == RoleBinding(
        profile="requiem-implementer",
        skills=("coder", "lsp"),
        model="anthropic/claude-sonnet-4",
    )
    closer = cfg.role("closer")
    assert closer.profile == "requiem-closer"
    assert closer.skills == ()
    assert closer.model is None


def test_roles_round_trip_through_snapshot(tmp_path: Path):
    _write_config(
        tmp_path,
        "roles:\n  reviewer:\n    profile: requiem-reviewer\n    skills: [code-review]\n",
    )
    cfg = load_process_config(tmp_path / CONFIG_DIRNAME / CONFIG_FILENAME)
    back = ProcessConfig.from_snapshot(cfg.to_snapshot())
    assert back.role("reviewer") == cfg.role("reviewer")


def test_role_missing_profile_fails_closed(tmp_path: Path):
    _write_config(tmp_path, "roles:\n  implementer:\n    skills: [coder]\n")
    with pytest.raises(ProcessConfigError):
        load_process_config(tmp_path / CONFIG_DIRNAME / CONFIG_FILENAME)


def test_role_skills_must_be_list(tmp_path: Path):
    _write_config(
        tmp_path,
        "roles:\n  implementer:\n    profile: p\n    skills: coder\n",
    )
    with pytest.raises(ProcessConfigError):
        load_process_config(tmp_path / CONFIG_DIRNAME / CONFIG_FILENAME)


def test_roles_must_be_mapping(tmp_path: Path):
    _write_config(tmp_path, "roles: [implementer, closer]\n")
    with pytest.raises(ProcessConfigError):
        load_process_config(tmp_path / CONFIG_DIRNAME / CONFIG_FILENAME)


# ---- tier policy: decomposable / implementable (#1) -------------------


def test_tier_for_type_classifies_against_configured_sets():
    cfg = ProcessConfig(
        decomposable_types=frozenset({"User Story"}),
        implementable_types=frozenset({"Task", "Bug"}),
    )
    assert cfg.tier_for_type("User Story") == "decomposable"
    assert cfg.tier_for_type("Task") == "implementable"
    assert cfg.tier_for_type("Bug") == "implementable"
    # A type named by neither set has no config opinion.
    assert cfg.tier_for_type("Feature") == "unspecified"
    # A missing type is unspecified at this layer (callers fail closed).
    assert cfg.tier_for_type(None) == "unspecified"


def test_default_config_has_no_tier_policy():
    cfg = default_process_config()
    assert not cfg.has_tier_policy()
    assert cfg.tier_for_type("Task") == "unspecified"


def test_has_tier_policy_true_when_either_set_present():
    assert ProcessConfig(implementable_types=frozenset({"Task"})).has_tier_policy()
    assert ProcessConfig(decomposable_types=frozenset({"Epic"})).has_tier_policy()


def test_tier_for_type_honors_aliases_on_input():
    cfg = ProcessConfig(
        type_aliases={"Issue": "Task"},
        implementable_types=frozenset({"Task"}),
    )
    assert cfg.tier_for_type("Issue") == "implementable"


def test_tier_for_type_honors_aliases_on_configured_set():
    # The configured set entry itself is alias-resolved, so an item whose raw
    # type equals the alias target classifies correctly.
    cfg = ProcessConfig(
        type_aliases={"Bug": "Task"},
        implementable_types=frozenset({"Bug"}),
    )
    assert cfg.tier_for_type("Task") == "implementable"


def test_contradictory_tier_sets_fail_closed():
    with pytest.raises(ProcessConfigError):
        ProcessConfig(
            decomposable_types=frozenset({"Task"}),
            implementable_types=frozenset({"Task"}),
        )


def test_alias_induced_tier_contradiction_fails_closed():
    # Bug -> Task makes the two sets overlap after normalization.
    with pytest.raises(ProcessConfigError):
        ProcessConfig(
            type_aliases={"Bug": "Task"},
            decomposable_types=frozenset({"Bug"}),
            implementable_types=frozenset({"Task"}),
        )


def test_contradiction_caught_loading_from_yaml(tmp_path: Path):
    path = _write_config(
        tmp_path,
        "decomposable_types: [Task]\nimplementable_types: [Task]\n",
    )
    with pytest.raises(ProcessConfigError):
        load_process_config(path)


def test_tier_sets_round_trip_through_snapshot():
    cfg = ProcessConfig(
        decomposable_types=frozenset({"Epic", "Feature"}),
        implementable_types=frozenset({"Task"}),
        type_aliases={"Issue": "Task"},
    )
    back = ProcessConfig.from_snapshot(cfg.to_snapshot())
    assert back.tier_for_type("Epic") == "decomposable"
    assert back.tier_for_type("Issue") == "implementable"



def test_load_overrides_root_parent_types(tmp_path: Path):
    path = _write_config(tmp_path, "root_parent_types: [Epic, Initiative]\n")
    cfg = load_process_config(path)
    assert cfg.root_parent_types == frozenset({"Epic", "Initiative"})
    assert cfg.is_root_parent_type("Initiative")
    assert not cfg.is_root_parent_type("Feature")
    assert cfg.source == path
    assert cfg.sha256 is not None


def test_load_empty_file_falls_back_to_defaults(tmp_path: Path):
    path = _write_config(tmp_path, "")
    cfg = load_process_config(path)
    assert cfg.root_parent_types == DEFAULT_ROOT_PARENT_TYPES


def test_load_empty_root_list_falls_back_to_defaults(tmp_path: Path):
    # An empty root set would route everything to a human gate — treat the
    # omission as "use defaults" rather than silently disabling dispatch.
    path = _write_config(tmp_path, "root_parent_types: []\n")
    cfg = load_process_config(path)
    assert cfg.root_parent_types == DEFAULT_ROOT_PARENT_TYPES


def test_load_type_aliases_and_reserved_fields(tmp_path: Path):
    path = _write_config(
        tmp_path,
        "root_parent_types: [Epic]\n"
        "type_aliases:\n"
        "  Bug: Task\n"
        "decomposable_types: [Epic, Feature]\n"
        "implementable_types: [Task, Bug]\n",
    )
    cfg = load_process_config(path)
    assert cfg.normalize_type("Bug") == "Task"
    assert cfg.decomposable_types == frozenset({"Epic", "Feature"})
    assert cfg.implementable_types == frozenset({"Task", "Bug"})


def test_load_tolerates_unknown_keys(tmp_path: Path):
    path = _write_config(
        tmp_path, "root_parent_types: [Epic]\nfuture_knob: whatever\n"
    )
    cfg = load_process_config(path)
    assert cfg.root_parent_types == frozenset({"Epic"})


def test_load_rejects_non_mapping(tmp_path: Path):
    path = _write_config(tmp_path, "- just\n- a\n- list\n")
    with pytest.raises(ProcessConfigError):
        load_process_config(path)


def test_load_rejects_wrong_typed_field(tmp_path: Path):
    path = _write_config(tmp_path, "root_parent_types: Epic\n")  # str, not list
    with pytest.raises(ProcessConfigError):
        load_process_config(path)


def test_load_rejects_non_string_entries(tmp_path: Path):
    path = _write_config(tmp_path, "root_parent_types: [Epic, 7]\n")
    with pytest.raises(ProcessConfigError):
        load_process_config(path)


def test_load_rejects_invalid_yaml(tmp_path: Path):
    path = _write_config(tmp_path, "root_parent_types: [Epic\n")  # unbalanced
    with pytest.raises(ProcessConfigError):
        load_process_config(path)


def test_load_missing_file_raises(tmp_path: Path):
    with pytest.raises(ProcessConfigError):
        load_process_config(tmp_path / "nope.yaml")


# ---- discovery --------------------------------------------------------


def test_discover_walks_up_to_find_config(tmp_path: Path):
    path = _write_config(tmp_path, "root_parent_types: [Initiative]\n")
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    cfg = discover_process_config(nested)
    assert cfg is not None
    assert cfg.root_parent_types == frozenset({"Initiative"})
    assert find_process_config_path(nested) == path


def test_discover_returns_default_when_absent(tmp_path: Path):
    cfg = discover_process_config(tmp_path)
    assert cfg == default_process_config()


def test_discover_returns_none_when_absent_and_no_default(tmp_path: Path):
    assert discover_process_config(tmp_path, default=False) is None


# ---- resolution -------------------------------------------------------


def test_resolve_prefers_explicit(tmp_path: Path):
    _write_config(tmp_path, "root_parent_types: [Initiative]\n")
    explicit = ProcessConfig(root_parent_types=frozenset({"Epic"}))
    assert resolve_process_config(explicit, tmp_path) is explicit


def test_resolve_discovers_then_defaults(tmp_path: Path):
    _write_config(tmp_path, "root_parent_types: [Initiative]\n")
    cfg = resolve_process_config(None, tmp_path)
    assert cfg.root_parent_types == frozenset({"Initiative"})
    # A dir with no config resolves to defaults.
    bare = tmp_path / "elsewhere"
    bare.mkdir()
    # bare has tmp_path as a parent, so discovery would still find the config;
    # use an isolated tree to prove the default fallback.


def test_resolve_falls_back_to_default_in_isolated_tree(tmp_path: Path):
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    cfg = resolve_process_config(None, isolated)
    assert cfg.root_parent_types == DEFAULT_ROOT_PARENT_TYPES


# ---- snapshot round-trip ----------------------------------------------


def test_snapshot_round_trip_is_stable():
    cfg = ProcessConfig(
        root_parent_types=frozenset({"Feature", "Epic"}),
        type_aliases={"Bug": "Task"},
        decomposable_types=frozenset({"Epic"}),
        implementable_types=frozenset({"Task"}),
        source=Path("/x/.requiem-config/process.yaml"),
        sha256="deadbeef",
    )
    snap = cfg.to_snapshot()
    # Order-stable for deterministic event-log bytes.
    assert snap["root_parent_types"] == ["Epic", "Feature"]
    restored = ProcessConfig.from_snapshot(snap)
    assert restored.root_parent_types == cfg.root_parent_types
    assert restored.type_aliases == cfg.type_aliases
    assert restored.decomposable_types == cfg.decomposable_types
    assert restored.sha256 == "deadbeef"
    assert restored.source == Path("/x/.requiem-config/process.yaml")


# ============================================================
# ADR-0026: per-type process config (facets/guidance/depth)
# ============================================================


def test_type_config_facets_drive_derived_flat_sets():
    """The new authoritative source of truth is `types: dict[str, TypeConfig]`.
    The flat decomposable_types / implementable_types sets MUST be derived from
    facets so legacy verb code keeps working without each verb knowing about
    facets. Rule: a type with `plannable` facet → decomposable; a type with
    `implementable` (and NOT `plannable`) → implementable. Types with BOTH
    facets (e.g. Feature in CVAPI) appear in decomposable only — the planner
    is invoked but it's free to return decomposable=false."""
    cfg = ProcessConfig(
        types={
            "Epic": TypeConfig(facets=("plannable",)),
            "Feature": TypeConfig(facets=("plannable", "implementable")),
            "Task": TypeConfig(facets=("implementable", "actionable")),
        }
    )
    assert cfg.decomposable_types == frozenset({"Epic", "Feature"})
    assert cfg.implementable_types == frozenset({"Task"})


def test_type_config_back_compat_legacy_flat_sets_synthesize_types():
    """A config that uses ONLY the legacy flat sets (no `types` map) must
    keep working. The constructor synthesizes a minimal types map from the
    flat sets so downstream code that reads `types` doesn't need a special
    case for legacy configs."""
    cfg = ProcessConfig(
        decomposable_types=frozenset({"Scenario"}),
        implementable_types=frozenset({"Task", "Bug"}),
    )
    # Downstream can read both shapes interchangeably.
    assert "Scenario" in cfg.types
    assert "plannable" in cfg.types["Scenario"].facets
    assert "Task" in cfg.types
    assert "implementable" in cfg.types["Task"].facets
    # And the derived flat sets are stable.
    assert cfg.decomposable_types == frozenset({"Scenario"})
    assert cfg.implementable_types == frozenset({"Task", "Bug"})


def test_type_config_ambiguous_when_both_flat_and_types_provided():
    """A config that mixes the new `types` map with legacy flat sets is
    ambiguous (which is the source of truth?). Fail loud at construction
    time per INV-NO-CORRUPT-FORWARD."""
    with pytest.raises(ProcessConfigError, match="ambiguous"):
        ProcessConfig(
            types={"Task": TypeConfig(facets=("implementable",))},
            decomposable_types=frozenset({"Epic"}),
        )


def test_type_config_decomposition_guidance_accessible_per_type():
    """The planner prompt machinery needs to look up per-type guidance.
    Provide a helper method that returns the guidance string or None."""
    cfg = ProcessConfig(
        types={
            "Scenario": TypeConfig(
                facets=("plannable",),
                decomposition_guidance=(
                    "Decompose into Features. NEVER directly into Tasks."
                ),
            ),
            "Task": TypeConfig(facets=("implementable",)),
        }
    )
    assert cfg.decomposition_guidance_for("Scenario") == (
        "Decompose into Features. NEVER directly into Tasks."
    )
    # Implementable types have no guidance — return None, not empty string.
    assert cfg.decomposition_guidance_for("Task") is None
    # Unknown types — also None (planner falls back to generic prompt).
    assert cfg.decomposition_guidance_for("Spike") is None
    # None input — None output (don't crash on missing work_item_type).
    assert cfg.decomposition_guidance_for(None) is None


def test_type_config_max_nesting_depth_per_type():
    """Each plannable type can carry its own recursion cap. None means
    no per-type cap (subject only to the global max_depth in build_engine)."""
    cfg = ProcessConfig(
        types={
            "Epic": TypeConfig(facets=("plannable",), max_nesting_depth=1),
            "Feature": TypeConfig(facets=("plannable", "implementable")),
            "Task": TypeConfig(facets=("implementable",)),
        }
    )
    assert cfg.max_nesting_depth_for("Epic") == 1
    assert cfg.max_nesting_depth_for("Feature") is None  # unbounded per-type
    assert cfg.max_nesting_depth_for("Task") is None  # leaf, never decomposes
    assert cfg.max_nesting_depth_for("Unknown") is None


def test_load_types_schema_from_yaml(tmp_path: Path):
    """End-to-end: a YAML file using the new schema parses into a config
    whose `types` map has facets, guidance, and depth caps."""
    cfg_path = _write_config(
        tmp_path,
        """
        types:
          Scenario:
            facets: [plannable]
            decomposition_guidance: |
              Decompose into Features. NEVER directly into Tasks.
            max_nesting_depth: 1
          Feature:
            facets: [plannable, implementable]
            decomposition_guidance: |
              Decompose into Tasks.
          Task:
            facets: [implementable, actionable]
            actionable_executor: requiem
        """,
    )
    cfg = load_process_config(cfg_path)
    assert set(cfg.types.keys()) == {"Scenario", "Feature", "Task"}
    sc = cfg.types["Scenario"]
    assert sc.facets == ("plannable",)
    assert "NEVER directly into Tasks" in sc.decomposition_guidance
    assert sc.max_nesting_depth == 1
    feat = cfg.types["Feature"]
    assert feat.facets == ("plannable", "implementable")
    assert feat.max_nesting_depth is None
    task = cfg.types["Task"]
    assert task.facets == ("implementable", "actionable")
    assert task.actionable_executor == "requiem"
    # Derived flat sets are correct.
    assert cfg.decomposable_types == frozenset({"Scenario", "Feature"})
    assert cfg.implementable_types == frozenset({"Task"})


def test_load_types_schema_with_legacy_flat_sets_fails(tmp_path: Path):
    """A YAML file that mixes both shapes must fail at load time, not
    silently prefer one over the other."""
    cfg_path = _write_config(
        tmp_path,
        """
        types:
          Task:
            facets: [implementable]
        decomposable_types: [Epic]
        """,
    )
    with pytest.raises(ProcessConfigError, match="ambiguous"):
        load_process_config(cfg_path)


def test_load_types_invalid_facet_fails_closed(tmp_path: Path):
    """Unknown facet values must fail loud — not silently ignored."""
    cfg_path = _write_config(
        tmp_path,
        """
        types:
          Task:
            facets: [implementable, hallucinated]
        """,
    )
    with pytest.raises(ProcessConfigError, match="(?i)facet"):
        load_process_config(cfg_path)


def test_types_round_trip_through_snapshot():
    """to_snapshot / from_snapshot must preserve the new per-type fields."""
    cfg = ProcessConfig(
        types={
            "Scenario": TypeConfig(
                facets=("plannable",),
                decomposition_guidance="Decompose into Features.",
                max_nesting_depth=2,
            ),
            "Task": TypeConfig(
                facets=("implementable", "actionable"),
                actionable_executor="requiem",
            ),
        }
    )
    snap = cfg.to_snapshot()
    restored = ProcessConfig.from_snapshot(snap)
    assert restored.types == cfg.types
    assert restored.decomposable_types == cfg.decomposable_types
    assert restored.implementable_types == cfg.implementable_types


def test_tier_for_type_works_with_types_schema():
    """tier_for_type (used everywhere downstream) MUST return the right
    classification when the config uses the new types-only schema."""
    cfg = ProcessConfig(
        types={
            "Scenario": TypeConfig(facets=("plannable",)),
            "Feature": TypeConfig(facets=("plannable", "implementable")),
            "Task": TypeConfig(facets=("implementable", "actionable")),
        }
    )
    assert cfg.tier_for_type("Scenario") == "decomposable"
    # Feature is BOTH plannable and implementable — decomposable wins
    # (planner is invoked, may still emit decomposable=false).
    assert cfg.tier_for_type("Feature") == "decomposable"
    assert cfg.tier_for_type("Task") == "implementable"
    assert cfg.tier_for_type("Unknown") == "unspecified"


# ---- models block (ADR-0030 §2 loader; run-#27 follow-up) -------------


def test_models_block_loaded_from_yaml(tmp_path):
    """ADR-0030 §2: ``models:`` in process.yaml round-trips through
    ``load_process_config`` so ``resolve_model_for_role`` finds it.

    Pre-fix gap: ``ProcessConfig.models`` existed as a dataclass field
    but ``_build_from_mapping`` never read the YAML key — every
    operator-supplied ``models:`` block was silently ignored. The unit
    tests for ``model_routing`` constructed ``ProcessConfig`` directly
    so the gap escaped notice until run #27 needed to pin the coder
    agent to a specific model via operator config."""
    from requiem.process_config import load_process_config
    from requiem.model_routing import resolve_model_for_role

    p = tmp_path / "process.yaml"
    p.write_text(
        "root_parent_types: [Scenario]\n"
        "models:\n"
        "  implementer:\n"
        "    provider: copilot\n"
        "    model: claude-sonnet-4.6\n"
        "  planner:\n"
        "    provider: anthropic\n"
        "    model: claude-opus-4.7\n"
        "    max_tokens: 16384\n",
        encoding="utf-8",
    )
    cfg = load_process_config(p)

    # Loaded into the dataclass field.
    assert "implementer" in cfg.models
    assert "planner" in cfg.models

    # Round-trips through the resolver — operator-supplied YAML actually
    # routes calls.
    impl = resolve_model_for_role("implementer", cfg)
    assert impl.provider == "copilot"
    assert impl.model == "claude-sonnet-4.6"
    assert impl.max_tokens is None

    planner = resolve_model_for_role("planner", cfg)
    assert planner.provider == "anthropic"
    assert planner.model == "claude-opus-4.7"
    assert planner.max_tokens == 16384


def test_models_block_absent_is_empty_dict(tmp_path):
    """When the YAML omits ``models:`` entirely, the dataclass field
    defaults to an empty dict (no error). Preserves backward-compat
    for the many process.yaml files in the wild that pre-date
    ADR-0030 §2."""
    from requiem.process_config import load_process_config

    p = tmp_path / "process.yaml"
    p.write_text("root_parent_types: [Scenario]\n", encoding="utf-8")
    cfg = load_process_config(p)
    assert cfg.models == {}


def test_models_block_rejects_non_mapping(tmp_path):
    """A malformed ``models:`` (e.g. a list) fails at load time with
    a path-pointing error — better than a confused KeyError later."""
    import pytest
    from requiem.process_config import load_process_config, ProcessConfigError

    p = tmp_path / "process.yaml"
    p.write_text(
        "root_parent_types: [Scenario]\n"
        "models:\n"
        "  - planner\n"
        "  - implementer\n",
        encoding="utf-8",
    )
    with pytest.raises(ProcessConfigError, match="models.*must be a mapping"):
        load_process_config(p)


def test_models_entry_rejects_non_mapping(tmp_path):
    """A role binding that's not a mapping (e.g. a string shorthand)
    fails at load time, not at first resolve."""
    import pytest
    from requiem.process_config import load_process_config, ProcessConfigError

    p = tmp_path / "process.yaml"
    p.write_text(
        "root_parent_types: [Scenario]\n"
        "models:\n"
        "  implementer: claude-sonnet-4.6\n",
        encoding="utf-8",
    )
    with pytest.raises(ProcessConfigError, match="implementer.*must be a mapping"):
        load_process_config(p)
