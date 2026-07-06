"""``kanban_executor`` — the Hermes-backed external fan-out executor.

This is the workflow that closes Requiem's largest v0 parity gap (the
fan-out executor, ADR-0013) by a different route than ADR-0013 assumed.
Instead of dispatching the ``implementation`` sub-workflow *in-process*
— which ADR-0013 §B1 proves falls back to a **fake** provider + fake
ADO/GitHub over real git, and therefore only *looks* successful — this
workflow dispatches each implementable leaf to a **real external
executor**: a Hermes kanban worker. See ADR-0014 for the decision and
the honest scope (this does NOT unblock in-process sub-workflow seam
propagation; it sidesteps it).

## Shape

    preflight        (script)  hermes on PATH + ensure dedicated board
      → resolve_leaves (script) implementable leaves from a committed plan
                                 (decomposable==False, type-agnostic) or inline
      → dispatch_leaves (script) two-phase: create-unassigned → link → assign
      → poll_kanban    (script)  retry until every leaf task is terminal
      → aggregate      (script)  receipts → verdict; NeedsHuman gate
      → end / fail_end (terminate)

## Safety rails (from the design critique)

* **Stable idempotency** — each leaf's task key is
  ``requiem:{root}:{leaf_id}`` (stable work identity, NOT the transient
  ``run_id``), so a *fresh* Requiem run over the same plan reuses tasks
  rather than duplicating them (ADR-0013 §B4).
* **Two-phase dispatch** — tasks are created *unassigned* (not claimable),
  dependencies are linked, and only then are tasks assigned/released. This
  removes the create→claim race a worker could otherwise win before links
  land.
* **Dry-run is a distinct, non-delivering outcome** — a dry-run plan never
  reports "implementation delivered"; the verdict says "planned only".
* **Receipts before delivered** — a leaf counts as delivered only when its
  latest run ``outcome == "completed"`` *and* the worker recorded a
  ``result``. Weak completions are surfaced to a human, never silently
  marked success.
* **No implicit fake fallback** — if no kanban client is on the toolbelt the
  workflow fails typed (``toolbelt.missing_client``); fakes are only ever
  injected explicitly (the demo / the harness).

## Default demo

``requiem run requiem.workflows.kanban_executor`` runs key-free and
side-effect-free against an in-process :class:`SimKanbanClient` that
simulates workers completing the leaves — so the full delivery narration
and verdict card render with zero Hermes dependency, exactly like the
``code_review`` and ``close_out`` demos. Real runs go through ``main()``
(``--live`` to spawn real workers; default there is a real-board dry-run).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from requiem import branch_model
from requiem.clients.kanban import (
    KanbanBoardMissingError,
    KanbanBusyError,
    KanbanClient,
    KanbanClientError,
    KanbanRun,
    KanbanTask,
    KanbanUnknownError,
    is_hermes_on_path,
)
from requiem.dsl import AgentRegistry, VerbRegistry, Workflow, WorkflowBuilder
from requiem.handoff import (
    HANDOFF_SCHEMA_VERSION,
    HandoffError,
    HandoffMetadata,
    extract_handoff,
)
from requiem.kernel import Engine
from requiem.plan_tree import PlanArtifactError, load_committed_leaves
from requiem.workflows.leaf_deps import (
    compute_blocked,
    releasable_leaves,
    validate_dep_graph,
)
from requiem.outcomes import (
    NeedsHuman,
    PermanentFailure,
    RetryableFailure,
    Success,
)
from requiem.toolbelt import Toolbelt

_TERMINAL_STATUSES = frozenset({"done", "blocked", "archived"})
_DELIVERED_OUTCOMES = frozenset({"completed"})
# Latest-run outcomes that mean the worker failed (not asked for human). A
# `blocked` task whose last run carries one of these is a circuit-breaker trip
# (the dispatcher only blocks after `kanban.failure_limit` consecutive failures).
_FAILURE_OUTCOMES = frozenset({"crashed", "failed", "error", "timed_out", "cancelled"})

# Requiem dispositions a kanban state translates to (ADR-0017 §6).
_OUT_DELIVERED = "delivered"
_OUT_NEEDS_HUMAN = "needs_human"
_OUT_PERMANENT_FAILURE = "permanent_failure"
_OUT_IN_FLIGHT = "in_flight"


# ---- inputs ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LeafSpec:
    """One implementable leaf to dispatch as a kanban task."""

    leaf_id: str
    title: str
    body: str = ""
    branch: str | None = None
    skills: tuple[str, ...] = ()
    deps: tuple[str, ...] = ()  # other leaf_ids this leaf depends on


@dataclass(frozen=True, slots=True)
class ExecInputs:
    root_item: str
    board: str
    assignee: str | None = None
    live: bool = False
    leaves: tuple[LeafSpec, ...] = ()
    plan_tree_path: Path | None = None
    committed_path: Path | None = None
    poll_interval_s: float = 5.0
    max_polls: int = 120
    skills: tuple[str, ...] = ()


# ---- verb library ----------------------------------------------------


def build_verb_registry(inputs: ExecInputs) -> VerbRegistry:
    verbs = VerbRegistry()

    def _require_kanban(ctx) -> KanbanClient | None:
        return ctx.toolbelt.kanban

    @verbs.register("preflight")
    async def _preflight(ctx):
        kanban = _require_kanban(ctx)
        if kanban is None:
            return PermanentFailure(
                error_kind="toolbelt.missing_client",
                message="no kanban client on the toolbelt; refusing to run "
                        "the executor with a silent fake (ADR-0013 §B1).",
            )
        if inputs.live and not is_hermes_on_path() and isinstance(kanban, KanbanClient) \
                and type(kanban) is KanbanClient:
            return NeedsHuman(
                gate="preflight",
                prompt="hermes is not on PATH; cannot spawn real workers.",
                options=("approve", "abort"),
                context={"board": inputs.board},
            )
        try:
            version = await kanban.version_async()
            await kanban.ensure_board_async(inputs.board)
        except KanbanBusyError as e:
            return RetryableFailure(
                retry_key=f"{ctx.run_id}:preflight",
                error_kind="kanban.busy",
                message=str(e),
                attempt=ctx.attempt,
                after=inputs.poll_interval_s,
            )
        except KanbanClientError as e:
            return NeedsHuman(
                gate="preflight",
                prompt=f"kanban preflight failed: {e}",
                options=("approve", "abort"),
                context={"board": inputs.board, "error": str(e)},
            )
        return Success(
            value={"hermes_version": version, "board": inputs.board, "live": inputs.live},
            receipts=({"kind": "kanban.preflight", "board": inputs.board,
                       "hermes_version": version},),
        )

    @verbs.register("resolve_leaves")
    async def _resolve(ctx):
        # Priority 1 — explicit inline leaves (demo/tests, and the driver's
        # atomic-root path where the root item *is* the single leaf).
        if inputs.leaves:
            leaves = list(inputs.leaves)
            return Success(value={
                "root_item": inputs.root_item,
                "source": "inline",
                "leaves": [_leaf_to_dict(l) for l in leaves],
            })

        # Priority 2 — the committed plan (the spec's `load_committed`
        # contract): enumerate `decomposable == False` leaves from the approved
        # plan tree + committed id_map. Type-agnostic: planning decided the
        # facets. No ADO-type classification, no twig depth-1 guess.
        if inputs.plan_tree_path is not None and inputs.committed_path is not None:
            try:
                resolved = load_committed_leaves(
                    inputs.plan_tree_path, inputs.committed_path
                )
            except PlanArtifactError as e:
                # A malformed/unapproved/dry-run/misaligned artifact is not
                # something an operator fixes by approving *this* node and
                # proceeding to dispatch — fail closed (ADR-0014 safety rail).
                return PermanentFailure(
                    error_kind=f"plan_artifact.{e.kind}",
                    message=str(e),
                    details={
                        "plan_tree_path": str(inputs.plan_tree_path),
                        "committed_path": str(inputs.committed_path),
                    },
                )
            leaves = [
                LeafSpec(
                    leaf_id=str(r.real_id),
                    title=r.title,
                    body=r.body,
                    branch=branch_model.impl_branch(inputs.root_item, r.real_id),
                    skills=inputs.skills,
                    deps=tuple(str(d) for d in r.deps),
                )
                for r in resolved
            ]
            return Success(
                value={
                    "root_item": inputs.root_item,
                    "source": "committed_plan",
                    "leaves": [_leaf_to_dict(l) for l in leaves],
                },
                # Stamp artifact identity into the log for audit + INV-RESTART:
                # resume replays this recorded value, never re-reads the files.
                receipts=({
                    "kind": "plan.committed",
                    "plan_tree_path": str(inputs.plan_tree_path),
                    "committed_path": str(inputs.committed_path),
                    "leaf_count": len(leaves),
                },),
            )

        # No leaf source at all — fail closed rather than dispatch nothing or
        # guess from ADO structure (a "no-children" scan conflates "not yet
        # decomposed" with "implementable").
        return PermanentFailure(
            error_kind="plan_artifact.no_source",
            message=(
                "no leaves to dispatch: provide inline leaves, or both "
                "plan_tree_path and committed_path (a committed plan)."
            ),
            details={"root_item": inputs.root_item},
        )

    @verbs.register("dispatch_leaves")
    async def _dispatch(ctx):
        kanban = _require_kanban(ctx)
        if kanban is None:
            return PermanentFailure(
                error_kind="toolbelt.missing_client", message="no kanban client",
            )
        leaves = [_leaf_from_dict(d) for d in ctx.completed["resolve_leaves"]["value"]["leaves"]]
        board = inputs.board
        leaf_to_task: dict[str, str] = {}
        idem_keys: dict[str, str] = {}

        # Pre-flight — validate the dependency graph before any side effects.
        # A dep referencing an unknown leaf, a self-dep, or a cycle must FAIL
        # CLOSED: silently skipping an unknown dep (the old behaviour) would
        # dispatch a dependent child as if it had no prerequisites, letting it
        # run before its parent is accepted. The ready frontier is the set of
        # leaves with no unmet dependencies — only those may be released now.
        graph_error, ready_frontier = _validate_dep_graph(leaves)
        if graph_error is not None:
            return NeedsHuman(
                gate="dispatch_leaves",
                prompt=f"leaf dependency graph is unsafe to dispatch: {graph_error}",
                options=("approve", "abort"),
                context={"board": board, "error": graph_error},
            )

        try:
            # The plan hash binds idempotency to *this* committed plan version
            # (ADR-0017 §5). If re-planning changes any leaf's identity (title,
            # body, branch, skills, deps, id), the hash changes and every task
            # gets a fresh key — a stale task from a superseded plan can never
            # be silently reused.
            plan_hash = _plan_hash(leaves)
            # Phase 1 — create every task UNASSIGNED (not yet claimable).
            for leaf in leaves:
                key = f"requiem:{inputs.root_item}:{plan_hash}:{leaf.leaf_id}"
                idem_keys[leaf.leaf_id] = key
                task = await kanban.create_async(
                    leaf.title,
                    board=board,
                    body=leaf.body or f"Implement leaf {leaf.leaf_id} of {inputs.root_item}.",
                    idempotency_key=key,
                    workspace="worktree",
                    branch=leaf.branch,
                    skills=leaf.skills,
                )
                mismatch = _reconcile(task, leaf, key)
                if mismatch is not None:
                    return NeedsHuman(
                        gate="dispatch_leaves",
                        prompt=f"existing kanban task {task.id} for {key} does not "
                               f"match intent: {mismatch}",
                        options=("approve", "abort"),
                        context={"task_id": task.id, "leaf_id": leaf.leaf_id,
                                 "mismatch": mismatch},
                    )
                leaf_to_task[leaf.leaf_id] = task.id

            # Phase 2 — link dependencies (parent leaf -> dependent leaf) for
            # board observability. Every dep is known (pre-flight guaranteed),
            # so no silent skip: an unknown dep would have failed closed above.
            for leaf in leaves:
                for dep in leaf.deps:
                    await kanban.link_async(
                        leaf_to_task[dep], leaf_to_task[leaf.leaf_id], board=board,
                    )

            # Phase 3 — release. Live: assign only the ready frontier (leaves
            # with no dependencies) and spawn. Dependent children are created +
            # linked but left UNASSIGNED — requiem releases them itself after
            # recording acceptance of their parents, so kanban `done`
            # promotion is never the acceptance authority (ADR-0017 §3).
            # Dry-run: plan only.
            if inputs.live:
                if inputs.assignee is None:
                    return NeedsHuman(
                        gate="dispatch_leaves",
                        prompt="live dispatch needs --assignee <profile> to spawn workers.",
                        options=("approve", "abort"),
                        context={"board": board},
                    )
                for lid in ready_frontier:
                    await kanban.assign_async(leaf_to_task[lid], inputs.assignee, board=board)
                dispatch = await kanban.dispatch_async(board=board, dry_run=False)
            else:
                dispatch = await kanban.dispatch_async(board=board, dry_run=True)
        except KanbanBusyError as e:
            return RetryableFailure(
                retry_key=f"{ctx.run_id}:dispatch_leaves",
                error_kind="kanban.busy", message=str(e),
                attempt=ctx.attempt, after=inputs.poll_interval_s,
            )
        except KanbanClientError as e:
            return NeedsHuman(
                gate="dispatch_leaves",
                prompt=f"kanban dispatch failed: {e}",
                options=("approve", "abort"),
                context={"board": board, "error": str(e)},
            )

        mode = "live" if inputs.live else "dry_run"
        held = [lid for lid in leaf_to_task if lid not in ready_frontier]
        return Success(
            value={
                "mode": mode,
                "board": board,
                "leaf_to_task": leaf_to_task,
                "idempotency_keys": idem_keys,
                "spawned": list(dispatch.spawned),
                "skipped_unassigned": list(dispatch.skipped_unassigned),
                "ready_frontier": list(ready_frontier),
                "held_pending_acceptance": held,
            },
            inspected_artifacts=tuple(f"kanban:{board}:{t}" for t in leaf_to_task.values()),
            receipts=({"kind": "kanban.dispatch", "board": board, "mode": mode,
                       "task_ids": list(leaf_to_task.values()),
                       "idempotency_keys": list(idem_keys.values())},),
        )

    @verbs.register("poll_kanban")
    async def _poll(ctx):
        disp = ctx.completed["dispatch_leaves"]["value"]
        mode = disp["mode"]
        board = disp["board"]
        leaf_to_task: dict[str, str] = disp["leaf_to_task"]

        # Dry-run never spawns a worker — there is nothing to wait for.
        if mode == "dry_run":
            return Success(value={"mode": mode, "terminal": True, "per_leaf": [
                {"leaf_id": lid, "task_id": tid, "status": "planned", "outcome": None,
                 "result": None}
                for lid, tid in leaf_to_task.items()
            ]})

        kanban = _require_kanban(ctx)
        leaves = [_leaf_from_dict(d) for d in ctx.completed["resolve_leaves"]["value"]["leaves"]]
        deps_of = {l.leaf_id: l.deps for l in leaves}
        try:
            plan_hash = _plan_hash(leaves)
            tasks = {t.id: t for t in await kanban.list_async(board=board)}
            per_leaf = []
            for lid, tid in leaf_to_task.items():
                task = tasks.get(tid)
                if task is None:
                    # The task vanished from the board (deleted/corrupted). Settle
                    # it as needs_human rather than polling a ghost forever.
                    per_leaf.append({
                        "leaf_id": lid, "task_id": tid, "status": "missing",
                        "outcome": None, "result": None, "summary": None,
                        "requiem_outcome": _OUT_NEEDS_HUMAN,
                        "reason": "task missing from board",
                    })
                    continue
                runs = await kanban.runs_async(tid, board=board)
                latest = _latest_run(runs)
                disposition, reason = translate_state(
                    status=task.status,
                    outcome=latest.outcome if latest else None,
                    result=task.result,
                    run_raw=latest.raw if latest else None,
                    expect={"leaf_id": lid, "root_item": inputs.root_item,
                            "plan_hash": plan_hash},
                )
                per_leaf.append({
                    "leaf_id": lid, "task_id": tid, "status": task.status,
                    "outcome": latest.outcome if latest else None,
                    "result": task.result,
                    "summary": latest.summary if latest else None,
                    "requiem_outcome": disposition, "reason": reason,
                })
        except KanbanBusyError as e:
            return RetryableFailure(
                retry_key=f"{ctx.run_id}:poll_kanban", error_kind="kanban.busy",
                message=str(e), attempt=ctx.attempt, after=inputs.poll_interval_s,
            )
        except KanbanClientError as e:
            return NeedsHuman(
                gate="poll_kanban", prompt=f"kanban poll failed: {e}",
                options=("approve", "abort"), context={"board": board, "error": str(e)},
            )

        # Requiem-owned release (ADR-0017 §3, §6). Each leaf is translated to a
        # requiem disposition; only a DELIVERED parent (a verified receipt)
        # releases its children — kanban link-promotion is never the acceptance
        # authority. A child whose parent settled non-delivered (needs_human /
        # permanent_failure) can never be released and is blocked, not waited on.
        by_leaf = {p["leaf_id"]: p for p in per_leaf}
        delivered = {lid for lid, p in by_leaf.items()
                     if p["requiem_outcome"] == _OUT_DELIVERED}
        settled_self = {lid for lid, p in by_leaf.items()
                        if p["requiem_outcome"] != _OUT_IN_FLIGHT}
        nondelivered = settled_self - delivered
        blocked = compute_blocked(
            deps_of, nondelivered=nondelivered, settled=settled_self,
        )

        # A child is releasable once every parent is delivered AND its task has
        # not yet started (status ready/todo). This covers both the fresh
        # (unassigned) child and the crash-resumed one (assigned but never
        # dispatched), so release is idempotent across a crash between
        # assign and dispatch — we (re)assign if needed, then (re)dispatch.
        candidates = releasable_leaves(
            deps_of, delivered=delivered, settled=settled_self, blocked=blocked,
        )
        to_release = [
            lid for lid in candidates
            if deps_of[lid]
            and leaf_to_task[lid] in tasks
            and tasks[leaf_to_task[lid]].status in ("ready", "todo")
        ]
        if to_release and inputs.assignee is not None:
            try:
                for lid in to_release:
                    t = tasks[leaf_to_task[lid]]
                    if t.assignee is None:
                        await kanban.assign_async(t.id, inputs.assignee, board=board)
                await kanban.dispatch_async(board=board, dry_run=False)
            except KanbanBusyError as e:
                return RetryableFailure(
                    retry_key=f"{ctx.run_id}:poll_kanban", error_kind="kanban.busy",
                    message=str(e), attempt=ctx.attempt, after=inputs.poll_interval_s,
                )
            except KanbanClientError as e:
                return NeedsHuman(
                    gate="poll_kanban", prompt=f"kanban release failed: {e}",
                    options=("approve", "abort"),
                    context={"board": board, "error": str(e)},
                )
            return RetryableFailure(
                retry_key=f"{ctx.run_id}:poll_kanban",
                error_kind="kanban.released_children",
                message=f"released {len(to_release)} child leaf(s) after parent delivery",
                attempt=ctx.attempt, after=inputs.poll_interval_s,
            )

        settled = settled_self | blocked
        if len(settled) == len(deps_of):
            return Success(value={"mode": mode, "terminal": True, "per_leaf": per_leaf})

        # Budget exhausted: settle with a value-bearing timeout rather than
        # exhausting kernel retries into a value-less aggregate (which would
        # KeyError). Unsettled leaves read as in_flight → a partial verdict.
        if ctx.attempt >= inputs.max_polls:
            return Success(value={"mode": mode, "terminal": False, "timed_out": True,
                                  "per_leaf": per_leaf})
        return RetryableFailure(
            retry_key=f"{ctx.run_id}:poll_kanban",
            error_kind="kanban.not_terminal",
            message=f"{len(deps_of) - len(settled)} leaf(s) still in flight",
            attempt=ctx.attempt,
            after=inputs.poll_interval_s,
        )

    @verbs.register("aggregate")
    async def _aggregate(ctx):
        poll = ctx.completed["poll_kanban"]["value"]
        mode = poll["mode"]
        per_leaf = poll["per_leaf"]

        if mode == "dry_run":
            return NeedsHuman(
                gate="aggregate",
                prompt=f"Planned {len(per_leaf)} Hermes task(s) — DRY RUN. "
                       f"No worker was spawned; nothing was delivered. Approve the plan?",
                options=("approve", "abort"),
                context={"mode": mode, "planned": len(per_leaf),
                         "per_leaf": per_leaf},
            )

        delivered = [p for p in per_leaf if _row_delivered(p)]
        failed = [p for p in per_leaf if not _row_delivered(p)]
        timed_out = poll.get("timed_out", False)
        timeout_note = " (poll budget exhausted — some workers never finished)" if timed_out else ""
        if not failed:
            return NeedsHuman(
                gate="aggregate",
                prompt=f"All {len(delivered)} leaf task(s) delivered. Approve the batch?",
                options=("approve", "abort"),
                context={"mode": mode, "delivered": len(delivered),
                         "failed": 0, "per_leaf": per_leaf},
            )
        return NeedsHuman(
            gate="aggregate",
            prompt=f"{len(delivered)}/{len(per_leaf)} leaves delivered; "
                   f"{len(failed)} need attention{timeout_note}. Accept the partial batch?",
            options=("approve", "abort"),
            context={"mode": mode, "delivered": len(delivered),
                     "failed": len(failed), "timed_out": timed_out,
                     "per_leaf": per_leaf},
        )

    return verbs


# ---- helpers ---------------------------------------------------------


def _leaf_to_dict(l: LeafSpec) -> dict[str, Any]:
    return {"leaf_id": l.leaf_id, "title": l.title, "body": l.body,
            "branch": l.branch, "skills": list(l.skills), "deps": list(l.deps)}


def _leaf_from_dict(d: dict[str, Any]) -> LeafSpec:
    return LeafSpec(
        leaf_id=str(d["leaf_id"]), title=d.get("title", ""), body=d.get("body", ""),
        branch=d.get("branch"), skills=tuple(d.get("skills") or ()),
        deps=tuple(d.get("deps") or ()),
    )


def _validate_dep_graph(
    leaves: list[LeafSpec],
) -> tuple[str | None, tuple[str, ...]]:
    """Validate inter-leaf dependencies and compute the ready frontier.

    Returns ``(error, ready_frontier)``. ``error`` is a human-readable string
    if the graph is unsafe to dispatch (unknown dep, self-dep, or cycle) — the
    caller fails closed. ``ready_frontier`` is the ids of leaves with no
    dependencies, the only leaves requiem may release immediately; the rest are
    held until requiem records acceptance of their parents (ADR-0017 §3).

    Thin wrapper over ``leaf_deps.validate_dep_graph`` (shared with the
    in-process ``fanout`` backend, ADR-00xx) — the committed plan tree can now
    carry real sibling deps (planner ``depends_on``), so this guard is no
    longer demo-only.
    """
    return validate_dep_graph({leaf.leaf_id: leaf.deps for leaf in leaves})


def _plan_hash(leaves: list[LeafSpec]) -> str:
    """A short stable digest of the committed plan's leaf set.

    Hashes the identity-defining fields of every leaf (id, title, body, branch,
    skills, deps) in a canonical order. Binds idempotency keys to one plan
    version so a superseded plan's tasks are never silently reused (ADR-0017 §5).
    """
    canonical = json.dumps(
        sorted((_leaf_to_dict(l) for l in leaves), key=lambda d: d["leaf_id"]),
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _reconcile(task: KanbanTask, leaf: LeafSpec, key: str) -> str | None:
    """Return a human-readable mismatch string if an idempotency-reused task
    does not match the leaf we intended to dispatch, else ``None``.

    Guards the rubber-duck's blocking concern: an existing task returned for
    our key must be the *same* unit of work, or we escalate rather than
    treat it as success. The plan hash already forces a fresh key on any plan
    change, so this is defence-in-depth against key collision or a board
    mutated out from under us (ADR-0017 §5 fail-closed reconcile).
    """
    if task.idempotency_key is not None and task.idempotency_key != key:
        return f"idempotency_key {task.idempotency_key!r} != intended {key!r}"
    if task.title != leaf.title:
        return f"title {task.title!r} != intended {leaf.title!r}"
    if leaf.branch is not None and task.branch_name not in (None, leaf.branch):
        return f"branch {task.branch_name!r} != intended {leaf.branch!r}"
    if "leaf_skills" in task.raw and list(task.raw["leaf_skills"]) != list(leaf.skills):
        return f"skills {task.raw['leaf_skills']!r} != intended {list(leaf.skills)!r}"
    return None


def _latest_run(runs: list[KanbanRun]) -> KanbanRun | None:
    return runs[-1] if runs else None


def _is_delivered(per_leaf_row: dict[str, Any]) -> bool:
    """Receipt check: a leaf is delivered only when the worker reached a
    ``completed`` run outcome AND recorded a result. Weak completions
    (blocked, crashed, no result) are NOT delivery."""
    return (
        per_leaf_row.get("outcome") in _DELIVERED_OUTCOMES
        and bool(per_leaf_row.get("result"))
    )


def _row_delivered(per_leaf_row: dict[str, Any]) -> bool:
    """Whether a per-leaf row counts as delivered for the verdict.

    For live rows (which carry a ``requiem_outcome`` from
    :func:`translate_state`) the disposition is authoritative — so a ``done``
    task whose evidence was rejected (misattributed/invalid/missing metadata)
    is NOT counted as delivered even though its raw outcome is ``completed``.
    Dry-run rows have no disposition; fall back to the raw receipt check.
    """
    if "requiem_outcome" in per_leaf_row:
        return per_leaf_row["requiem_outcome"] == _OUT_DELIVERED
    return _is_delivered(per_leaf_row)


def _handoff_mismatch(handoff: HandoffMetadata, expect: Mapping[str, str]) -> str | None:
    """Reject worker evidence that belongs to a different leaf or plan version —
    evidence must never be misattributed (ADR-0017 §4)."""
    for field_name in ("leaf_id", "root_item", "plan_hash"):
        want = expect.get(field_name)
        got = getattr(handoff, field_name)
        if want is not None and got != want:
            return f"{field_name} {got!r} != expected {want!r}"
    return None


def translate_state(
    *,
    status: str,
    outcome: str | None,
    result: str | None,
    run_raw: Mapping[str, Any] | None,
    expect: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Map a kanban task's state to a Requiem disposition (ADR-0017 §6).

    Returns ``(requiem_outcome, reason)`` where ``requiem_outcome`` is one of
    ``delivered`` / ``needs_human`` / ``permanent_failure`` / ``in_flight``.

    The table:

    * non-terminal status (todo/ready/running) → ``in_flight``. Crash-reclaim
      retries land here too — the dispatcher owns the bounded retry, so a leaf
      mid-retry simply reads as still in flight and we keep polling.
    * ``done`` + ``completed`` + a result → validate the worker's handoff
      evidence; misattributed/invalid metadata DOWNGRADES to ``needs_human``
      (never silently accepted). Otherwise ``delivered``.
    * ``done`` without a delivery receipt (no ``completed`` outcome or no
      result) → ``needs_human`` — an ambiguous green, surfaced not accepted.
    * ``blocked`` after a failure outcome → ``permanent_failure`` (the
      circuit breaker tripped). ``blocked`` otherwise → ``needs_human`` (a
      worker explicitly asked for human ops).
    * ``archived`` → ``permanent_failure`` (the task was removed).

    Missing-profile is handled earlier (preflight fails closed; a task with no
    valid assignee is never dispatched), so it does not appear here.
    """
    if status not in _TERMINAL_STATUSES:
        return _OUT_IN_FLIGHT, f"task still in flight (status={status!r})"

    if status == "done":
        if outcome in _DELIVERED_OUTCOMES and result:
            try:
                handoff = extract_handoff(run_raw or {})
            except HandoffError as e:
                return _OUT_NEEDS_HUMAN, f"done but handoff invalid: {e}"
            if expect is not None:
                # In the gating path we require attributable evidence: a done
                # task with no handoff metadata is an evidence-less completion
                # we cannot attribute to this leaf/plan — surface, never accept.
                if handoff is None:
                    return _OUT_NEEDS_HUMAN, "done but missing handoff metadata"
                mism = _handoff_mismatch(handoff, expect)
                if mism is not None:
                    return _OUT_NEEDS_HUMAN, f"done but evidence misattributed: {mism}"
            return _OUT_DELIVERED, "done + completed + result (receipt)"
        return _OUT_NEEDS_HUMAN, (
            f"done but no delivery receipt (outcome={outcome!r}, "
            f"result={'present' if result else 'absent'})"
        )

    if status == "blocked":
        if outcome in _FAILURE_OUTCOMES:
            return _OUT_PERMANENT_FAILURE, f"circuit-breaker blocked after {outcome!r}"
        return _OUT_NEEDS_HUMAN, f"worker blocked for human (outcome={outcome!r})"

    return _OUT_PERMANENT_FAILURE, f"task {status!r} (removed/archived)"


# ---- workflow --------------------------------------------------------


def build_workflow() -> Workflow:
    return (
        WorkflowBuilder(
            "kanban-executor",
            module="requiem.workflows.kanban_executor",
            version="0.1",
        )
        .entry("preflight")
        .script("preflight", verb="preflight", retry_max=3)
            .edge("preflight", on="success", to="resolve_leaves")
            .edge("preflight", on="retry_exhausted", to="fail_end")
            .edge("preflight", on="permanent_failure", to="fail_end")
            .edge("preflight", on="needs_human:approve", to="resolve_leaves")
            .edge("preflight", on="needs_human:abort", to="fail_end")
        .script("resolve_leaves", verb="resolve_leaves")
            .edge("resolve_leaves", on="success", to="dispatch_leaves")
            .edge("resolve_leaves", on="permanent_failure", to="fail_end")
        .script("dispatch_leaves", verb="dispatch_leaves", retry_max=3)
            .edge("dispatch_leaves", on="success", to="poll_kanban")
            .edge("dispatch_leaves", on="retry_exhausted", to="fail_end")
            .edge("dispatch_leaves", on="permanent_failure", to="fail_end")
            .edge("dispatch_leaves", on="needs_human:approve", to="poll_kanban")
            .edge("dispatch_leaves", on="needs_human:abort", to="fail_end")
        .script("poll_kanban", verb="poll_kanban", retry_max=120)
            .edge("poll_kanban", on="success", to="aggregate")
            .edge("poll_kanban", on="retry_exhausted", to="fail_end")
            .edge("poll_kanban", on="needs_human:approve", to="fail_end")
            .edge("poll_kanban", on="needs_human:abort", to="fail_end")
        .script("aggregate", verb="aggregate")
            .edge("aggregate", on="needs_human:approve", to="end")
            .edge("aggregate", on="needs_human:abort", to="fail_end")
        .terminate("end", disposition="completed")
        .terminate("fail_end", disposition="failed")
        .humanize({
            "preflight": "Preflight — board + hermes",
            "resolve_leaves": "Resolved implementable leaves",
            "dispatch_leaves": "Dispatched leaves to Hermes kanban",
            "poll_kanban": "Awaiting Hermes workers",
            "aggregate": "approve delivery?",
            "end": "kanban-executor",
            "fail_end": "kanban-executor",
        })
        .build()
    )


# ---- gate handler + render hints -------------------------------------


def _default_gate_handler(node_id: str, prompt: str, options: tuple[str, ...]) -> str:
    """Demo gate handler — auto-takes the first (happy) option."""
    return options[0] if options else "approve"


_default_gate_handler.__requiem_auto__ = True  # type: ignore[attr-defined]


def _detail_dispatch(value: dict) -> str:
    n = len(value.get("leaf_to_task") or {})
    return f"{n} task(s) on board {value.get('board', '?')} ({value.get('mode')})"


def _detail_resolve(value: dict) -> str:
    return f"{len(value.get('leaves') or [])} leaf(s) under {value.get('root_item', '?')}"


def _gate_context_aggregate(completed: dict) -> str:
    poll = completed.get("poll_kanban", {}).get("value") or {}
    per_leaf = poll.get("per_leaf") or []
    if poll.get("mode") == "dry_run":
        return f"planned {len(per_leaf)} task(s) — nothing delivered (dry run)"
    delivered = sum(1 for p in per_leaf if _row_delivered(p))
    return f"{delivered}/{len(per_leaf)} delivered"


def render_hints() -> dict:
    return {
        "artifact_name": "implementable leaves",
        "details": {
            "resolve_leaves": _detail_resolve,
            "dispatch_leaves": _detail_dispatch,
        },
        "gate_contexts": {"aggregate": _gate_context_aggregate},
        "silent_nodes": frozenset({"end", "fail_end"}),
    }


def verdict_card(completed: dict) -> str | None:
    poll = completed.get("poll_kanban", {}).get("value") or {}
    per_leaf = poll.get("per_leaf") or []
    if not per_leaf:
        return None
    mode = poll.get("mode")
    lines = ["─── Delivery ────────────────────────────────────────────────────────"]
    if mode == "dry_run":
        lines.append(f"  📋 Planned {len(per_leaf)} task(s) — DRY RUN (nothing delivered)")
    else:
        delivered = sum(1 for p in per_leaf if _row_delivered(p))
        head = "✓ Delivered" if delivered == len(per_leaf) else "⚠ Partial delivery"
        lines.append(f"  {head}: {delivered}/{len(per_leaf)} leaves")
    for p in per_leaf:
        if mode == "dry_run":
            mark = "·"
        else:
            mark = "✓" if _row_delivered(p) else "✗"
        lines.append(f"      {mark} {p['leaf_id']} → {p['task_id']} "
                     f"[{p.get('status')}/{p.get('outcome') or '—'}]")
    lines.append("─────────────────────────────────────────────────────────────────────")
    return "\n".join(lines)


# ---- demo simulation client ------------------------------------------


def _sim_handoff_metadata(task: "KanbanTask") -> dict[str, Any]:
    """Synthesize the handoff evidence a compliant worker would emit.

    A real kanban worker calls ``kanban_complete(metadata={...})``; the sim has
    no worker, so it reconstructs that blob from the task's own identity. The
    idempotency key (``requiem:{root}:{plan_hash}:{leaf}``) carries the strict
    identity fields the executor attributes evidence by; ``assignee`` is the
    worker profile. Tasks created outside requiem (no parseable key) yield an
    empty blob — an evidence-less completion the executor surfaces, not accepts.
    """
    key = task.idempotency_key or ""
    parts = key.split(":")
    if len(parts) != 4 or parts[0] != "requiem":
        return {}
    _, root_item, plan_hash, leaf_id = parts
    return {
        "metadata": {
            "schema_version": HANDOFF_SCHEMA_VERSION,
            "leaf_id": leaf_id,
            "root_item": root_item,
            "plan_hash": plan_hash,
            "worker_profile": task.assignee or "sim-worker",
            "branch": task.branch_name,
        }
    }


class SimKanbanClient(KanbanClient):
    """In-process simulation of a Hermes kanban board for the key-free demo
    and the unit tests.

    Subclasses :class:`KanbanClient` (so it satisfies ``Toolbelt.kanban``'s
    type) but overrides every async method to operate on an in-memory board
    instead of shelling out. ``dispatch`` simulates workers immediately
    completing assigned tasks so the demo renders a full delivery narration.
    """

    def __init__(self, *, fail_leaf_ids: tuple[str, ...] = ()) -> None:
        super().__init__()
        self._boards: dict[str, dict[str, KanbanTask]] = {}
        self._runs: dict[str, list[KanbanRun]] = {}
        self._by_key: dict[str, str] = {}
        self._seq = 0
        self._fail_leaf_ids = set(fail_leaf_ids)

    async def version_async(self) -> str:
        return "SimKanban (in-process)"

    async def ensure_board_async(self, slug: str) -> None:
        self._boards.setdefault(slug, {})

    async def create_async(self, title: str, *, board: str, body=None,
                           idempotency_key=None, workspace=None, branch=None,
                           assignee=None, skills=(), max_runtime=None,
                           created_by="requiem") -> KanbanTask:
        self._boards.setdefault(board, {})
        if idempotency_key is not None and idempotency_key in self._by_key:
            return self._boards[board][self._by_key[idempotency_key]]
        self._seq += 1
        tid = f"t_sim{self._seq:04d}"
        task = KanbanTask(
            id=tid, title=title, status="ready", assignee=assignee,
            workspace_kind=workspace or "scratch", branch_name=branch,
            result=None, idempotency_key=idempotency_key,
            raw={"leaf_skills": list(skills)},
        )
        self._boards[board][tid] = task
        self._runs[tid] = []
        if idempotency_key is not None:
            self._by_key[idempotency_key] = tid
        return task

    async def link_async(self, parent_id: str, child_id: str, *, board: str) -> None:
        return None

    async def assign_async(self, task_id: str, assignee: str, *, board: str) -> None:
        for tasks in self._boards.values():
            if task_id in tasks:
                t = tasks[task_id]
                tasks[task_id] = KanbanTask(
                    id=t.id, title=t.title, status=t.status, assignee=assignee,
                    workspace_kind=t.workspace_kind, branch_name=t.branch_name,
                    result=t.result, idempotency_key=t.idempotency_key, raw=t.raw,
                )

    async def list_async(self, *, board: str, status=None) -> list[KanbanTask]:
        tasks = list(self._boards.get(board, {}).values())
        if status is not None:
            tasks = [t for t in tasks if t.status == status]
        return tasks

    async def runs_async(self, task_id: str, *, board: str) -> list[KanbanRun]:
        return list(self._runs.get(task_id, []))

    async def dispatch_async(self, *, board, dry_run=False, max_spawns=None):
        from requiem.clients.kanban import DispatchResult
        tasks = self._boards.get(board, {})
        spawned = []
        skipped = []
        for tid, t in list(tasks.items()):
            if t.status in _TERMINAL_STATUSES:
                continue  # already settled — a real dispatcher never re-spawns it
            if t.assignee is None:
                skipped.append(tid)
                continue
            if dry_run:
                continue
            # Simulate a worker running the task to completion (or failure).
            failed = any(k in self._fail_leaf_ids for k in (t.branch_name or "", t.title))
            self._seq += 1
            outcome = "crashed" if failed else "completed"
            # A compliant worker calls kanban_complete(metadata={...}); model that
            # by emitting the handoff evidence the executor consumes, attributed
            # via the task's own idempotency key (requiem:{root}:{hash}:{leaf}).
            run_raw = {} if failed else _sim_handoff_metadata(t)
            self._runs[tid].append(KanbanRun(
                id=self._seq, status="done", outcome=outcome,
                summary=f"sim worker {outcome}", profile=t.assignee, raw=run_raw,
            ))
            tasks[tid] = KanbanTask(
                id=t.id, title=t.title,
                status="blocked" if failed else "done", assignee=t.assignee,
                workspace_kind=t.workspace_kind, branch_name=t.branch_name,
                result=None if failed else f"PR opened for {t.branch_name or t.title}",
                idempotency_key=t.idempotency_key, raw=t.raw,
            )
            spawned.append(tid)
        return DispatchResult(
            spawned=tuple(spawned), skipped_unassigned=tuple(skipped),
            promoted=0, reclaimed=0, auto_blocked=(), dry_run=dry_run, raw={},
        )


# ---- demo fixture ----------------------------------------------------


def _demo_leaves() -> tuple[LeafSpec, ...]:
    return (
        LeafSpec(
            leaf_id="22001",
            title="Add retry budget to the lint verb",
            body="Wire a bounded retry on the flaky lint step.",
            branch="impl/demo-22001",
            skills=("coding",),
        ),
        LeafSpec(
            leaf_id="22002",
            title="Render the retry budget in the events view",
            body="Surface retry attempts in `requiem events`.",
            branch="impl/demo-22002",
            skills=("coding",),
            deps=("22001",),
        ),
    )


# ---- engine factory --------------------------------------------------


def build_engine(
    log_dir: Path,
    *,
    inputs: ExecInputs | None = None,
    toolbelt: Toolbelt | None = None,
    gate_handler=_default_gate_handler,
) -> Engine:
    """Construct a runnable Engine.

    With no arguments this ships a key-free, side-effect-free demo: a
    :class:`SimKanbanClient` standing in for Hermes, two inline leaves (one
    depending on the other), ``live`` simulation so the full delivery
    narration renders. Real runs go through :func:`main`.
    """
    if inputs is None:
        inputs = ExecInputs(
            root_item="demo",
            board="requiem-demo",
            assignee="requiem-worker",
            live=True,
            leaves=_demo_leaves(),
            poll_interval_s=0.0,
            max_polls=5,
        )
    if toolbelt is None:
        toolbelt = Toolbelt(
            git=Toolbelt.real().git,
            files=Toolbelt.real().files,
            kanban=SimKanbanClient(),
        )

    return Engine(
        workflow=build_workflow(),
        verbs=build_verb_registry(inputs),
        agents=AgentRegistry(),
        provider=_NullProvider(),
        toolbelt=toolbelt,
        log_dir=log_dir,
        gate_handler=gate_handler,
    )


class _NullProvider:
    """This workflow has no agent nodes; the engine still requires a provider."""

    async def invoke(self, call):  # pragma: no cover - never called
        raise RuntimeError("kanban_executor has no agent nodes")


# ---- CLI -------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kanban_executor",
        description="Dispatch implementable leaves of an ADO work item to "
                    "Hermes kanban workers.",
    )
    p.add_argument("--item", required=False, default="demo",
                   help="Root ADO work-item id (its implementable children become leaves).")
    p.add_argument("--board", default=None,
                   help="Kanban board slug (default: requiem-<item>). Never 'default'.")
    p.add_argument("--assignee", default=None,
                   help="Hermes profile that executes the leaf tasks (required for --live).")
    p.add_argument("--live", action="store_true",
                   help="Spawn real Hermes workers. Without this, a real-board DRY RUN.")
    p.add_argument("--skill", action="append", default=[], dest="skills",
                   help="Skill to force-load into each worker (repeatable).")
    p.add_argument("--poll-interval", type=float, default=5.0)
    p.add_argument("--max-polls", type=int, default=120)
    p.add_argument("--run-id", default=None)
    p.add_argument("--log-dir", default=".runs")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    log_dir = Path(args.log_dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    run_id = args.run_id or f"kanban-exec-{int(time.time())}"
    board = args.board or f"requiem-{args.item}"
    if board == "default":
        print("refusing to use the 'default' board", file=sys.stderr)
        return 2

    inputs = ExecInputs(
        root_item=str(args.item),
        board=board,
        assignee=args.assignee,
        live=args.live,
        leaves=() if str(args.item) != "demo" else _demo_leaves(),
        poll_interval_s=args.poll_interval,
        max_polls=args.max_polls,
        skills=tuple(args.skills),
    )
    # Real Hermes client for CLI runs (demo item still uses the simulator).
    if str(args.item) == "demo":
        toolbelt = Toolbelt(git=Toolbelt.real().git, files=Toolbelt.real().files,
                            kanban=SimKanbanClient())
    else:
        toolbelt = Toolbelt.real()

    engine = build_engine(log_dir, inputs=inputs, toolbelt=toolbelt)

    from requiem.cli.render import render_event
    from requiem.cli.main import _render_context_for, _print_verdict_card

    mod = sys.modules[__name__]
    cx = _render_context_for(mod, engine.workflow.name, engine.workflow.humanize)
    print(f"requiem.workflows.kanban_executor — run_id={run_id}")
    print(f"log: {engine.log_path(run_id)}")
    print("─" * 72)

    def _observer(envelope: dict[str, Any]) -> None:
        for line in render_event(envelope, cx):
            print(line)

    engine.on_event = _observer
    result = asyncio.run(engine.run(run_id))
    print("─" * 72)
    _print_verdict_card(mod, cx)
    print(f"result: {type(result).__name__}")
    print(f"log: {engine.log_path(run_id)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
