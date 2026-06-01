"""Assertion helpers — free functions + `ScenarioResult` method delegates.

Every helper either passes silently or raises ``AssertionError`` with a
diagnostic message that names the run and quotes the offending events.
The helpers never modify the result; they only read it. They are
designed to be called from outside the harness (``from
requiem.harness import assert_completed``) as well as from inside
``ScenarioResult.assert_*`` methods.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

from requiem.kernel import Completed, Failed, Suspended

if TYPE_CHECKING:  # avoid runtime circular import
    from requiem.harness.scenario import ScenarioResult


def _kinds(result: "ScenarioResult", kind: str) -> list[dict]:
    return [e for e in result.events if e.get("kind") == kind]


def assert_completed(
    result: "ScenarioResult",
    *,
    disposition: str | None = None,
    final_node: str | None = None,
) -> None:
    """Pass iff the run finished with `Completed`. Optionally pin disposition / final node."""
    raw = result.raw
    if not isinstance(raw, Completed):
        raise AssertionError(
            f"run {result.run_id!r}: expected Completed, got "
            f"{type(raw).__name__}({raw!r})"
        )
    if disposition is not None and raw.disposition != disposition:
        raise AssertionError(
            f"run {result.run_id!r}: expected disposition {disposition!r}, "
            f"got {raw.disposition!r}"
        )
    if final_node is not None and raw.final_node != final_node:
        raise AssertionError(
            f"run {result.run_id!r}: expected final_node {final_node!r}, "
            f"got {raw.final_node!r}"
        )


def assert_needs_human(
    result: "ScenarioResult", *, gate: str | None = None
) -> None:
    """Pass iff the run suspended at a human gate. Optionally pin gate id."""
    raw = result.raw
    if not isinstance(raw, Suspended):
        raise AssertionError(
            f"run {result.run_id!r}: expected Suspended (NeedsHuman), got "
            f"{type(raw).__name__}({raw!r})"
        )
    if gate is not None and raw.node_id != gate:
        raise AssertionError(
            f"run {result.run_id!r}: expected suspension at {gate!r}, "
            f"got {raw.node_id!r}"
        )


def assert_visited_nodes(
    result: "ScenarioResult",
    nodes: Iterable[str],
    *,
    exact: bool = False,
    in_order: bool = True,
) -> None:
    """Assert every named node was entered.

    Defaults to **ordered subsequence**: the named nodes must appear in
    the given order, possibly interleaved with unnamed nodes. Set
    ``exact=True`` to require the entered sequence equals ``nodes``
    exactly. Set ``in_order=False`` to require set membership only.
    """
    visited = [e.get("node_id") for e in _kinds(result, "node_entered")]
    expected = list(nodes)
    if exact:
        if visited != expected:
            raise AssertionError(
                f"run {result.run_id!r}: visited sequence {visited!r} "
                f"!= expected {expected!r}"
            )
        return
    if not in_order:
        missing = [n for n in expected if n not in visited]
        if missing:
            raise AssertionError(
                f"run {result.run_id!r}: never entered nodes {missing!r}; "
                f"visited={visited!r}"
            )
        return
    # ordered-subsequence default
    i = 0
    for n in visited:
        if i < len(expected) and n == expected[i]:
            i += 1
    if i != len(expected):
        raise AssertionError(
            f"run {result.run_id!r}: expected ordered subsequence "
            f"{expected!r} not found in visited={visited!r} "
            f"(stuck at expected[{i}]={expected[i]!r})"
        )


def assert_no_retry(result: "ScenarioResult") -> None:
    retries = _kinds(result, "retry_attempted")
    if retries:
        first = retries[0]
        raise AssertionError(
            f"run {result.run_id!r}: expected zero retries but found "
            f"{len(retries)} (first at node {first.get('node_id')!r}: "
            f"{first.get('payload', {}).get('reason')!r})"
        )


def assert_cancelled(result: "ScenarioResult") -> None:
    """Pass iff the run terminated cancelled.

    A cancel may surface as:
    * `Failed(error_kind="cancelled")` — the kernel's RunResult shape, OR
    * the `Completed` shape if a future cancel path returns one (defensive).
    """
    raw = result.raw
    if isinstance(raw, Failed) and raw.error_kind == "cancelled":
        return
    completed = _kinds(result, "run_completed")
    if completed and completed[-1].get("payload", {}).get("terminal") == "cancelled":
        return
    raise AssertionError(
        f"run {result.run_id!r}: expected cancelled, got "
        f"{type(raw).__name__}({raw!r}); "
        f"last run_completed payload={completed[-1] if completed else None!r}"
    )


def assert_short_circuited(result: "ScenarioResult") -> None:
    """Pass iff INV-CANCEL-SHORT-CIRCUITS-RETRY held.

    Concretely: no `retry_attempted` event appears after the first
    `cancel_requested` event (or after the verb-completed event that
    carried the `Cancelled` outcome — whichever came first).
    """
    cancel_idx: int | None = None
    for i, e in enumerate(result.events):
        kind = e.get("kind")
        if kind == "cancel_requested":
            cancel_idx = i
            break
        if kind == "verb_completed":
            o = e.get("payload", {}).get("outcome", {}) or {}
            if o.get("kind") == "cancelled":
                cancel_idx = i
                break
    if cancel_idx is None:
        raise AssertionError(
            f"run {result.run_id!r}: assert_short_circuited called but no "
            f"cancel signal was observed in the event log"
        )
    post_cancel_retries = [
        e
        for e in result.events[cancel_idx + 1 :]
        if e.get("kind") == "retry_attempted"
    ]
    if post_cancel_retries:
        raise AssertionError(
            f"run {result.run_id!r}: INV-CANCEL-SHORT-CIRCUITS-RETRY violated — "
            f"{len(post_cancel_retries)} retry_attempted event(s) after cancel "
            f"(first at event_id={post_cancel_retries[0].get('event_id')})"
        )


def assert_terminal_state_matches(
    first: "ScenarioResult", second: "ScenarioResult"
) -> None:
    """Pass iff two runs reached the same terminal disposition + final node.

    Used by INV-RESTART resume tests: full-run result vs resumed-run
    result must be indistinguishable to a downstream consumer that
    only sees the terminal projection.
    """
    a, b = first.raw, second.raw
    if type(a) is not type(b):
        raise AssertionError(
            f"terminal mismatch: {type(a).__name__} vs {type(b).__name__}"
        )
    if isinstance(a, Completed) and isinstance(b, Completed):
        if (a.disposition, a.final_node) != (b.disposition, b.final_node):
            raise AssertionError(
                f"terminal mismatch: ({a.disposition!r}, {a.final_node!r}) "
                f"vs ({b.disposition!r}, {b.final_node!r})"
            )
        return
    if isinstance(a, Suspended) and isinstance(b, Suspended):
        if (a.node_id, a.options) != (b.node_id, b.options):
            raise AssertionError(
                f"suspension mismatch: ({a.node_id!r}, {a.options!r}) "
                f"vs ({b.node_id!r}, {b.options!r})"
            )
        return
    if isinstance(a, Failed) and isinstance(b, Failed):
        if (a.node_id, a.error_kind) != (b.node_id, b.error_kind):
            raise AssertionError(
                f"failure mismatch: ({a.node_id!r}, {a.error_kind!r}) "
                f"vs ({b.node_id!r}, {b.error_kind!r})"
            )
        return
    raise AssertionError(  # pragma: no cover — defensive
        f"terminal types matched but no comparison arm fired: {a!r} vs {b!r}"
    )
