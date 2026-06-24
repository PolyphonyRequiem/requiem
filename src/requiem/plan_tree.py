"""Faithful enumeration of *implementable leaves* from a committed plan.

This is the spec's ``load_committed`` contract (ADR-0013 §"Intended design",
step 1), shared by any executor that fans implementation work out over a
planned tree.

The source of truth for "implementable leaf" is **the plan**, not the ADO
work-item type. ``planning`` decides ``decomposable`` per node; a leaf is a
node whose plan says ``decomposable == False``. That keeps the executor
*type-agnostic* — which ADO types are plannable/implementable/actionable is a
process-config concern (ADR-0010), never hardcoded here.

Two artifacts are consumed together:

* the approved ``<run>.plan.tree.json`` (structure + per-node ``proposals`` +
  ``decomposable`` facets), written by ``planning`` with synthetic child ids;
* the ``<run>.plan.committed.json`` manifest, written by ``commit_plan``,
  carrying the ``id_map`` that maps each synthetic id to the real ADO id the
  seeding step created.

Anything that makes a *real* enumeration impossible — a missing/malformed
artifact, an unapproved plan, a dry-run (preview) manifest with no real ids,
a structurally misaligned tree, or a leaf with no real id — raises
:class:`PlanArtifactError` rather than guessing. Failing loud is the whole
point: a silent half-enumeration would dispatch the wrong work (or nothing).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Mirror ``planning.PLAN_TREE_SCHEMA_VERSION`` / ``commit_plan.MIN_SCHEMA_VERSION``:
# v2 is the first self-describing tree where every node carries its own
# ``proposals`` list (the metadata a leaf task needs).
MIN_SCHEMA_VERSION = 2


class PlanArtifactError(Exception):
    """A committed-plan artifact cannot yield a faithful real leaf list.

    ``kind`` is a stable, machine-classifiable tag (e.g. ``"missing"``,
    ``"not_approved"``, ``"dry_run"``, ``"misaligned"``, ``"unmapped_leaf"``)
    so callers can map it onto an outcome/error_kind without string-matching
    the message.
    """

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True, slots=True)
class ResolvedLeaf:
    """One implementable leaf, resolved to its real ADO identity.

    ``title``/``body``/``work_item_type`` come from the *parent's* proposal
    block aligned by index (the leaf node itself does not carry them), so the
    dispatched task preserves the planner's intent for the work.
    """

    synth_id: int
    real_id: int
    title: str
    body: str
    work_item_type: str
    review_group: str | None = None
    depth: int = 0


def _synth_of(prop: dict[str, Any], parent_synth: int, index: int) -> int:
    """Synthetic id for a proposal — pinned id if present, else derived.

    Identical convention to ``planning._synth_child_id`` and
    ``commit_plan._synth_of`` (``parent * 100 + index + 1``); kept in lockstep
    so this enumeration lines up with how the tree was seeded.
    """
    pinned = prop.get("item_id")
    if isinstance(pinned, int):
        return pinned
    return parent_synth * 100 + (index + 1)


def _read_json(path: Path, *, what: str) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise PlanArtifactError(f"{what} not found: {path}", kind="missing") from e
    except OSError as e:
        raise PlanArtifactError(f"{what} unreadable: {path} ({e})", kind="missing") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise PlanArtifactError(f"{what} is not valid JSON: {e}", kind="bad_json") from e
    if not isinstance(data, dict):
        raise PlanArtifactError(f"{what} is not a JSON object", kind="bad_json")
    return data


def _validate_tree_header(tree: dict[str, Any]) -> None:
    sv = tree.get("schema_version")
    if not isinstance(sv, int) or sv < MIN_SCHEMA_VERSION:
        raise PlanArtifactError(
            f"plan tree schema_version {sv!r} < {MIN_SCHEMA_VERSION}; "
            "re-run planning to regenerate a self-describing artifact",
            kind="unsupported_schema",
        )
    if tree.get("verdict") not in ("approved", "needs_human"):
        # ADR-0027 accept-last (run #28): mirror the relaxation done
        # in commit_plan.load_tree on 9898c68. When the operator
        # accepted a needs_human planner output, the resulting tree
        # carries verdict=needs_human; resolve_leaves must accept
        # it too (the new _walk branch below handles the
        # partially-decomposed shape).
        raise PlanArtifactError(
            f"plan verdict is {tree.get('verdict')!r}, "
            "not 'approved' or 'needs_human'",
            kind="not_approved",
        )
    if not tree.get("decomposable"):
        raise PlanArtifactError(
            "plan tree root is a leaf (decomposable=False); use the atomic-root "
            "path, not committed-tree enumeration",
            kind="leaf_root",
        )
    if not isinstance(tree.get("item_id"), int):
        raise PlanArtifactError(
            f"plan tree root item_id is {tree.get('item_id')!r}, expected int",
            kind="bad_root_id",
        )


def _load_id_map(committed: dict[str, Any], tree: dict[str, Any]) -> dict[int, int]:
    if committed.get("dry_run"):
        raise PlanArtifactError(
            "committed manifest is a dry-run preview — it records would-create "
            "proposals, not real ADO ids; re-run commit_plan without --dry-run "
            "before dispatching",
            kind="dry_run",
        )
    root = committed.get("root_item_id")
    if not isinstance(root, int) or root != int(tree["item_id"]):
        raise PlanArtifactError(
            f"manifest root_item_id {root!r} does not match plan tree root "
            f"{tree.get('item_id')!r}",
            kind="root_mismatch",
        )
    raw = committed.get("id_map") or {}
    if not isinstance(raw, dict):
        raise PlanArtifactError("manifest id_map is not an object", kind="bad_id_map")
    try:
        return {int(k): int(v) for k, v in raw.items()}
    except (TypeError, ValueError) as e:
        raise PlanArtifactError(
            f"manifest id_map has non-integer entries: {e}", kind="bad_id_map"
        ) from e


def _map_real(prop: dict[str, Any], synth: int, id_map: dict[int, int]) -> int:
    mapped = id_map.get(synth)
    if isinstance(prop.get("item_id"), int):
        # Pinned: the proposal already names a real ADO id. commit_plan also
        # records pinned reuses in id_map (synth == pinned == real), so when a
        # mapping is present it must agree; if absent we trust the pin.
        pinned = int(prop["item_id"])
        if isinstance(mapped, int) and mapped != pinned:
            raise PlanArtifactError(
                f"pinned leaf {pinned} disagrees with manifest id_map "
                f"({synth}→{mapped})",
                kind="misaligned",
            )
        return pinned
    if not isinstance(mapped, int):
        raise PlanArtifactError(
            f"implementable leaf synth {synth} has no real id in the committed "
            "manifest id_map — the tree and manifest are out of sync",
            kind="unmapped_leaf",
        )
    return mapped


def _walk(
    node: dict[str, Any],
    *,
    parent_synth: int,
    id_map: dict[int, int],
    depth: int,
    out: list[ResolvedLeaf],
) -> None:
    proposals = node.get("proposals") or []
    children = node.get("children") or []
    # ADR-0027 accept-last + ADR-0030 §1 follow-up (run #28):
    # When a decomposable node was escalated and the operator accepted
    # the planner's last output (final_verdict == "needs_human") BUT
    # the planner never recursed into the children sub-workflows
    # (because escalation_gate routed to record_needs_human BEFORE
    # aggregate_children ran), the node carries `proposals` from the
    # planner but `children` is empty. The 1:1 alignment check below
    # would falsely flag the tree as misaligned.
    #
    # Two ways the tree gets into this shape:
    #   (a) The node itself escalated at the parent's escalation_gate
    #       — we should treat the node's PROPOSALS as leaves (one
    #       leaf per proposal, each implementable). This is the
    #       "accept-last didn't finish decomposing" semantic.
    #   (b) The node's parent escalated — we'd never recurse into
    #       this node at all (the parent would already be a leaf
    #       per the caller). N/A here.
    #
    # When needs_human: emit one leaf PER proposal (synth IDs derived
    # the same way as if children had been populated), pulling
    # work_item_type/title/description from the proposal, and a real
    # id from id_map (commit_plan DOES seed an ADO id for every
    # synth in the planner's tree, including escalated-needs-human
    # nodes — the manifest is the source of truth here).
    if (node.get("final_verdict") == "needs_human") and not children and proposals:
        for i, prop in enumerate(proposals):
            if "title" not in prop or "work_item_type" not in prop:
                raise PlanArtifactError(
                    f"node synth {parent_synth}: needs_human proposal[{i}] "
                    "missing title/work_item_type",
                    kind="bad_proposal",
                )
            synth = _synth_of(prop, parent_synth, i)
            out.append(
                ResolvedLeaf(
                    synth_id=synth,
                    real_id=_map_real(prop, synth, id_map),
                    title=str(prop["title"]),
                    body=str(prop.get("description", "")),
                    work_item_type=str(prop["work_item_type"]),
                    review_group=prop.get("review_group"),
                    depth=depth + 1,
                )
            )
        return
    # Normal path: within _walk every node is decomposable (the header
    # guarantees the root is, and we only recurse into decomposable
    # children), so children and proposals must align 1:1.
    if len(children) != len(proposals):
        raise PlanArtifactError(
            f"node synth {parent_synth}: {len(children)} children != "
            f"{len(proposals)} proposals — artifact misaligned",
            kind="misaligned",
        )
    for i, prop in enumerate(proposals):
        if "title" not in prop or "work_item_type" not in prop:
            raise PlanArtifactError(
                f"node synth {parent_synth}: proposal[{i}] missing "
                "title/work_item_type",
                kind="bad_proposal",
            )
        synth = _synth_of(prop, parent_synth, i)
        child = children[i]
        if int(child.get("item_id", 0)) != synth:
            raise PlanArtifactError(
                f"node synth {parent_synth}: child[{i}].item_id "
                f"{child.get('item_id')!r} != expected synth {synth}",
                kind="misaligned",
            )
        # Exact boolean: a malformed/missing `decomposable` must fail loud, not
        # be silently read as a leaf (which would truncate real grandchildren).
        decomposable = child.get("decomposable")
        if decomposable is True:
            _walk(child, parent_synth=synth, id_map=id_map, depth=depth + 1, out=out)
        elif decomposable is False:
            out.append(
                ResolvedLeaf(
                    synth_id=synth,
                    real_id=_map_real(prop, synth, id_map),
                    title=str(prop["title"]),
                    body=str(prop.get("description", "")),
                    work_item_type=str(prop["work_item_type"]),
                    review_group=prop.get("review_group"),
                    depth=depth + 1,
                )
            )
        else:
            raise PlanArtifactError(
                f"node synth {synth}: decomposable is {decomposable!r}, "
                "expected a boolean",
                kind="bad_node",
            )


def load_committed_leaves(
    tree_path: Path, committed_path: Path
) -> list[ResolvedLeaf]:
    """Enumerate ``decomposable == False`` leaves depth-first, mapped to real ids.

    Raises :class:`PlanArtifactError` on any condition that prevents a faithful
    real enumeration. A successful return guarantees: at least one leaf, every
    leaf carries a real ADO id, and real ids are unique (so per-leaf branch and
    task identity don't collide).
    """
    tree = _read_json(tree_path, what="plan tree")
    committed = _read_json(committed_path, what="committed manifest")
    _validate_tree_header(tree)
    # Guard against pairing a tree with a manifest from a *different* plan for
    # the same root: synth ids are position-derived, so a stale manifest could
    # otherwise map this tree's leaves onto ids seeded for another plan shape.
    tree_plan_id = tree.get("plan_id")
    manifest_plan_id = committed.get("plan_id")
    if tree_plan_id and manifest_plan_id and tree_plan_id != manifest_plan_id:
        raise PlanArtifactError(
            f"plan tree plan_id {tree_plan_id!r} != manifest plan_id "
            f"{manifest_plan_id!r} — mismatched artifacts",
            kind="plan_mismatch",
        )
    id_map = _load_id_map(committed, tree)

    leaves: list[ResolvedLeaf] = []
    _walk(tree, parent_synth=int(tree["item_id"]), id_map=id_map, depth=0, out=leaves)

    if not leaves:
        raise PlanArtifactError(
            "approved plan tree has zero implementable leaves", kind="no_leaves"
        )
    seen: dict[int, int] = {}
    for leaf in leaves:
        if leaf.real_id in seen:
            raise PlanArtifactError(
                f"leaves {seen[leaf.real_id]} and {leaf.synth_id} both map to "
                f"real id {leaf.real_id}",
                kind="duplicate_real_id",
            )
        seen[leaf.real_id] = leaf.synth_id
    return leaves
