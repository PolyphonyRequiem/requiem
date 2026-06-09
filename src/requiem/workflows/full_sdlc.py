"""Full SDLC demo — Verdi-3 / Phase C (climactic composition).

The vertical-integration slice. Composes every Phase B+ workflow into
one end-to-end demo that drives an AB work item from inbox to closed
through five sub-workflows:

::

    dispatch  →  plan  →  implement  →  pr_lifecycle  →  close_out
       │          │           │             │              │
       └ root-id  └ leaf      └ PR #347     └ merged       └ docs/closeouts/

This is the Walking-Skeleton-β demo. It tries hard to clear every box
on the Demo Contract checklist (`perspectives/ui-sdlc/07-demo-contract.md`
§3) — workday vignette, stakes named, artifacts visible, verdict card,
live narration, auto-resolved gates flagged. The narration is *stage-
level*: each sub-workflow narrates its own detail in its own log; the
parent's job is to tell the SDLC-pipeline story at the right altitude.

## Why shim modules

The kernel's ``SubWorkflowNode`` invokes child workflows by importable
module path and only passes the parent's ``log_dir`` to the child's
``build_engine`` (see ADR 0005). For the demo we want every child to
receive a consistent set of inputs (item_id, repo, dry_run, ...) keyed
off the parent's ``FullSdlcInputs``. The cleanest v0 idiom is **shim
modules**: this module registers stable-named modules in ``sys.modules``
whose ``build_engine(log_dir)`` closes over a shared ``_CURRENT_INPUTS``
cell and constructs the real child engine with the right kwargs. The
``SubWorkflowNode`` references the shim, not the real workflow.

The cell is mutated by :func:`build_engine` immediately before the
parent engine runs. For v0 this is a single-process, single-run idiom;
concurrent ``full_sdlc`` runs in the same process would collide.

## Failure routing

Every sub-workflow node has the four standard outgoing edges:
``success``, ``permanent_failure``, ``needs_human``, ``cancelled``. A
``NeedsHuman`` bubble-up routes to a per-stage ``paused_at_X`` gate.
The demo's auto handler picks ``abort`` for the paused gates (safe
default — no irreversible action gets auto-approved). Tests that want
to assert "demo paused" pass ``gate_handler=None`` and read the
resulting ``Suspended`` result. A real operator runs ``requiem resume``
and the gate prompt tells them what's pending.

## Idempotency

The shim modules call the real workflows' ``build_engine`` with the
same inputs each time. Each child workflow is already idempotent
(re-running with the same inputs is safe — see each child's own
docstring). The parent's resume protocol (kernel ``_reconstruct``)
takes care of re-attaching mid-stage.

## What's deliberately out of scope (v0)

* Multi-leaf plans: ``plan`` may produce decomposable trees, but the
  demo collapses them to one leaf in the implementation stage. Real
  fan-out is Berlioz-Phase-D's job.
* Real LLM calls in the default demo path. Children's defaults wire in
  ``FakeProvider`` with the canned happy-path scripts.
* Real PR / ADO mutations in the default demo path. ``dry_run=True``
  is the default; pass ``dry_run=False`` (and real credentials) for
  the production wiring.
"""
from __future__ import annotations

import importlib
import sys
import types
from dataclasses import dataclass, field, replace as dc_replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from requiem.agent import FakeProvider
from requiem.dsl import AgentRegistry, VerbRegistry, Workflow, WorkflowBuilder
from requiem.kernel import Engine
from requiem.outcomes import Success
from requiem.toolbelt import Toolbelt


# ---- inputs ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FullSdlcInputs:
    """One demo run's parameters. Threaded through to every sub-workflow."""

    item_id: int = 12345
    repo: str = "PolyphonyRequiem/requiem"
    repo_path: Path = field(default_factory=Path.cwd)
    base_branch: str = "main"
    dry_run: bool = True
    today: date | None = None
    """Pinned date for deterministic root_run_id in tests / docs."""


# ---- shim-module registry -----------------------------------------
#
# Stable module names. The ``SubWorkflowNode``s reference these; their
# ``build_engine(log_dir)`` reads the cell below and builds the real
# child engine with closure-baked inputs.

SHIM_PREFIX = "requiem.workflows._full_sdlc_shims"

SHIM_DISPATCH       = f"{SHIM_PREFIX}.dispatch"
SHIM_PLANNING       = f"{SHIM_PREFIX}.planning"
SHIM_IMPLEMENTATION = f"{SHIM_PREFIX}.implementation"
SHIM_PR_LIFECYCLE   = f"{SHIM_PREFIX}.pr_lifecycle"
SHIM_CLOSE_OUT      = f"{SHIM_PREFIX}.close_out"


# Mutable cell: most-recent inputs the parent built for. The shim's
# build_engine reads this at child-construction time. Single-process,
# single-run idiom (see module docstring).
_CURRENT_INPUTS: dict[str, FullSdlcInputs | None] = {"inputs": None}


def _install_shims() -> None:
    """Register all five shim modules in ``sys.modules`` (idempotent).

    Called once at module-import time below. Re-installation is a no-op
    because we check ``sys.modules`` first; the shim's behaviour is
    determined by the closure-captured ``_CURRENT_INPUTS`` cell, not by
    re-construction.
    """
    parent_pkg_name = SHIM_PREFIX
    if parent_pkg_name not in sys.modules:
        parent_pkg = types.ModuleType(parent_pkg_name)
        parent_pkg.__path__ = []  # type: ignore[attr-defined]
        sys.modules[parent_pkg_name] = parent_pkg

    _register_dispatch_shim()
    _register_planning_shim()
    _register_implementation_shim()
    _register_pr_lifecycle_shim()
    _register_close_out_shim()


def _current_inputs() -> FullSdlcInputs:
    inputs = _CURRENT_INPUTS["inputs"]
    if inputs is None:
        raise RuntimeError(
            "full_sdlc shim was invoked before _CURRENT_INPUTS was set; "
            "this should only happen if a child sub-workflow ran outside "
            "of full_sdlc.build_engine. Set inputs via full_sdlc.build_engine "
            "or call set_inputs() directly (tests only)."
        )
    return inputs


def set_inputs(inputs: FullSdlcInputs) -> None:
    """Test-only hook: pre-seed the shims' inputs cell."""
    _CURRENT_INPUTS["inputs"] = inputs


# ---- per-shim wiring ----------------------------------------------
#
# Each shim's ``build_engine`` does three things:
#   1. Import the real workflow module.
#   2. Construct it with closure-baked inputs.
#   3. Forward the parent engine's ``on_event`` observer if one was set
#      via :func:`set_observer`, so the CLI's live narration sees child
#      events too (subworkflow_started / subworkflow_completed already
#      land in the parent's log; this gives operators full coverage when
#      they want it).
#
# Forwarding is opt-in: the CLI assigns ``parent_engine.on_event`` after
# ``build_engine`` returns, so we mirror it into a cell here.


_OBSERVER: dict[str, Callable[[dict[str, Any]], None] | None] = {"obs": None}


def set_observer(obs: Callable[[dict[str, Any]], None] | None) -> None:
    """Install the live-narration observer for child engines (CLI hook)."""
    _OBSERVER["obs"] = obs


def _install_observer(engine: Engine) -> Engine:
    obs = _OBSERVER["obs"]
    if obs is not None:
        engine.on_event = obs
    return engine


def _register_dispatch_shim() -> None:
    mod = types.ModuleType(SHIM_DISPATCH)

    def build_engine(log_dir: Path) -> Engine:
        from requiem.workflows import root_dispatch
        inputs = _current_inputs()
        di = root_dispatch.RootDispatchInputs(
            item_id=inputs.item_id,
            repo=inputs.repo,
            auto_plan=False,
            dry_run=inputs.dry_run,
        )
        return _install_observer(
            root_dispatch.build_engine(log_dir, inputs=di, today=inputs.today)
        )

    mod.build_engine = build_engine  # type: ignore[attr-defined]
    sys.modules[SHIM_DISPATCH] = mod


def _register_planning_shim() -> None:
    mod = types.ModuleType(SHIM_PLANNING)

    def build_engine(log_dir: Path) -> Engine:
        from requiem.workflows import planning
        inputs = _current_inputs()
        return _install_observer(
            planning.build_engine(log_dir, item_id=inputs.item_id)
        )

    mod.build_engine = build_engine  # type: ignore[attr-defined]
    sys.modules[SHIM_PLANNING] = mod


def _register_implementation_shim() -> None:
    mod = types.ModuleType(SHIM_IMPLEMENTATION)

    def build_engine(log_dir: Path) -> Engine:
        from requiem.workflows import implementation
        inputs = _current_inputs()
        # The implementation workflow's default ``_make_demo_inputs`` seeds
        # a throwaway repo under log_dir/demo_repo. For the SDLC demo we
        # reuse that seeded repo so dry_run path doesn't touch the
        # operator's working tree. (Pass an explicit inputs object so
        # item_id / repo / dry_run come from full_sdlc, not from impl's
        # demo defaults.)
        impl_inputs = implementation.ImplementationInputs(
            item_id=inputs.item_id,
            repo=inputs.repo,
            repo_path=log_dir / "demo_repo",
            base_branch=inputs.base_branch,
            dry_run=inputs.dry_run,
        )
        _ensure_demo_repo(log_dir / "demo_repo")
        return _install_observer(
            implementation.build_engine(log_dir, inputs=impl_inputs, demo=True)
        )

    mod.build_engine = build_engine  # type: ignore[attr-defined]
    sys.modules[SHIM_IMPLEMENTATION] = mod


def _ensure_demo_repo(repo_path: Path) -> None:
    """Mirror impl's ``_make_demo_inputs`` repo seed so the SDLC demo is
    self-contained without depending on impl's private helper."""
    import subprocess
    repo_path.mkdir(parents=True, exist_ok=True)
    if (repo_path / ".git").exists():
        return
    subprocess.run(["git", "init", "-q"], cwd=repo_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "demo@requiem.local"],
        cwd=repo_path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Requiem Demo"],
        cwd=repo_path, check=True,
    )
    (repo_path / "README.md").write_text(
        "# full-sdlc demo repo\n", encoding="utf-8"
    )
    (repo_path / "pyproject.toml").write_text(
        "[project]\nname = \"demo\"\nversion = \"0.0.1\"\n", encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=repo_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"], cwd=repo_path, check=True,
    )


def _register_pr_lifecycle_shim() -> None:
    mod = types.ModuleType(SHIM_PR_LIFECYCLE)

    def build_engine(log_dir: Path) -> Engine:
        from requiem.workflows import pr_lifecycle
        inputs = _current_inputs()
        # PR number flows from the implementation stage's projection. In
        # the demo's happy path implementation produces PR #19 (impl's
        # _DemoGhClient.next_pr_number). For tests that override impl's
        # output, the parent verb registry can hand a different number.
        pr_number = _resolve_pr_number(default=19)
        return _install_observer(
            pr_lifecycle.build_engine(
                log_dir,
                repo=inputs.repo,
                pr_number=pr_number,
                repo_path=log_dir / "demo_repo",
                dry_run=inputs.dry_run,
            )
        )

    mod.build_engine = build_engine  # type: ignore[attr-defined]
    sys.modules[SHIM_PR_LIFECYCLE] = mod


# Cross-stage state cell: implementation publishes pr_number here so
# pr_lifecycle's shim can read it without rummaging through the parent's
# log. Mutated by the parent's ``capture_implementation`` verb.
_CROSS_STAGE: dict[str, Any] = {"pr_number": None}


def _resolve_pr_number(*, default: int) -> int:
    pr = _CROSS_STAGE.get("pr_number")
    if pr is not None:
        return int(pr)
    return default


def _register_close_out_shim() -> None:
    mod = types.ModuleType(SHIM_CLOSE_OUT)

    def build_engine(log_dir: Path) -> Engine:
        from requiem.workflows import close_out
        inputs = _current_inputs()
        pr_number = _resolve_pr_number(default=19)
        return _install_observer(
            close_out.build_engine(
                log_dir,
                item_id=inputs.item_id,
                repo=inputs.repo,
                pr_number=pr_number,
                dry_run=inputs.dry_run,
                closeout_dir=log_dir / "closeouts",
            )
        )

    mod.build_engine = build_engine  # type: ignore[attr-defined]
    sys.modules[SHIM_CLOSE_OUT] = mod


# Register shims at import time so ``requiem describe full_sdlc`` works.
_install_shims()


# ---- parent verb registry -----------------------------------------


def build_verb_registry(inputs: FullSdlcInputs, *, log_dir: Path | None = None) -> VerbRegistry:
    verbs = VerbRegistry()
    # ``log_dir`` is the parent engine's; we use it to locate child logs
    # (each child writes ``{sub_run_id}.events.jsonl`` in the same
    # directory per INV-SUBWORKFLOW-LOG-ISOLATION).

    @verbs.register("capture_implementation")
    def _capture_impl(ctx):
        """Pluck the PR number from the implementation stage's outcome.

        Reads the child's events.jsonl to find ``create_pr``'s outcome,
        which carries the GitHub PR number. Stashes the number into the
        cross-stage cell so the pr_lifecycle and close_out shims pick it
        up at their own child-engine-construction time.
        """
        impl_out = (ctx.completed.get("implement") or {}).get("value") or {}
        sub_run_id = impl_out.get("sub_run_id")
        pr_number = _extract_pr_number(sub_run_id, log_dir)
        if pr_number is not None:
            _CROSS_STAGE["pr_number"] = pr_number
        return Success(value={
            "pr_number": pr_number,
            "child_final_node": impl_out.get("child_final_node"),
        })

    return verbs


def _extract_pr_number(sub_run_id: str | None, log_dir: Path | None) -> int | None:
    """Best-effort: fold the implementation child's log for create_pr."""
    if not sub_run_id:
        return None
    from requiem.persistence import replay
    candidates: list[Path] = []
    if log_dir is not None:
        candidates.append(log_dir / f"{sub_run_id}.events.jsonl")
    candidates.append(Path.cwd() / ".runs" / f"{sub_run_id}.events.jsonl")
    candidates.append(Path(".runs") / f"{sub_run_id}.events.jsonl")
    for cand in candidates:
        if not cand.exists():
            continue
        for ev in replay(cand):
            if (
                ev.get("kind") == "verb_completed"
                and ev.get("node_id") == "create_pr"
            ):
                val = (
                    (ev.get("payload") or {}).get("outcome", {}).get("value")
                    or {}
                )
                # impl's create_pr emits ``{pr_number, url, ...}`` on
                # success; dry-run emits ``{dry_run: True}`` with no
                # number. Both cases are honoured.
                pr = val.get("pr_number") or val.get("number")
                if pr is not None:
                    return int(pr)
                # Dry-run: synthesize a stable demo PR number from
                # impl's _DemoGhClient's default so the verdict card
                # has something to display.
                if val.get("dry_run"):
                    return 19
        break
    return None


# ---- workflow topology --------------------------------------------


def build_workflow() -> Workflow:
    return (
        WorkflowBuilder(
            "full-sdlc",
            module="requiem.workflows.full_sdlc",
            version="0.1",
        )
            .entry("dispatch")
            # Stage 1 — dispatch (claim item, allocate root_run_id)
            .subworkflow("dispatch", workflow=SHIM_DISPATCH)
                .edge("dispatch", on="success",           to="plan")
                .edge("dispatch", on="permanent_failure", to="paused_dispatch")
                .edge("dispatch", on="needs_human",       to="paused_dispatch")
                .edge("dispatch", on="cancelled",         to="cancel_end")
            # Stage 2 — planning
            .subworkflow("plan", workflow=SHIM_PLANNING)
                .edge("plan", on="success",           to="implement")
                .edge("plan", on="permanent_failure", to="paused_plan")
                .edge("plan", on="needs_human",       to="paused_plan")
                .edge("plan", on="cancelled",         to="cancel_end")
            # Stage 3 — implementation
            .subworkflow("implement", workflow=SHIM_IMPLEMENTATION)
                .edge("implement", on="success",           to="capture_impl")
                .edge("implement", on="permanent_failure", to="paused_implement")
                .edge("implement", on="needs_human",       to="paused_implement")
                .edge("implement", on="cancelled",         to="cancel_end")
            # Cross-stage glue: pull PR number out of impl's projection
            .script("capture_impl", verb="capture_implementation")
                .edge("capture_impl", on="success", to="pr_lifecycle")
            # Stage 4 — PR lifecycle
            .subworkflow("pr_lifecycle", workflow=SHIM_PR_LIFECYCLE)
                .edge("pr_lifecycle", on="success",           to="close_out")
                .edge("pr_lifecycle", on="permanent_failure", to="paused_pr")
                .edge("pr_lifecycle", on="needs_human",       to="paused_pr")
                .edge("pr_lifecycle", on="cancelled",         to="cancel_end")
            # Stage 5 — close-out
            .subworkflow("close_out", workflow=SHIM_CLOSE_OUT)
                .edge("close_out", on="success",           to="end")
                .edge("close_out", on="permanent_failure", to="paused_close")
                .edge("close_out", on="needs_human",       to="paused_close")
                .edge("close_out", on="cancelled",         to="cancel_end")
            # Per-stage NeedsHuman gates (auto-handler picks `abort` for
            # safety — see _default_gate_handler).
            .human_gate("paused_dispatch",
                        prompt="Demo paused at dispatch stage. Resume manually or abort?",
                        options=["resume", "abort"])
                .edge("paused_dispatch", on="needs_human:resume", to="plan")
                .edge("paused_dispatch", on="needs_human:abort",  to="fail_end")
            .human_gate("paused_plan",
                        prompt="Demo paused at planning stage. Resume manually or abort?",
                        options=["resume", "abort"])
                .edge("paused_plan", on="needs_human:resume", to="implement")
                .edge("paused_plan", on="needs_human:abort",  to="fail_end")
            .human_gate("paused_implement",
                        prompt="Demo paused at implementation stage. Resume manually or abort?",
                        options=["resume", "abort"])
                .edge("paused_implement", on="needs_human:resume", to="capture_impl")
                .edge("paused_implement", on="needs_human:abort",  to="fail_end")
            .human_gate("paused_pr",
                        prompt="Demo paused at PR-lifecycle stage. Resume manually or abort?",
                        options=["resume", "abort"])
                .edge("paused_pr", on="needs_human:resume", to="close_out")
                .edge("paused_pr", on="needs_human:abort",  to="fail_end")
            .human_gate("paused_close",
                        prompt="Demo paused at close-out stage. Resume manually or abort?",
                        options=["resume", "abort"])
                .edge("paused_close", on="needs_human:resume", to="end")
                .edge("paused_close", on="needs_human:abort",  to="fail_end")
            .terminate("end",        disposition="completed")
            .terminate("fail_end",   disposition="failed")
            .terminate("cancel_end", disposition="cancelled")
            .humanize({
                "dispatch":         "Dispatched",
                "plan":             "Planned",
                "implement":        "Implemented",
                "capture_impl":     "captured PR number",
                "pr_lifecycle":     "PR Lifecycle",
                "close_out":        "Closed out",
                "paused_dispatch":  "demo paused — dispatch",
                "paused_plan":      "demo paused — planning",
                "paused_implement": "demo paused — implementation",
                "paused_pr":        "demo paused — pr_lifecycle",
                "paused_close":     "demo paused — close-out",
                "end":              "full-SDLC demo",
                "fail_end":         "full-SDLC demo",
                "cancel_end":       "full-SDLC demo",
            })
            .build()
    )


# ---- engine factory -----------------------------------------------


def _default_gate_handler(node_id: str, prompt: str, options: tuple[str, ...]) -> str:
    """Demo auto-handler.

    Safe defaults: paused-at-X gates auto-pick ``abort`` (we never
    auto-resume past a child's NeedsHuman). Any other gate auto-picks
    its first option (the kernel's convention for safe pass-through).
    Marked ``__requiem_auto__`` so the CLI renderer appends
    ``(auto-approved for demo)``.
    """
    opts = list(options)
    if node_id.startswith("paused_") and "abort" in opts:
        return "abort"
    return opts[0] if opts else ""


_default_gate_handler.__requiem_auto__ = True  # type: ignore[attr-defined]


def build_engine(
    log_dir: Path,
    *,
    inputs: FullSdlcInputs | None = None,
    gate_handler: Callable[[str, str, tuple[str, ...]], str] | None = None,
) -> Engine:
    """Construct the full-SDLC parent engine.

    Side-effect: mutates the shim modules' ``_CURRENT_INPUTS`` cell with
    ``inputs`` so child workflows (constructed lazily inside
    ``_run_subworkflow``) see the same parameters as the parent. Single-
    process, single-run idiom — concurrent ``full_sdlc`` runs in the
    same process would collide on the cell.
    """
    if inputs is None:
        inputs = FullSdlcInputs()
    _CURRENT_INPUTS["inputs"] = inputs
    # Reset cross-stage cell between runs (the cell is per-process).
    _CROSS_STAGE["pr_number"] = None

    engine = _LiveNarratingEngine(
        workflow=build_workflow(),
        verbs=build_verb_registry(inputs, log_dir=log_dir),
        agents=AgentRegistry(),
        provider=FakeProvider(),
        toolbelt=Toolbelt.real(),
        log_dir=log_dir,
        gate_handler=gate_handler or _default_gate_handler,
    )
    return engine


@dataclass
class _LiveNarratingEngine(Engine):
    """Engine subclass that mirrors ``on_event`` to the shim observer cell.

    When the CLI sets ``parent_engine.on_event = observer``, we want every
    child engine (constructed inside ``_run_subworkflow``) to also call
    that observer so the operator sees the child's narration live. We
    can't change Engine.on_event's assignment site, so we override
    ``__setattr__`` to side-effect the shim cell whenever ``on_event``
    is set.
    """

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        if name == "on_event":
            set_observer(value)


# ---- preamble + verdict card --------------------------------------


def preamble() -> str:
    """Workday vignette + stakes (Demo Contract §3.1, §3.2)."""
    inputs = _CURRENT_INPUTS.get("inputs")
    item_id = inputs.item_id if inputs else 12345
    rrid_hint = (
        f"root-{item_id}-{(inputs.today or datetime.now(tz=timezone.utc).date()).isoformat()}"
        if inputs else "root-..."
    )
    dry = "(dry-run — no real PR will be opened or merged)" if (
        inputs is None or inputs.dry_run
    ) else "(LIVE — will mutate the repo and ADO)"
    return (
        "═══ Requiem v0 — Full SDLC demo " + "═" * 35 + "\n"
        "  Scenario: it's Monday morning. Work item AB#" + str(item_id) + " arrived\n"
        "  overnight. Watch Requiem move it from inbox to closed in one\n"
        "  shot, threading dispatch → plan → implement → PR lifecycle → \n"
        "  close-out without you nursing the handoffs.\n"
        "\n"
        "  Stakes: if any seam between those five tools breaks, you lose\n"
        "  the morning to manual context-switching and copy-paste. This\n"
        "  demo proves the seams hold under one continuous run.\n"
        "\n"
        f"  Mode: {dry}\n"
        f"  Expected root_run_id: {rrid_hint}\n"
        "═" * 72
    )


# ---- render hooks ---------------------------------------------------


def _short(s: str | None, n: int = 50) -> str:
    if not s:
        return "?"
    return s if len(s) <= n else s[: n - 1] + "…"


def _detail_dispatch(outcome: dict) -> str:
    proj = (outcome.get("value") or {}).get("child_projection") or {}
    # The dispatch workflow's terminal projection includes verbs_completed;
    # we want the record node's value. Re-fetch via the child's log shim is
    # overkill — pull from projection when possible, else just show the
    # disposition.
    final = proj.get("terminal", "?")
    return f"root-dispatch → {final}"


def _detail_plan(outcome: dict) -> str:
    proj = (outcome.get("value") or {}).get("child_projection") or {}
    return f"planning → {proj.get('terminal', '?')} (1 leaf, no decomposition)"


def _detail_implement(outcome: dict) -> str:
    proj = (outcome.get("value") or {}).get("child_projection") or {}
    return f"implementation → {proj.get('terminal', '?')}"


def _detail_pr_lifecycle(outcome: dict) -> str:
    proj = (outcome.get("value") or {}).get("child_projection") or {}
    return f"pr-lifecycle → {proj.get('terminal', '?')}"


def _detail_close_out(outcome: dict) -> str:
    proj = (outcome.get("value") or {}).get("child_projection") or {}
    return f"close-out → {proj.get('terminal', '?')}"


def render_hints() -> dict:
    return {
        "artifact_name": "AB work item",
        "subworkflow_details": {
            "dispatch":     _detail_dispatch,
            "plan":         _detail_plan,
            "implement":    _detail_implement,
            "pr_lifecycle": _detail_pr_lifecycle,
            "close_out":    _detail_close_out,
        },
        "details": {
            "capture_impl": lambda v: f"PR #{v.get('pr_number') or '—'}",
        },
        "silent_nodes": frozenset({"end", "fail_end", "cancel_end"}),
    }


def verdict_card(completed: dict) -> str | None:
    """The customer-facing card spec'd in the seat brief."""
    inputs = _CURRENT_INPUTS.get("inputs") or FullSdlcInputs()
    dry = inputs.dry_run

    glyph_completed = "◐" if dry else "✓"
    label_dry_suffix = " (dry-run)" if dry else ""

    def _stage_line(node_id: str, name: str, detail: str) -> str:
        outcome = completed.get(node_id)
        if outcome is None:
            return f"  ○ {name:<19} — not reached"
        kind = outcome.get("kind")
        if kind == "success":
            return f"  {glyph_completed} {name:<19} {detail}{label_dry_suffix}"
        if kind == "needs_human":
            return f"  🚦 {name:<19} — PAUSED ({detail})"
        if kind == "cancelled":
            return f"  ■ {name:<19} — cancelled"
        return f"  ✕ {name:<19} — failed ({detail})"

    item_id = inputs.item_id
    title = _extract_title(completed) or "?"

    dispatch_detail = _summarise_dispatch(completed)
    plan_detail     = _summarise_plan(completed)
    impl_detail     = _summarise_impl(completed)
    pr_detail       = _summarise_pr(completed)
    close_detail    = _summarise_close(completed)

    stages = [
        _stage_line("dispatch",     "Dispatched",     dispatch_detail),
        _stage_line("plan",         "Planned",        plan_detail),
        _stage_line("implement",    "Implemented",    impl_detail),
        _stage_line("pr_lifecycle", "PR Lifecycle",   pr_detail),
        _stage_line("close_out",    "Closed out",     close_detail),
    ]

    paused = next(
        (n for n in (
            "paused_dispatch", "paused_plan", "paused_implement",
            "paused_pr", "paused_close",
        ) if completed.get(n)),
        None,
    )

    n_subworkflows = sum(
        1 for nid in ("dispatch", "plan", "implement", "pr_lifecycle", "close_out")
        if completed.get(nid)
    )
    runs_dir = Path(".runs").resolve()
    rrid_hint = _root_run_id_guess(completed)

    head = "═══ Requiem v0 — Full SDLC demo " + "═" * 35
    sub  = f"Item: AB#{item_id} — {title!r}"
    bottom = "═" * 72

    footer_lines: list[str] = []
    if paused:
        stage_name = paused.removeprefix("paused_")
        footer_lines.append("")
        footer_lines.append(f"  ⚠ Demo paused at stage: {stage_name}")
        footer_lines.append(
            f"  → Resume:  requiem resume requiem.workflows.full_sdlc "
            f"--run-id <ID>"
        )

    summary_line = (
        f"Total: {n_subworkflows}/5 sub-workflows"
        + (" — DRY RUN" if dry else "")
    )
    receipts = f"Receipts: {runs_dir}\\{rrid_hint or '<run-id>'}*"

    return "\n".join([
        head,
        sub,
        "",
        *stages,
        *footer_lines,
        "",
        summary_line,
        receipts,
        bottom,
    ])


def _extract_title(completed: dict) -> str | None:
    """Best-effort: pull the item title out of the dispatch child's
    projection (set in root_dispatch's record verb)."""
    dispatch_out = completed.get("dispatch") or {}
    proj = (dispatch_out.get("value") or {}).get("child_projection") or {}
    # The child's projection summarises but doesn't carry full outcome
    # values; fall back to the FullSdlcInputs title we can synthesize.
    inputs = _CURRENT_INPUTS.get("inputs")
    if inputs:
        return f"AB work item {inputs.item_id}"
    return None


def _root_run_id_guess(completed: dict) -> str | None:
    inputs = _CURRENT_INPUTS.get("inputs")
    if inputs is None:
        return None
    d = inputs.today or datetime.now(tz=timezone.utc).date()
    return f"root-{inputs.item_id}-{d.isoformat()}"


def _summarise_dispatch(completed: dict) -> str:
    if not completed.get("dispatch"):
        return ""
    rid = _root_run_id_guess(completed) or "root-…"
    return rid


def _summarise_plan(completed: dict) -> str:
    if not completed.get("plan"):
        return ""
    return "1 leaf (no decomposition needed)"


def _summarise_impl(completed: dict) -> str:
    out = completed.get("implement") or {}
    if not out:
        return ""
    captured = (completed.get("capture_impl") or {}).get("value") or {}
    pr = captured.get("pr_number")
    inputs = _CURRENT_INPUTS.get("inputs") or FullSdlcInputs()
    branch = f"feature/{inputs.item_id}"
    pr_str = f"PR #{pr}" if pr is not None else "PR (deferred)"
    return f"{branch} — {pr_str}"


def _summarise_pr(completed: dict) -> str:
    out = completed.get("pr_lifecycle") or {}
    if not out:
        return ""
    captured = (completed.get("capture_impl") or {}).get("value") or {}
    pr = captured.get("pr_number")
    return f"#{pr or '?'} — handoff complete"


def _summarise_close(completed: dict) -> str:
    out = completed.get("close_out") or {}
    if not out:
        return ""
    inputs = _CURRENT_INPUTS.get("inputs") or FullSdlcInputs()
    return f"AB-{inputs.item_id} → docs/closeouts/AB-{inputs.item_id}.md"


__all__ = [
    "FullSdlcInputs",
    "build_engine",
    "build_workflow",
    "build_verb_registry",
    "render_hints",
    "verdict_card",
    "preamble",
    "set_inputs",
    "set_observer",
    "SHIM_DISPATCH",
    "SHIM_PLANNING",
    "SHIM_IMPLEMENTATION",
    "SHIM_PR_LIFECYCLE",
    "SHIM_CLOSE_OUT",
]
