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
    EXIT_CODE_FAILED,
    RenderContext,
    exit_code_for,
    render_event,
    style_for_line,
)
from requiem.kernel import Completed, Engine, Failed, Suspended
from requiem.persistence import replay


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

    _say(f"requiem run — {args.workflow_module}  (run_id={run_id})", style="bold")
    _say(f"log: {engine.log_path(run_id)}", style="dim")
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

    _say(f"requiem resume — {args.workflow_module}  (run_id={args.run_id})", style="bold")
    _say(f"log: {log_path}", style="dim")
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
        for ev in replay(log_path):
            print(json.dumps(ev, separators=(",", ":")))
        return 0

    mod: ModuleType | None = None
    humanize: dict[str, str] = {}
    workflow_name = ""
    if args.workflow:
        mod = _import_module(args.workflow)
        wf = _load_workflow(args.workflow)
        humanize = dict(wf.humanize)
        workflow_name = wf.name
    cx = _render_context_for(mod, workflow_name, humanize)

    for ev in replay(log_path):
        _emit_lines(render_event(ev, cx))
    return 0


# ---- post-run helpers ----------------------------------------------


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
    run.set_defaults(func=cmd_run)

    res = sub.add_parser("resume", help="resume a partially-finished run")
    res.add_argument("workflow_module")
    res.add_argument("run_id")
    res.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    res.set_defaults(func=cmd_resume)

    desc = sub.add_parser("describe", help="print a workflow's topology")
    desc.add_argument("workflow_module")
    desc.set_defaults(func=cmd_describe)

    ev = sub.add_parser("events", help="print a run's event log")
    ev.add_argument("run_id")
    ev.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    ev.add_argument(
        "--workflow",
        default=None,
        help="workflow module (for humanize map and verdict card on stored logs)",
    )
    ev.add_argument(
        "--raw",
        action="store_true",
        help="emit raw JSONL instead of human English (CI consumers)",
    )
    ev.set_defaults(func=cmd_events)

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
