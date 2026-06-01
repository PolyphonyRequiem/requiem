"""Root-dispatch workflow — Verdi-3 / Phase C (the SDLC pipeline entry).

The first stage of the full SDLC. Polyphony's equivalent is
``polyphony dispatch <item>``: it claims a work item, generates the
root run id, and emits the run-start banner. Everything that follows
(planning, implementation, PR lifecycle, close-out) hangs off that
root id.

For v0, dispatch is a single-script workflow that:

* fetches the work item via ``ctx.toolbelt.twig`` (or the closure-baked
  client supplied by ``build_engine``),
* synthesises a stable root run id of the form ``root-<item_id>-<date>``
  (date-stamped so a re-run on the same day is idempotent),
* emits ``Success`` with the dispatch envelope downstream stages read.

When ``auto_plan=False`` (the brief's default), this workflow stops at
``dispatch_recorded`` — it's the SDLC pipeline's caller's job to invoke
the planning stage. The full-SDLC composer (``full_sdlc``) does exactly
that via ``.subworkflow(...)``.

Public entry points:

* ``build_workflow() -> Workflow``
* ``build_engine(log_dir, *, inputs=None, ...) -> Engine``
* ``render_hints()`` / ``verdict_card(completed)``
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from requiem.agent import FakeProvider
from requiem.clients.twig import (
    TwigClientError,
    TwigItem,
    TwigItemNotFoundError,
)
from requiem.dsl import AgentRegistry, VerbRegistry, Workflow, WorkflowBuilder
from requiem.kernel import Engine
from requiem.outcomes import PermanentFailure, Success
from requiem.toolbelt import Toolbelt


# ---- inputs ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DispatchInputs:
    item_id: int = 12345
    repo: str = "PolyphonyRequiem/requiem"
    auto_plan: bool = False
    """Reserved: when True, root_dispatch would chain straight into
    planning. v0 leaves this False — the full-SDLC composer chains."""
    today: date | None = None
    """Pinned date for deterministic root_run_id in tests."""


# ---- demo twig client (matches FakeTwigClient method shape) --------


@dataclass
class _DemoTwigClient:
    item_id: int = 12345
    title: str = "Refactor outcome dispatch in kernel"
    state: str = "Active"
    raise_on_show: Exception | None = None
    show_calls: list[int] = field(default_factory=list)

    def show(self, item_id: int) -> TwigItem:
        self.show_calls.append(item_id)
        if self.raise_on_show is not None:
            raise self.raise_on_show
        return TwigItem(
            id=item_id,
            title=self.title,
            state=self.state,
            area_path="Requiem\\Phase C",
            work_item_type="Task",
            parent_id=None,
            raw={"id": item_id, "title": self.title, "state": self.state},
        )


# ---- verbs ----------------------------------------------------------


class _TwigShowProto(Protocol):
    def show(self, item_id: int) -> TwigItem: ...


def _root_run_id(item_id: int, today: date | None) -> str:
    d = today or datetime.now(tz=timezone.utc).date()
    return f"root-{item_id}-{d.isoformat()}"


def build_verb_registry(inputs: DispatchInputs, *, twig: _TwigShowProto) -> VerbRegistry:
    verbs = VerbRegistry()

    @verbs.register("claim_item")
    def _claim(ctx):
        try:
            item = twig.show(inputs.item_id)
        except TwigItemNotFoundError as e:
            return PermanentFailure(
                error_kind="dispatch.item_not_found",
                message=f"work item {inputs.item_id} not found: {e}",
            )
        except TwigClientError as e:
            return PermanentFailure(
                error_kind="dispatch.twig_unknown",
                message=f"twig.show({inputs.item_id}) failed: {e}",
            )
        rrid = _root_run_id(inputs.item_id, inputs.today)
        return Success(
            value={
                "root_run_id": rrid,
                "item_id": item.id,
                "title": item.title,
                "state": item.state,
                "repo": inputs.repo,
                "auto_plan": inputs.auto_plan,
            },
            inspected_artifacts=(f"twig:item:{item.id}",),
        )

    @verbs.register("record_dispatch")
    def _record(ctx):
        claim = ctx.completed["start"]["value"]
        return Success(
            value={
                "root_run_id": claim["root_run_id"],
                "item_id": claim["item_id"],
                "title": claim["title"],
                "repo": claim["repo"],
                "stage": "dispatched",
            }
        )

    return verbs


# ---- workflow -------------------------------------------------------


def build_workflow() -> Workflow:
    return (
        WorkflowBuilder(
            "root-dispatch",
            module="requiem.workflows.root_dispatch",
            version="0.1",
        )
            .entry("start")
            .script("start", verb="claim_item")
                .edge("start", on="success", to="record")
                .edge("start", on="permanent_failure", to="fail_end")
            .script("record", verb="record_dispatch")
                .edge("record", on="success", to="end")
            .terminate("end", disposition="completed")
            .terminate("fail_end", disposition="failed")
            .humanize({
                "start":    "Claim AB work item",
                "record":   "Allocate root run id",
                "end":      "root-dispatch",
                "fail_end": "root-dispatch",
            })
            .build()
    )


# ---- engine factory + demo wiring ------------------------------------


def _default_gate_handler(node_id: str, prompt: str, options: tuple[str, ...]) -> str:
    return options[0] if options else ""


_default_gate_handler.__requiem_auto__ = True  # type: ignore[attr-defined]


def build_engine(
    log_dir: Path,
    *,
    inputs: DispatchInputs | None = None,
    twig: _TwigShowProto | None = None,
    gate_handler: Callable[[str, str, tuple[str, ...]], str] | None = None,
) -> Engine:
    if inputs is None:
        inputs = DispatchInputs()
    if twig is None:
        twig = _DemoTwigClient(item_id=inputs.item_id)
    return Engine(
        workflow=build_workflow(),
        verbs=build_verb_registry(inputs, twig=twig),
        agents=AgentRegistry(),
        provider=FakeProvider(),
        toolbelt=Toolbelt.real(),
        log_dir=log_dir,
        gate_handler=gate_handler or _default_gate_handler,
    )


# ---- render hooks ---------------------------------------------------


def _detail_start(value: dict) -> str:
    title = value.get("title") or "?"
    return f"AB#{value.get('item_id', '?')} — {title!r}"


def _detail_record(value: dict) -> str:
    return f"root_run_id={value.get('root_run_id', '?')}"


def render_hints() -> dict:
    return {
        "artifact_name": "AB work item",
        "details": {
            "start":  _detail_start,
            "record": _detail_record,
        },
        "silent_nodes": frozenset({"end", "fail_end"}),
    }


def verdict_card(completed: dict) -> str | None:
    rec = (completed.get("record") or {}).get("value") or {}
    if not rec:
        return None
    return (
        "─── Dispatched ──────────────────────────────────────────────────────\n"
        f"  ✓ AB#{rec.get('item_id', '?')} — {rec.get('title', '?')!r}\n"
        f"      root_run_id: {rec.get('root_run_id', '?')}\n"
        f"      repo:        {rec.get('repo', '?')}\n"
        "─────────────────────────────────────────────────────────────────────"
    )


__all__ = [
    "DispatchInputs",
    "build_workflow",
    "build_engine",
    "build_verb_registry",
    "render_hints",
    "verdict_card",
]
