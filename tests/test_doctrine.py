"""Doctrine loader tests — the house-style artifact (ADR-0016 / ADR-0017).

Covers: empty default with no file, load + hash determinism, discovery walking
up the tree, explicit > discovered > default resolution, snapshot round-trip
(including sha recompute on reconstruct), and fail-loud on a present-but-
unreadable file.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from requiem.doctrine import (
    DOCTRINE_FILENAME,
    Doctrine,
    DoctrineError,
    default_doctrine,
    discover_doctrine,
    find_doctrine_path,
    load_doctrine,
    resolve_doctrine,
)
from requiem.process_config import CONFIG_DIRNAME


def _write_doctrine(root: Path, body: str) -> Path:
    cfg_dir = root / CONFIG_DIRNAME
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / DOCTRINE_FILENAME
    path.write_text(body, encoding="utf-8")
    return path


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---- defaults ---------------------------------------------------------


def test_default_doctrine_is_empty():
    d = default_doctrine()
    assert d.text == ""
    assert d.is_empty
    assert d.source is None
    assert d.sha256 == _sha("")


def test_whitespace_only_doctrine_is_empty():
    assert Doctrine(text="   \n\t  ").is_empty


def test_nonempty_doctrine_is_not_empty():
    assert not Doctrine(text="# House style\nuse targeted pytest").is_empty


# ---- load + hash ------------------------------------------------------


def test_load_reads_text_and_hashes_it(tmp_path: Path):
    body = "# Doctrine\n\n- tests run via targeted pytest, never the full suite\n"
    path = _write_doctrine(tmp_path, body)

    d = load_doctrine(path)

    assert d.text == body
    assert d.source == path
    assert d.sha256 == _sha(body)


def test_hash_is_deterministic_and_path_independent(tmp_path: Path):
    body = "branch naming: feature/<item_id>\n"
    a = load_doctrine(_write_doctrine(tmp_path / "a", body))
    b = load_doctrine(_write_doctrine(tmp_path / "b", body))

    assert a.sha256 == b.sha256
    assert a.source != b.source


def test_load_present_but_unreadable_fails_loud(tmp_path: Path):
    # A directory at the doctrine path reads as present but cannot be read as text.
    missing = tmp_path / "nope" / DOCTRINE_FILENAME
    with pytest.raises(DoctrineError) as exc:
        load_doctrine(missing)
    assert exc.value.path == missing


# ---- discovery --------------------------------------------------------


def test_find_walks_up_to_repo_root(tmp_path: Path):
    _write_doctrine(tmp_path, "root doctrine\n")
    deep = tmp_path / "src" / "pkg" / "mod"
    deep.mkdir(parents=True)

    found = find_doctrine_path(deep)

    assert found == tmp_path / CONFIG_DIRNAME / DOCTRINE_FILENAME


def test_find_returns_none_when_absent(tmp_path: Path):
    assert find_doctrine_path(tmp_path) is None


def test_discover_falls_back_to_empty_default(tmp_path: Path):
    d = discover_doctrine(tmp_path)
    assert d is not None and d.is_empty


def test_discover_without_default_returns_none(tmp_path: Path):
    assert discover_doctrine(tmp_path, default=False) is None


def test_discover_loads_nearest(tmp_path: Path):
    _write_doctrine(tmp_path, "outer\n")
    inner = tmp_path / "sub"
    _write_doctrine(inner, "inner\n")

    d = discover_doctrine(inner / "deep")

    assert d is not None and d.text == "inner\n"


# ---- resolution -------------------------------------------------------


def test_resolve_prefers_explicit(tmp_path: Path):
    _write_doctrine(tmp_path, "on disk\n")
    explicit = Doctrine(text="explicit\n", sha256=_sha("explicit\n"))

    assert resolve_doctrine(explicit, tmp_path).text == "explicit\n"


def test_resolve_discovers_from_repo_path(tmp_path: Path):
    _write_doctrine(tmp_path, "discovered\n")
    assert resolve_doctrine(None, tmp_path).text == "discovered\n"


def test_resolve_empty_when_nothing(tmp_path: Path):
    assert resolve_doctrine(None, tmp_path).is_empty


# ---- snapshot round-trip ---------------------------------------------


def test_snapshot_round_trip_preserves_content(tmp_path: Path):
    d = load_doctrine(_write_doctrine(tmp_path, "# rules\nfail closed\n"))
    snap = d.to_snapshot()

    back = Doctrine.from_snapshot(snap)

    assert back.text == d.text
    assert back.sha256 == d.sha256
    assert back.source == d.source


def test_snapshot_is_json_safe():
    import json

    d = Doctrine(text="x\n", source=Path("/repo/.requiem-config/doctrine.md"))
    payload = json.dumps(d.to_snapshot())
    assert "sha256" in payload


def test_from_snapshot_recomputes_sha_from_text():
    # A tampered snapshot whose sha disagrees with its text must not be trusted.
    tampered = {"text": "real content\n", "source": None, "sha256": "deadbeef"}
    back = Doctrine.from_snapshot(tampered)
    assert back.sha256 == _sha("real content\n")
    assert back.sha256 != "deadbeef"


def test_from_snapshot_handles_missing_text():
    back = Doctrine.from_snapshot({})
    assert back.text == ""
    assert back.is_empty
