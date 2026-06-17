"""Plan-commit seeding workflow — turns plan *proposals* into real ADO items.

The recursive ``planning`` workflow (``requiem.workflows.planning``) produces
**proposals**: it synthesises child ids (``parent*100 + index+1``) and never
writes to ADO. This workflow is the deliberate plan→reality transition. It
consumes the approved ``.plan.tree.json`` artifact and **idempotently seeds the
proposed children as real ADO work items**, depth-first, recording a
``synth → real`` id map.

See ADR-0011 for the decision and the rubber-duck-hardened design. Highlights:

* **Separate workflow, not seeding-in-recursion** (ADR-0011 option B): planning
  stays a pure proposal producer; all ADO writes live here.
* **Self-describing artifact** (``schema_version >= 2``): each recursive node
  carries its own ``proposals`` (creatable metadata) plus its ``children``
  (recursive sub-plans), so this workflow needs no per-sub-run event logs.
* **Marker idempotency** (NOT title/type): every created item's *description* is
  stamped with ``<!-- requiem-commit plan_id=<plan_id> synth_id=<synth_id> -->``.
  Re-runs (or a second commit of the same plan) match on the marker first and
  reuse rather than duplicate — covering the crash-after-create-before-log
  window and surviving human renames. ``(title, work_item_type)`` is only a
  fallback for hand-created items; an *ambiguous* fallback routes to a human.
* **Pinned proposals** (``item_id`` set) mean "this item already exists" —
  validated via ``show_async`` and reused, never re-created.
* **Ravel L-1**: rate-limit → ``RetryableFailure``; not-found →
  ``PermanentFailure``; unclassified → ``NeedsHuman``. Whole-verb re-run is safe
  by construction (marker dedupe); partial progress rides on the failure.
* **Dry-run default ON** (per ``close_out`` convention): walks + lists but never
  creates, emitting an explicit ``would_create`` / ``would_reuse`` / ``ambiguous``
  / ``missing_pinned`` preview. Operators opt into writes with ``dry_run=False``.

Topology::

    start → load_tree → seed_tree → write_manifest → end_success
       (load_tree / seed_tree route failures to end_failed / end_human)

Public entry points (the contract ``requiem run`` consumes):

* ``build_workflow() -> Workflow``
* ``build_engine(log_dir, *, …) -> Engine``  (zero-arg = canned dry-run demo)
* ``verdict_card(completed) -> str | None``
* ``commit_plan_result(completed, final_node) -> CommitPlanResult``

NOT in this slice (ADR-0011 "Deferred"): the freeze/supersede lifecycle for
re-planning after a commit — that depends on the unbuilt merge-group +
plan-generation subsystem.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from requiem.clients.twig import (
    TwigClient,
    TwigClientError,
    TwigItem,
    TwigItemNotFoundError,
    TwigRateLimitedError,
    TwigUnknownError,
)
from requiem.dsl import AgentRegistry, VerbRegistry, Workflow, WorkflowBuilder
from requiem.kernel import Engine
from requiem.outcomes import NeedsHuman, PermanentFailure, RetryableFailure, Success
from requiem.toolbelt import Toolbelt

# ---- closed `error_kind` vocabulary (ADR 0004 §4.2) --------------------

EK_ARTIFACT_MISSING   = "artifact_missing"
EK_BAD_ARTIFACT       = "bad_artifact"
EK_UNSUPPORTED_SCHEMA = "schema_mismatch"
EK_NOT_APPROVED       = "not_approved"
EK_NOT_DECOMPOSABLE   = "not_decomposable"
EK_VALIDATION         = "validation_failed"
EK_TOO_LARGE          = "tree_too_large"
EK_NOT_FOUND          = "not_found"
EK_RATE_LIMITED       = "rate_limited"

# ---- gate identifiers (free-form but namespaced) -----------------------

GATE_PINNED_MISSING = "commit_plan.pinned_item_missing"
GATE_AMBIGUOUS      = "commit_plan.ambiguous_existing_child"
GATE_UNKNOWN_TWIG   = "commit_plan.unknown_twig_failure"

# ---- artifact schema ---------------------------------------------------

# Minimum `.plan.tree.json` schema this workflow accepts. v2 added the
# per-node `proposals` list (see planning.PLAN_TREE_SCHEMA_VERSION); below
# that, the recursive tree lacks the creatable metadata we need.
MIN_SCHEMA_VERSION = 2

# Default ceiling on the number of items a single commit may create. A guard
# against an accidental commit of a huge tree (MAX_CHILDREN=8 × max_depth=4
# could in principle be thousands of items). Tunable per invocation.
DEFAULT_MAX_CREATES = 200

_MARKER_RE = re.compile(
    r"<!--\s*requiem-commit\s+plan_id=(?P<plan>\S+)\s+synth_id=(?P<synth>\d+)\s*-->"
)


def _marker(plan_id: str, synth_id: int) -> str:
    return f"<!-- requiem-commit plan_id={plan_id} synth_id={synth_id} -->"


def _description_of(item: TwigItem) -> str:
    """Best-effort extraction of an item's description across twig payload shapes."""
    raw = item.raw or {}
    for key in ("description", "Description"):
        val = raw.get(key)
        if val:
            return str(val)
    fields = raw.get("fields") or {}
    return str(fields.get("System.Description") or "")


def _find_by_marker(
    existing: list[TwigItem], plan_id: str, synth_id: int
) -> TwigItem | None:
    needle = str(synth_id)
    for it in existing:
        m = _MARKER_RE.search(_description_of(it))
        if m and m.group("plan") == str(plan_id) and m.group("synth") == needle:
            return it
    return None


# ---- public result type -------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommitPlanResult:
    """Workflow-level result, derived from the event-log projection at run end."""
    plan_id: str
    root_item_id: int
    verdict: Literal["committed", "previewed", "needs_human", "failed"]
    created_count: int
    reused_count: int
    id_map: dict[int, int]
    manifest_path: Path | None
    dry_run: bool


# ---- workflow inputs ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommitPlanInputs:
    """Resolved-at-build-time inputs. Stamped into ``start``'s payload so a
    resumed run reads identical inputs from the log even if the engine factory
    is later invoked with different defaults (INV-RESTART)."""
    plan_tree_path: Path
    dry_run: bool
    area_path: str | None
    max_creates: int
    manifest_path: Path | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "plan_tree_path": str(self.plan_tree_path),
            "dry_run": self.dry_run,
            "area_path": self.area_path,
            "max_creates": self.max_creates,
            "manifest_path": str(self.manifest_path) if self.manifest_path else None,
        }


# ---- twig seam ----------------------------------------------------------


@dataclass
class FakeTwigClient:
    """Stateful in-memory twig stand-in for the demo and tests.

    Unlike the planning workflow's read-only fake, this one is *mutating*:
    ``create_child_async`` actually inserts a child so a subsequent
    ``list_children_async`` reflects it — which is exactly what idempotency /
    partial-seed-recovery tests need to exercise.
    """
    items: dict[int, TwigItem] = field(default_factory=dict)
    next_id: int = 900000
    fail_on_title: dict[str, Exception] = field(default_factory=dict)
    created_titles: list[str] = field(default_factory=list)

    async def show_async(self, item_id: int) -> TwigItem:
        if item_id not in self.items:
            raise TwigItemNotFoundError(f"fake: item {item_id} not found")
        return self.items[item_id]

    async def list_children_async(self, parent_id: int) -> list[TwigItem]:
        return [it for it in self.items.values() if it.parent_id == parent_id]

    async def create_child_async(
        self,
        *,
        parent_id: int,
        title: str,
        work_item_type: str,
        area_path: str | None = None,
        description: str | None = None,
    ) -> TwigItem:
        if title in self.fail_on_title:
            raise self.fail_on_title[title]
        new_id = self.next_id
        self.next_id += 1
        item = TwigItem(
            id=new_id,
            title=title,
            state="New",
            area_path=area_path or "",
            work_item_type=work_item_type,
            parent_id=parent_id,
            raw={"description": description or ""},
        )
        self.items[new_id] = item
        self.created_titles.append(title)
        return item


# ---- structural validation (load_tree) ---------------------------------


def _synth_of(prop: dict[str, Any], parent_synth: int, index: int) -> int:
    pinned = prop.get("item_id")
    if isinstance(pinned, int):
        return pinned
    return parent_synth * 100 + (index + 1)


def _validate_node(
    node: dict[str, Any], *, parent_synth: int, depth: int, errors: list[str]
) -> int:
    """Recursively validate a plan node; return the number of *creates* it implies.

    Pinned proposals (``item_id`` set) are reuse, not creates, so they do not
    count toward the size cap. Records every structural problem into ``errors``.
    """
    proposals = node.get("proposals") or []
    children = node.get("children") or []
    if node.get("decomposable") and len(children) != len(proposals):
        errors.append(
            f"depth {depth} (synth {parent_synth}): {len(children)} children "
            f"!= {len(proposals)} proposals — artifact misaligned"
        )
        return 0
    total = 0
    for i, prop in enumerate(proposals):
        if "title" not in prop or "work_item_type" not in prop:
            errors.append(f"depth {depth}: proposal[{i}] missing title/work_item_type")
            continue
        synth = _synth_of(prop, parent_synth, i)
        if not isinstance(prop.get("item_id"), int):
            total += 1  # a create (pinned ids are reuse)
        child = children[i] if i < len(children) else None
        if child is not None:
            if int(child.get("item_id", 0)) != int(synth):
                errors.append(
                    f"depth {depth}: child[{i}].item_id {child.get('item_id')!r} "
                    f"!= expected synth {synth}"
                )
            fv = child.get("final_verdict")
            # ``policy-forced-leaf`` is the synthetic verdict written by
            # the planning workflow's ADR-0025 Gap A short-circuit
            # (implementable types skip planner+reviewer entirely).
            # Treat it as terminal-approved for commit_plan's purposes —
            # the policy IS the approval.
            if fv not in (None, "approved", "policy-forced-leaf"):
                errors.append(
                    f"depth {depth}: child[{i}] final_verdict {fv!r} is not approved"
                )
            if child.get("decomposable"):
                total += _validate_node(
                    child, parent_synth=synth, depth=depth + 1, errors=errors
                )
    return total


def _read_tree(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


# ---- internal control-flow signal --------------------------------------


class _SeedAbort(Exception):
    """Raised inside the recursive walk to bubble a terminal Outcome to the verb."""

    def __init__(self, outcome: Any) -> None:
        super().__init__("seed aborted")
        self.outcome = outcome


# ---- verb library -------------------------------------------------------


def build_verb_registry(inputs: CommitPlanInputs, *, twig: Any, log_dir: Path) -> VerbRegistry:
    verbs = VerbRegistry()

    @verbs.register("start_run")
    def _start(ctx) -> Success:
        return Success(value=inputs.to_payload())

    @verbs.register("load_tree")
    def _load_tree(ctx):
        path = inputs.plan_tree_path
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return PermanentFailure(
                error_kind=EK_ARTIFACT_MISSING,
                message=f"plan tree not found: {path}",
                details={"path": str(path)},
            )
        try:
            tree = json.loads(raw)
        except json.JSONDecodeError as e:
            return PermanentFailure(
                error_kind=EK_BAD_ARTIFACT,
                message=f"plan tree is not valid JSON: {e}",
                details={"path": str(path)},
            )
        sv = tree.get("schema_version")
        if not isinstance(sv, int) or sv < MIN_SCHEMA_VERSION:
            return PermanentFailure(
                error_kind=EK_UNSUPPORTED_SCHEMA,
                message=(
                    f"plan tree schema_version {sv!r} < {MIN_SCHEMA_VERSION}; "
                    "re-run planning to regenerate a self-describing artifact"
                ),
                details={"path": str(path), "schema_version": sv},
            )
        if tree.get("verdict") != "approved":
            return PermanentFailure(
                error_kind=EK_NOT_APPROVED,
                message=f"plan verdict is {tree.get('verdict')!r}, not 'approved'",
                details={"verdict": tree.get("verdict")},
            )
        if not tree.get("decomposable"):
            return PermanentFailure(
                error_kind=EK_NOT_DECOMPOSABLE,
                message="plan is a leaf — nothing to seed",
                details={"item_id": tree.get("item_id")},
            )
        if not isinstance(tree.get("item_id"), int):
            return PermanentFailure(
                error_kind=EK_BAD_ARTIFACT,
                message=f"plan tree root item_id is {tree.get('item_id')!r}, expected int",
                details={"item_id": tree.get("item_id")},
            )
        errors: list[str] = []
        total = _validate_node(
            tree, parent_synth=int(tree["item_id"]), depth=0, errors=errors
        )
        if errors:
            return PermanentFailure(
                error_kind=EK_VALIDATION,
                message="plan tree failed structural validation",
                details={"errors": errors[:20]},
            )
        if total > inputs.max_creates:
            return PermanentFailure(
                error_kind=EK_TOO_LARGE,
                message=(
                    f"plan would create {total} items, exceeding the cap "
                    f"{inputs.max_creates}; raise max_creates to proceed"
                ),
                details={"total_creates": total, "cap": inputs.max_creates},
            )
        return Success(
            value={
                "root_item_id": int(tree["item_id"]),
                "plan_id": tree.get("plan_id"),
                "total_creates": total,
            },
        )

    @verbs.register("seed_tree")
    async def _seed_tree(ctx):
        tree = _read_tree(inputs.plan_tree_path)
        plan_id = str(tree.get("plan_id") or f"plan-{tree.get('item_id')}")
        root_id = int(tree["item_id"])
        ledger: list[dict[str, Any]] = []
        id_map: dict[int, int] = {}

        async def existing_of(real_parent: int | None) -> list[TwigItem]:
            if real_parent is None:
                return []
            return await twig.list_children_async(real_parent)

        async def seed_level(
            real_parent: int | None, synth_parent: int,
            proposals: list[dict[str, Any]], subplans: list[dict[str, Any]],
        ) -> None:
            existing = await existing_of(real_parent)
            used: set[int] = set()
            for i, prop in enumerate(proposals):
                synth = _synth_of(prop, synth_parent, i)
                pinned = isinstance(prop.get("item_id"), int)
                title = prop["title"]
                wit = prop["work_item_type"]
                desc = prop.get("description") or ""
                sub = subplans[i] if i < len(subplans) else None
                sub_decomp = bool(sub and sub.get("decomposable"))
                sub_props = (sub or {}).get("proposals") or []
                sub_children = (sub or {}).get("children") or []

                real: int | None = None
                status: str

                if pinned:
                    try:
                        item = await twig.show_async(synth)
                    except TwigItemNotFoundError:
                        if inputs.dry_run:
                            ledger.append(_rec(synth, None, title, wit, real_parent, "missing_pinned"))
                            continue
                        raise _SeedAbort(NeedsHuman(
                            gate=GATE_PINNED_MISSING,
                            prompt=(
                                f"proposal pins item {synth} but twig reports it "
                                "missing. Create it manually or abort the commit."
                            ),
                            options=("retry", "abort"),
                            context={"synth_id": synth, "title": title, "created": ledger},
                        ))
                    real, status = item.id, "pinned_reuse"
                else:
                    marker_item = _find_by_marker(existing, plan_id, synth)
                    if marker_item is not None:
                        real, status = marker_item.id, "reused"
                    else:
                        cands = [
                            it for it in existing
                            if it.id not in used
                            and it.title == title
                            and it.work_item_type == wit
                            and _MARKER_RE.search(_description_of(it)) is None
                        ]
                        if len(cands) > 1:
                            if inputs.dry_run:
                                ledger.append(_rec(synth, None, title, wit, real_parent, "ambiguous"))
                                continue
                            raise _SeedAbort(NeedsHuman(
                                gate=GATE_AMBIGUOUS,
                                prompt=(
                                    f"{len(cands)} existing children under {real_parent} "
                                    f"match (title={title!r}, type={wit}) with no commit "
                                    "marker — cannot disambiguate. Resolve in ADO or abort."
                                ),
                                options=("retry", "abort"),
                                context={"title": title, "candidate_ids": [c.id for c in cands], "created": ledger},
                            ))
                        elif len(cands) == 1:
                            real, status = cands[0].id, "adopted"
                        elif inputs.dry_run:
                            ledger.append(_rec(synth, None, title, wit, real_parent, "would_create"))
                            if sub_decomp:
                                await seed_level(None, synth, sub_props, sub_children)
                            continue
                        else:
                            marker = _marker(plan_id, synth)
                            full_desc = f"{desc}\n\n{marker}" if desc else marker
                            created = await twig.create_child_async(
                                parent_id=real_parent,
                                title=title,
                                work_item_type=wit,
                                area_path=inputs.area_path,
                                description=full_desc,
                            )
                            real, status = created.id, "created"

                used.add(real)
                id_map[synth] = real
                ledger.append(_rec(synth, real, title, wit, real_parent, status))
                if sub_decomp:
                    await seed_level(real, synth, sub_props, sub_children)

        try:
            await seed_level(
                root_id, root_id, tree.get("proposals") or [], tree.get("children") or []
            )
        except _SeedAbort as ab:
            return ab.outcome
        except TwigRateLimitedError as e:
            return RetryableFailure(
                retry_key=f"{ctx.run_id}:seed_tree",
                error_kind=EK_RATE_LIMITED,
                message=f"twig rate-limited mid-seed: {e}",
                attempt=ctx.attempt,
            )
        except TwigItemNotFoundError as e:
            return PermanentFailure(
                error_kind=EK_NOT_FOUND,
                message=f"twig lost a parent mid-seed: {e}",
                details={"created": ledger},
            )
        except TwigUnknownError as e:
            return NeedsHuman(
                gate=GATE_UNKNOWN_TWIG,
                prompt=(
                    "twig returned an unclassified failure mid-seed. Some children "
                    "may already exist (idempotent re-run is safe). Retry or abort."
                ),
                options=("retry", "abort"),
                context={
                    "created": ledger,
                    "exit_code": e.exit_code,
                    "stderr": (e.stderr or "")[:512],
                },
            )
        except TwigClientError as e:
            return NeedsHuman(
                gate=GATE_UNKNOWN_TWIG,
                prompt="twig client error mid-seed; retry (idempotent) or abort.",
                options=("retry", "abort"),
                context={"created": ledger, "error": str(e)[:512]},
            )

        created_count = sum(1 for r in ledger if r["status"] == "created")
        would_create = sum(1 for r in ledger if r["status"] == "would_create")
        reused_count = sum(
            1 for r in ledger if r["status"] in ("reused", "adopted", "pinned_reuse")
        )
        return Success(
            value={
                "dry_run": inputs.dry_run,
                "plan_id": plan_id,
                "root_item_id": root_id,
                "id_map": {str(k): v for k, v in id_map.items()},
                "ledger": ledger,
                "created_count": created_count,
                "would_create_count": would_create,
                "reused_count": reused_count,
            },
        )

    @verbs.register("write_manifest")
    def _write_manifest(ctx):
        seed = ctx.completed["seed_tree"]["value"]
        manifest = {
            "schema_version": 1,
            "plan_id": seed.get("plan_id"),
            "root_item_id": seed.get("root_item_id"),
            "dry_run": seed.get("dry_run"),
            "created_count": seed.get("created_count"),
            "would_create_count": seed.get("would_create_count"),
            "reused_count": seed.get("reused_count"),
            "id_map": seed.get("id_map"),
            "ledger": seed.get("ledger"),
        }
        path = inputs.manifest_path or (log_dir / f"{ctx.run_id}.plan.committed.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return Success(
            value={
                "manifest_path": str(path),
                "dry_run": seed.get("dry_run"),
                "created_count": seed.get("created_count"),
                "would_create_count": seed.get("would_create_count"),
                "reused_count": seed.get("reused_count"),
            },
            inspected_artifacts=(f"file:{path}",),
        )

    return verbs


def _rec(
    synth: int, real: int | None, title: str, wit: str,
    parent_real: int | None, status: str,
) -> dict[str, Any]:
    return {
        "synth_id": synth,
        "real_id": real,
        "title": title,
        "work_item_type": wit,
        "parent_real_id": parent_real,
        "status": status,
    }


# ---- topology -----------------------------------------------------------


def build_workflow() -> Workflow:
    return (
        WorkflowBuilder(
            "commit-plan",
            module="requiem.workflows.commit_plan",
            version="0.1",
        )
            .entry("start")
            .script("start", verb="start_run")
                .edge("start", on="success", to="load_tree")
            .script("load_tree", verb="load_tree")
                .edge("load_tree", on="success", to="seed_tree")
                .edge("load_tree", on="permanent_failure", to="end_failed")
            .script("seed_tree", verb="seed_tree")
                .edge("seed_tree", on="success", to="write_manifest")
                .edge("seed_tree", on="permanent_failure", to="end_failed")
                .edge("seed_tree", on="needs_human:retry", to="seed_tree")
                .edge("seed_tree", on="needs_human:abort", to="end_human")
            .script("write_manifest", verb="write_manifest")
                .edge("write_manifest", on="success", to="end_success")
                .edge("write_manifest", on="permanent_failure", to="end_failed")
            .terminate("end_success", disposition="completed")
            .terminate("end_failed",  disposition="failed")
            .terminate("end_human",   disposition="failed")
            .humanize({
                "start":          "Starting plan commit",
                "load_tree":      "Loaded & validated plan tree",
                "seed_tree":      "Seeded ADO children",
                "write_manifest": "Wrote commit manifest",
                "end_success":    "commit-plan",
                "end_failed":     "commit-plan",
                "end_human":      "commit-plan",
            })
            .build()
    )


# ---- default gate handler (auto-abort; demo never gates under dry-run) --


def _default_gate_handler(node_id: str, prompt: str, options: tuple[str, ...]) -> str:
    return "abort" if "abort" in options else options[-1]


_default_gate_handler.__requiem_auto__ = True  # type: ignore[attr-defined]


# ---- demo artifact ------------------------------------------------------


def _demo_tree(root_id: int = 4242) -> dict[str, Any]:
    """A canned, schema-v2 approved tree: 2 children, the first decomposable
    into 2 leaf grandchildren. Used by the zero-arg demo engine so
    ``requiem run requiem.workflows.commit_plan`` works key-free."""
    c1 = root_id * 100 + 1
    c2 = root_id * 100 + 2
    return {
        "schema_version": 2,
        "plan_id": f"plan-{root_id}-demo",
        "item_id": root_id,
        "item_title": "Demo root",
        "decomposable": True,
        "verdict": "approved",
        "proposals": [
            {"title": "Data layer", "description": "schema + migrations", "work_item_type": "Task"},
            {"title": "API layer", "description": "endpoints", "work_item_type": "Task"},
        ],
        "children": [
            {
                "item_id": c1, "plan_id": f"plan-{c1}", "decomposable": True,
                "summary": "split", "review_iterations": 1, "final_verdict": "approved",
                "proposals": [
                    {"title": "Define schema", "description": "tables", "work_item_type": "Task"},
                    {"title": "Write migration", "description": "up/down", "work_item_type": "Task"},
                ],
                "children": [
                    {"item_id": c1 * 100 + 1, "plan_id": "p", "decomposable": False,
                     "summary": "", "review_iterations": 1, "final_verdict": "approved",
                     "proposals": [], "children": []},
                    {"item_id": c1 * 100 + 2, "plan_id": "p", "decomposable": False,
                     "summary": "", "review_iterations": 1, "final_verdict": "approved",
                     "proposals": [], "children": []},
                ],
            },
            {
                "item_id": c2, "plan_id": f"plan-{c2}", "decomposable": False,
                "summary": "leaf", "review_iterations": 1, "final_verdict": "approved",
                "proposals": [], "children": [],
            },
        ],
    }


def _demo_twig(root_id: int = 4242) -> FakeTwigClient:
    return FakeTwigClient(items={
        root_id: TwigItem(
            id=root_id, title="Demo root", state="Active",
            area_path="Polyphony\\Engine", work_item_type="User Story",
            parent_id=None, raw={},
        ),
    })


# ---- engine factory -----------------------------------------------------


def build_engine(
    log_dir: Path,
    *,
    plan_tree_path: Path | None = None,
    dry_run: bool | None = None,
    area_path: str | None = None,
    max_creates: int | None = None,
    manifest_path: Path | None = None,
    twig: Any | None = None,
    toolbelt: Toolbelt | None = None,
    gate_handler=_default_gate_handler,
) -> Engine:
    """Build an Engine for ``commit-plan``.

    Zero-arg (``build_engine(log_dir)``) ships a canned, dry-run, side-effect-free
    demo: a 4-item schema-v2 tree written into ``log_dir`` and an in-memory
    ``FakeTwigClient`` — so ``requiem run requiem.workflows.commit_plan`` works
    key-free, mirroring the ``close_out`` demo shape.

    Environment overrides (read once here):

    * ``REQUIEM_COMMIT_PLAN_TREE``    — path to a ``.plan.tree.json``
    * ``REQUIEM_COMMIT_PLAN_DRY_RUN`` — "1" / "true" / "yes"
    * ``REQUIEM_COMMIT_PLAN_AREA``    — ADO area path for created items
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    env_tree = os.environ.get("REQUIEM_COMMIT_PLAN_TREE")
    if plan_tree_path is None and env_tree:
        plan_tree_path = Path(env_tree)
    if dry_run is None:
        env = os.environ.get("REQUIEM_COMMIT_PLAN_DRY_RUN")
        dry_run = (env or "").strip().lower() in ("1", "true", "yes") if env else True
    if area_path is None:
        area_path = os.environ.get("REQUIEM_COMMIT_PLAN_AREA")

    if plan_tree_path is None:
        # Canned demo — write the artifact and use the in-memory fake twig.
        plan_tree_path = log_dir / "commit-plan-demo.plan.tree.json"
        plan_tree_path.write_text(json.dumps(_demo_tree(), indent=2) + "\n", encoding="utf-8")
        if twig is None:
            twig = _demo_twig()

    if twig is None:
        twig = TwigClient()

    inputs = CommitPlanInputs(
        plan_tree_path=plan_tree_path,
        dry_run=bool(dry_run),
        area_path=area_path,
        max_creates=max_creates if max_creates is not None else DEFAULT_MAX_CREATES,
        manifest_path=manifest_path,
    )

    return Engine(
        workflow=build_workflow(),
        verbs=build_verb_registry(inputs, twig=twig, log_dir=log_dir),
        agents=AgentRegistry(),
        provider=None,
        toolbelt=toolbelt or Toolbelt.real(),
        log_dir=log_dir,
        gate_handler=gate_handler,
    )


# ---- result projection (for tests + callers) ---------------------------


def commit_plan_result(completed: dict, final_node: str) -> CommitPlanResult:
    seed = (completed.get("seed_tree") or {}).get("value") or {}
    manifest = (completed.get("write_manifest") or {}).get("value") or {}
    raw_map = seed.get("id_map") or {}
    id_map = {int(k): int(v) for k, v in raw_map.items()}
    dry_run = bool(seed.get("dry_run"))
    if final_node == "end_success":
        verdict: Literal["committed", "previewed", "needs_human", "failed"] = (
            "previewed" if dry_run else "committed"
        )
    elif final_node == "end_human":
        verdict = "needs_human"
    else:
        verdict = "failed"
    mp = manifest.get("manifest_path")
    return CommitPlanResult(
        plan_id=str(seed.get("plan_id") or ""),
        root_item_id=int(seed.get("root_item_id") or 0),
        verdict=verdict,
        created_count=int(seed.get("created_count") or 0),
        reused_count=int(seed.get("reused_count") or 0),
        id_map=id_map,
        manifest_path=Path(mp) if mp else None,
        dry_run=dry_run,
    )


def verdict_card(completed: dict) -> str | None:
    seed = (completed.get("seed_tree") or {}).get("value")
    if not seed:
        return None
    dry = seed.get("dry_run")
    head = "  ◐ Dry run (preview)" if dry else "  ✓ Committed"
    if dry:
        line = f"would create {seed.get('would_create_count', 0)}, reuse {seed.get('reused_count', 0)}"
    else:
        line = f"created {seed.get('created_count', 0)}, reused {seed.get('reused_count', 0)}"
    return f"{head}\n  plan {seed.get('plan_id')} under AB#{seed.get('root_item_id')} — {line}"


# ---- __main__ -----------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Commit a planning artifact into real ADO children.")
    p.add_argument("--tree", type=Path, default=None, help="path to a .plan.tree.json")
    p.add_argument("--run-id", default="commit-plan")
    p.add_argument("--log-dir", type=Path, default=Path("runs"))
    p.add_argument("--area-path", default=None)
    p.add_argument("--max-creates", type=int, default=None)
    mx = p.add_mutually_exclusive_group()
    mx.add_argument("--dry-run", dest="dry_run", action="store_true")
    mx.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    p.set_defaults(dry_run=None)
    return p


async def _amain(argv: list[str]) -> int:
    args = _build_arg_parser().parse_args(argv)
    engine = build_engine(
        args.log_dir,
        plan_tree_path=args.tree,
        dry_run=args.dry_run,
        area_path=args.area_path,
        max_creates=args.max_creates,
    )
    result = await engine.run(args.run_id)
    completed = {}
    try:
        from requiem.workflows.planning import completed_from_log  # reuse the folder
        completed = completed_from_log(engine.log_path(args.run_id))
    except Exception:  # pragma: no cover — best-effort verdict card
        pass
    card = verdict_card(completed)
    if card:
        print(card)
    return 0 if type(result).__name__ == "Completed" else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
