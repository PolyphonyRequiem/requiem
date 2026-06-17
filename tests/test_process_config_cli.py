"""Tests for the `--process-config <path>` CLI flag (ADR-0025, decision §1).

When operators have a per-machine process config they don't want to
commit to the target repo (e.g. early dogfood against CVAPI), they
should be able to point requiem-end-to-end at an explicit path that
takes precedence over the `.requiem-config/process.yaml` walking-up
discovery.

Pinned behaviour:
- Explicit `--process-config <path>` wins over discovery.
- Explicit path that doesn't exist is a clear error (no silent fallback
  to defaults — that's how we ended up running with the wrong config in
  the SKU-fallback dogfood).
- When `--process-config` is omitted, discovery still works.
- When discovery finds nothing AND no explicit path, defaults apply.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from requiem.end_to_end import _resolve_process_config
from requiem.process_config import ProcessConfigError, default_process_config


# ---- the explicit path wins ----------------------------------------------


def test_explicit_path_loads_that_file_not_discovered_one(tmp_path: Path):
    """When --process-config <path> is set, that file is loaded even if
    a .requiem-config/process.yaml exists in the discovery path."""
    # Two configs: a "discovered" one in the repo's .requiem-config/,
    # and an explicit one that should win.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".requiem-config").mkdir()
    (repo / ".requiem-config" / "process.yaml").write_text(
        "root_parent_types:\n  - Epic\n", encoding="utf-8",
    )
    explicit = tmp_path / "my-overrides.yaml"
    explicit.write_text(
        "root_parent_types:\n  - Feature\n"
        "decomposable_types:\n  - Feature\n"
        "implementable_types:\n  - Task\n",
        encoding="utf-8",
    )

    cfg = _resolve_process_config(explicit_path=explicit, repo_path=repo)

    # The explicit one's content won, not the discovered one's.
    assert "Feature" in cfg.root_parent_types
    assert "Epic" not in cfg.root_parent_types
    assert "Task" in cfg.implementable_types


def test_explicit_path_that_does_not_exist_raises_clearly(tmp_path: Path):
    """If the operator passes --process-config <path> and the path
    doesn't exist, error LOUDLY. Silent fallback to defaults is what
    caused the 2026-06-17 dogfood to recurse 4 levels deep on Tasks."""
    repo = tmp_path / "repo"
    repo.mkdir()
    bogus = tmp_path / "definitely-not-here.yaml"

    with pytest.raises(ProcessConfigError) as ei:
        _resolve_process_config(explicit_path=bogus, repo_path=repo)
    # Error message must mention the path so the operator can fix it.
    assert str(bogus) in str(ei.value)


def test_explicit_path_invalid_yaml_raises_clearly(tmp_path: Path):
    """Malformed YAML at the explicit path also errors loudly (no silent
    fallback)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    bad = tmp_path / "bad.yaml"
    bad.write_text("not: valid: yaml: at all: :", encoding="utf-8")

    with pytest.raises(ProcessConfigError):
        _resolve_process_config(explicit_path=bad, repo_path=repo)


# ---- discovery still works when explicit is None -------------------------


def test_no_explicit_path_falls_back_to_discovery(tmp_path: Path):
    """When --process-config is omitted, discovery (walking up from
    repo_path) is used exactly as before."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".requiem-config").mkdir()
    (repo / ".requiem-config" / "process.yaml").write_text(
        "implementable_types:\n  - Task\n  - Bug\n",
        encoding="utf-8",
    )

    cfg = _resolve_process_config(explicit_path=None, repo_path=repo)

    assert "Task" in cfg.implementable_types
    assert "Bug" in cfg.implementable_types


def test_no_explicit_no_discovery_returns_defaults(tmp_path: Path):
    """No explicit path AND no .requiem-config in the tree → polyphony
    defaults apply (root_parent_types={Epic, Feature}, no tier policy)."""
    empty = tmp_path / "empty-repo"
    empty.mkdir()
    cfg = _resolve_process_config(explicit_path=None, repo_path=empty)

    # Equivalent to default_process_config()
    expected = default_process_config()
    assert cfg.root_parent_types == expected.root_parent_types
    assert cfg.decomposable_types == expected.decomposable_types
    assert cfg.implementable_types == expected.implementable_types


# ---- argparse plumbing ---------------------------------------------------


def test_run_arg_parser_accepts_process_config():
    """The `requiem-end-to-end` argparser should expose
    `--process-config` as an optional path argument."""
    from requiem.end_to_end import _build_arg_parser

    p = _build_arg_parser()
    args = p.parse_args([
        "--item", "1", "--board", "x",
        "--process-config", "/tmp/my-config.yaml",
    ])
    assert args.process_config == Path("/tmp/my-config.yaml")


def test_run_arg_parser_process_config_defaults_to_none():
    """When --process-config is omitted, the parsed namespace has
    `process_config = None` (not a falsy str or missing attribute)."""
    from requiem.end_to_end import _build_arg_parser

    p = _build_arg_parser()
    args = p.parse_args(["--item", "1", "--board", "x"])
    assert args.process_config is None


def test_integrate_arg_parser_accepts_process_config():
    """Mirror change on the requiem-integrate entrypoint — integration
    runs also need to know the type policy to reason about the leaf-PR
    map's children correctly."""
    from requiem.end_to_end import _build_integrate_arg_parser

    p = _build_integrate_arg_parser()
    args = p.parse_args([
        "--item", "1", "--ado-repo", "org/proj/repo",
        "--process-config", "/tmp/my-config.yaml",
    ])
    assert args.process_config == Path("/tmp/my-config.yaml")
