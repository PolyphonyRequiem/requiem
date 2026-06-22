"""Requiem CLI — argparse plumbing and four subcommands.

* ``requiem run <workflow_module>``
    Streams customer-English narration as the engine runs, then prints a
    verdict card (if the workflow module exposes ``verdict_card``).

* ``requiem resume <workflow_module> <run_id>``
    Replays prior narration from the log, then streams continuation
    events as the engine resumes.

* ``requiem describe <workflow_module>``
    Prints the workflow's topology (nodes, edges, retry budgets, agents).

* ``requiem events <run_id> [--workflow MOD] [--raw]``
    Renders a stored event log. Default: customer English. ``--raw``
    emits JSONL verbatim for CI consumers.

Workflow modules opt into rich narration with two optional hooks:

* ``render_hints() -> dict[str, Any]`` returning some of
  ``{"artifact_name", "details", "gate_contexts"}``.
* ``verdict_card(completed) -> str | None`` producing the post-run summary.

Both are optional. The CLI degrades to node-id labels when missing.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

from requiem.cli.render import (
    EXIT_CODE_CANCELLED,
    EXIT_CODE_FAILED,
    EXIT_CODE_OK,
    RenderContext,
    exit_code_for,
    render_event,
    style_for_line,
)
from requiem.events import EventEmitter
from requiem.kernel import Completed, Engine, Failed, Suspended
from requiem.persistence import EventStore, replay


DEFAULT_LOG_DIR = Path(".runs")


# ---- colour layer (degrades gracefully) ----------------------------

try:
    from rich.console import Console as _RichConsole

    _CONSOLE: Any = _RichConsole(highlight=False)
    _HAS_RICH = True
except ImportError:
    _CONSOLE = None
    _HAS_RICH = False


def _say(text: str = "", *, style: str | None = None) -> None:
    if _HAS_RICH and style:
        _CONSOLE.print(text, style=style)
    else:
        print(text)


def _emit_lines(lines: list[str]) -> None:
    for line in lines:
        _say(line, style=style_for_line(line))


# ---- module loading -------------------------------------------------


def _import_module(dotted: str) -> ModuleType:
    try:
        return importlib.import_module(dotted)
    except ImportError as e:
        raise SystemExit(f"requiem: cannot import workflow module {dotted!r}: {e}")


def _load_engine(module_path: str, log_dir: Path) -> Engine:
    mod = _import_module(module_path)
    factory = getattr(mod, "build_engine", None)
    if factory is None:
        raise SystemExit(
            f"requiem: module {module_path!r} has no build_engine(log_dir) function"
        )
    engine = factory(log_dir)
    if not isinstance(engine, Engine):
        raise SystemExit(
            f"requiem: {module_path}.build_engine did not return an Engine"
        )
    return engine


def _load_workflow(module_path: str):
    mod = _import_module(module_path)
    if hasattr(mod, "build_workflow"):
        return mod.build_workflow()
    if hasattr(mod, "build_engine"):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            return mod.build_engine(Path(td)).workflow
    raise SystemExit(
        f"requiem: module {module_path!r} has neither build_workflow nor build_engine"
    )


# ---- render context construction -----------------------------------


def _render_context_for(mod: ModuleType | None, workflow_name: str,
                        humanize: dict[str, str]) -> RenderContext:
    cx = RenderContext(workflow_name=workflow_name, humanize=dict(humanize))
    if mod is None:
        return cx
    hints_fn = getattr(mod, "render_hints", None)
    if hints_fn is None:
        return cx
    hints = hints_fn() or {}
    if "artifact_name" in hints:
        cx.artifact_name = str(hints["artifact_name"])
    if "details" in hints:
        cx.details.update(hints["details"])
    if "gate_contexts" in hints:
        cx.gate_contexts.update(hints["gate_contexts"])
    if "subworkflow_details" in hints:
        cx.subworkflow_details.update(hints["subworkflow_details"])
    if "silent_nodes" in hints:
        cx.silent_nodes = frozenset(hints["silent_nodes"])
    return cx


# ---- subcommands ----------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    log_dir = Path(args.log_dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    run_id = args.run_id or f"run-{int(time.time())}"
    mod = _import_module(args.workflow_module)
    engine = _load_engine(args.workflow_module, log_dir)
    cx = _render_context_for(mod, engine.workflow.name, engine.workflow.humanize)
    if getattr(args, "interactive", False):
        engine.gate_handler = _make_interactive_gate_handler(cx)

    _say(f"requiem run — {args.workflow_module}  (run_id={run_id})", style="bold")
    _say(f"log: {engine.log_path(run_id)}", style="dim")
    _print_preamble(mod)
    _say("─" * 72, style="dim")

    def _observer(envelope: dict[str, Any]) -> None:
        _emit_lines(render_event(envelope, cx))

    engine.on_event = _observer
    t0 = time.perf_counter()
    result = asyncio.run(engine.run(run_id))
    elapsed_ms = (time.perf_counter() - t0) * 1000

    _say("─" * 72, style="dim")
    _print_verdict_card(mod, cx)
    _print_run_footer(result, engine.log_path(run_id), elapsed_ms)
    return exit_code_for(result)


def cmd_resume(args: argparse.Namespace) -> int:
    log_dir = Path(args.log_dir).resolve()
    mod = _import_module(args.workflow_module)
    engine = _load_engine(args.workflow_module, log_dir)
    log_path = engine.log_path(args.run_id)
    if not log_path.exists():
        raise SystemExit(f"requiem: no event log at {log_path}")
    cx = _render_context_for(mod, engine.workflow.name, engine.workflow.humanize)
    if getattr(args, "interactive", False):
        engine.gate_handler = _make_interactive_gate_handler(cx)

    _say(f"requiem resume — {args.workflow_module}  (run_id={args.run_id})", style="bold")
    _say(f"log: {log_path}", style="dim")
    _print_preamble(mod)
    _say("─" * 72, style="dim")

    prior = list(replay(log_path))
    for ev in prior:
        _emit_lines(render_event(ev, cx))
    _say(f"… resuming from {len(prior)} prior events", style="dim")

    def _observer(envelope: dict[str, Any]) -> None:
        _emit_lines(render_event(envelope, cx))

    engine.on_event = _observer
    t0 = time.perf_counter()
    result = asyncio.run(engine.run(args.run_id))
    elapsed_ms = (time.perf_counter() - t0) * 1000

    _say("─" * 72, style="dim")
    _print_verdict_card(mod, cx)
    _print_run_footer(result, log_path, elapsed_ms)
    return exit_code_for(result)


def cmd_describe(args: argparse.Namespace) -> int:
    wf = _load_workflow(args.workflow_module)
    _say(f"workflow: {wf.name}", style="bold")
    _say(f"  module: {args.workflow_module}")
    _say(f"  entry : {wf.entry}")
    _say(f"  nodes ({len(wf.nodes)}):", style="bold")
    for n in wf.nodes:
        extras = []
        for attr in ("verb", "agent", "prompt_verb", "disposition"):
            v = getattr(n, attr, None)
            if v:
                extras.append(f"{attr}={v}")
        rmax = getattr(n, "retry_max", 0)
        if rmax:
            extras.append(f"retry_max={rmax}")
        branches = getattr(n, "branches", None)
        if branches:
            extras.append(f"branches=[{','.join(b.agent for b in branches)}]")
        opts = getattr(n, "options", None)
        if opts:
            extras.append(f"options={list(opts)}")
        suffix = "  " + " ".join(extras) if extras else ""
        _say(f"    - [{n.kind:10s}] {n.node_id}{suffix}")
    _say(f"  edges ({len(wf.edges)}):", style="bold")
    for e in wf.edges:
        _say(f"    {e.from_node}  --[{e.on}]-->  {e.to_node}")
    if wf.humanize:
        _say(f"  humanize ({len(wf.humanize)} entries):", style="bold")
        for nid, label in wf.humanize.items():
            _say(f"    {nid}: {label}")
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    log_dir = Path(args.log_dir).resolve()
    log_path = log_dir / f"{args.run_id}.events.jsonl"
    if not log_path.exists():
        raise SystemExit(f"requiem: no event log at {log_path}")

    if args.raw:
        if args.follow:
            _tail_raw(log_path)
            return 0
        for ev in replay(log_path):
            print(json.dumps(ev, separators=(",", ":")))
        return 0

    # Resolve the workflow module: explicit `--workflow` wins; otherwise
    # auto-load from the `run_started` event's `workflow_module` field
    # (Gap 1 — workflow identity in run_started).
    workflow_dotted = args.workflow or _workflow_module_from_log(log_path)
    mod: ModuleType | None = None
    humanize: dict[str, str] = {}
    workflow_name = ""
    if workflow_dotted:
        try:
            mod = _import_module(workflow_dotted)
            wf = _load_workflow(workflow_dotted)
            humanize = dict(wf.humanize)
            workflow_name = wf.name
        except SystemExit as e:
            _say(f"(could not auto-load workflow {workflow_dotted!r}: {e}); "
                 f"falling back to raw node ids", style="yellow")
    cx = _render_context_for(mod, workflow_name, humanize)

    if args.follow:
        _tail_rendered(log_path, cx)
        return 0

    events = list(replay(log_path))
    for ev in events:
        _emit_lines(render_event(ev, cx))
    # ADR-0030 §3b: render the cost block after the timeline. Looks for
    # the `run_cost_summary` event the kernel emits on terminal disposition
    # and renders the totals + per-role + per-model breakdown.
    _render_cost_block(events)
    return 0


def _render_cost_block(events: list[dict[str, Any]]) -> None:
    """Render the cost block from a run_cost_summary event if present.

    ADR-0030 §3b shape::

        ─── Cost ──────────────────────────────────────────────
          42 agent calls · 19,134 tokens · 87.1s aggregate latency
           planner:    8 calls · claude-opus-4.7    · 6,600 tok · 32.0s
           reviewer:   9 calls · claude-sonnet-4    · 7,000 tok · 28.0s
           ...
        ───────────────────────────────────────────────────────
    """
    summary_event = next(
        (e for e in events if e.get("kind") == "run_cost_summary"), None,
    )
    if summary_event is None:
        return
    p = summary_event.get("payload", {}) or {}
    totals = p.get("totals", {}) or {}
    per_role = p.get("per_role", {}) or {}
    per_model = p.get("per_model", {}) or {}

    total_tokens = (totals.get("input_tokens", 0) or 0) + (
        totals.get("output_tokens", 0) or 0
    )
    latency_s = (totals.get("total_latency_ms", 0) or 0) / 1000.0
    line = "─" * 55
    _say(f"─── Cost {line[8:]}", style="dim")
    _say(
        f"  {totals.get('agent_call_count', 0)} agent calls · "
        f"{total_tokens:,} tokens · {latency_s:.1f}s aggregate latency"
    )
    # For each role, find a representative model from per_model whose calls
    # include this role's count. Simpler: just list per_role with role name.
    for role in sorted(per_role.keys()):
        r = per_role[role]
        r_tokens = (r.get("input_tokens", 0) or 0) + (
            r.get("output_tokens", 0) or 0
        )
        r_latency_s = (r.get("latency_ms", 0) or 0) / 1000.0
        # Pick the model with the most calls attributed to this role for
        # display. If no per-call role attribution flowed back, fall back
        # to the highest-call model overall.
        model_label = ""
        if r.get("model"):
            model_label = r["model"]
        elif per_model:
            model_label = max(
                per_model.items(),
                key=lambda kv: kv[1].get("calls", 0),
            )[0]
        _say(
            f"   {role}: {r.get('calls', 0)} calls · "
            f"{model_label} · {r_tokens:,} tok · {r_latency_s:.1f}s"
        )
    _say(line, style="dim")
    return None


def cmd_list_runs(args: argparse.Namespace) -> int:
    log_dir = Path(args.log_dir).resolve()
    if not log_dir.exists():
        _say(f"(no runs — log dir {log_dir} does not exist)", style="dim")
        return 0

    rows: list[dict[str, Any]] = []
    for path in sorted(log_dir.glob("*.events.jsonl")):
        run_id = path.name[: -len(".events.jsonl")]
        info = _summarize_run(path)
        rows.append({"run_id": run_id, **info})

    if not rows:
        _say(f"(no runs in {log_dir})", style="dim")
        return 0

    rows.sort(key=lambda r: r.get("started") or "")
    if _HAS_RICH:
        from rich.table import Table
        table = Table(show_header=True, header_style="bold", expand=False)
        table.add_column("RUN_ID", overflow="fold")
        table.add_column("WORKFLOW")
        table.add_column("STARTED (UTC)")
        table.add_column("STATUS")
        table.add_column("DURATION", justify="right")
        table.add_column("EVENTS", justify="right")
        for r in rows:
            table.add_row(
                r["run_id"],
                r.get("workflow") or "—",
                _short_ts(r.get("started")),
                _style_status(r.get("status") or "?"),
                r.get("duration") or "—",
                str(r.get("events") or 0),
            )
        _CONSOLE.print(table)
    else:
        _say(
            f"{'RUN_ID':24s} {'WORKFLOW':18s} {'STARTED':22s} {'STATUS':12s} "
            f"{'DURATION':>10s} {'EVENTS':>7s}"
        )
        for r in rows:
            _say(
                f"{r['run_id']:24s} {(r.get('workflow') or '—'):18s} "
                f"{_short_ts(r.get('started')):22s} "
                f"{(r.get('status') or '?'):12s} "
                f"{(r.get('duration') or '—'):>10s} "
                f"{str(r.get('events') or 0):>7s}"
            )
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    log_dir = Path(args.log_dir).resolve()
    log_path = log_dir / f"{args.run_id}.events.jsonl"
    if not log_path.exists():
        raise SystemExit(f"requiem: no event log at {log_path}")

    # Has the run already terminated? Then cancel is a no-op (and we say so).
    info = _summarize_run(log_path)
    status = info.get("status")
    if status in ("Completed", "Failed", "Cancelled"):
        _say(
            f"run {args.run_id!r} already {status.lower()} — cancel is a no-op",
            style="yellow",
        )
        return 0

    store = EventStore(log_path)
    emitter = EventEmitter(args.run_id, store.append)
    reason = args.reason or "operator"
    emitter.emit_cancel_requested(reason=reason, requested_by="cli")
    _say(
        f"✕ cancel_requested written to {log_path}",
        style="bright_black",
    )
    _say(
        "The run will short-circuit on its next loop tick (in-process), or on "
        "the next `requiem resume <run_id>` (out-of-process). "
        "INV-CANCEL-SHORT-CIRCUITS-RETRY: no further retries will be attempted.",
        style="dim",
    )
    return EXIT_CODE_OK


# ---- follow / tail loop (Gap 2) ------------------------------------


_TAIL_POLL_INTERVAL = 0.2  # seconds; conservative for v0


def _tail_rendered(log_path: Path, cx: RenderContext) -> None:
    """Render the file then tail for new events; stops on run_completed or Ctrl-C."""
    seen = 0
    terminal = False
    try:
        while not terminal:
            new_events = _read_events_after(log_path, seen)
            for ev in new_events:
                _emit_lines(render_event(ev, cx))
                if ev.get("kind") == "run_completed":
                    terminal = True
            seen += len(new_events)
            if not terminal:
                time.sleep(_TAIL_POLL_INTERVAL)
    except KeyboardInterrupt:
        _say("(stopped tailing)", style="dim")


def _tail_raw(log_path: Path) -> None:
    seen = 0
    terminal = False
    try:
        while not terminal:
            new_events = _read_events_after(log_path, seen)
            for ev in new_events:
                print(json.dumps(ev, separators=(",", ":")), flush=True)
                if ev.get("kind") == "run_completed":
                    terminal = True
            seen += len(new_events)
            if not terminal:
                time.sleep(_TAIL_POLL_INTERVAL)
    except KeyboardInterrupt:
        pass


def _read_events_after(log_path: Path, already_seen: int) -> list[dict[str, Any]]:
    """Return events past index `already_seen`. Cheap re-read (v0 demo-scale)."""
    if not log_path.exists():
        return []
    out: list[dict[str, Any]] = []
    for idx, ev in enumerate(replay(log_path)):
        if idx >= already_seen:
            out.append(ev)
    return out


# ---- list-runs helpers (Gap 4) -------------------------------------


def _summarize_run(log_path: Path) -> dict[str, Any]:
    """Parse a run's log into a one-row summary for `list-runs`."""
    workflow = ""
    started: str | None = None
    last_ts: str | None = None
    status = "Running"
    events = 0
    final_node = ""
    for ev in replay(log_path):
        events += 1
        kind = ev.get("kind", "")
        ts = ev.get("ts")
        if started is None and ts:
            started = ts
        if ts:
            last_ts = ts
        if kind == "run_started":
            workflow = ev["payload"].get("workflow", "")
        elif kind == "gate_opened":
            status = "Suspended"
        elif kind == "gate_resolved":
            status = "Running"
        elif kind == "cancel_requested":
            status = "Cancelled"
        elif kind == "run_completed":
            terminal = ev["payload"].get("terminal", "")
            final_node = ev["payload"].get("final_node", "")
            if terminal == "cancelled":
                status = "Cancelled"
            elif terminal == "failed":
                status = "Failed"
            else:
                status = "Completed"
    duration = _format_duration(started, last_ts) if started and last_ts else None
    return {
        "workflow": workflow,
        "started": started,
        "status": status,
        "duration": duration,
        "events": events,
        "final_node": final_node,
    }


def _short_ts(ts: str | None) -> str:
    if not ts:
        return "—"
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%MZ")
    except ValueError:
        return ts[:19]


def _format_duration(start: str, end: str) -> str:
    try:
        from datetime import datetime
        d0 = datetime.fromisoformat(start)
        d1 = datetime.fromisoformat(end)
        delta_ms = (d1 - d0).total_seconds() * 1000
        if delta_ms < 1000:
            return f"{delta_ms:.0f} ms"
        if delta_ms < 60_000:
            return f"{delta_ms / 1000:.1f} s"
        return f"{delta_ms / 60_000:.1f} min"
    except ValueError:
        return "—"


def _style_status(status: str) -> str:
    if not _HAS_RICH:
        return status
    colour = {
        "Completed": "green",
        "Suspended": "cyan",
        "Cancelled": "bright_black",
        "Failed":    "red",
        "Running":   "yellow",
    }.get(status, "white")
    return f"[{colour}]{status}[/{colour}]"


def _workflow_module_from_log(log_path: Path) -> str | None:
    """Recover the workflow's importable module path from the run_started event.

    Returns ``None`` if the log has no ``run_started`` or it predates the
    workflow-identity field (Gap 1).
    """
    for ev in replay(log_path):
        if ev.get("kind") == "run_started":
            return ev.get("payload", {}).get("workflow_module")
        break
    return None


# ---- interactive gate handler (Gap 3) ------------------------------


def _make_interactive_gate_handler(cx: RenderContext):
    """Build a gate handler that prompts the operator at each NeedsHuman.

    The `gate_opened` event has already been emitted (and rendered) when the
    kernel reaches the handler — the operator sees the gate prompt + the
    workflow's gate-context line above this prompt. The handler asks for a
    choice from the offered options and returns it.

    Uses `rich.prompt.Prompt` when available; falls back to plain `input()`.
    """
    if _HAS_RICH:
        try:
            from rich.prompt import Prompt
        except ImportError:
            Prompt = None  # type: ignore[assignment]
    else:
        Prompt = None  # type: ignore[assignment]

    def _handler(node_id: str, prompt: str, options: tuple[str, ...]) -> str:
        opts = list(options)
        default = opts[0]
        if Prompt is not None:
            choice = Prompt.ask(
                f"[bold cyan]?[/bold cyan] {prompt}",
                choices=opts,
                default=default,
                show_choices=True,
                show_default=True,
            )
        else:
            choice = ""
            while choice not in opts:
                raw = input(f"? {prompt} {opts} [{default}]: ").strip()
                choice = raw or default
        return choice

    # Not auto: this is a real human decision, so the renderer should NOT
    # append "(auto-approved for demo)".
    _handler.__requiem_auto__ = False  # type: ignore[attr-defined]
    return _handler


# ---- post-run helpers ----------------------------------------------


def _print_preamble(mod: ModuleType | None) -> None:
    """Print the workflow's preamble (vignette + stakes) before the run.

    Per Debussy Demo Contract §3.1 (workday framing) and §3.2 (stakes).
    Workflow modules opt in by exposing ``preamble() -> str | None`` that
    returns ready-to-print text. The CLI prints it after the ``log:``
    line and before the first horizontal rule.
    """
    if mod is None:
        return
    fn = getattr(mod, "preamble", None)
    if fn is None:
        return
    try:
        text = fn()
    except Exception as e:  # noqa: BLE001
        _say(f"(preamble render error: {type(e).__name__}: {e})", style="red")
        return
    if not text:
        return
    _say()
    _say(text)


def _print_verdict_card(mod: ModuleType | None, cx: RenderContext) -> None:
    if mod is None:
        return
    fn = getattr(mod, "verdict_card", None)
    if fn is None:
        return
    try:
        card = fn(cx.completed)
    except Exception as e:  # noqa: BLE001
        _say(f"(verdict card render error: {type(e).__name__}: {e})", style="red")
        return
    if not card:
        return
    _say()
    _say(card)
    _say()


def _print_run_footer(result: Any, log_path: Path, elapsed_ms: float) -> None:
    match result:
        case Completed(disposition=d, final_node=n, projection=proj):
            _say(
                f"Completed ({d}, final_node={n}, "
                f"{proj.get('total_events')} events, {elapsed_ms:.0f} ms)",
                style="green",
            )
        case Suspended(node_id=n, prompt=p, options=opts):
            _say(f"Suspended at {n}: {p}  options={list(opts)}", style="cyan")
        case Failed(node_id=n, error_kind=ek, message=m):
            style = "bright_black" if ek == "cancelled" else "red"
            _say(f"Failed at {n}: [{ek}] {m}", style=style)
    _say(f"log: {log_path}", style="dim")


def cmd_clean(args: argparse.Namespace) -> int:
    """Reset state for one item so the next run starts from scratch.

    Removes all artifacts the end-to-end driver writes under ``<log-dir>``
    for the named ``--item``: plan/commit/trunk/exec/fanout/leafpr/featurepr
    events.jsonl, the plan.md sidecar, leaf-pr-map JSON, committed plan
    JSON, and any ``__child_*`` / ``__leaf-*`` subworkflow logs.

    With ``--ado-delete``, also deletes the work item itself from ADO via
    twig (unparenting first if needed). Use this for scratch/test
    Scenarios that should NOT survive cleanup; production items should
    almost never use this flag.

    With ``--keep-artifacts``, only deletes durable side-effects (twig
    work item, leaf-pr-map referencing real PR numbers) and preserves
    the event-log artifacts so an operator can still inspect the
    transcript. The default is to nuke everything under the log dir.

    Never touches the repo working tree. Never deletes seeded ADO
    children (that needs a separate `--cascade-children` flag, deliberately
    not added in v0 because it's a footgun — operator should confirm
    each child individually).

    Refuses to clean if a leaf PR appears in the leaf-pr-map (that's a
    real PR in flight; cleaning would lose the linkage). Pass
    ``--force`` to override.
    """
    item_id = args.item
    log_dir = Path(args.log_dir).resolve()
    dry_run = args.dry_run
    keep_artifacts = args.keep_artifacts
    ado_delete = args.ado_delete
    force = args.force

    _say(f"requiem clean — item {item_id}, log_dir={log_dir}", style="bold")
    if dry_run:
        _say("  (dry-run — nothing will actually be removed)", style="yellow")

    # ---- gather the artifacts -----------------------------------------
    # Two pattern families:
    #
    #   1) ALWAYS-cleanable: ephemeral per-run state that is safe to
    #      regenerate. Event logs (.events.jsonl) and the human-readable
    #      plan sidecars (.plan.md, .plan.tree.json) at any recursion
    #      depth.
    #
    #   2) IDEMPOTENCY state (commit manifest, leaf-pr-map): names ADO /
    #      git side-effects from a prior run. PRESERVE BY DEFAULT so
    #      retries can adopt prior children / leaf PRs (ADR-0026
    #      follow-up: ADO doesn't preserve our HTML-comment markers, so
    #      the manifest is the ONLY surviving cross-run idempotency
    #      hook). Pass --include-manifest to nuke them too — that's the
    #      explicit "I really want a fresh seed" path.
    always_patterns = [
        # top-level events + sidecars
        f"plan-{item_id}.events.jsonl",
        f"plan-{item_id}.plan.md",
        f"plan-{item_id}.plan.tree.json",
        # one-level subworkflows (planning recursion, fanout)
        f"plan-{item_id}__child_*.events.jsonl",
        f"plan-{item_id}__child_*.plan.md",
        f"plan-{item_id}__child_*.plan.tree.json",
        f"plan-{item_id}-plan-{item_id}__child_*.events.jsonl",
        # deeper recursion (two+ levels — Scenario → Feature → Task)
        f"plan-{item_id}__child_*__child_*.events.jsonl",
        f"plan-{item_id}__child_*__child_*.plan.md",
        f"plan-{item_id}__child_*__child_*.plan.tree.json",
        f"plan-{item_id}__child_*__child_*__child_*.events.jsonl",
        f"plan-{item_id}__child_*__child_*__child_*.plan.md",
        f"plan-{item_id}__child_*__child_*__child_*.plan.tree.json",
        f"plan-{item_id}__child_*__child_*__child_*__child_*.events.jsonl",
        f"plan-{item_id}__child_*__child_*__child_*__child_*.plan.md",
        f"plan-{item_id}__child_*__child_*__child_*__child_*.plan.tree.json",
        # commit/trunk/exec/leafpr/featurepr event logs
        f"commit-{item_id}.events.jsonl",
        f"trunk-{item_id}.events.jsonl",
        f"exec-{item_id}.events.jsonl",
        f"fanout-{item_id}.events.jsonl",
        f"fanout-{item_id}__leaf-*.events.jsonl",
        f"leafpr-{item_id}.events.jsonl",
        f"featurepr-{item_id}.events.jsonl",
    ]
    manifest_patterns = [
        # commit_plan's idempotency manifest — preserve by default
        # so retries can recognise prior ADO children.
        f"commit-{item_id}.plan.committed.json",
        # leaf-pr-map — points at real PR numbers
        f"leaf-pr-map-{item_id}.json",
    ]
    patterns = list(always_patterns)
    if getattr(args, "include_manifest", False):
        patterns.extend(manifest_patterns)
    matched: list[Path] = []
    if log_dir.exists():
        for pat in patterns:
            matched.extend(sorted(log_dir.glob(pat)))

    # ---- safety: in-flight leaf PRs check -----------------------------
    leaf_pr_map_path = log_dir / f"leaf-pr-map-{item_id}.json"
    in_flight_prs: list[tuple[str, int]] = []
    if leaf_pr_map_path.exists():
        try:
            payload = json.loads(leaf_pr_map_path.read_text(encoding="utf-8"))
            for leaf in payload.get("leaves", []):
                pr_num = leaf.get("pr_number")
                if pr_num:
                    in_flight_prs.append((leaf.get("leaf_id", "?"), int(pr_num)))
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    if in_flight_prs and not force:
        _say(
            f"\nrefusing to clean: {len(in_flight_prs)} leaf PR(s) recorded:",
            style="red",
        )
        for leaf_id, pr_num in in_flight_prs:
            _say(f"  • leaf {leaf_id}: PR #{pr_num}", style="red")
        _say(
            "\nPass --force to clean anyway (loses the leaf↔PR linkage).",
            style="dim",
        )
        return EXIT_CODE_FAILED

    # ---- report what we'd touch ---------------------------------------
    if not matched:
        _say(f"  (no artifacts matched for item {item_id})", style="dim")
    else:
        _say(f"  {len(matched)} artifact(s) to remove:")
        for p in matched:
            kind = "subworkflow log" if "__" in p.name else "artifact"
            _say(f"    {kind:18s} {p.relative_to(log_dir)}", style="bright_black")

    if ado_delete:
        _say(
            f"  ado-delete: will call `twig delete {item_id} --force`",
            style="yellow",
        )

    if dry_run:
        _say("\n(dry-run complete; nothing removed)", style="yellow")
        return EXIT_CODE_OK

    # ---- destructive section ------------------------------------------
    removed = 0
    if not keep_artifacts:
        for p in matched:
            try:
                p.unlink()
                removed += 1
            except OSError as e:
                _say(f"  ! failed to remove {p}: {e}", style="red")

    if ado_delete:
        rc = _twig_delete(item_id)
        if rc != 0:
            _say(
                f"  ! twig delete failed (rc={rc}); local artifacts already removed",
                style="red",
            )
            return EXIT_CODE_FAILED

    _say(
        f"\n✓ cleaned item {item_id}: "
        f"{removed} artifact(s) removed"
        + ("; ADO item deleted" if ado_delete else "")
        + ("; artifacts kept" if keep_artifacts else ""),
        style="green",
    )
    return EXIT_CODE_OK


def _twig_delete(item_id: int) -> int:
    """Invoke `twig delete <id> --force`, unparenting first if needed.

    Twig refuses to delete an item that has parent or other links; this
    helper performs the minimum unparenting required.
    """
    import subprocess
    # Set active first (twig link unparent operates on the active item).
    subprocess.run(
        ["twig", "set", str(item_id)],
        capture_output=True, text=True, timeout=30,
    )
    # Best-effort unparent (no-op if already unparented).
    subprocess.run(
        ["twig", "link", "unparent"],
        capture_output=True, text=True, timeout=30,
    )
    # Now delete.
    result = subprocess.run(
        ["twig", "delete", str(item_id), "--force"],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        _say(f"  twig stderr: {result.stderr.strip()}", style="red")
    return result.returncode


# ---- argparse plumbing ---------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="requiem",
        description="Requiem — single-process SDLC orchestration engine.",
    )
    p.add_argument("--version", action="store_true", help="print version and exit")
    sub = p.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run a workflow by importable module path")
    run.add_argument("workflow_module")
    run.add_argument("--run-id", default=None)
    run.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    run.add_argument(
        "--interactive", "-i", action="store_true",
        help="prompt the operator at each human gate (default: auto-resolve per workflow)",
    )
    run.set_defaults(func=cmd_run)

    res = sub.add_parser("resume", help="resume a partially-finished run")
    res.add_argument("workflow_module")
    res.add_argument("run_id")
    res.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    res.add_argument(
        "--interactive", "-i", action="store_true",
        help="prompt the operator at each human gate",
    )
    res.set_defaults(func=cmd_resume)

    desc = sub.add_parser("describe", help="print a workflow's topology")
    desc.add_argument("workflow_module")
    desc.set_defaults(func=cmd_describe)

    ev = sub.add_parser("events", help="print a run's event log (live English by default)")
    ev.add_argument("run_id")
    ev.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    ev.add_argument(
        "--workflow",
        default=None,
        help="workflow module override (default: auto-load from run_started event)",
    )
    ev.add_argument(
        "--raw",
        action="store_true",
        help="emit raw JSONL instead of human English (CI consumers)",
    )
    ev.add_argument(
        "--follow", "-f", action="store_true",
        help="tail the log; render new events as they arrive (stops on run_completed or Ctrl-C)",
    )
    ev.set_defaults(func=cmd_events)

    ls = sub.add_parser("list-runs", help="list runs found under --log-dir")
    ls.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    ls.set_defaults(func=cmd_list_runs)

    cn = sub.add_parser("cancel", help="cancel a running or suspended run")
    cn.add_argument("run_id")
    cn.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    cn.add_argument(
        "--reason", default=None,
        help="free-text reason recorded in the cancel_requested event payload",
    )
    cn.set_defaults(func=cmd_cancel)

    cl = sub.add_parser(
        "clean",
        help="reset state for one item — nuke per-item event logs + artifacts",
    )
    cl.add_argument(
        "--item", type=int, required=True,
        help="ADO work-item id to clean up state for.",
    )
    cl.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    cl.add_argument(
        "--ado-delete", action="store_true",
        help=(
            "Also delete the work item from ADO via `twig delete`. "
            "DESTRUCTIVE; use for scratch/test Scenarios only."
        ),
    )
    cl.add_argument(
        "--keep-artifacts", action="store_true",
        help=(
            "Keep the event-log files (don't remove them). Useful when "
            "you want to keep a forensic copy of the run transcript "
            "but still need to re-run from scratch."
        ),
    )
    cl.add_argument(
        "--dry-run", action="store_true",
        help="Print what WOULD be removed without actually removing anything.",
    )
    cl.add_argument(
        "--force", action="store_true",
        help=(
            "Override the in-flight-PR safety check. Default behaviour "
            "refuses to clean an item whose leaf-pr-map shows real PR "
            "numbers (cleaning would lose the linkage)."
        ),
    )
    cl.add_argument(
        "--include-manifest", action="store_true",
        help=(
            "ALSO delete the commit_plan idempotency manifest "
            "(commit-{N}.plan.committed.json) and the leaf-pr-map. "
            "Default behaviour preserves them so retries can adopt "
            "prior ADO children / leaf PRs (ADR-0026: ADO doesn't "
            "preserve our HTML-comment markers, so the manifest is "
            "the only surviving cross-run idempotency hook). Set "
            "this flag for the explicit \"I want a truly fresh seed\" "
            "case — next run will create NEW ADO children instead of "
            "adopting the prior ones."
        ),
    )
    cl.set_defaults(func=cmd_clean)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.version:
        from requiem import __version__
        print(f"requiem {__version__}")
        return 0
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        _say("Cancelled (SIGINT)", style="bright_black")
        return 130
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        _say(f"requiem: {type(e).__name__}: {e}", style="red")
        return EXIT_CODE_FAILED


if __name__ == "__main__":
    sys.exit(main())
