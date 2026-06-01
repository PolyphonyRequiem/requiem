"""Requiem CLI.

Four verbs:

* ``requiem run <workflow_module> [--run-id ID] [--log-dir DIR]``
    Run a workflow by importable module path. Prints a human-readable
    trace; on success, writes the run's event log.

* ``requiem resume <workflow_module> <run_id> [--log-dir DIR]``
    Resume a partially-finished run from its event log. The workflow
    module is required so the engine knows what to construct (the log
    deliberately has no sidecar manifest pinning module identity).

* ``requiem describe <workflow_module>``
    Print the workflow's topology: nodes, edges, registered agents.

* ``requiem events <run_id> [--log-dir DIR] [--json]``
    Print the event log in a human-readable form, with outcome-kind
    colour hints (via `rich` if installed). `--json` falls back to raw
    JSONL.

Module loading: the workflow argument must expose either:

* ``build_engine(log_dir: Path, **kwargs) -> Engine``   (preferred)
* ``build_workflow() -> Workflow``                       (describe only)
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
import time
from pathlib import Path
from typing import Any

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


# Debussy's outcome-kind-as-colour palette (used both by `events` and
# the inline run trace).
_OUTCOME_COLOUR = {
    "success":            "green",
    "retryable_failure":  "yellow",
    "permanent_failure":  "red",
    "bad_output":         "magenta",
    "needs_human":        "cyan",
    "cancelled":          "bright_black",
}

# Event kind → colour for the trace.
_EVENT_COLOUR = {
    "run_started":             "bold blue",
    "node_entered":            "blue",
    "verb_completed":          None,  # coloured by outcome
    "retry_attempted":         "yellow",
    "route_taken":             "dim",
    "team_dispatched":         "cyan",
    "team_branch_completed":   None,
    "gate_opened":             "cyan",
    "gate_resolved":           "cyan",
    "run_completed":           "bold green",
}


def _say(text: str, *, style: str | None = None) -> None:
    if _HAS_RICH and style:
        _CONSOLE.print(text, style=style)
    else:
        print(text)


# ---- module loading -------------------------------------------------


def _import_module(dotted: str):
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
        # construct against a throwaway log dir just to read topology
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            return mod.build_engine(Path(td)).workflow
    raise SystemExit(
        f"requiem: module {module_path!r} has neither build_workflow nor build_engine"
    )


# ---- shared run-printer ---------------------------------------------


def _print_run_header(module_path: str, run_id: str, log_dir: Path, engine: Engine) -> None:
    wf = engine.workflow
    _say("=" * 72)
    _say(f"requiem run — {module_path}", style="bold")
    _say("=" * 72)
    _say(f"run_id    : {run_id}")
    _say(f"workflow  : {wf.name}  ({len(wf.nodes)} nodes, {len(wf.edges)} edges)")
    _say(f"log_dir   : {log_dir}")
    _say("-" * 72)


def _print_run_result(result, log_path: Path, elapsed_ms: float) -> int:
    _say("-" * 72)
    _say(f"wall-clock: {elapsed_ms:.1f} ms")
    match result:
        case Completed(disposition=d, final_node=n, projection=proj):
            _say(f"result    : Completed", style="bold green")
            _say(f"disposition: {d}  (final_node={n})")
            _say(f"events    : {proj.get('total_events')}")
            _say(f"nodes     : {' → '.join(proj.get('nodes_entered', []))}")
        case Suspended(node_id=n, prompt=p, options=opts):
            _say(f"result    : Suspended", style="bold cyan")
            _say(f"  at      : {n}")
            _say(f"  prompt  : {p}")
            _say(f"  options : {opts}")
        case Failed(node_id=n, error_kind=ek, message=m):
            _say(f"result    : Failed", style="bold red")
            _say(f"  at      : {n}")
            _say(f"  kind    : {ek}")
            _say(f"  message : {m}")
    _say(f"event log : {log_path}")
    _say("=" * 72)
    return 0 if isinstance(result, Completed) else (2 if isinstance(result, Suspended) else 1)


# ---- subcommands ----------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    log_dir = Path(args.log_dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    run_id = args.run_id or f"run-{int(time.time())}"
    engine = _load_engine(args.workflow_module, log_dir)
    _print_run_header(args.workflow_module, run_id, log_dir, engine)
    t0 = time.perf_counter()
    result = asyncio.run(engine.run(run_id))
    return _print_run_result(result, engine.log_path(run_id), (time.perf_counter() - t0) * 1000)


def cmd_resume(args: argparse.Namespace) -> int:
    log_dir = Path(args.log_dir).resolve()
    engine = _load_engine(args.workflow_module, log_dir)
    log_path = engine.log_path(args.run_id)
    if not log_path.exists():
        raise SystemExit(f"requiem: no event log at {log_path}")
    _print_run_header(args.workflow_module, args.run_id, log_dir, engine)
    _say(f"  resuming from {sum(1 for _ in replay(log_path))} prior events")
    _say("-" * 72)
    t0 = time.perf_counter()
    result = asyncio.run(engine.run(args.run_id))
    return _print_run_result(result, log_path, (time.perf_counter() - t0) * 1000)


def cmd_describe(args: argparse.Namespace) -> int:
    wf = _load_workflow(args.workflow_module)
    _say("=" * 72)
    _say(f"workflow: {wf.name}", style="bold")
    _say(f"  module: {args.workflow_module}")
    _say(f"  entry : {wf.entry}")
    _say("-" * 72)
    _say(f"nodes ({len(wf.nodes)}):", style="bold")
    for n in wf.nodes:
        kind = n.kind
        extras = []
        if hasattr(n, "verb"):
            extras.append(f"verb={n.verb}")
        if hasattr(n, "agent") and getattr(n, "agent", None):
            extras.append(f"agent={n.agent}")
        if hasattr(n, "retry_max") and n.retry_max:
            extras.append(f"retry_max={n.retry_max}")
        if hasattr(n, "branches"):
            extras.append(f"branches=[{','.join(b.agent for b in n.branches)}]")
        if hasattr(n, "options") and getattr(n, "options", None):
            extras.append(f"options={list(n.options)}")
        if hasattr(n, "disposition"):
            extras.append(f"disposition={n.disposition}")
        suffix = "  " + " ".join(extras) if extras else ""
        _say(f"  - [{kind:10s}] {n.node_id}{suffix}")
    _say("-" * 72)
    _say(f"edges ({len(wf.edges)}):", style="bold")
    for e in wf.edges:
        _say(f"  {e.from_node}  --[{e.on}]-->  {e.to_node}")
    _say("=" * 72)
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    log_dir = Path(args.log_dir).resolve()
    log_path = log_dir / f"{args.run_id}.events.jsonl"
    if not log_path.exists():
        raise SystemExit(f"requiem: no event log at {log_path}")

    if args.json:
        for ev in replay(log_path):
            print(json.dumps(ev, separators=(",", ":")))
        return 0

    _say(f"event log: {log_path}", style="bold")
    _say("-" * 72)
    for ev in replay(log_path):
        _print_event(ev)
    _say("-" * 72)
    return 0


def _print_event(ev: dict[str, Any]) -> None:
    """Render one event line. Colour by event kind (and by outcome kind
    where applicable — Debussy's outcome-kind-as-colour recommendation).
    """
    eid = ev["event_id"]
    kind = ev["kind"]
    node = ev.get("node_id") or "-"
    payload = ev.get("payload", {})

    detail = _event_detail(kind, ev, payload)
    style = _EVENT_COLOUR.get(kind)

    if kind in ("verb_completed", "team_branch_completed"):
        outcome_kind = payload.get("outcome", {}).get("kind", "?")
        style = _OUTCOME_COLOUR.get(outcome_kind, "white")

    line = f"  #{eid:03d}  {kind:24s}  {node:18s}  {detail}"
    _say(line, style=style)


def _event_detail(kind: str, ev: dict[str, Any], payload: dict[str, Any]) -> str:
    if kind == "run_started":
        return f"workflow={payload.get('workflow')}"
    if kind == "node_entered":
        return f"attempt={payload.get('attempt', 1)}"
    if kind == "verb_completed":
        o = payload.get("outcome", {})
        oc = o.get("kind", "?")
        if oc == "success":
            return "outcome=success"
        if oc == "retryable_failure":
            return f"outcome=retryable_failure  [{o.get('error_kind')}] {o.get('message','')}"
        if oc == "permanent_failure":
            return f"outcome=permanent_failure  [{o.get('error_kind')}] {o.get('message','')}"
        if oc == "bad_output":
            errs = o.get("validation_errors") or []
            return f"outcome=bad_output  [{o.get('error_kind')}] errors={len(errs)}"
        if oc == "needs_human":
            return f"outcome=needs_human  gate={o.get('gate')}"
        if oc == "cancelled":
            return f"outcome=cancelled  cause={o.get('cause')}"
        return f"outcome={oc}"
    if kind == "retry_attempted":
        return f"{payload.get('attempt')} → {payload.get('next_attempt')}  reason={payload.get('reason')}"
    if kind == "route_taken":
        return f"on={payload.get('key')}  to={payload.get('to_node')}"
    if kind == "team_dispatched":
        return f"team={ev.get('team_id')}  branches={payload.get('branches')}"
    if kind == "team_branch_completed":
        o = payload.get("outcome", {})
        return f"agent={ev.get('agent_id')}  outcome={o.get('kind')}"
    if kind == "gate_opened":
        return f"prompt={payload.get('prompt')!r}  options={payload.get('options')}"
    if kind == "gate_resolved":
        return f"choice={payload.get('choice')}"
    if kind == "run_completed":
        return f"terminal={payload.get('terminal')}  final_node={payload.get('final_node')}"
    return json.dumps(payload, separators=(",", ":"))


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

    res = sub.add_parser("resume", help="resume a partially-finished run from its event log")
    res.add_argument("workflow_module")
    res.add_argument("run_id")
    res.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    res.set_defaults(func=cmd_resume)

    desc = sub.add_parser("describe", help="print a workflow's topology")
    desc.add_argument("workflow_module")
    desc.set_defaults(func=cmd_describe)

    ev = sub.add_parser("events", help="print a run's event log human-readably")
    ev.add_argument("run_id")
    ev.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    ev.add_argument("--json", action="store_true", help="emit raw JSONL instead")
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
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
