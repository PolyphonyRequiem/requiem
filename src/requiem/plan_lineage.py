"""Durable Requiem plan lineage and legacy-manifest migration."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from requiem.clients.twig import TwigItem


_DURABLE_MARKER_RE = re.compile(
    r"Requiem-Lineage-v1:\s+scenario_id=(?P<scenario>\d+)\s+"
    r"plan_id=(?P<plan>[^\s<]+)\s+synth_id=(?P<synth>\d+)"
)
_LEGACY_COMMIT_MARKER_RE = re.compile(
    r"<!--\s*requiem-commit\s+plan_id=(?P<plan>\S+)\s+"
    r"synth_id=(?P<synth>\d+)\s*-->"
)
_PLAN_SCENARIO_RE = re.compile(r"^plan-(?P<scenario>\d+)(?:-|$)")
_MAX_MIGRATION_ITEMS = 1000


class LineageMigrationError(ValueError):
    """A manifest or live ADO hierarchy cannot be migrated safely."""


class LineageMigrationClient(Protocol):
    async def show_async(self, item_id: int) -> TwigItem: ...

    async def list_children_async(self, parent_id: int) -> list[TwigItem]: ...

    async def append_description_async(
        self, item_id: int, text: str
    ) -> TwigItem: ...


@dataclass(frozen=True, slots=True)
class CommitMarker:
    scenario_id: int | None
    plan_id: str
    synth_id: int
    durable: bool


@dataclass(frozen=True, slots=True)
class LineageMigrationEntry:
    synth_id: int
    real_id: int
    parent_real_id: int
    title: str
    work_item_type: str


@dataclass(frozen=True, slots=True)
class LineageMigrationResult:
    scenario_id: int
    plan_id: str
    verified_count: int
    already_durable_count: int
    pending_count: int
    migrated_count: int
    applied: bool


def format_commit_marker(
    plan_id: str,
    synth_id: int,
    *,
    scenario_id: int | None = None,
) -> str:
    """Format a visible marker that survives ADO description sanitization."""
    root_id = (
        int(scenario_id)
        if scenario_id is not None
        else scenario_id_from_plan_id(plan_id)
    )
    if root_id is None:
        raise ValueError(
            f"cannot derive Scenario id from plan_id {plan_id!r}; "
            "pass scenario_id explicitly"
        )
    return (
        f"Requiem-Lineage-v1: scenario_id={root_id} "
        f"plan_id={plan_id} synth_id={int(synth_id)}"
    )


def item_description(item: TwigItem) -> str:
    """Extract an item's description across the Twig payload shapes we support."""
    raw = item.raw or {}
    for key in ("description", "Description"):
        value = raw.get(key)
        if value:
            return str(value)
    fields = raw.get("fields") or {}
    return str(fields.get("System.Description") or "")


def parse_commit_marker(item: TwigItem) -> CommitMarker | None:
    description = item_description(item)
    durable = _DURABLE_MARKER_RE.search(description)
    if durable is not None:
        return CommitMarker(
            scenario_id=int(durable.group("scenario")),
            plan_id=durable.group("plan"),
            synth_id=int(durable.group("synth")),
            durable=True,
        )
    legacy = _LEGACY_COMMIT_MARKER_RE.search(description)
    if legacy is None:
        return None
    plan_id = legacy.group("plan")
    return CommitMarker(
        scenario_id=scenario_id_from_plan_id(plan_id),
        plan_id=plan_id,
        synth_id=int(legacy.group("synth")),
        durable=False,
    )


def scenario_id_from_plan_id(plan_id: str) -> int | None:
    match = _PLAN_SCENARIO_RE.match(plan_id)
    return int(match.group("scenario")) if match is not None else None


def marker_belongs_to_scenario(marker: CommitMarker | None, scenario_id: int) -> bool:
    return (
        marker is not None
        and marker.durable
        and marker.scenario_id == int(scenario_id)
    )


def marker_matches(
    marker: CommitMarker | None,
    *,
    scenario_id: int,
    plan_id: str,
    synth_id: int,
) -> bool:
    return (
        marker_belongs_to_scenario(marker, scenario_id)
        and marker is not None
        and marker.plan_id == str(plan_id)
        and marker.synth_id == int(synth_id)
    )


def _load_manifest(
    path: Path,
    *,
    scenario_id: int,
) -> tuple[str, tuple[LineageMigrationEntry, ...]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LineageMigrationError(f"manifest not found: {path}") from exc
    except OSError as exc:
        raise LineageMigrationError(f"manifest unreadable: {path} ({exc})") from exc
    except json.JSONDecodeError as exc:
        raise LineageMigrationError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise LineageMigrationError("manifest root must be a JSON object")
    if payload.get("dry_run") is not False:
        raise LineageMigrationError("manifest must record a real commit, not a dry-run")
    if payload.get("root_item_id") != int(scenario_id):
        raise LineageMigrationError(
            f"manifest root_item_id {payload.get('root_item_id')!r} does not "
            f"match Scenario {scenario_id}"
        )
    plan_id = payload.get("plan_id")
    if not isinstance(plan_id, str) or not plan_id:
        raise LineageMigrationError("manifest plan_id must be a non-empty string")
    if scenario_id_from_plan_id(plan_id) != int(scenario_id):
        raise LineageMigrationError(
            f"manifest plan_id {plan_id!r} is not rooted at Scenario {scenario_id}"
        )

    raw_ledger = payload.get("ledger")
    if not isinstance(raw_ledger, list) or not raw_ledger:
        raise LineageMigrationError("manifest ledger must be a non-empty list")
    entries: list[LineageMigrationEntry] = []
    for index, raw in enumerate(raw_ledger):
        if not isinstance(raw, dict):
            raise LineageMigrationError(f"ledger[{index}] must be an object")
        values = {
            "synth_id": raw.get("synth_id"),
            "real_id": raw.get("real_id"),
            "parent_real_id": raw.get("parent_real_id"),
        }
        for name, value in values.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise LineageMigrationError(
                    f"ledger[{index}].{name} must be an integer"
                )
        title = raw.get("title")
        work_item_type = raw.get("work_item_type")
        if not isinstance(title, str) or not title:
            raise LineageMigrationError(
                f"ledger[{index}].title must be a non-empty string"
            )
        if not isinstance(work_item_type, str) or not work_item_type:
            raise LineageMigrationError(
                f"ledger[{index}].work_item_type must be a non-empty string"
            )
        entries.append(
            LineageMigrationEntry(
                synth_id=values["synth_id"],
                real_id=values["real_id"],
                parent_real_id=values["parent_real_id"],
                title=title,
                work_item_type=work_item_type,
            )
        )

    synth_ids = [entry.synth_id for entry in entries]
    real_ids = [entry.real_id for entry in entries]
    if len(set(synth_ids)) != len(synth_ids):
        raise LineageMigrationError("manifest ledger contains duplicate synth ids")
    if len(set(real_ids)) != len(real_ids):
        raise LineageMigrationError("manifest ledger contains duplicate real ids")

    raw_id_map = payload.get("id_map")
    if not isinstance(raw_id_map, dict):
        raise LineageMigrationError("manifest id_map must be an object")
    try:
        id_map = {int(key): int(value) for key, value in raw_id_map.items()}
    except (TypeError, ValueError) as exc:
        raise LineageMigrationError(
            f"manifest id_map contains non-integer entries: {exc}"
        ) from exc
    ledger_map = {entry.synth_id: entry.real_id for entry in entries}
    if id_map != ledger_map:
        raise LineageMigrationError(
            "manifest id_map does not exactly match its ledger"
        )

    real_id_set = set(real_ids)
    for entry in entries:
        if (
            entry.parent_real_id != int(scenario_id)
            and entry.parent_real_id not in real_id_set
        ):
            raise LineageMigrationError(
                f"AB#{entry.real_id} names parent AB#{entry.parent_real_id}, "
                "which is outside the manifest hierarchy"
            )

    parent_by_real = {
        entry.real_id: entry.parent_real_id
        for entry in entries
    }
    for entry in entries:
        seen: set[int] = set()
        current = entry.real_id
        while current != int(scenario_id):
            if current in seen:
                raise LineageMigrationError(
                    f"manifest hierarchy contains a cycle at AB#{current}"
                )
            seen.add(current)
            parent = parent_by_real.get(current)
            if parent is None:
                raise LineageMigrationError(
                    f"manifest hierarchy for AB#{entry.real_id} does not reach "
                    f"Scenario AB#{scenario_id}"
                )
            current = parent
    return plan_id, tuple(entries)


async def _inventory_scenario(
    twig: LineageMigrationClient,
    *,
    scenario_id: int,
) -> dict[int, TwigItem]:
    root = await twig.show_async(scenario_id)
    if root.id != int(scenario_id) or root.work_item_type != "Scenario":
        raise LineageMigrationError(
            f"AB#{scenario_id} is not the exact Scenario root "
            f"(id={root.id}, type={root.work_item_type!r})"
        )
    items: dict[int, TwigItem] = {root.id: root}
    queue = [root.id]
    while queue:
        parent_id = queue.pop(0)
        children = await twig.list_children_async(parent_id)
        for child in children:
            if child.parent_id != parent_id:
                raise LineageMigrationError(
                    f"ADO listed AB#{child.id} under AB#{parent_id}, but its "
                    f"authoritative parent is {child.parent_id!r}"
                )
            if child.id in items:
                raise LineageMigrationError(
                    f"ADO Scenario hierarchy repeats AB#{child.id}"
                )
            if len(items) >= _MAX_MIGRATION_ITEMS:
                raise LineageMigrationError(
                    f"Scenario AB#{scenario_id} exceeds the "
                    f"{_MAX_MIGRATION_ITEMS}-item migration safety cap"
                )
            items[child.id] = child
            queue.append(child.id)
    return items


def _validate_live_hierarchy(
    items: dict[int, TwigItem],
    entries: tuple[LineageMigrationEntry, ...],
    *,
    scenario_id: int,
    plan_id: str,
) -> list[LineageMigrationEntry]:
    by_parent: dict[int, list[TwigItem]] = {}
    for item in items.values():
        if item.parent_id is not None:
            by_parent.setdefault(item.parent_id, []).append(item)

    expected_by_key = {
        (plan_id, entry.synth_id): entry.real_id
        for entry in entries
    }
    for item in items.values():
        marker = parse_commit_marker(item)
        if not marker_belongs_to_scenario(marker, scenario_id):
            continue
        assert marker is not None
        expected_real = expected_by_key.get((marker.plan_id, marker.synth_id))
        if expected_real is not None and expected_real != item.id:
            raise LineageMigrationError(
                f"lineage {marker.plan_id}/{marker.synth_id} is already claimed "
                f"by AB#{item.id}, expected AB#{expected_real}"
            )

    pending: list[LineageMigrationEntry] = []
    for entry in entries:
        item = items.get(entry.real_id)
        if item is None:
            raise LineageMigrationError(
                f"manifest item AB#{entry.real_id} is not under Scenario "
                f"AB#{scenario_id}"
            )
        conflicts: list[str] = []
        if item.parent_id != entry.parent_real_id:
            conflicts.append(
                f"parent {item.parent_id!r} != {entry.parent_real_id}"
            )
        if item.title != entry.title:
            conflicts.append(f"title {item.title!r} != {entry.title!r}")
        if item.work_item_type != entry.work_item_type:
            conflicts.append(
                f"type {item.work_item_type!r} != {entry.work_item_type!r}"
            )
        exact_siblings = [
            sibling
            for sibling in by_parent.get(entry.parent_real_id, [])
            if sibling.title == entry.title
            and sibling.work_item_type == entry.work_item_type
        ]
        if len(exact_siblings) != 1 or exact_siblings[0].id != entry.real_id:
            conflicts.append(
                f"exact sibling match count is {len(exact_siblings)}, expected "
                f"only AB#{entry.real_id}"
            )
        marker = parse_commit_marker(item)
        if marker is None:
            pending.append(entry)
        elif (
            marker.scenario_id == int(scenario_id)
            and marker.plan_id == plan_id
            and marker.synth_id == entry.synth_id
            and not marker.durable
        ):
            pending.append(entry)
        elif not marker_matches(
            marker,
            scenario_id=scenario_id,
            plan_id=plan_id,
            synth_id=entry.synth_id,
        ):
            conflicts.append(
                "existing Requiem lineage does not match the manifest"
            )
        if conflicts:
            raise LineageMigrationError(
                f"AB#{entry.real_id} failed migration validation: "
                + "; ".join(conflicts)
            )
    return pending


async def migrate_commit_manifest(
    twig: LineageMigrationClient,
    manifest_path: Path,
    *,
    scenario_id: int,
    apply: bool = False,
) -> LineageMigrationResult:
    """Verify, then optionally stamp, durable lineage from a commit manifest."""
    plan_id, entries = _load_manifest(
        manifest_path,
        scenario_id=scenario_id,
    )
    items = await _inventory_scenario(twig, scenario_id=scenario_id)
    pending = _validate_live_hierarchy(
        items,
        entries,
        scenario_id=scenario_id,
        plan_id=plan_id,
    )
    if not apply:
        return LineageMigrationResult(
            scenario_id=scenario_id,
            plan_id=plan_id,
            verified_count=len(entries),
            already_durable_count=len(entries) - len(pending),
            pending_count=len(pending),
            migrated_count=0,
            applied=False,
        )

    for entry in pending:
        marker_text = format_commit_marker(
            plan_id,
            entry.synth_id,
            scenario_id=scenario_id,
        )
        updated = await twig.append_description_async(
            entry.real_id,
            marker_text,
        )
        if not marker_matches(
            parse_commit_marker(updated),
            scenario_id=scenario_id,
            plan_id=plan_id,
            synth_id=entry.synth_id,
        ):
            raise LineageMigrationError(
                f"AB#{entry.real_id} did not preserve its durable lineage marker"
            )

    verified_items = await _inventory_scenario(twig, scenario_id=scenario_id)
    remaining = _validate_live_hierarchy(
        verified_items,
        entries,
        scenario_id=scenario_id,
        plan_id=plan_id,
    )
    if remaining:
        raise LineageMigrationError(
            f"{len(remaining)} item(s) remain without durable lineage after apply"
        )
    return LineageMigrationResult(
        scenario_id=scenario_id,
        plan_id=plan_id,
        verified_count=len(entries),
        already_durable_count=len(entries) - len(pending),
        pending_count=0,
        migrated_count=len(pending),
        applied=True,
    )
