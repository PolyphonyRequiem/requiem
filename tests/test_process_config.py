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
