"""`Scenario` + `ScenarioResult` + `run_scenario` — the harness entry surface.

A `Scenario` declares everything the harness needs to drive one engine
run: which workflow to import, which inputs to seed it with, what each
scripted agent says, what each scripted tool call returns, and which
human-gate option to auto-pick. `run_scenario(scn)` constructs the
engine via the workflow module's `build_engine` factory, swaps in the
scripted provider + toolbelt, runs it to completion / suspension /
failure, and packages the result with the full event log.

The factory contract a workflow module MUST honour to be harness-driven:

    def build_engine(log_dir: Path, **kwargs) -> Engine

That's it. The harness will pass:

    build_engine(log_dir=..., **scenario.inputs, **scenario.extra_engine_kwargs)

and then override ``engine.provider``, ``engine.toolbelt``, and
``engine.gate_handler`` from the scenario. Workflows that do not accept
the inputs you pass should declare them in ``extra_engine_kwargs`` (which
is documented as the workflow-specific escape hatch).
"""
from __future__ import annotations

import importlib
import inspect
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from requiem.harness import assertions as _A
from requiem.harness.fakes import FakeAgent, FakeToolbelt
from requiem.kernel import Completed, Engine, Failed, RunResult, Suspended
from requiem.persistence import EventStore, replay


# ---- scenario ---------------------------------------------------------


GateChoice = str | dict[str, str] | Callable[[str, str, tuple[str, ...]], str]


@dataclass(frozen=True)
class Scenario:
    """A single harness scenario — pure data; reusable across runs."""

    workflow: str
    """Importable module path, e.g. ``"requiem.workflows.code_review_demo"``."""

    inputs: dict[str, Any] = field(default_factory=dict)
    """Forwarded to ``build_engine(log_dir, **inputs)``."""

    agent_outputs: dict[str, Any] = field(default_factory=dict)
    """``{agent_name: dict | list[dict | Outcome]}``. See `FakeAgent`."""

    tool_outputs: dict[tuple, Any] = field(default_factory=dict)
    """``{(tool, method, *args): value}``. See `FakeToolbelt`."""

    extra_engine_kwargs: dict[str, Any] = field(default_factory=dict)
    """Workflow-specific kwargs `inputs` doesn't cover (e.g. ``snippet_path``)."""

    expected_terminal: str | None = None
    """Optional pin for `assert_completed(disposition=...)` defaulting."""

    expected_disposition: str | None = None
    """Alias for `expected_terminal` per the brief's example."""

    gate_choices: GateChoice | None = "approve"
    """How human gates resolve. ``str`` → always pick this option (falls back
    to first option if the named choice isn't offered). ``dict`` → per-node
    map ``{node_id: choice}``. Callable → user-supplied gate handler
    matching the engine's ``GateHandler`` shape. ``None`` → no handler;
    the kernel returns `Suspended` on the first gate."""

    run_id: str = "scenario"
    """Default run_id; `Harness.run` may override per-call."""


def scenario(**kwargs: Any) -> Scenario:
    """Build a Scenario; thin sugar over ``Scenario(**kwargs)``."""
    return Scenario(**kwargs)


# ---- result ----------------------------------------------------------


@dataclass
class ScenarioResult:
    """The result of a single harness run.

    Carries the raw `RunResult`, the full event log, and convenience
    assertion methods that delegate to ``requiem.harness.assertions``.
    """

    scenario: Scenario
    run_id: str
    log_path: Path
    raw: RunResult
    events: list[dict]
    agent: FakeAgent
    toolbelt_scripts: dict[tuple, Any]

    @property
    def visited(self) -> list[str]:
        return [e.get("node_id") for e in self.events if e.get("kind") == "node_entered"]

    @property
    def retries(self) -> int:
        return sum(1 for e in self.events if e.get("kind") == "retry_attempted")

    @property
    def agent_calls(self) -> list[str]:
        return [c["agent"] for c in self.agent.calls]

    # ---- thin method-style delegates --------------------------------

    def assert_completed(
        self, *, disposition: str | None = None, final_node: str | None = None
    ) -> None:
        d = disposition or self.scenario.expected_terminal or self.scenario.expected_disposition
        _A.assert_completed(self, disposition=d, final_node=final_node)

    def assert_needs_human(self, *, gate: str | None = None) -> None:
        _A.assert_needs_human(self, gate=gate)

    def assert_visited(
        self, nodes: Iterable[str], *, exact: bool = False, in_order: bool = True
    ) -> None:
        _A.assert_visited_nodes(self, nodes, exact=exact, in_order=in_order)

    # alias to match the brief's exact spelling
    def assert_visited_nodes(
        self, nodes: Iterable[str], *, exact: bool = False, in_order: bool = True
    ) -> None:
        _A.assert_visited_nodes(self, nodes, exact=exact, in_order=in_order)

    def assert_no_retry(self) -> None:
        _A.assert_no_retry(self)

    def assert_cancelled(self) -> None:
        _A.assert_cancelled(self)

    def assert_short_circuited(self) -> None:
        _A.assert_short_circuited(self)

    def assert_terminal_state_matches(self, other: "ScenarioResult") -> None:
        _A.assert_terminal_state_matches(self, other)


# ---- engine construction --------------------------------------------


def _import_build_engine(module_path: str) -> Callable[..., Engine]:
    mod = importlib.import_module(module_path)
    fn = getattr(mod, "build_engine", None)
    if fn is None:
        raise AttributeError(
            f"workflow module {module_path!r} has no `build_engine` factory; "
            f"the harness contract requires `build_engine(log_dir, **kwargs) -> Engine`"
        )
    return fn


def _filter_kwargs(fn: Callable[..., Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    """Pass only kwargs the factory actually accepts.

    Workflows shouldn't have to swallow every scenario input; we filter
    to the factory's signature. ``**kwargs`` factories receive everything.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):  # pragma: no cover — builtin / C-extension factory
        return kwargs
    if any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    ):
        return kwargs
    accepted = {
        name
        for name, p in sig.parameters.items()
        if p.kind
        in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
    }
    return {k: v for k, v in kwargs.items() if k in accepted}


def _make_gate_handler(
    workflow,
    gate_choices: GateChoice | None,
) -> Callable[[str, str, tuple[str, ...]], str] | None:
    """Translate the scenario `gate_choices` into an engine `GateHandler`.

    Returning ``None`` instructs the kernel to surface `Suspended` for
    any gate, so test code can assert the gate-shape contract without
    auto-resolving.
    """
    if gate_choices is None:
        return None

    if callable(gate_choices):
        return gate_choices  # user-supplied

    if isinstance(gate_choices, dict):
        choices = dict(gate_choices)

        def _per_gate(node_id: str, prompt: str, options: tuple[str, ...]) -> str:
            if node_id in choices:
                pick = choices[node_id]
            elif options:
                pick = options[0]
            else:  # pragma: no cover — kernel always supplies options
                pick = ""
            return pick

        _per_gate.__requiem_auto__ = True  # type: ignore[attr-defined]
        return _per_gate

    # str fallback — always pick that option, else first
    desired = gate_choices

    def _always(node_id: str, prompt: str, options: tuple[str, ...]) -> str:
        if desired in options:
            return desired
        return options[0] if options else desired

    _always.__requiem_auto__ = True  # type: ignore[attr-defined]
    return _always


def _build_engine_for(
    scn: Scenario,
    *,
    log_dir: Path,
    gate_handler: Callable[[str, str, tuple[str, ...]], str] | None = None,
) -> tuple[Engine, FakeAgent, dict[tuple, Any]]:
    """Construct the engine, override its seams, return (engine, agent, scripts)."""
    factory = _import_build_engine(scn.workflow)
    merged_kwargs: dict[str, Any] = {**scn.inputs, **scn.extra_engine_kwargs}
    accepted = _filter_kwargs(factory, merged_kwargs)
    engine = factory(log_dir=log_dir, **accepted)

    agent = FakeAgent.from_outputs(scn.agent_outputs)
    engine.provider = agent

    if scn.tool_outputs:
        engine.toolbelt = FakeToolbelt(dict(scn.tool_outputs)).build()  # type: ignore[assignment]

    handler = gate_handler if gate_handler is not None else _make_gate_handler(engine.workflow, scn.gate_choices)
    engine.gate_handler = handler
    return engine, agent, dict(scn.tool_outputs)


# ---- cancel injector ------------------------------------------------


class _CancelAfter:
    """Observer that writes a `cancel_requested` event after N events.

    The engine's `on_event` is called once per durably-appended event;
    after the Nth call, we append a `cancel_requested` envelope directly
    via a sibling EventStore. The kernel's next loop tick reads the
    tail of the log via `_pending_cancel` and terminates the run.
    """

    def __init__(self, log_path: Path, after: int) -> None:
        self.log_path = log_path
        self.after = after
        self._seen = 0
        self._fired = False

    def __call__(self, envelope: dict[str, Any]) -> None:
        self._seen += 1
        if self._fired or self._seen < self.after:
            return
        store = EventStore(self.log_path)
        store.append(
            {
                "run_id": envelope.get("run_id"),
                "ts": envelope.get("ts"),
                "kind": "cancel_requested",
                "schema_version": envelope.get("schema_version", 1),
                "node_id": None,
                "team_id": None,
                "agent_id": None,
                "payload": {
                    "reason": "harness.cancel_after_event",
                    "requested_by": "requiem.harness",
                    "after_event": self.after,
                },
            }
        )
        self._fired = True


# ---- run_scenario ---------------------------------------------------


def run_scenario(
    scn: Scenario,
    *,
    log_dir: Path | None = None,
    run_id: str | None = None,
    cancel_after_event: int | None = None,
) -> ScenarioResult:
    """Run one scenario to completion / suspension / failure.

    `log_dir` defaults to a fresh tempdir per call (so callers that
    don't want to manage tmp paths get isolation for free). Tests
    typically prefer `Harness(log_dir=tmp_path)` for inspection.
    """
    import asyncio

    own_tmp = False
    if log_dir is None:
        log_dir = Path(tempfile.mkdtemp(prefix="requiem-harness-"))
        own_tmp = True
    log_dir.mkdir(parents=True, exist_ok=True)
    rid = run_id or scn.run_id

    engine, agent, tool_scripts = _build_engine_for(scn, log_dir=log_dir)
    log_path = engine.log_path(rid)
    if cancel_after_event is not None:
        engine.on_event = _CancelAfter(log_path, cancel_after_event)

    raw = asyncio.run(engine.run(rid))
    events = list(replay(log_path))

    result = ScenarioResult(
        scenario=scn,
        run_id=rid,
        log_path=log_path,
        raw=raw,
        events=events,
        agent=agent,
        toolbelt_scripts=tool_scripts,
    )

    if own_tmp:
        # Don't auto-clean; pytest captures via tmp_path normally and
        # the user may want to inspect on failure. The OS reaps `/tmp`.
        pass
    return result
