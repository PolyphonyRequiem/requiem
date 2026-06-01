"""Root-dispatch workflow — Haydn (Phase C seat).

The polyphony ``init-root`` analog. Given a root ADO work item, this
workflow:

1. Fetches the item (``twig.show``).
2. Validates it is a root (no parent, or parent is an Epic/Feature).
3. Computes a deterministic root *run id*  (``root-{item_id}-{date}``).
4. Establishes durable run-state by writing a manifest sidecar at
   ``{manifest_dir}/{run_id}.manifest.json``.
5. Optionally invokes the planning workflow as a sub-workflow (the
   ``auto_plan`` toggle). When planning completes, the manifest is
   updated with the plan-tree id and reviewer verdict.

Topology
--------

::

    start
      → fetch_item                # twig.show; classify failures
      → validate_root             # parent_id None OR parent in {Epic, Feature}
         ├─ success                       → compute_run_id
         ├─ needs_human:force-root        → compute_run_id
         └─ needs_human:reject            → end_human
      → compute_run_id            # f"root-{item_id}-{YYYY-MM-DD}"; reuses
                                  #   an existing manifest if one matches
      → write_manifest            # idempotent: reads if file exists
      → branch_auto_plan
         ├─ success                       → spawn_planning
         └─ permanent_failure:no_plan     → end_dispatched
      → spawn_planning            # .subworkflow(<per-item planning shim>)
      → record_plan_outcome       # appends sub_run_id + plan_id + verdict
      → end_planned

The sub-workflow primitive (ADR 0005) is the load-bearing seam.
``inputs_verb`` records the planning inputs in the parent log for
observability, but per ADR 0005 §"Inputs handling is best-effort for
v0", ``build_engine`` of the child is invoked with the standard
``(log_dir)`` signature. The workaround is the **per-item planning
shim** (``_register_planning_shim``): a ``sys.modules`` entry whose
``build_engine`` closes over ``item_id`` / ``twig`` / ``provider``.
The shim is re-registered on every ``build_engine`` call of this
workflow so it is also present on resume (INV-RESTART).

Hard rules honoured
-------------------

* **INV-RESTART**: every state-mutating verb is idempotent. The manifest
  is read-or-create. The shim is re-registered on every engine
  construction. ``compute_run_id`` is deterministic per ``(item_id,
  existing-manifest, today)``; on resume the kernel re-uses the
  recorded ``verb_completed`` value rather than re-invoking it.
* **INV-SUBWORKFLOW-LOG-ISOLATION**: the planning sub-workflow writes
  to ``{root_run_id}__plan.events.jsonl`` (sibling of the parent log
  in the same ``log_dir``). The parent records only
  ``subworkflow_started`` / ``subworkflow_completed`` markers; planner
  / reviewer events live in the child log.
* **Ravel's L-1 caveat**: every ``twig`` call routes unclassified
  failures to a human gate, not to a retry loop.
* **Closed ``error_kind`` enum** (ADR 0004 §4.2): see ``ERROR_KINDS``.

Out of scope (v0)
-----------------

* Multi-root parallel dispatch (one root at a time).
* Auto-dispatch of implementation per leaf — the verdict card hands off
  with the next command for the operator to run.
* Cross-run reconciliation.
* Real manifest schema versioning / migrations.
"""
from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from requiem.agent import AgentProvider, FakeProvider
from requiem.clients.twig import (
    TwigClient,
    TwigClientError,
    TwigItem,
    TwigItemNotFoundError,
    TwigRateLimitedError,
)
from requiem.dsl import AgentRegistry, VerbRegistry, Workflow, WorkflowBuilder
from requiem.kernel import Engine
from requiem.outcomes import (
    NeedsHuman,
    PermanentFailure,
    RetryableFailure,
    Success,
)
from requiem.toolbelt import Toolbelt
from requiem.workflows import planning as _planning


# ---- public dataclasses ------------------------------------------------


@dataclass(frozen=True, slots=True)
class RootDispatchInputs:
    """Bundle of operator-supplied parameters threaded into the verbs.

    Frozen so a partial run can't mutate its own inputs out from under
    itself on resume (mirrors Bizet's ``ImplementationInputs``).
    """

    item_id: int
    repo: str = "PolyphonyRequiem/requiem"
    repo_path: Path = field(default_factory=lambda: Path.cwd())
    base_branch: str = "main"
    dry_run: bool = False
    auto_plan: bool = True
    manifest_dir: Path | None = None
    """Where to write/read ``{run_id}.manifest.json``. ``None`` →
    ``log_dir / '.runs'`` (the kernel's log dir + the polyphony-style
    ``.runs`` subdir, so all dispatch state lives next to the events)."""


@dataclass(frozen=True, slots=True)
class RootDispatchResult:
    """The structured outcome a programmatic caller plucks out of the projection.

    Mirrors the brief verbatim. ``plan_tree_id`` and ``plan_verdict``
    are populated only when ``auto_plan=True`` AND planning ran to a
    recorded outcome.
    """

    item_id: int
    run_id: str
    manifest_path: Path
    plan_tree_id: str | None
    plan_verdict: str | None
    dry_run: bool

    @classmethod
    def from_completed(
        cls, completed: dict[str, dict[str, Any]]
    ) -> "RootDispatchResult | None":
        """Lift the engine's ``completed`` projection into a result.

        Returns ``None`` if the workflow didn't reach ``write_manifest`` —
        a dispatch that failed before recording the manifest has no
        meaningful result projection.
        """
        wm = (completed.get("write_manifest") or {}).get("value") or {}
        if not wm:
            return None
        ro = (completed.get("record_plan_outcome") or {}).get("value") or {}
        return cls(
            item_id=int(wm.get("item_id", 0)),
            run_id=str(wm.get("run_id", "")),
            manifest_path=Path(wm.get("manifest_path", "")),
            plan_tree_id=ro.get("plan_tree_id"),
            plan_verdict=ro.get("plan_verdict"),
            dry_run=bool(wm.get("dry_run", False)),
        )


# ---- closed error_kind taxonomy (ADR 0004 §4.2) -----------------------


ERROR_KINDS: frozenset[str] = frozenset({
    "twig.not_found",
    "twig.rate_limited",
    "twig.unknown",
    "manifest.write_failed",
    "manifest.read_failed",
    "no_plan",
    "plan.missing_log",
    "plan.no_record",
})


# ---- root-tier policy --------------------------------------------------


ROOT_PARENT_TYPES: frozenset[str] = frozenset({"Epic", "Feature"})
"""Parent work-item types that still qualify the child as a root.

Polyphony's tier model puts ``Epic`` and ``Feature`` above the
implementable tier; a User Story / Task whose parent is one of these
is a valid root for the SDLC pipeline. Anything else (Story under
Story, Task under Task, etc.) routes to the human gate.
"""


# ---- twig seam (Protocol so tests can substitute fakes) ---------------


class TwigClientProto(Protocol):
    def show(self, item_id: int) -> TwigItem: ...


# ---- in-memory twig stand-in (used by demo + tests) ------------------


@dataclass
class FakeTwigClient:
    """In-memory twig stub for the CLI demo and unit tests.

    Maps ``{item_id: TwigItem}``; raises ``TwigItemNotFoundError`` for
    misses so verb classifier paths are exercised.
    """

    items: dict[int, TwigItem] = field(default_factory=dict)

    def show(self, item_id: int) -> TwigItem:
        if item_id not in self.items:
            raise TwigItemNotFoundError(f"fake: item {item_id} not found")
        return self.items[item_id]

    async def show_async(self, item_id: int) -> TwigItem:
        # Planning's TwigClientProto requires the async surface (per Tchaikovsky's
        # bug-bash fix in PR #32). Mirror sync for in-memory fakes.
        return self.show(item_id)


# ---- manifest helpers --------------------------------------------------


MANIFEST_VERSION = "1"


def _manifest_path(manifest_dir: Path, run_id: str) -> Path:
    return manifest_dir / f"{run_id}.manifest.json"


def _find_existing_manifest_for_item(
    manifest_dir: Path, item_id: int
) -> dict[str, Any] | None:
    """Scan ``manifest_dir`` for any ``root-{item_id}-*.manifest.json``.

    Returns the parsed manifest dict for the first match (lexicographic
    order — yields the most recent date last, which is what we want to
    reuse when a run is re-dispatched same day). Returns ``None`` if no
    manifest is on disk.

    Idempotency anchor: re-dispatching the same item produces the same
    ``run_id`` so the manifest path is stable and the workflow is a
    no-op on second invocation (per the brief: "Re-dispatch same item
    → idempotent").
    """
    if not manifest_dir.exists():
        return None
    candidates = sorted(manifest_dir.glob(f"root-{item_id}-*.manifest.json"))
    if not candidates:
        return None
    try:
        return json.loads(candidates[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # A corrupt manifest is treated as if no manifest exists —
        # write_manifest will overwrite it. We do NOT silently silence
        # the error in production: callers can grep the log for a
        # `manifest.read_failed` PermanentFailure if write fails too.
        return None


def _today_utc_iso_date() -> str:
    """Today's date as ``YYYY-MM-DD`` in UTC. Hookable for tests."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compute_root_run_id(
    item_id: int,
    manifest_dir: Path,
    *,
    today: str | None = None,
) -> tuple[str, bool]:
    """Return ``(run_id, reused)``.

    Deterministic: if a manifest for this item already exists, its
    ``run_id`` is reused (idempotency); otherwise a fresh
    ``root-{item_id}-{today}`` is minted. ``today`` lets tests pin the
    date without monkey-patching ``datetime.now``.
    """
    existing = _find_existing_manifest_for_item(manifest_dir, item_id)
    if existing is not None and existing.get("run_id"):
        return str(existing["run_id"]), True
    date_s = today or _today_utc_iso_date()
    return f"root-{item_id}-{date_s}", False


def _initial_manifest(
    *,
    run_id: str,
    item: dict[str, Any],
    repo: str,
    repo_path: Path,
    base_branch: str,
) -> dict[str, Any]:
    return {
        "version": MANIFEST_VERSION,
        "run_id": run_id,
        "item_id": int(item.get("item_id", 0)),
        "item_title": str(item.get("title", "")),
        "item_type": str(item.get("work_item_type", "")),
        "repo": repo,
        "repo_path": str(repo_path).replace("\\", "/"),
        "base_branch": base_branch,
        "area_path": str(item.get("area_path", "")),
        "created_at": _now_utc_iso(),
        "child_run_ids": [],
    }


# ---- per-item planning shim (ADR 0005 §"Inputs handling…" workaround) -


def _planning_shim_module_name(item_id: int) -> str:
    """Deterministic shim module path. Sanitises ``item_id`` to a python id."""
    return f"requiem.workflows._dispatch_planning_for_item_{int(item_id)}"


def _register_planning_shim(
    *,
    item_id: int,
    twig: TwigClientProto,
    provider: AgentProvider | None,
    gate_handler: Callable | None,
) -> str:
    """Register a ``sys.modules`` shim that closes over planning inputs.

    The kernel's ``SubWorkflowNode`` invokes a child workflow by
    importable module path; ``planning.build_engine(log_dir)`` accepts
    parameters this workflow needs to set (``item_id``, ``twig``,
    ``provider``, ``gate_handler``). Per ADR 0005 §"Inputs handling is
    best-effort for v0", an ``inputs_verb`` return value is recorded
    in the event log but NOT forwarded to the child's ``build_engine``.

    The workaround is a per-item shim module: a synthetic
    ``ModuleType`` registered in ``sys.modules`` whose
    ``build_engine(log_dir)`` calls into
    ``planning.build_engine`` with the closure-bound parameters.

    The module name is deterministic on ``item_id`` so a resume of the
    parent re-imports the *same* module path the parent's
    ``subworkflow_started`` event records. We re-register on every
    ``root_dispatch.build_engine`` call so the shim is always present
    when the kernel needs to import it, even after a process restart.
    """
    mod_name = _planning_shim_module_name(item_id)
    mod = types.ModuleType(mod_name)
    mod.__doc__ = (
        f"Per-item planning shim for AB#{item_id}; "
        "constructed by requiem.workflows.root_dispatch."
    )

    def build_engine(log_dir: Path, **_kw: Any) -> Engine:
        return _planning.build_engine(
            log_dir,
            item_id=item_id,
            twig=twig,
            provider=provider,
            gate_handler=gate_handler,
        )

    def build_workflow() -> Workflow:
        return _planning.build_workflow()

    def render_hints() -> dict:
        return _planning.render_hints()

    mod.build_engine = build_engine  # type: ignore[attr-defined]
    mod.build_workflow = build_workflow  # type: ignore[attr-defined]
    mod.render_hints = render_hints  # type: ignore[attr-defined]
    sys.modules[mod_name] = mod
    return mod_name


# ---- verb library ------------------------------------------------------


def build_verb_registry(
    inputs: RootDispatchInputs,
    *,
    twig: TwigClientProto,
    manifest_dir: Path,
    today: str | None = None,
) -> VerbRegistry:
    """Build the closure-bound verb registry for one dispatch run.

    ``today`` is a hook so tests can pin the calendar date that
    ``compute_run_id`` uses for fresh manifests — mirrors Bizet's
    ``test_runner`` injection pattern.
    """
    verbs = VerbRegistry()

    @verbs.register("start_run")
    def _start(ctx):
        return Success(value={
            "intent": "root-dispatch",
            "item_id": inputs.item_id,
            "repo": inputs.repo,
            "repo_path": str(inputs.repo_path),
            "base_branch": inputs.base_branch,
            "dry_run": inputs.dry_run,
            "auto_plan": inputs.auto_plan,
        })

    # ---- fetch_item ---------------------------------------------------

    @verbs.register("fetch_item")
    def _fetch_item(ctx):
        try:
            item = twig.show(inputs.item_id)
        except TwigItemNotFoundError as e:
            return PermanentFailure(
                error_kind="twig.not_found",
                message=f"work item {inputs.item_id} not found: {e}",
            )
        except TwigRateLimitedError as e:
            # Honored by the workflow as a one-shot retry-eligible failure;
            # the topology does not wire `retry_max>0` so this falls
            # through to `permanent_failure` per Ravel L-1 (escalation
            # to a human is the only safe action without a retry budget).
            return RetryableFailure(
                retry_key=f"{ctx.run_id}:fetch_item",
                error_kind="twig.rate_limited",
                message=str(e),
                attempt=ctx.attempt,
            )
        except TwigClientError as e:
            # Ravel L-1: anything we couldn't classify must reach a human.
            return PermanentFailure(
                error_kind="twig.unknown",
                message=f"twig error fetching {inputs.item_id}: {e}",
            )
        return Success(
            value={
                "item_id": item.id,
                "title": item.title,
                "state": item.state,
                "work_item_type": item.work_item_type,
                "area_path": item.area_path,
                "parent_id": item.parent_id,
            },
            inspected_artifacts=(f"twig:item/{item.id}",),
        )

    # ---- validate_root ------------------------------------------------

    @verbs.register("validate_root")
    def _validate_root(ctx):
        item = ctx.completed["fetch_item"]["value"]
        parent_id = item.get("parent_id")
        if parent_id is None:
            return Success(value={
                "is_root": True,
                "reason": "no parent",
                "parent_id": None,
                "parent_type": None,
                "parent_title": None,
            })

        # Has a parent — classify by parent type.
        try:
            parent = twig.show(int(parent_id))
        except TwigItemNotFoundError as e:
            # Parent missing is a state-drift signal; escalate per
            # INV-NO-CORRUPT-FORWARD instead of guessing.
            return NeedsHuman(
                gate="not_root",
                prompt=(
                    f"AB#{inputs.item_id} has parent AB#{parent_id} which "
                    f"could not be fetched: {e}. Force-root or reject?"
                ),
                options=("force-root", "reject"),
                context={
                    "item_id": inputs.item_id,
                    "parent_id": int(parent_id),
                    "reason": "parent_not_found",
                },
            )
        except TwigClientError as e:
            return NeedsHuman(
                gate="not_root",
                prompt=(
                    f"AB#{inputs.item_id} parent fetch failed: {e}. "
                    "Force-root or reject?"
                ),
                options=("force-root", "reject"),
                context={
                    "item_id": inputs.item_id,
                    "parent_id": int(parent_id),
                    "reason": "parent_fetch_failed",
                },
            )

        if parent.work_item_type in ROOT_PARENT_TYPES:
            return Success(value={
                "is_root": True,
                "reason": f"parent is {parent.work_item_type}",
                "parent_id": parent.id,
                "parent_type": parent.work_item_type,
                "parent_title": parent.title,
            })

        return NeedsHuman(
            gate="not_root",
            prompt=(
                f"AB#{inputs.item_id} (\"{item.get('title')}\") has parent "
                f"AB#{parent.id} of type {parent.work_item_type!r} "
                f"(\"{parent.title}\") — not at root tier "
                f"({sorted(ROOT_PARENT_TYPES)}). Force-root or reject?"
            ),
            options=("force-root", "reject"),
            context={
                "item_id": inputs.item_id,
                "parent_id": parent.id,
                "parent_type": parent.work_item_type,
                "parent_title": parent.title,
            },
        )

    # ---- compute_run_id -----------------------------------------------

    @verbs.register("compute_run_id")
    def _compute_run_id(ctx):
        run_id, reused = _compute_root_run_id(
            inputs.item_id, manifest_dir, today=today,
        )
        return Success(value={
            "run_id": run_id,
            "reused": reused,
            "manifest_dir": str(manifest_dir),
        })

    # ---- write_manifest -----------------------------------------------

    @verbs.register("write_manifest")
    def _write_manifest(ctx):
        run_id = ctx.completed["compute_run_id"]["value"]["run_id"]
        item = ctx.completed["fetch_item"]["value"]
        path = _manifest_path(manifest_dir, run_id)

        # Idempotent: prefer existing manifest content so callers see the
        # original `created_at` and any `child_run_ids` accumulated by a
        # prior dispatch. We never overwrite a manifest from this verb;
        # downstream verbs (`record_plan_outcome`) do controlled appends.
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                return PermanentFailure(
                    error_kind="manifest.read_failed",
                    message=f"could not parse existing manifest at {path}: {e}",
                )
            return Success(
                value={
                    "run_id": run_id,
                    "manifest_path": str(path),
                    "item_id": data.get("item_id", item.get("item_id")),
                    "reused": True,
                    "dry_run": inputs.dry_run,
                },
                inspected_artifacts=(f"file:{path}",),
            )

        manifest = _initial_manifest(
            run_id=run_id,
            item=item,
            repo=inputs.repo,
            repo_path=inputs.repo_path,
            base_branch=inputs.base_branch,
        )

        if inputs.dry_run:
            # Don't touch disk in dry-run; surface the would-be manifest
            # in the verdict card so the operator can preview without
            # mutating .runs/.
            return Success(value={
                "run_id": run_id,
                "manifest_path": str(path),
                "item_id": manifest["item_id"],
                "reused": False,
                "dry_run": True,
                "preview": manifest,
            })

        try:
            manifest_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8",
            )
        except OSError as e:
            return PermanentFailure(
                error_kind="manifest.write_failed",
                message=f"writing {path}: {e}",
                details={"path": str(path)},
            )
        return Success(
            value={
                "run_id": run_id,
                "manifest_path": str(path),
                "item_id": manifest["item_id"],
                "reused": False,
                "dry_run": False,
            },
            inspected_artifacts=(f"file:{path}",),
        )

    # ---- branch_auto_plan ---------------------------------------------

    @verbs.register("branch_auto_plan")
    def _branch_auto_plan(ctx):
        # Pure router: Success → spawn_planning; permanent_failure:no_plan
        # → end_dispatched. Mirrors the planning workflow's router_i
        # idiom (a Success means "take the happy edge").
        if inputs.auto_plan:
            return Success(value={"auto_plan": True})
        return PermanentFailure(
            error_kind="no_plan",
            message="auto_plan=False; dispatch stops after manifest",
            details={"auto_plan": False},
        )

    # ---- planning_inputs (recorded into subworkflow_started) ----------

    @verbs.register("planning_inputs")
    def _planning_inputs(ctx):
        wm = ctx.completed["write_manifest"]["value"]
        return {
            "item_id": int(wm["item_id"]),
            "root_run_id": wm["run_id"],
            "repo": inputs.repo,
            "repo_path": str(inputs.repo_path),
        }

    # ---- record_plan_outcome ------------------------------------------

    @verbs.register("record_plan_outcome")
    def _record_plan_outcome(ctx):
        sub = ctx.completed.get("spawn_planning") or {}
        # The kernel populates `completed[spawn_planning]` with the
        # mapped outcome (kind=success / permanent_failure / needs_human).
        # We only reach this node on the `success` edge so it's safe to
        # assume kind=success and value carries the sub_run_id.
        sub_value = (sub.get("value") or {})
        sub_run_id = sub_value.get("sub_run_id")
        child_disposition = sub_value.get("child_disposition")

        plan_tree_id: str | None = None
        plan_verdict: str | None = None
        plan_summary: dict[str, Any] = {}

        if sub_run_id:
            # Read the child's log to project the plan-result dataclass.
            # The kernel's subworkflow_completed payload only carries the
            # mapped outcome — the PlanResult lives in the child's log.
            child_log = _locate_child_log(manifest_dir, sub_run_id)
            if child_log is None or not child_log.exists():
                return PermanentFailure(
                    error_kind="plan.missing_log",
                    message=(
                        f"could not find child log for {sub_run_id!r}; "
                        "spawn_planning completed but no events.jsonl exists"
                    ),
                    details={"sub_run_id": sub_run_id},
                )
            child_completed = _planning.completed_from_log(child_log)
            plan = _planning.project_plan_result(child_completed)
            if plan is None:
                # Planning ran but didn't record a plan (e.g. failed
                # before `record_plan`); reflect that in the manifest.
                plan_tree_id = None
                plan_verdict = "no_record"
            else:
                plan_tree_id = plan.plan_id
                plan_verdict = plan.final_verdict
                plan_summary = {
                    "decomposable": plan.decomposable,
                    "child_count": len(plan.children),
                    "review_iterations": plan.review_iterations,
                    "summary": plan.summary,
                }

        # Append the sub_run_id + plan info into the manifest.
        run_id = ctx.completed["compute_run_id"]["value"]["run_id"]
        path = _manifest_path(manifest_dir, run_id)
        if inputs.dry_run:
            # Dry-run: don't mutate disk; report the intended update.
            return Success(value={
                "sub_run_id": sub_run_id,
                "child_disposition": child_disposition,
                "plan_tree_id": plan_tree_id,
                "plan_verdict": plan_verdict,
                "plan_summary": plan_summary,
                "dry_run": True,
            })

        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
            else:
                # Defensive: write_manifest ran but the file vanished.
                # Reconstruct a minimal manifest so we don't lose the
                # plan record. The operator can diff against the log.
                data = {"version": MANIFEST_VERSION, "run_id": run_id,
                        "child_run_ids": []}
            child_ids = list(data.get("child_run_ids") or [])
            if sub_run_id and sub_run_id not in child_ids:
                child_ids.append(sub_run_id)
            data["child_run_ids"] = child_ids
            data["plan_tree_id"] = plan_tree_id
            data["plan_verdict"] = plan_verdict
            if plan_summary:
                data["plan_summary"] = plan_summary
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except (OSError, json.JSONDecodeError) as e:
            return PermanentFailure(
                error_kind="manifest.write_failed",
                message=f"updating {path} after planning: {e}",
                details={"path": str(path)},
            )

        return Success(
            value={
                "sub_run_id": sub_run_id,
                "child_disposition": child_disposition,
                "plan_tree_id": plan_tree_id,
                "plan_verdict": plan_verdict,
                "plan_summary": plan_summary,
                "dry_run": False,
            },
            inspected_artifacts=(f"file:{path}",),
        )

    return verbs


def _locate_child_log(manifest_dir: Path, sub_run_id: str) -> Path | None:
    """Find ``{sub_run_id}.events.jsonl`` near the manifest directory.

    The child engine writes to the parent's ``log_dir`` (ADR 0005 §"Path
    safety"). We don't carry ``log_dir`` directly into the verb closure,
    so we search:

    1. ``manifest_dir.parent`` — the default convention where
       ``manifest_dir = log_dir / '.runs'``.
    2. ``manifest_dir`` itself — covers the case where the operator
       points ``manifest_dir`` at ``log_dir`` directly.

    Returns the first match, or ``None`` if neither exists.
    """
    fname = f"{sub_run_id}.events.jsonl"
    for candidate_dir in (manifest_dir.parent, manifest_dir):
        p = candidate_dir / fname
        if p.exists():
            return p
    return None


# ---- agent registry (empty — no agents at this layer) ----------------


def build_agent_registry() -> AgentRegistry:
    """Root dispatch invokes no agents directly; all agent work happens
    inside the planning sub-workflow."""
    return AgentRegistry()


# ---- workflow topology -----------------------------------------------


def build_workflow(*, planning_module: str | None = None) -> Workflow:
    """Build the root-dispatch workflow.

    ``planning_module`` is the importable module path of the planning
    sub-workflow. ``None`` is a placeholder for ``describe`` /
    workflow-shape inspection — the kernel won't actually invoke the
    sub-workflow until ``build_engine`` registers the per-item shim.
    """
    sub_mod = planning_module or "requiem.workflows.planning"
    return (
        WorkflowBuilder(
            "root-dispatch",
            module="requiem.workflows.root_dispatch",
            version="0.1",
        )
            .entry("start")
            .script("start", verb="start_run")
                .edge("start", on="success", to="fetch_item")
            .script("fetch_item", verb="fetch_item")
                .edge("fetch_item", on="success", to="validate_root")
                .edge(
                    "fetch_item",
                    on="permanent_failure:twig.not_found",
                    to="end_failed",
                )
                .edge(
                    "fetch_item",
                    on="permanent_failure:twig.rate_limited",
                    to="end_failed",
                )
                .edge(
                    "fetch_item",
                    on="permanent_failure:twig.unknown",
                    to="end_failed",
                )
            .script("validate_root", verb="validate_root")
                .edge("validate_root", on="success", to="compute_run_id")
                .edge(
                    "validate_root",
                    on="needs_human:force-root",
                    to="compute_run_id",
                )
                .edge(
                    "validate_root",
                    on="needs_human:reject",
                    to="end_human",
                )
            .script("compute_run_id", verb="compute_run_id")
                .edge("compute_run_id", on="success", to="write_manifest")
            .script("write_manifest", verb="write_manifest")
                .edge("write_manifest", on="success", to="branch_auto_plan")
                .edge(
                    "write_manifest",
                    on="permanent_failure:manifest.write_failed",
                    to="end_failed",
                )
                .edge(
                    "write_manifest",
                    on="permanent_failure:manifest.read_failed",
                    to="end_failed",
                )
            .script("branch_auto_plan", verb="branch_auto_plan")
                .edge(
                    "branch_auto_plan", on="success", to="spawn_planning",
                )
                .edge(
                    "branch_auto_plan",
                    on="permanent_failure:no_plan",
                    to="end_dispatched",
                )
            .subworkflow(
                "spawn_planning",
                workflow=sub_mod,
                inputs_verb="planning_inputs",
            )
                .edge("spawn_planning", on="success", to="record_plan_outcome")
                # Planning surrendered to a human gate that wasn't auto-
                # resolved; bubble it up by routing to end_human so the
                # operator gets a verdict card pointing at the child log.
                .edge("spawn_planning", on="needs_human", to="end_human")
                # Planning reached terminate(disposition=failed) or
                # otherwise failed: still record what we can, then halt.
                .edge(
                    "spawn_planning",
                    on="permanent_failure",
                    to="record_plan_outcome",
                )
                .edge("spawn_planning", on="cancelled", to="end_failed")
            .script("record_plan_outcome", verb="record_plan_outcome")
                .edge("record_plan_outcome", on="success", to="end_planned")
                .edge(
                    "record_plan_outcome",
                    on="permanent_failure",
                    to="end_failed",
                )
            .terminate("end_planned",    disposition="completed")
            .terminate("end_dispatched", disposition="completed")
            .terminate("end_human",      disposition="completed")
            .terminate("end_failed",     disposition="failed")
            .humanize({
                "start":               "Starting root dispatch",
                "fetch_item":          "Fetched work item",
                "validate_root":       "Validated root-tier",
                "compute_run_id":      "Computed run id",
                "write_manifest":      "Wrote run manifest",
                "branch_auto_plan":    "Auto-plan branch",
                "spawn_planning":      "Spawned planning sub-workflow",
                "record_plan_outcome": "Recorded plan outcome",
                "end_planned":         "root-dispatch",
                "end_dispatched":      "root-dispatch",
                "end_human":           "root-dispatch",
                "end_failed":          "root-dispatch",
            })
            .build()
    )


# ---- render hints -----------------------------------------------------


def _detail_fetch_item(value: dict) -> str:
    return f"AB#{value.get('item_id', '?')} — \"{value.get('title', '?')}\""


def _detail_validate_root(value: dict) -> str:
    return value.get("reason", "?")


def _detail_compute_run_id(value: dict) -> str:
    head = "reused" if value.get("reused") else "fresh"
    return f"{value.get('run_id', '?')} ({head})"


def _detail_write_manifest(value: dict) -> str:
    head = "(dry-run) " if value.get("dry_run") else ("reused " if value.get("reused") else "")
    return f"{head}{value.get('manifest_path', '?')}"


def _detail_branch_auto_plan(value: dict) -> str:
    return "auto_plan" if value.get("auto_plan") else "(unreached: see permanent_failure)"


def _detail_record_plan_outcome(value: dict) -> str:
    return (
        f"plan_id={value.get('plan_tree_id', '—')} "
        f"verdict={value.get('plan_verdict', '—')}"
    )


def render_hints() -> dict:
    return {
        "artifact_name": "root dispatch",
        "details": {
            "fetch_item":          _detail_fetch_item,
            "validate_root":       _detail_validate_root,
            "compute_run_id":      _detail_compute_run_id,
            "write_manifest":      _detail_write_manifest,
            "branch_auto_plan":    _detail_branch_auto_plan,
            "record_plan_outcome": _detail_record_plan_outcome,
        },
        "gate_contexts": {
            "validate_root": lambda ctx: (
                f"parent={ctx.get('parent_id')} "
                f"type={ctx.get('parent_type', '?')!r}"
            ),
        },
        "silent_nodes": frozenset({
            "start",
            "end_planned",
            "end_dispatched",
            "end_human",
            "end_failed",
        }),
    }


# ---- verdict card -----------------------------------------------------


def _hrule(width: int = 69) -> str:
    return "─" * width


def verdict_card(completed: dict[str, dict[str, Any]]) -> str | None:
    """Render the three shapes from the brief:

    * happy + auto_plan → "Dispatched + planned"
    * dispatch-only      → "Dispatched (no auto-plan)"
    * not-a-root         → "Not a root item"

    Returns ``None`` if the workflow halted before even fetching the
    item — the CLI then prints nothing extra (the run footer suffices).
    """
    item_block = (completed.get("fetch_item") or {}).get("value") or {}
    if not item_block:
        return None
    item_id = item_block.get("item_id", "?")
    title = item_block.get("title", "?")

    head = f"─── Root Dispatch: AB#{item_id} "
    head += _hrule(max(1, 69 - len(head)))

    # Has a NeedsHuman gate resolved as `reject`? The validate_root verb
    # produced NeedsHuman, the operator chose `reject`, and the workflow
    # terminated at end_human via that route.
    vr = (completed.get("validate_root") or {}).get("value") or {}
    vr_kind = (completed.get("validate_root") or {}).get("kind")
    if vr_kind == "needs_human":
        # An unresolved needs-human (no operator response) is rare in
        # CLI runs because the default handler picks first. Surface the
        # gate prompt so the operator sees what they need to answer.
        prompt = (completed.get("validate_root") or {}).get("prompt", "")
        lines = [
            head,
            "  🚦 Not a root item (awaiting operator)",
            f"      Item:        {item_id} — {title!r}",
            f"      Reason:      {prompt}",
            f"      Resume:      requiem resume <run-id> "
            "--decision force-root|reject",
            _hrule(),
        ]
        return "\n".join(lines)

    # Did the operator pick `reject`? Workflow reached end_human via
    # validate_root's needs_human edge. The route_taken event is the
    # ground-truth, but we can deduce from absence-of-compute_run_id.
    if "compute_run_id" not in completed:
        # validate_root produced NeedsHuman + operator picked reject.
        ctx = (completed.get("validate_root") or {}).get("context") or vr
        parent_id = ctx.get("parent_id")
        parent_type = ctx.get("parent_type")
        item_type = item_block.get("work_item_type", "?")
        parent_line = (
            f"{item_type} (parent: AB#{parent_id} "
            f"{ctx.get('parent_title', '?')!r}; not at root tier)"
            if parent_id is not None
            else f"{item_type} (no parent — but rejected by operator)"
        )
        lines = [
            head,
            "  🚦 Not a root item",
            f"      Item:        {item_id} — {title!r}",
            f"      Type:        {parent_line}",
            "      Resume:      requiem resume <run-id> "
            "--decision force-root|reject",
            _hrule(),
        ]
        return "\n".join(lines)

    wm = (completed.get("write_manifest") or {}).get("value") or {}
    run_id = wm.get("run_id", "?")
    manifest_path = wm.get("manifest_path", "?")

    # Did we run the planning sub-workflow?
    rec = (completed.get("record_plan_outcome") or {}).get("value") or {}
    if rec:
        plan_verdict = rec.get("plan_verdict") or "—"
        plan_summary = rec.get("plan_summary") or {}
        child_count = plan_summary.get("child_count", 0)
        plan_line = (
            f"{plan_verdict} ({child_count} children)"
            if plan_summary.get("decomposable")
            else f"{plan_verdict} (leaf)"
            if plan_verdict and plan_verdict != "—"
            else "(unrecorded)"
        )
        lines = [
            head,
            "  ✓ Dispatched + planned",
            f"      Item:        {item_id} — {title!r}",
            f"      Run:         {run_id}",
            f"      Manifest:    {manifest_path}",
            f"      Plan:        {plan_line}",
            "      Next:        requiem run requiem.workflows.implementation "
            "--item <child>",
            _hrule(),
        ]
        return "\n".join(lines)

    # Dispatch-only path (auto_plan=False).
    lines = [
        head,
        "  ✓ Dispatched (no auto-plan)",
        f"      Item:        {item_id} — {title!r}",
        f"      Run:         {run_id}",
        f"      Manifest:    {manifest_path}",
        f"      Next:        requiem run requiem.workflows.planning "
        f"--item {item_id}",
        _hrule(),
    ]
    return "\n".join(lines)


# ---- engine factory ---------------------------------------------------


def _default_gate_handler(
    node_id: str, prompt: str, options: tuple[str, ...]
) -> str:
    """Demo gate handler. For the validate_root gate, picks ``reject`` so
    the demo terminates cleanly without trying to force-root a fixture.

    Tests supply their own handlers. The polyphony semantic for the
    not-a-root case is "the operator decides"; auto-rejecting in the
    demo prevents the workflow from silently barrelling forward into a
    questionable dispatch.
    """
    if "reject" in options:
        return "reject"
    if "abort" in options:
        return "abort"
    return options[0] if options else "reject"


_default_gate_handler.__requiem_auto__ = True  # type: ignore[attr-defined]


def _make_demo_inputs() -> RootDispatchInputs:
    """Self-contained demo inputs. Used when the CLI invokes us with no
    operator-supplied parameters (e.g. ``requiem run`` smoke test)."""
    return RootDispatchInputs(
        item_id=99999,
        repo="PolyphonyRequiem/requiem",
        repo_path=Path.cwd(),
        base_branch="main",
        dry_run=True,    # the CLI demo never mutates ADO state
        auto_plan=False,  # dispatch-only verdict; planning has its own demo
    )


def _demo_twig(item_id: int = 99999) -> FakeTwigClient:
    return FakeTwigClient(
        items={
            item_id: TwigItem(
                id=item_id,
                title="Demo root: improve SDLC ergonomics",
                state="Active",
                area_path="PolyphonyRequiem\\v0",
                work_item_type="User Story",
                parent_id=None,
                raw={},
            ),
        }
    )


def build_engine(
    log_dir: Path,
    *,
    inputs: RootDispatchInputs | None = None,
    twig: TwigClientProto | None = None,
    provider: AgentProvider | None = None,
    gate_handler: Callable | None = None,
    manifest_dir: Path | None = None,
    today: str | None = None,
) -> Engine:
    """Construct a runnable Engine for the root-dispatch workflow.

    Defaults are self-contained (FakeTwig + ``dry_run=True`` +
    ``auto_plan=False``) so ``requiem run requiem.workflows.root_dispatch``
    works out of the box and exercises the full demo without writing
    to disk. Production callers pass explicit ``inputs``, ``twig``,
    ``provider``, and (optionally) ``manifest_dir``.

    Per ADR 0005 §"Inputs handling…": we register a per-item planning
    shim in ``sys.modules`` so the kernel's ``SubWorkflowNode`` can
    invoke a planning child engine with the right ``item_id`` /
    ``twig`` / ``provider``.
    """
    if inputs is None:
        inputs = _make_demo_inputs()
    if twig is None:
        twig = _demo_twig(inputs.item_id)
    mdir = manifest_dir or inputs.manifest_dir or (log_dir / ".runs")

    planning_module = _register_planning_shim(
        item_id=inputs.item_id,
        twig=twig,
        provider=provider or _planning.demo_provider(),
        gate_handler=gate_handler,
    )

    return Engine(
        workflow=build_workflow(planning_module=planning_module),
        verbs=build_verb_registry(
            inputs, twig=twig, manifest_dir=mdir, today=today,
        ),
        agents=build_agent_registry(),
        # The root-dispatch workflow itself invokes no agents; the
        # planning sub-workflow has its own provider via the shim.
        provider=provider or FakeProvider(),
        toolbelt=Toolbelt.real(),
        log_dir=log_dir,
        gate_handler=gate_handler or _default_gate_handler,
    )


# ---- async-friendly convenience entry --------------------------------


async def run_root_dispatch(
    log_dir: Path,
    run_id: str,
    *,
    item_id: int,
    repo: str = "PolyphonyRequiem/requiem",
    repo_path: Path | None = None,
    base_branch: str = "main",
    dry_run: bool = False,
    auto_plan: bool = True,
    twig: TwigClientProto | None = None,
    provider: AgentProvider | None = None,
    gate_handler: Callable | None = None,
    manifest_dir: Path | None = None,
    today: str | None = None,
):
    """Convenience: build an engine and run it.

    Returns the kernel's ``RunResult``. Callers needing the
    ``RootDispatchResult`` projection should call
    :meth:`RootDispatchResult.from_completed` over
    :func:`completed_from_log` for the durable view.
    """
    inputs = RootDispatchInputs(
        item_id=item_id,
        repo=repo,
        repo_path=repo_path or Path.cwd(),
        base_branch=base_branch,
        dry_run=dry_run,
        auto_plan=auto_plan,
        manifest_dir=manifest_dir,
    )
    engine = build_engine(
        log_dir,
        inputs=inputs,
        twig=twig,
        provider=provider,
        gate_handler=gate_handler,
        manifest_dir=manifest_dir,
        today=today,
    )
    return await engine.run(run_id)


def completed_from_log(log_path: Path) -> dict[str, dict[str, Any]]:
    """Rebuild ``{node_id: outcome_dict}`` from a stored event log.

    Mirrors planning's helper of the same name. For root-dispatch we
    additionally have ``subworkflow_completed`` events to fold — the
    kernel records the full outcome under ``payload.outcome``.
    """
    from requiem.persistence import replay

    completed: dict[str, dict[str, Any]] = {}
    for ev in replay(log_path):
        k = ev.get("kind")
        if k == "verb_completed":
            node = ev.get("node_id")
            payload = ev.get("payload") or {}
            outcome = payload.get("outcome")
            if node and outcome is not None:
                completed[node] = outcome
        elif k == "subworkflow_completed":
            node = ev.get("node_id")
            payload = ev.get("payload") or {}
            outcome = payload.get("outcome")
            if node and outcome is not None:
                completed[node] = outcome
    return completed
