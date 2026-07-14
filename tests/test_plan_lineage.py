from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from requiem.clients.twig import TwigItem
from requiem.plan_lineage import (
    LineageMigrationError,
    format_commit_marker,
    marker_belongs_to_scenario,
    migrate_commit_manifest,
    parse_commit_marker,
)


ROOT = 100


def _item(
    item_id: int,
    title: str,
    work_item_type: str,
    parent_id: int | None,
    description: str = "",
) -> TwigItem:
    return TwigItem(
        id=item_id,
        title=title,
        state="Proposed",
        area_path="Area",
        work_item_type=work_item_type,
        parent_id=parent_id,
        raw={"fields": {"System.Description": description}},
    )


@dataclass
class FakeLineageTwig:
    items: dict[int, TwigItem]
    append_calls: list[tuple[int, str]] = field(default_factory=list)

    async def show_async(self, item_id: int) -> TwigItem:
        return self.items[item_id]

    async def list_children_async(self, parent_id: int) -> list[TwigItem]:
        return [
            item for item in self.items.values()
            if item.parent_id == parent_id
        ]

    async def append_description_async(
        self,
        item_id: int,
        text: str,
    ) -> TwigItem:
        self.append_calls.append((item_id, text))
        old = self.items[item_id]
        description = (
            (old.raw.get("fields") or {}).get("System.Description") or ""
        )
        updated = _item(
            old.id,
            old.title,
            old.work_item_type,
            old.parent_id,
            f"{description}<p>{text}</p>",
        )
        self.items[item_id] = updated
        return updated


def _twig() -> FakeLineageTwig:
    return FakeLineageTwig(
        items={
            ROOT: _item(ROOT, "Root", "Scenario", None),
            201: _item(201, "Deliverable", "Deliverable", ROOT),
            202: _item(202, "Task", "Task", 201),
        }
    )


def _manifest(path: Path) -> Path:
    payload = {
        "schema_version": 1,
        "plan_id": f"plan-{ROOT}-prior",
        "root_item_id": ROOT,
        "dry_run": False,
        "created_count": 2,
        "would_create_count": 0,
        "reused_count": 0,
        "id_map": {"10001": 201, "1000101": 202},
        "ledger": [
            {
                "synth_id": 10001,
                "real_id": 201,
                "title": "Deliverable",
                "work_item_type": "Deliverable",
                "parent_real_id": ROOT,
                "status": "created",
            },
            {
                "synth_id": 1000101,
                "real_id": 202,
                "title": "Task",
                "work_item_type": "Task",
                "parent_real_id": 201,
                "status": "created",
            },
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_durable_marker_survives_ado_html_wrapping():
    text = format_commit_marker(
        f"plan-{ROOT}-prior",
        10001,
        scenario_id=ROOT,
    )
    marker = parse_commit_marker(
        _item(201, "Deliverable", "Deliverable", ROOT, f"<p>{text}</p>")
    )

    assert marker is not None
    assert marker.durable is True
    assert marker.scenario_id == ROOT
    assert marker_belongs_to_scenario(marker, ROOT)


def test_legacy_html_comment_is_parsed_but_not_durable():
    marker = parse_commit_marker(
        _item(
            201,
            "Deliverable",
            "Deliverable",
            ROOT,
            f"<!-- requiem-commit plan_id=plan-{ROOT}-prior synth_id=10001 -->",
        )
    )

    assert marker is not None
    assert marker.durable is False
    assert not marker_belongs_to_scenario(marker, ROOT)


async def test_manifest_migration_previews_then_applies_idempotently(
    tmp_path: Path,
):
    twig = _twig()
    manifest = _manifest(tmp_path / "committed.json")

    preview = await migrate_commit_manifest(
        twig,
        manifest,
        scenario_id=ROOT,
    )
    assert preview.verified_count == 2
    assert preview.pending_count == 2
    assert preview.migrated_count == 0
    assert twig.append_calls == []

    applied = await migrate_commit_manifest(
        twig,
        manifest,
        scenario_id=ROOT,
        apply=True,
    )
    assert applied.migrated_count == 2
    assert applied.pending_count == 0
    assert [item_id for item_id, _ in twig.append_calls] == [201, 202]

    repeated = await migrate_commit_manifest(
        twig,
        manifest,
        scenario_id=ROOT,
        apply=True,
    )
    assert repeated.already_durable_count == 2
    assert repeated.migrated_count == 0
    assert len(twig.append_calls) == 2


async def test_manifest_migration_converts_exact_legacy_marker(
    tmp_path: Path,
):
    twig = _twig()
    twig.items[201] = _item(
        201,
        "Deliverable",
        "Deliverable",
        ROOT,
        f"<!-- requiem-commit plan_id=plan-{ROOT}-prior synth_id=10001 -->",
    )

    result = await migrate_commit_manifest(
        twig,
        _manifest(tmp_path / "committed.json"),
        scenario_id=ROOT,
        apply=True,
    )

    assert result.migrated_count == 2
    marker = parse_commit_marker(twig.items[201])
    assert marker is not None and marker.durable


async def test_manifest_migration_fails_before_writes_on_title_drift(
    tmp_path: Path,
):
    twig = _twig()
    twig.items[202] = _item(202, "Renamed task", "Task", 201)

    with pytest.raises(LineageMigrationError, match="title"):
        await migrate_commit_manifest(
            twig,
            _manifest(tmp_path / "committed.json"),
            scenario_id=ROOT,
            apply=True,
        )

    assert twig.append_calls == []


async def test_manifest_migration_rejects_duplicate_exact_sibling(
    tmp_path: Path,
):
    twig = _twig()
    twig.items[203] = _item(203, "Task", "Task", 201)

    with pytest.raises(LineageMigrationError, match="exact sibling"):
        await migrate_commit_manifest(
            twig,
            _manifest(tmp_path / "committed.json"),
            scenario_id=ROOT,
            apply=True,
        )

    assert twig.append_calls == []
