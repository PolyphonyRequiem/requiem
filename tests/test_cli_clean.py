"""Tests for `requiem clean` — per-item state reset subcommand."""
from __future__ import annotations

import json
import sys as _sys
from pathlib import Path
from unittest.mock import patch

import pytest

import requiem.cli.main  # ensure the submodule is registered

cli_main = _sys.modules["requiem.cli.main"]


# ---- helpers ----------------------------------------------------------


def _seed_full_run(
    log_dir: Path,
    item_id: int,
    *,
    with_subworkflows: bool = True,
    with_deep_recursion: bool = False,
) -> list[Path]:
    """Create the full set of artifacts the end-to-end driver writes for
    one item. Returns the list of paths created so tests can check each.

    Per ADR-0026, the commit_plan manifest and leaf-pr-map are
    *preserved by default* (they encode cross-run idempotency state
    that ADO doesn't preserve via HTML-comment markers). To check
    both groups together, use ``_seed_ephemeral`` + ``_seed_manifest``
    helpers separately, or inspect the returned list element-by-element.
    """
    ephemeral, manifest = _seed_ephemeral(
        log_dir,
        item_id,
        with_subworkflows=with_subworkflows,
        with_deep_recursion=with_deep_recursion,
    ), _seed_manifest(log_dir, item_id)
    return ephemeral + manifest


def _seed_ephemeral(
    log_dir: Path,
    item_id: int,
    *,
    with_subworkflows: bool = True,
    with_deep_recursion: bool = False,
) -> list[Path]:
    """Files that ``requiem clean`` removes by default."""
    paths = [
        log_dir / f"plan-{item_id}.events.jsonl",
        log_dir / f"plan-{item_id}.plan.md",
        log_dir / f"plan-{item_id}.plan.tree.json",
        log_dir / f"commit-{item_id}.events.jsonl",
        log_dir / f"trunk-{item_id}.events.jsonl",
        log_dir / f"exec-{item_id}.events.jsonl",
        log_dir / f"fanout-{item_id}.events.jsonl",
        log_dir / f"leafpr-{item_id}.events.jsonl",
        log_dir / f"featurepr-{item_id}.events.jsonl",
    ]
    if with_subworkflows:
        paths.extend([
            log_dir / f"plan-{item_id}__child_1.events.jsonl",
            log_dir / f"plan-{item_id}__child_2.events.jsonl",
            log_dir / f"plan-{item_id}__child_1.plan.tree.json",
            log_dir / f"fanout-{item_id}__leaf-7777.events.jsonl",
        ])
    if with_deep_recursion:
        # Scenario → Feature → Task spawns plan-N__child_X__child_Y.*
        # ADR-0026 dogfood: these were noted as not cleaned previously.
        paths.extend([
            log_dir / f"plan-{item_id}__child_1__child_1.events.jsonl",
            log_dir / f"plan-{item_id}__child_1__child_2.events.jsonl",
            log_dir / f"plan-{item_id}__child_1__child_1.plan.md",
            log_dir / f"plan-{item_id}__child_2__child_3.plan.tree.json",
        ])
    for p in paths:
        p.write_text("{}", encoding="utf-8")
    return paths


def _seed_manifest(log_dir: Path, item_id: int) -> list[Path]:
    """Files that ``requiem clean`` preserves by default (ADR-0026)."""
    paths = [
        log_dir / f"commit-{item_id}.plan.committed.json",
        log_dir / f"leaf-pr-map-{item_id}.json",
    ]
    for p in paths:
        p.write_text("{}", encoding="utf-8")
    return paths


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs"
    d.mkdir()
    return d


# ---- dry-run ----------------------------------------------------------


def test_clean_dry_run_lists_artifacts_without_removing(
    log_dir: Path, capsys: pytest.CaptureFixture[str]
):
    """--dry-run prints what would be removed but leaves the files alone."""
    paths = _seed_ephemeral(log_dir, item_id=42)
    rc = cli_main.main([
        "clean", "--item", "42", "--log-dir", str(log_dir), "--dry-run",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    # All files still exist.
    for p in paths:
        assert p.exists(), f"dry-run should NOT delete {p}"
    # Every artifact listed in output.
    for p in paths:
        assert p.name in out, f"expected {p.name} listed in dry-run output"
    assert "nothing removed" in out.lower()


def test_clean_dry_run_with_no_matches_is_quiet(
    log_dir: Path, capsys: pytest.CaptureFixture[str]
):
    """--dry-run for an item with no artifacts says so cleanly."""
    rc = cli_main.main([
        "clean", "--item", "999", "--log-dir", str(log_dir), "--dry-run",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no artifacts matched" in out.lower()


# ---- destructive (default) -------------------------------------------


def test_clean_removes_ephemeral_artifacts_for_one_item(
    log_dir: Path, capsys: pytest.CaptureFixture[str]
):
    """Default `clean` removes every EPHEMERAL per-item artifact + subworkflow
    log, but PRESERVES the commit-plan manifest and leaf-pr-map (ADR-0026)."""
    ephemeral = _seed_ephemeral(log_dir, item_id=42)
    manifest = _seed_manifest(log_dir, item_id=42)
    # Also seed an UNRELATED item's artifacts to prove we don't touch them.
    other = log_dir / "plan-999.events.jsonl"
    other.write_text("{}", encoding="utf-8")

    rc = cli_main.main([
        "clean", "--item", "42", "--log-dir", str(log_dir),
    ])
    assert rc == 0
    for p in ephemeral:
        assert not p.exists(), f"clean should have removed ephemeral file {p}"
    for p in manifest:
        assert p.exists(), (
            f"clean (without --include-manifest) MUST preserve manifest "
            f"file {p} per ADR-0026 (cross-run idempotency)"
        )
    # Unrelated item untouched.
    assert other.exists(), "clean must not touch artifacts for other items"


def test_clean_include_manifest_removes_everything(log_dir: Path):
    """--include-manifest opts into nuking the manifest + leaf-pr-map too,
    for the explicit "I want a truly fresh seed" path."""
    ephemeral = _seed_ephemeral(log_dir, item_id=42)
    manifest = _seed_manifest(log_dir, item_id=42)
    rc = cli_main.main([
        "clean", "--item", "42", "--log-dir", str(log_dir),
        "--include-manifest",
    ])
    assert rc == 0
    for p in ephemeral + manifest:
        assert not p.exists(), (
            f"--include-manifest should have removed {p}"
        )


def test_clean_finds_deep_recursion_subworkflow_logs(log_dir: Path):
    """ADR-0026: Scenario → Feature → Task creates two-level subworkflow
    logs (plan-N__child_X__child_Y.events.jsonl). These weren't matched
    by the original glob; the fix added explicit deeper-recursion patterns."""
    ephemeral = _seed_ephemeral(
        log_dir, item_id=42, with_deep_recursion=True
    )
    rc = cli_main.main([
        "clean", "--item", "42", "--log-dir", str(log_dir),
    ])
    assert rc == 0
    for p in ephemeral:
        assert not p.exists(), f"deep recursion file not cleaned: {p}"
    # And there's nothing left matching the deep glob.
    leftover = list(log_dir.glob("plan-42__child_*__child_*"))
    assert leftover == [], (
        f"deep subworkflow logs not fully cleaned: {leftover}"
    )


def test_clean_finds_subworkflow_logs(log_dir: Path):
    """The recursion fix creates `plan-N__child_*.events.jsonl` files;
    `clean` must match them by glob."""
    _seed_full_run(log_dir, item_id=42, with_subworkflows=True)
    rc = cli_main.main(["clean", "--item", "42", "--log-dir", str(log_dir)])
    assert rc == 0
    remaining_subwf = list(log_dir.glob("plan-42__child_*.events.jsonl"))
    assert remaining_subwf == [], (
        f"subworkflow logs should have been removed: {remaining_subwf}"
    )


def test_clean_finds_fanout_leaf_logs(log_dir: Path):
    """Same for `fanout-N__leaf-*.events.jsonl` (in-process backend)."""
    _seed_full_run(log_dir, item_id=42, with_subworkflows=True)
    rc = cli_main.main(["clean", "--item", "42", "--log-dir", str(log_dir)])
    assert rc == 0
    leaf_logs = list(log_dir.glob("fanout-42__leaf-*.events.jsonl"))
    assert leaf_logs == []


# ---- safety: in-flight PR check --------------------------------------


def test_clean_refuses_when_leaf_pr_map_has_real_pr_numbers(
    log_dir: Path, capsys: pytest.CaptureFixture[str]
):
    """If leaf-pr-map shows a real PR number, refuse to clean (cleaning
    would lose the linkage). Operator must pass --force to override."""
    _seed_full_run(log_dir, item_id=42)
    # Overwrite the leaf-pr-map with a real PR number reference.
    map_path = log_dir / "leaf-pr-map-42.json"
    map_path.write_text(json.dumps({
        "item_id": 42,
        "leaves": [{"leaf_id": "L1", "pr_number": 12345}],
    }), encoding="utf-8")

    rc = cli_main.main(["clean", "--item", "42", "--log-dir", str(log_dir)])
    out = capsys.readouterr().out
    assert rc != 0, "should refuse to clean when in-flight PRs exist"
    assert "PR #12345" in out
    assert "--force" in out
    # All files untouched.
    assert (log_dir / "plan-42.events.jsonl").exists()


def test_clean_force_overrides_in_flight_pr_check(log_dir: Path):
    """--force lets the operator clean even when leaf-pr-map has PR refs.
    Note: --force only bypasses the in-flight safety check, NOT the
    default manifest-preservation; combine with --include-manifest to
    nuke everything."""
    ephemeral = _seed_ephemeral(log_dir, item_id=42)
    manifest = _seed_manifest(log_dir, item_id=42)
    map_path = log_dir / "leaf-pr-map-42.json"
    map_path.write_text(json.dumps({
        "item_id": 42,
        "leaves": [{"leaf_id": "L1", "pr_number": 12345}],
    }), encoding="utf-8")

    rc = cli_main.main([
        "clean", "--item", "42", "--log-dir", str(log_dir), "--force",
    ])
    assert rc == 0
    for p in ephemeral:
        assert not p.exists(), f"--force should have removed ephemeral {p}"
    # Manifest still preserved (--force ≠ --include-manifest).
    for p in manifest:
        assert p.exists(), (
            f"--force should NOT touch manifest {p}; "
            f"only --include-manifest does that"
        )


def test_clean_leaf_pr_map_without_pr_numbers_is_fine(log_dir: Path):
    """A leaf-pr-map with null pr_number values (dry-run state) is NOT
    in-flight and should clean ephemeral files without --force.
    The manifest+leaf-pr-map itself is still preserved per ADR-0026."""
    ephemeral = _seed_ephemeral(log_dir, item_id=42)
    map_path = log_dir / "leaf-pr-map-42.json"
    map_path.write_text(json.dumps({
        "item_id": 42,
        "leaves": [{"leaf_id": "L1", "pr_number": None}],
    }), encoding="utf-8")

    rc = cli_main.main(["clean", "--item", "42", "--log-dir", str(log_dir)])
    assert rc == 0
    for p in ephemeral:
        assert not p.exists()
    # leaf-pr-map preserved (manifest semantics) even though pr_number was null.
    assert map_path.exists(), (
        "leaf-pr-map preserved by default; nullable pr_number doesn't change that"
    )


def test_clean_corrupt_leaf_pr_map_is_tolerated(
    log_dir: Path, capsys: pytest.CaptureFixture[str]
):
    """A corrupt leaf-pr-map shouldn't crash clean; treat as 'no in-flight'."""
    _seed_full_run(log_dir, item_id=42)
    (log_dir / "leaf-pr-map-42.json").write_text(
        "not json {{{", encoding="utf-8",
    )
    rc = cli_main.main(["clean", "--item", "42", "--log-dir", str(log_dir)])
    assert rc == 0


# ---- --keep-artifacts -------------------------------------------------


def test_keep_artifacts_preserves_event_logs(log_dir: Path):
    """--keep-artifacts means the event logs survive (useful for forensic
    inspection while still allowing a re-run)."""
    paths = _seed_full_run(log_dir, item_id=42)
    rc = cli_main.main([
        "clean", "--item", "42", "--log-dir", str(log_dir), "--keep-artifacts",
    ])
    assert rc == 0
    for p in paths:
        assert p.exists(), f"--keep-artifacts should have preserved {p}"


# ---- --ado-delete -----------------------------------------------------


def test_ado_delete_invokes_twig_delete(log_dir: Path):
    """--ado-delete calls twig delete <id> --force (after best-effort
    unparenting via twig set + twig link unparent)."""
    _seed_full_run(log_dir, item_id=42)
    twig_calls: list[list[str]] = []

    def fake_run(cmd, *_, **__):
        twig_calls.append(list(cmd))
        from subprocess import CompletedProcess
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with patch.object(cli_main, "_twig_delete", wraps=cli_main._twig_delete):
        with patch("subprocess.run", side_effect=fake_run):
            rc = cli_main.main([
                "clean", "--item", "42", "--log-dir", str(log_dir), "--ado-delete",
            ])
    assert rc == 0
    # We expect the three twig calls: set, link unparent, delete --force.
    delete_cmd = next(
        (c for c in twig_calls if len(c) >= 3 and c[1] == "delete"), None,
    )
    assert delete_cmd == ["twig", "delete", "42", "--force"], twig_calls
    # Set + link unparent should also have been called.
    assert any(c[:3] == ["twig", "set", "42"] for c in twig_calls)
    assert any(c[:3] == ["twig", "link", "unparent"] for c in twig_calls)


def test_ado_delete_failure_surfaces_exit_code(log_dir: Path):
    """If twig delete fails (rc=1), clean exits non-zero and says so."""
    _seed_full_run(log_dir, item_id=42)

    def fake_run(cmd, *_, **__):
        from subprocess import CompletedProcess
        rc = 1 if cmd[:2] == ["twig", "delete"] else 0
        return CompletedProcess(
            args=cmd, returncode=rc, stdout="", stderr="permission denied",
        )

    with patch("subprocess.run", side_effect=fake_run):
        rc = cli_main.main([
            "clean", "--item", "42", "--log-dir", str(log_dir), "--ado-delete",
        ])
    assert rc != 0


def test_clean_without_ado_delete_does_not_touch_twig(log_dir: Path):
    """Default `clean` (no --ado-delete) must NEVER call twig."""
    _seed_full_run(log_dir, item_id=42)

    def fake_run(cmd, *_, **__):
        raise AssertionError(
            f"subprocess.run should not be invoked without --ado-delete; "
            f"got {cmd!r}"
        )

    with patch("subprocess.run", side_effect=fake_run):
        rc = cli_main.main([
            "clean", "--item", "42", "--log-dir", str(log_dir),
        ])
    assert rc == 0


# ---- log dir missing --------------------------------------------------


def test_clean_with_missing_log_dir_is_a_noop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """Cleaning against a log-dir that doesn't exist is fine — there's
    just nothing to clean."""
    nonexistent = tmp_path / "never-existed"
    rc = cli_main.main([
        "clean", "--item", "42", "--log-dir", str(nonexistent),
    ])
    assert rc == 0
