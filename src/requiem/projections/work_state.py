"""requiem.projections.work_state — read-only tree projection (R4).

Builds a :class:`WorkStateProjection` for any ADO work item: walks the
Hierarchy-Forward link tree from the root, fetches ADO fields (state +
schedule dates) for every node, and surfaces the artifact linkage requiem
already owns (the per-leaf impl branch and the leaf PR, when one exists).

This is the data layer for ADR-0031 (R4 read-only projection); R3
(computed roll-up) is a SEPARATE forthcoming ADR that will derive
``computed_state`` on top of this raw tree. Keep this module strictly
read-only — no writes to ADO, no writes to the repo, no event-log
mutation.

## Wiring sketch

::

    twig: TwigClient (or async-compatible stub) — for ADO work-item reads
    repo_client: RepoPlatform — branch + PR existence on the host repo
    log_dir: Path | None — looks for ``leaf-pr-map-<root>.json`` if present

    projection = await compute_work_state(
        root_item_id=62759077,
        twig=twig,
        repo_client=repo_client,
        log_dir=Path(".runs"),
        github_repo="Owner/Repo",   # OR ado_repo="org/proj/repo"
    )

Consumers (R3, dashboards, CLI) walk ``projection.tree`` — a recursive
:class:`WorkItemNode` with ``children`` populated bottom-up.

## What gets fetched, and what doesn't

For each work item we fetch ``state``, ``title``, ``work_item_type``,
``parent_id``, plus the three ``Microsoft.VSTS.Scheduling.*`` schedule
dates. We do NOT pull the full work-item payload (description, comments,
attachments) — projections are cheap reads, and a tree of 50 items
should not require 50 full work-item fetches.

For repo linkage we compute the canonical impl branch via
:func:`requiem.branch_model.impl_branch` and ask ``repo_client``:

1. ``find_open_pr_for_branch(head=impl_branch)`` — surfaces an open PR.
2. If nothing open and a leaf-pr-map artifact is present, fetch the
   recorded PR number via ``pr_view`` and surface the (possibly merged)
   state. This is the only path that can witness a *merged* PR — the
   ``find_open_pr_for_branch`` query is open-only by Protocol contract.

A missing branch is NOT an error: many nodes (the root scenario, every
non-leaf deliverable) never get an impl branch, and even leaves only get
one after their first dispatch.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from requiem import branch_model


# ---- node + projection dataclasses ---------------------------------------


@dataclass(frozen=True)
class WorkItemNode:
    """One node in the work-state tree.

    Carries the raw ADO state vocabulary verbatim (``Proposed`` / ``Active``
    / ``Resolved`` / ``Closed`` — ADO process-template specific). R3 will
    derive a roll-up on top of this; this module ships the raw values
    unmodified so the derivation has full fidelity.

    Schedule dates are pulled from ``Microsoft.VSTS.Scheduling.{Start,
    Target, Finish}Date`` and surfaced as the canonical ISO 8601 string
    ADO emits. ``None`` means the field is unset (a missing date is not
    an error).

    Repo linkage:
    * ``impl_branch`` is the canonical branch name from
      :func:`requiem.branch_model.impl_branch(root, item)` — ALWAYS set
      (it's computed locally), but says nothing about whether the
      branch exists.
    * ``leaf_pr_number`` / ``leaf_pr_url`` / ``leaf_pr_state`` are set
      iff a real PR was discovered (either via an active-PR search OR
      via the persisted leaf-pr-map artifact). ``leaf_pr_state`` is one
      of the neutral :class:`requiem.clients.repo.RepoPrState` values:
      ``open`` / ``closed`` / ``merged``.
    """

    item_id: int
    title: str
    work_item_type: str        # Scenario / Deliverable / Task / Bug / …
    state: str                 # raw ADO state, e.g. "Active"
    start_date: str | None     # ISO 8601 from Microsoft.VSTS.Scheduling.StartDate
    target_date: str | None    # ISO 8601 from Microsoft.VSTS.Scheduling.TargetDate
    finish_date: str | None    # ISO 8601 from Microsoft.VSTS.Scheduling.FinishDate
    parent_id: int | None
    children: list["WorkItemNode"]
    # Artifact linkage (R1 surfacing — already structural in requiem; the
    # projection exposes it here so consumers don't have to re-derive).
    impl_branch: str | None        # branch_model.impl_branch(root, item)
    leaf_pr_number: int | None     # from active search OR leaf-pr-map artifact
    leaf_pr_url: str | None
    leaf_pr_state: str | None      # open / closed / merged

    def to_dict(self) -> dict[str, Any]:
        """JSON-able dict (children recurse). Preserves declared field
        order so the projection serialises deterministically."""
        return {
            "item_id": self.item_id,
            "title": self.title,
            "work_item_type": self.work_item_type,
            "state": self.state,
            "start_date": self.start_date,
            "target_date": self.target_date,
            "finish_date": self.finish_date,
            "parent_id": self.parent_id,
            "impl_branch": self.impl_branch,
            "leaf_pr_number": self.leaf_pr_number,
            "leaf_pr_url": self.leaf_pr_url,
            "leaf_pr_state": self.leaf_pr_state,
            "children": [c.to_dict() for c in self.children],
        }


@dataclass(frozen=True)
class WorkStateProjection:
    """The root projection — root id + a timestamp + the tree."""

    root_item_id: int
    computed_at: str            # ISO 8601 UTC
    tree: WorkItemNode

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_item_id": self.root_item_id,
            "computed_at": self.computed_at,
            "tree": self.tree.to_dict(),
        }


# ---- ADO field helpers ---------------------------------------------------


# The canonical Microsoft VSTS process-template fields R4 surfaces. Kept
# here as a module-level tuple so the dashboard + tests can re-export the
# field set without depending on the implementation details of compute.
ADO_DATE_FIELDS: tuple[str, ...] = (
    "Microsoft.VSTS.Scheduling.StartDate",
    "Microsoft.VSTS.Scheduling.TargetDate",
    "Microsoft.VSTS.Scheduling.FinishDate",
)

ADO_CORE_FIELDS: tuple[str, ...] = (
    "System.Title",
    "System.WorkItemType",
    "System.State",
    "System.Parent",
)

# All the fields ``compute_work_state`` requests per work item — the union.
ADO_PROJECTION_FIELDS: tuple[str, ...] = ADO_CORE_FIELDS + ADO_DATE_FIELDS


# ---- repo-client lookup helper ------------------------------------------
#
# Both GhClient and AdoClient implement RepoPlatform; the projection only
# touches ``find_open_pr_for_branch`` + ``pr_view``. Reads are read-only;
# any platform-typed error is surfaced as "no PR found" rather than
# escalated — the projection is intentionally best-effort on repo linkage
# (a missing branch is the norm for non-leaf nodes, not an exception).


class _RepoLookup(Protocol):
    """The narrow subset of :class:`RepoPlatform` the projection uses."""

    async def find_open_pr_for_branch(
        self, repo: str, *, head: str, limit: int = 30
    ) -> list[Any]: ...

    async def pr_view(self, repo: str, number: int) -> Any: ...


# ---- main entry point ----------------------------------------------------


# Clock seam for testability — defaults to wall-clock UTC.
Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def compute_work_state(
    *,
    root_item_id: int,
    twig: Any,                  # TwigClient or async-compatible stub
    repo_client: Any | None = None,  # RepoPlatform impl, or None
    log_dir: Path | None = None,
    github_repo: str | None = None,
    ado_repo: str | None = None,
    clock: Clock = _utc_now,
) -> WorkStateProjection:
    """Build the work-state projection rooted at ``root_item_id``.

    The walk:

    1. Fetch the root via ``twig.show_async`` (already cached in TwigClient's
       subprocess; the projection rides the same path).
    2. For each child id under ``raw.children`` (twig's projection of
       System.LinkTypes.Hierarchy-Forward), fetch the child via
       ``twig.show_async`` recursively.
    3. For every node, compute the impl branch and ask the repo client
       whether a PR for that branch exists (open OR — via leaf-pr-map
       lookup — merged).

    Args:
        root_item_id: the ADO work-item id to anchor the projection.
        twig: a TwigClient or async-compatible stub exposing
            ``show_async(id) -> TwigItem``.
        repo_client: RepoPlatform impl for repo linkage; if ``None``, the
            projection still produces the tree but no impl_branch /
            leaf_pr_* fields are populated.
        log_dir: directory that may contain
            ``leaf-pr-map-<root>.json`` — used to surface MERGED PRs that
            an open-only search can't witness.
        github_repo / ado_repo: repo identifier (one of the two, in the
            shape the chosen repo_client expects). When BOTH are ``None``
            we skip repo lookups entirely; when both are set we prefer
            ``github_repo`` (matches the rest of the codebase, which
            treats ``ado_repo`` as the explicit-non-GitHub override).
        clock: callable returning the UTC datetime for ``computed_at``.
            Defaults to wall-clock UTC; tests inject a fixed clock for
            reproducibility.

    Returns:
        A :class:`WorkStateProjection` whose ``tree`` is the full
        hierarchy. Each :class:`WorkItemNode`'s ``children`` is populated
        bottom-up. ``computed_at`` is the timestamp at which this
        projection was generated.
    """
    repo_id = github_repo or ado_repo
    leaf_pr_map = _load_leaf_pr_map(log_dir, root_item_id) if log_dir else {}

    tree = await _build_node(
        item_id=root_item_id,
        root_item_id=root_item_id,
        twig=twig,
        repo_client=repo_client,
        repo_id=repo_id,
        leaf_pr_map=leaf_pr_map,
    )
    return WorkStateProjection(
        root_item_id=root_item_id,
        computed_at=clock().isoformat(),
        tree=tree,
    )


# ---- internal walk -------------------------------------------------------


async def _build_node(
    *,
    item_id: int,
    root_item_id: int,
    twig: Any,
    repo_client: Any | None,
    repo_id: str | None,
    leaf_pr_map: dict[int, int],
) -> WorkItemNode:
    """Build one node + its children, depth-first.

    The recursive descent is sequential per branch but parallel across
    sibling children (asyncio.gather). For a tree of 50 items that's a
    handful of parallel ADO reads — well within twig's local-cache
    window, no rate-limit concern.
    """
    item = await twig.show_async(item_id)
    fields = _extract_fields_from_raw(item.raw)

    # impl branch is purely local — always computable.
    impl_branch_name: str | None = None
    try:
        impl_branch_name = branch_model.impl_branch(root_item_id, item_id)
    except branch_model.BranchModelError:
        # Non-alphanumeric ids can't be encoded — leave None and continue.
        impl_branch_name = None

    pr_number, pr_url, pr_state = await _resolve_pr_linkage(
        repo_client=repo_client,
        repo_id=repo_id,
        impl_branch_name=impl_branch_name,
        item_id=item_id,
        leaf_pr_map=leaf_pr_map,
    )

    # Recurse into children, in parallel (id-stable order via the raw
    # twig payload — twig itself returns them in API order).
    child_ids = _child_ids_from_raw(item.raw)
    if child_ids:
        children = list(await asyncio.gather(*(
            _build_node(
                item_id=cid,
                root_item_id=root_item_id,
                twig=twig,
                repo_client=repo_client,
                repo_id=repo_id,
                leaf_pr_map=leaf_pr_map,
            )
            for cid in child_ids
        )))
    else:
        children = []

    return WorkItemNode(
        item_id=item.id,
        title=item.title,
        work_item_type=item.work_item_type,
        state=item.state,
        start_date=fields.get("start_date"),
        target_date=fields.get("target_date"),
        finish_date=fields.get("finish_date"),
        parent_id=item.parent_id,
        children=children,
        impl_branch=impl_branch_name,
        leaf_pr_number=pr_number,
        leaf_pr_url=pr_url,
        leaf_pr_state=pr_state,
    )


def _child_ids_from_raw(raw: dict[str, Any]) -> list[int]:
    """Lift child ids from the twig payload.

    Twig's ``show --output json`` payload carries a ``children`` array of
    ``{id, title, type, …}`` stubs (one entry per System.LinkTypes
    .Hierarchy-Forward child). This helper is the single place that
    parses that shape, mirroring :meth:`TwigClient.list_children_async`.
    Defensive on shape: missing/empty/None ``children`` returns ``[]``.
    """
    raw_children = raw.get("children") or []
    if not isinstance(raw_children, list):
        return []
    out: list[int] = []
    for c in raw_children:
        if isinstance(c, dict) and "id" in c:
            try:
                out.append(int(c["id"]))
            except (TypeError, ValueError):
                continue
    return out


def _extract_fields_from_raw(raw: dict[str, Any]) -> dict[str, str | None]:
    """Pull the three Microsoft.VSTS.Scheduling.* dates from twig's payload.

    Two payload shapes show up in practice:

    1. The raw ADO REST shape — fields live under ``raw["fields"]`` as
       ``Microsoft.VSTS.Scheduling.{Start,Target,Finish}Date``.
    2. Twig's projected shape — twig may pre-promote some fields onto
       the top-level object as e.g. ``startDate``, ``targetDate``,
       ``finishDate``. We accept either.

    Always returns a dict with the three keys (start/target/finish_date),
    valued with the ISO string or ``None``.
    """
    out: dict[str, str | None] = {
        "start_date": None,
        "target_date": None,
        "finish_date": None,
    }
    # Shape 2 — top-level camelCase.
    for src, dst in (
        ("startDate", "start_date"),
        ("targetDate", "target_date"),
        ("finishDate", "finish_date"),
    ):
        v = raw.get(src)
        if isinstance(v, str) and v:
            out[dst] = v
    # Shape 1 — ADO REST fields dict (preferred when present; overrides
    # the camelCase shape because it's the canonical wire form).
    fields = raw.get("fields")
    if isinstance(fields, dict):
        for src, dst in (
            ("Microsoft.VSTS.Scheduling.StartDate", "start_date"),
            ("Microsoft.VSTS.Scheduling.TargetDate", "target_date"),
            ("Microsoft.VSTS.Scheduling.FinishDate", "finish_date"),
        ):
            v = fields.get(src)
            if isinstance(v, str) and v:
                out[dst] = v
    return out


async def _resolve_pr_linkage(
    *,
    repo_client: Any | None,
    repo_id: str | None,
    impl_branch_name: str | None,
    item_id: int,
    leaf_pr_map: dict[int, int],
) -> tuple[int | None, str | None, str | None]:
    """Resolve (pr_number, pr_url, pr_state) for one node.

    Three signals, in order of trust:

    1. ``find_open_pr_for_branch`` — the canonical \"is there a PR open
       right now?\" answer. Returns the open PR if any.
    2. leaf-pr-map ``{leaf_id: pr_number}`` artifact — when the PR has
       been merged (or closed), step 1 returns empty; the leaf-pr-map
       carries the surviving PR number so we can ``pr_view`` it.
    3. None on every other path (no repo client, no branch, no map
       entry, transient error).

    Errors from the repo client are intentionally swallowed and returned
    as \"no PR.\" The projection is a best-effort surface; an ADO API
    blip MUST NOT torch a whole-tree fetch.
    """
    if repo_client is None or repo_id is None or impl_branch_name is None:
        return None, None, None

    # 1) active-PR search
    try:
        active = await repo_client.find_open_pr_for_branch(
            repo_id, head=impl_branch_name, limit=5,
        )
    except Exception:  # noqa: BLE001 — best-effort projection
        active = []
    if active:
        pr = active[0]
        return (
            int(getattr(pr, "number", 0)) or None,
            getattr(pr, "url", None),
            getattr(pr, "state", None),
        )

    # 2) leaf-pr-map fallback (for merged/closed PRs)
    pr_number = leaf_pr_map.get(item_id)
    if pr_number is None:
        return None, None, None
    try:
        pr = await repo_client.pr_view(repo_id, pr_number)
    except Exception:  # noqa: BLE001 — best-effort projection
        # Map points at a PR the client can't fetch — surface the number
        # so consumers know it exists but state stays unknown.
        return int(pr_number), None, None
    return (
        int(getattr(pr, "number", pr_number)),
        getattr(pr, "url", None),
        getattr(pr, "state", None),
    )


def _load_leaf_pr_map(log_dir: Path, root_item_id: int) -> dict[int, int]:
    """Parse ``log_dir/leaf-pr-map-<root>.json`` into a {leaf_id: pr_number} dict.

    The file is written by :func:`requiem.end_to_end._persist_leaf_pr_map`
    after a fan-out completes. Missing file, missing keys, malformed
    JSON all degrade to an empty map — a missing artifact is the common
    case (no run has produced one yet for this root).
    """
    path = Path(log_dir) / f"leaf-pr-map-{root_item_id}.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    leaves = payload.get("leaves") if isinstance(payload, dict) else None
    if not isinstance(leaves, list):
        return {}
    out: dict[int, int] = {}
    for entry in leaves:
        if not isinstance(entry, dict):
            continue
        leaf_id = entry.get("leaf_id")
        pr_number = entry.get("pr_number")
        if pr_number is None:
            continue
        try:
            out[int(leaf_id)] = int(pr_number)
        except (TypeError, ValueError):
            continue
    return out
