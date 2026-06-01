"""Renderer registry exhaustiveness — Demo Contract enforcement.

Every event kind the kernel emits must have a registered renderer. If a
new kind is added without a renderer, this test fails before any demo
output can leak `[unrendered kind: ...]` to a customer.
"""
from __future__ import annotations

from requiem.cli.render import EVENT_RENDERERS, RenderContext, render_event
from requiem.events import EVENT_KINDS


def test_every_event_kind_has_a_renderer():
    missing = EVENT_KINDS - set(EVENT_RENDERERS.keys())
    assert not missing, f"event kinds without a renderer: {sorted(missing)}"


def test_no_renderer_for_unknown_kind():
    extra = set(EVENT_RENDERERS.keys()) - EVENT_KINDS
    assert not extra, (
        f"renderers registered for kinds not in EVENT_KINDS: {sorted(extra)} "
        "(add to events.EVENT_KINDS or remove the renderer)"
    )


def _envelope(kind: str, **payload):
    """Minimal envelope helper."""
    return {
        "event_id": 1,
        "run_id": "r",
        "ts": "2026-05-31T00:00:00+00:00",
        "kind": kind,
        "schema_version": 1,
        "node_id": payload.pop("_node", None),
        "team_id": payload.pop("_team", None),
        "agent_id": payload.pop("_agent", None),
        "payload": payload,
    }


def test_each_renderer_returns_list_of_strings():
    cx = RenderContext(workflow_name="demo", artifact_name="x.py")
    cx.attempts["n"] = 1
    samples = {
        "run_started":           _envelope("run_started", workflow="demo"),
        "node_entered":          _envelope("node_entered", _node="n", attempt=1),
        "verb_invoked":          _envelope("verb_invoked", _node="n", verb="v"),
        "verb_completed":        _envelope(
            "verb_completed", _node="n",
            outcome={"kind": "success", "value": {}},
        ),
        "retry_attempted":       _envelope(
            "retry_attempted", _node="n", attempt=1, next_attempt=2,
            reason="transient",
        ),
        "route_taken":           _envelope("route_taken", _node="n", key="success", to_node="m"),
        "team_dispatched":       _envelope(
            "team_dispatched", _node="t", _team="reviewers",
            branches=["a", "b"],
        ),
        "team_branch_completed": _envelope(
            "team_branch_completed", _node="t", _team="reviewers", _agent="a",
            outcome={"kind": "success", "value": {}},
        ),
        "gate_opened":           _envelope(
            "gate_opened", _node="g", prompt="ok?", options=["yes", "no"],
            context={}, auto=True,
        ),
        "gate_resolved":         _envelope(
            "gate_resolved", _node="g", choice="yes", auto=True,
        ),
        "run_completed":         _envelope(
            "run_completed", terminal="completed", final_node="end",
        ),
    }
    for kind, ev in samples.items():
        out = render_event(ev, cx)
        assert isinstance(out, list), f"{kind} renderer must return list"
        for line in out:
            assert isinstance(line, str), f"{kind} renderer produced non-str: {line!r}"


def test_retry_collapses_into_single_retry_line():
    """RetryableFailure verb_completed is suppressed; retry_attempted speaks."""
    cx = RenderContext(workflow_name="d", humanize={"flaky_lint": "Lint"})
    cx.attempts["flaky_lint"] = 1
    vc = _envelope(
        "verb_completed", _node="flaky_lint",
        outcome={"kind": "retryable_failure", "error_kind": "lint.transient",
                 "message": "linter OOM", "retry_key": "k", "attempt": 1},
    )
    ra = _envelope(
        "retry_attempted", _node="flaky_lint", attempt=1, next_attempt=2,
        reason="linter OOM",
    )
    assert render_event(vc, cx) == []
    [line] = render_event(ra, cx)
    assert "Lint" in line and "retrying" in line and "attempt 2" in line


def test_post_retry_success_says_attempt_n():
    cx = RenderContext(workflow_name="d", humanize={"flaky_lint": "Lint"})
    cx.attempts["flaky_lint"] = 2
    ev = _envelope(
        "verb_completed", _node="flaky_lint",
        outcome={"kind": "success", "value": {}},
    )
    [line] = render_event(ev, cx)
    assert "Lint" in line and "attempt 2" in line


def test_auto_gate_renders_inline_suffix_and_suppresses_resolved():
    cx = RenderContext(workflow_name="d")
    opened = _envelope(
        "gate_opened", _node="g", prompt="approve verdict?",
        options=["approve", "reject"], context={}, auto=True,
    )
    resolved = _envelope(
        "gate_resolved", _node="g", choice="approve", auto=True,
    )
    [line] = render_event(opened, cx)
    assert "auto-approved for demo" in line
    assert render_event(resolved, cx) == []


def test_gate_context_callback_renders_secondary_line():
    cx = RenderContext(
        workflow_name="d",
        gate_contexts={"g": lambda c: f"verdict: {c['s']['v']}"},
    )
    cx.completed["s"] = {"v": "don't merge"}
    ev = _envelope(
        "gate_opened", _node="g", prompt="ok?", options=["yes"],
        context={}, auto=False,
    )
    out = render_event(ev, cx)
    assert any("don't merge" in line for line in out)


def test_unknown_kind_is_clearly_flagged():
    cx = RenderContext(workflow_name="d")
    ev = _envelope("not_a_real_kind")
    [line] = render_event(ev, cx)
    assert "unrendered kind" in line


def test_humanize_falls_back_to_node_id():
    cx = RenderContext(workflow_name="d")
    assert cx.label("never_seen") == "never_seen"
    assert cx.label(None) == "—"


def test_exit_codes():
    from requiem.cli.render import (
        EXIT_CODE_CANCELLED, EXIT_CODE_FAILED, EXIT_CODE_NEEDS_HUMAN,
        EXIT_CODE_OK, exit_code_for,
    )
    from requiem.kernel import Completed, Failed, Suspended

    assert exit_code_for(Completed("r", "completed", "end", {})) == EXIT_CODE_OK
    assert exit_code_for(Suspended("r", "g", "p", ("a",))) == EXIT_CODE_NEEDS_HUMAN
    assert exit_code_for(Failed("r", "n", "cancelled", "operator cancel")) == EXIT_CODE_CANCELLED
    assert exit_code_for(Failed("r", "n", "verb.crash", "boom")) == EXIT_CODE_FAILED
    assert exit_code_for(Failed("r", "n", "bad_output", "boom")) == EXIT_CODE_FAILED
