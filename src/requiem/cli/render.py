"""Customer-facing rendering layer for the CLI.

Per Debussy's Demo Contract (`perspectives/ui-sdlc/07-demo-contract.md`):

* Default `requiem events` and live `requiem run` output is HUMAN ENGLISH.
* `--raw` returns the JSONL stream verbatim (CI consumers).
* Every event kind in `requiem.events.EVENT_KINDS` MUST have a registered
  renderer (enforced by `tests/test_renderer_registry.py`).

A renderer receives the event envelope plus a `RenderContext` and returns
a (possibly empty) list of lines. Returning `[]` deliberately suppresses
the event — used for routing/dispatch events that are mechanism, not story
(`route_taken`, `verb_invoked`, first `node_entered`), and for retry-block
collapsing (Demo Contract §3.7).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from requiem.events import EVENT_KINDS
from requiem.kernel import Completed, Failed, RunResult, Suspended


# ---- glyph alphabet (Demo Contract §4.5) ---------------------------

GLYPH_ACTION = "▶"
GLYPH_OK     = "✓"
GLYPH_RETRY  = "🔁"
GLYPH_GATE   = "🚦"
GLYPH_FAIL   = "✕"
GLYPH_END    = "■"


OUTCOME_COLOUR: dict[str, str] = {
    "success":           "green",
    "retryable_failure": "yellow",
    "permanent_failure": "red",
    "bad_output":        "magenta",
    "needs_human":       "cyan",
    "cancelled":         "bright_black",
}

GLYPH_COLOUR: dict[str, str] = {
    GLYPH_ACTION: "dim",
    GLYPH_OK:     "green",
    GLYPH_RETRY:  "yellow",
    GLYPH_GATE:   "cyan",
    GLYPH_FAIL:   "red",
    GLYPH_END:    "bold",
}


# ---- render context ------------------------------------------------

DetailFn = Callable[[dict[str, Any]], str]
"""Turns a verb_completed `value` payload into a short noun phrase."""

GateContextFn = Callable[[dict[str, dict[str, Any]]], str]
"""Pulls a gate-context line out of the streamed `completed` map."""

SubworkflowDetailFn = Callable[[dict[str, Any]], str]
"""Turns a subworkflow_completed `outcome` payload into a short detail phrase.

The full SDLC demo (and any other parent workflow that composes children)
uses this to render stage-level narration like ``"✓ Planned — 1 leaf"``
instead of the generic ``"Child workflow returned: completed (plan)"``.
"""


@dataclass
class RenderContext:
    workflow_name: str = ""
    artifact_name: str = ""
    active_workflow_stack: list[str] = field(default_factory=list)
    """Stack of ``run_started.payload.workflow`` names — pushed on
    ``run_started``, popped on ``run_completed``. Lets child workflows
    that share the parent's render context (e.g. the full-SDLC demo's
    live narration) label correctly without leaking into the parent's
    own terminator line.
    """
    humanize: dict[str, str] = field(default_factory=dict)
    details: dict[str, DetailFn] = field(default_factory=dict)
    gate_contexts: dict[str, GateContextFn] = field(default_factory=dict)
    subworkflow_details: dict[str, SubworkflowDetailFn] = field(default_factory=dict)
    """Per-subworkflow-node detail formatter (parent-narration polish)."""
    silent_nodes: frozenset[str] = field(default_factory=frozenset)
    """Nodes whose `verb_completed` line is suppressed.

    Use for orchestration scaffolding (entry stubs, terminate nodes, team
    aggregators) whose story is already told by other events.
    """

    completed: dict[str, dict[str, Any]] = field(default_factory=dict)
    attempts: dict[str, int] = field(default_factory=dict)

    def label(self, node_id: str | None) -> str:
        if not node_id:
            return "—"
        return self.humanize.get(node_id, node_id)


# ---- renderer registry --------------------------------------------

RenderResult = list[str]
"""0+ lines for a single event. Empty list = suppress."""

Renderer = Callable[[dict[str, Any], RenderContext], RenderResult]


EVENT_RENDERERS: dict[str, Renderer] = {}


def _register(kind: str) -> Callable[[Renderer], Renderer]:
    def deco(fn: Renderer) -> Renderer:
        EVENT_RENDERERS[kind] = fn
        return fn
    return deco


# ---- per-kind renderers -------------------------------------------


@_register("run_started")
def _r_run_started(ev: dict[str, Any], cx: RenderContext) -> RenderResult:
    wf = ev["payload"].get("workflow") or cx.workflow_name or "workflow"
    cx.active_workflow_stack.append(wf)
    suffix = f" on {cx.artifact_name}" if cx.artifact_name else ""
    return [f"{GLYPH_ACTION} run_started — {wf}{suffix}"]


@_register("node_entered")
def _r_node_entered(ev: dict[str, Any], cx: RenderContext) -> RenderResult:
    node = ev.get("node_id") or ""
    cx.attempts[node] = ev["payload"].get("attempt", 1)
    return []


@_register("verb_invoked")
def _r_verb_invoked(ev: dict[str, Any], cx: RenderContext) -> RenderResult:
    return []


@_register("verb_completed")
def _r_verb_completed(ev: dict[str, Any], cx: RenderContext) -> RenderResult:
    node = ev.get("node_id") or ""
    outcome = ev["payload"].get("outcome", {})
    kind = outcome.get("kind", "?")
    cx.completed[node] = outcome
    label = cx.label(node)
    attempt = cx.attempts.get(node, 1)

    if kind == "success":
        if node in cx.silent_nodes:
            return []
        detail_fn = cx.details.get(node)
        detail = ""
        if detail_fn is not None:
            try:
                detail = detail_fn(outcome.get("value", {}))
            except Exception as e:  # noqa: BLE001
                detail = f"<detail render error: {type(e).__name__}>"
        if attempt > 1:
            return [f"{GLYPH_OK} {label} passed on attempt {attempt}"]
        if detail:
            return [f"{GLYPH_OK} {label} — {detail}"]
        return [f"{GLYPH_OK} {label}"]

    if kind == "retryable_failure":
        # The following retry_attempted event renders the 🔁 line.
        return []

    if kind == "permanent_failure":
        msg = outcome.get("message", "")
        return [f"{GLYPH_FAIL} {label} failed — {msg}"]

    if kind == "bad_output":
        errs = outcome.get("validation_errors") or []
        n = len(errs)
        plural = "s" if n != 1 else ""
        return [f"{GLYPH_FAIL} {label} produced bad output ({n} validation error{plural})"]

    if kind == "needs_human":
        # gate_opened renders the line.
        return []

    if kind == "cancelled":
        cause = outcome.get("cause", "?")
        return [f"{GLYPH_END} {label} cancelled — {cause}"]

    return [f"{GLYPH_FAIL} {label} — unknown outcome {kind!r}"]


@_register("retry_attempted")
def _r_retry_attempted(ev: dict[str, Any], cx: RenderContext) -> RenderResult:
    p = ev["payload"]
    label = cx.label(ev.get("node_id"))
    reason = p.get("reason", "")
    nxt = p.get("next_attempt", "?")
    after = p.get("after")
    if after is not None and after > 0:
        return [
            f"{GLYPH_RETRY} {label} failed: {reason} — "
            f"retrying (attempt {nxt}, after {after:.1f}s)"
        ]
    return [f"{GLYPH_RETRY} {label} failed: {reason} — retrying (attempt {nxt})"]


@_register("route_taken")
def _r_route_taken(ev: dict[str, Any], cx: RenderContext) -> RenderResult:
    return []


@_register("team_dispatched")
def _r_team_dispatched(ev: dict[str, Any], cx: RenderContext) -> RenderResult:
    branches = ev["payload"].get("branches") or []
    label = cx.label(ev.get("node_id"))
    return [f"{GLYPH_ACTION} Started {len(branches)} {label} in parallel"]


@_register("team_branch_completed")
def _r_team_branch_completed(ev: dict[str, Any], cx: RenderContext) -> RenderResult:
    agent = ev.get("agent_id") or "?"
    outcome = ev["payload"].get("outcome", {})
    kind = outcome.get("kind", "?")
    if kind != "success":
        return [f"  {GLYPH_FAIL} {agent} — {kind}"]
    parsed = outcome.get("value", {}).get("parsed") or {}
    sev = parsed.get("severity")
    summary = parsed.get("summary")
    if sev and summary:
        return [f"  {GLYPH_OK} {agent}: {sev} — {summary}"]
    return [f"  {GLYPH_OK} {agent}"]


@_register("gate_opened")
def _r_gate_opened(ev: dict[str, Any], cx: RenderContext) -> RenderResult:
    p = ev["payload"]
    node = ev.get("node_id") or ""
    prompt = p.get("prompt") or cx.label(node)
    auto = p.get("auto", False)
    ctx_fn = cx.gate_contexts.get(node)
    context_text = ""
    if ctx_fn is not None:
        try:
            context_text = ctx_fn(cx.completed)
        except Exception as e:  # noqa: BLE001
            context_text = f"<gate context error: {type(e).__name__}>"
    suffix = " (auto-approved for demo)" if auto else ""
    line = f"{GLYPH_GATE} Gate: {prompt}{suffix}"
    if context_text:
        return [line, f"     ↳ {context_text}"]
    return [line]


@_register("gate_resolved")
def _r_gate_resolved(ev: dict[str, Any], cx: RenderContext) -> RenderResult:
    p = ev["payload"]
    if p.get("auto"):
        # Already announced inline on gate_opened.
        return []
    choice = p.get("choice", "?")
    return [f"  → {choice}"]


@_register("context_pack_truncated")
def _r_context_pack_truncated(ev: dict[str, Any], cx: RenderContext) -> RenderResult:
    """No-op renderer.

    Truncation is observability metadata, not a story beat — operators
    inspect it via ``requiem events --raw`` rather than seeing it in the
    live narrated stream. Returning ``[]`` keeps the demo output focused
    on user-visible transitions (Demo Contract §3).
    """
    return []


@_register("cancel_requested")
def _r_cancel_requested(ev: dict[str, Any], cx: RenderContext) -> RenderResult:
    p = ev["payload"]
    reason = p.get("reason", "operator")
    who = p.get("requested_by", "cli")
    return [f"{GLYPH_END} Cancel requested by {who} — {reason}"]


@_register("subworkflow_started")
def _r_subworkflow_started(ev: dict[str, Any], cx: RenderContext) -> RenderResult:
    p = ev["payload"]
    module = p.get("sub_workflow_module") or "?"
    sub_run_id = p.get("sub_run_id") or "?"
    return [
        f"{GLYPH_ACTION} Spawning child workflow: {module} (sub-run: {sub_run_id})"
    ]


@_register("subworkflow_completed")
def _r_subworkflow_completed(ev: dict[str, Any], cx: RenderContext) -> RenderResult:
    p = ev["payload"]
    node = ev.get("node_id") or ""
    disposition = p.get("disposition", "?")
    outcome = p.get("outcome") or {}
    cx.completed[node] = outcome
    label = cx.label(node)
    detail_fn = cx.subworkflow_details.get(node)
    if disposition == "completed":
        if detail_fn is not None:
            try:
                detail = detail_fn(outcome)
            except Exception as e:  # noqa: BLE001
                detail = f"<sub-detail render error: {type(e).__name__}>"
            if detail:
                return [f"{GLYPH_OK} {label} — {detail}"]
            return [f"{GLYPH_OK} {label}"]
        return [f"{GLYPH_OK} Child workflow returned: {disposition} ({label})"]
    if disposition == "needs_human":
        # The parent's `gate_opened` event (queued right after) tells the
        # operator-facing story; suppress the duplicate line here.
        return []
    if disposition == "cancelled":
        return [f"{GLYPH_END} Child workflow returned: cancelled ({label})"]
    if detail_fn is not None:
        try:
            detail = detail_fn(outcome)
        except Exception as e:  # noqa: BLE001
            detail = f"<sub-detail render error: {type(e).__name__}>"
        if detail:
            return [f"{GLYPH_FAIL} {label} — {detail}"]
    return [f"{GLYPH_FAIL} Child workflow returned: {disposition} ({label})"]


@_register("subworkflow_cancelled")
def _r_subworkflow_cancelled(ev: dict[str, Any], cx: RenderContext) -> RenderResult:
    p = ev["payload"]
    sub_run_id = p.get("sub_run_id") or "?"
    return [f"{GLYPH_END} Child workflow cancelled ({sub_run_id})"]


@_register("run_completed")
def _r_run_completed(ev: dict[str, Any], cx: RenderContext) -> RenderResult:
    p = ev["payload"]
    terminal = (p.get("terminal") or "?").title()
    wf = (
        cx.active_workflow_stack.pop()
        if cx.active_workflow_stack
        else (cx.workflow_name or "workflow")
    )
    suffix = f" on {cx.artifact_name}" if cx.artifact_name else ""
    return [f"{GLYPH_END} {terminal} — {wf}{suffix}"]


# ---- public surface -----------------------------------------------


def render_event(ev: dict[str, Any], cx: RenderContext) -> RenderResult:
    """Dispatch an envelope to its registered renderer."""
    fn = EVENT_RENDERERS.get(ev.get("kind", ""))
    if fn is None:
        return [f"[unrendered kind: {ev.get('kind')!r}]"]
    return fn(ev, cx)


def style_for_line(line: str) -> str | None:
    """Pick a rich style for a rendered line based on its leading glyph."""
    if not line:
        return None
    head = line.lstrip()[:1]
    return GLYPH_COLOUR.get(head)


# ---- exit codes (Demo Contract §4 hard rule) ----------------------

EXIT_CODE_OK          = 0
EXIT_CODE_FAILED      = 1
EXIT_CODE_NEEDS_HUMAN = 2
EXIT_CODE_CANCELLED   = 130


def exit_code_for(result: RunResult) -> int:
    """Map a RunResult to a stable POSIX exit code.

    Completed → 0; Suspended (gate awaiting decision) → 2;
    Failed(cancelled) → 130 (SIGINT convention); other Failed → 1.
    """
    if isinstance(result, Completed):
        return EXIT_CODE_OK
    if isinstance(result, Suspended):
        return EXIT_CODE_NEEDS_HUMAN
    if isinstance(result, Failed):
        if result.error_kind == "cancelled":
            return EXIT_CODE_CANCELLED
        return EXIT_CODE_FAILED
    return EXIT_CODE_FAILED


__all__ = [
    "EVENT_KINDS",
    "EVENT_RENDERERS",
    "RenderContext",
    "RenderResult",
    "Renderer",
    "DetailFn",
    "GateContextFn",
    "SubworkflowDetailFn",
    "render_event",
    "style_for_line",
    "exit_code_for",
    "GLYPH_ACTION", "GLYPH_OK", "GLYPH_RETRY",
    "GLYPH_GATE", "GLYPH_FAIL", "GLYPH_END",
    "GLYPH_COLOUR", "OUTCOME_COLOUR",
    "EXIT_CODE_OK", "EXIT_CODE_FAILED",
    "EXIT_CODE_NEEDS_HUMAN", "EXIT_CODE_CANCELLED",
]
