"""End-to-end tests for the close-out workflow.

Pattern: real engine, fake clients, scripted FakeProvider. The fakes
match the real client's typed errors so verbs exercise their production
outcome-translation arms even though no subprocess fires.

Covers (the brief, every box):

* Happy path: 3 criteria, all met → closeout file written, item closed
* Partial verifier verdict (2/3) → NeedsHuman with gaps
* PR not merged → NeedsHuman immediately
* PR not linked → NeedsHuman ("PR not linked")
* Verifier BadOutput → NeedsHuman, NO auto-retry
* INV-RESTART: kill mid-run, resume → identical terminal + identical
  CloseOutResult + no double mutation
* Dry-run: same verdict, no fs.write_text, no twig.set_state_async
* Topology + render-hints sanity checks
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from requiem.agent import FakeProvider
from requiem.events import EventEmitter
from requiem.kernel import Completed, Failed
from requiem.outcomes import BadOutput
from requiem.persistence import EventStore, replay
from requiem.toolbelt import RealFileClient, RealGitClient, Toolbelt
from requiem.workflows.close_out import (
    CloseOutInputs,
    CloseOutResult,
    GATE_BAD_OUTPUT,
    GATE_CRITERIA_GAPS,
    GATE_PR_NOT_LINKED,
    GATE_PR_NOT_MERGED,
    VerifierOutput,
    build_engine,
    build_workflow,
    close_out_result,
    render_hints,
    verdict_card,
)

from tests.fakes.clients import (
    FakeFilesystemClient,
    FakeGhClient,
    FakeTwigClient,
    make_criterion,
    make_pr,
    make_twig_item,
)


# ---- gate handlers --------------------------------------------------


def _abort_handler(node_id: str, prompt: str, options: tuple[str, ...]) -> str:
    """Always pick a stop-the-run option: `abort`, then `reject`, then first."""
    if "abort" in options:
        return "abort"
    if "reject" in options:
        return "reject"
    return options[0]


_abort_handler.__requiem_auto__ = True  # type: ignore[attr-defined]


def _close_anyway_handler(node_id: str, prompt: str, options: tuple[str, ...]) -> str:
    if "close_anyway" in options:
        return "close_anyway"
    if "abort" in options:
        return "abort"
    return options[0]


_close_anyway_handler.__requiem_auto__ = True  # type: ignore[attr-defined]


# ---- builders -------------------------------------------------------


def _toolbelt(*, twig=None, gh=None, fs=None) -> Toolbelt:
    return Toolbelt(
        git=RealGitClient(),
        files=RealFileClient(),
        twig=twig,  # type: ignore[arg-type]
        gh=gh,
        fs=fs,  # type: ignore[arg-type]
    )


def _scripted_verifier(
    *,
    overall: str = "all_met",
    met: list[int],
    unmet: list[dict[str, Any]] | None = None,
    notes: str = "test",
    bad: bool = False,
) -> FakeProvider:
    """FakeProvider that returns one VerifierOutput shape."""
    if bad:
        # Deliberately invalid payload — fails Pydantic validation,
        # produces a BadOutput from FakeProvider.
        entry: Any = {"overall": "definitely_not_a_literal", "met_criteria": "not-a-list"}
    else:
        entry = {
            "overall": overall,
            "met_criteria": met,
            "unmet_criteria": unmet or [],
            "notes": notes,
        }
    return FakeProvider(scripts={"verifier": [entry]})


def _engine(
    log_dir: Path,
    *,
    item_id: int = 12345,
    repo: str = "acme/widgets",
    pr_number: int | None = 347,
    dry_run: bool = False,
    twig=None,
    gh=None,
    fs=None,
    provider: FakeProvider | None = None,
    gate_handler=_abort_handler,
    closeout_dir: Path | None = None,
):
    return build_engine(
        log_dir,
        item_id=item_id,
        repo=repo,
        pr_number=pr_number,
        dry_run=dry_run,
        closeout_dir=closeout_dir or Path("docs/closeouts"),
        toolbelt=_toolbelt(twig=twig, gh=gh, fs=fs),
        provider=provider or _scripted_verifier(met=[]),
        gate_handler=gate_handler,
        now_fn=lambda: datetime(2026, 6, 1, 9, 0, 0, tzinfo=timezone.utc),
    )


def _completed_projection(log_path: Path) -> dict[str, dict[str, Any]]:
    completed: dict[str, dict[str, Any]] = {}
    for ev in replay(log_path):
        if ev.get("kind") == "verb_completed":
            completed[ev["node_id"]] = ev["payload"]["outcome"]
    return completed


def _final_node(log_path: Path) -> str:
    for ev in reversed(list(replay(log_path))):
        if ev.get("kind") == "run_completed":
            return ev["payload"].get("final_node") or ""
    return ""


# ---- topology sanity ------------------------------------------------


def test_workflow_builds_without_topology_errors():
    wf = build_workflow()
    assert wf.validate_topology() == []
    assert wf.entry == "start"
    assert any(n.node_id == "end_success" for n in wf.nodes)
    assert any(n.node_id == "end_failed" for n in wf.nodes)
    assert any(n.node_id == "end_human" for n in wf.nodes)


def test_render_hints_humanizes_every_visible_node():
    wf = build_workflow()
    hints = render_hints()
    silent = hints["silent_nodes"]
    for n in wf.nodes:
        if n.node_id in silent:
            continue
        assert n.node_id in wf.humanize, f"missing humanize entry: {n.node_id}"


def test_verifier_output_pydantic_contract():
    """Round-trip the brief's VerifierOutput shape through Pydantic."""
    payload = {
        "overall": "partial",
        "met_criteria": [101, 102],
        "unmet_criteria": [
            {"criterion_id": 103, "criterion_title": "X", "gap": "no test"},
        ],
        "notes": "ok",
    }
    out = VerifierOutput.model_validate(payload)
    assert out.overall == "partial"
    assert out.met_criteria == [101, 102]
    assert out.unmet_criteria[0].criterion_id == 103


# ---- happy path: 3 criteria, all met -------------------------------


async def test_happy_path_all_criteria_met_writes_closeout_and_closes(tmp_path: Path):
    log_dir = tmp_path / "runs"
    closeout_dir = tmp_path / "docs" / "closeouts"
    parent = make_twig_item(
        item_id=12345,
        title="Refactor outcome dispatch",
        state="In Review",
        linked_prs=[{"repo": "acme/widgets", "number": 347, "title": ""}],
    )
    crit1 = make_criterion(22001, "kernel routes BadOutput", parent_id=12345)
    crit2 = make_criterion(22002, "all 6 outcomes covered", parent_id=12345)
    crit3 = make_criterion(22003, "dispatch table extensible", parent_id=12345)
    twig = FakeTwigClient(
        items={12345: parent, 22001: crit1, 22002: crit2, 22003: crit3},
        children_by_parent={12345: [22001, 22002, 22003]},
    )
    pr = make_pr(number=347, merged=True)
    gh = FakeGhClient(pr_by_number={("acme/widgets", 347): pr})
    fs = FakeFilesystemClient()

    provider = _scripted_verifier(
        overall="all_met",
        met=[22001, 22002, 22003],
        unmet=[],
        notes="All three criteria observed in PR diff and tests.",
    )

    engine = _engine(
        log_dir, item_id=12345, pr_number=347,
        twig=twig, gh=gh, fs=fs,
        provider=provider,
        closeout_dir=closeout_dir,
    )
    result = await engine.run("happy")
    assert isinstance(result, Completed), result
    assert result.final_node == "end_success"
    assert result.disposition == "completed"

    # Closeout markdown was written, with the brief's shape.
    expected_path = closeout_dir / "AB-12345.md"
    assert expected_path in fs.files
    body = fs.files[expected_path]
    assert "# AB#12345 — Refactor outcome dispatch" in body
    assert "**PR:** #347" in body
    assert "**Merge SHA:** a3f9c7e1234567890abcdef0123456789abcdef0" in body
    assert "**Run:** happy" in body
    assert "- [x] AB#22001 — kernel routes BadOutput" in body
    assert "- [x] AB#22002 — all 6 outcomes covered" in body
    assert "- [x] AB#22003 — dispatch table extensible" in body
    assert "All three criteria observed" in body

    # Item was transitioned to Closed.
    assert twig.state_transitions == [
        {"item_id": 12345, "from": "In Review", "to": "Closed"}
    ]

    # CloseOutResult shape matches the brief.
    completed = _completed_projection(log_dir / "happy.events.jsonl")
    cor = close_out_result(completed, result.final_node)
    assert cor == CloseOutResult(
        item_id=12345,
        pr_number=347,
        verdict="closed",
        closeout_path=expected_path,
        gaps=[],
        dry_run=False,
    )

    # Verdict card has the brief's signature lines.
    card = verdict_card(completed)
    assert card is not None
    assert "✓ Closed" in card
    assert "AB#12345" in card
    assert "Criteria:    3/3 met" in card
    assert "In Review → Closed" in card


# ---- partial: 2 of 3 met → NeedsHuman with gaps --------------------


async def test_partial_verifier_verdict_raises_needs_human_with_gaps(tmp_path: Path):
    log_dir = tmp_path / "runs"
    closeout_dir = tmp_path / "docs" / "closeouts"
    parent = make_twig_item(item_id=12346, title="Partial work",
                            linked_prs=[{"repo": "acme/widgets", "number": 348}])
    c1 = make_criterion(33001, "thing one done")
    c2 = make_criterion(33002, "thing two done")
    c3 = make_criterion(33003, "thing three done")
    twig = FakeTwigClient(
        items={12346: parent, 33001: c1, 33002: c2, 33003: c3},
        children_by_parent={12346: [33001, 33002, 33003]},
    )
    pr = make_pr(number=348, merged=True)
    gh = FakeGhClient(pr_by_number={("acme/widgets", 348): pr})
    fs = FakeFilesystemClient()

    provider = _scripted_verifier(
        overall="partial",
        met=[33001, 33002],
        unmet=[
            {
                "criterion_id": 33003,
                "criterion_title": "thing three done",
                "gap": "No mention of cancellation handling in PR",
            },
        ],
        notes="2/3 satisfied.",
    )

    engine = _engine(
        log_dir, item_id=12346, pr_number=348,
        twig=twig, gh=gh, fs=fs,
        provider=provider,
        gate_handler=_abort_handler,
        closeout_dir=closeout_dir,
    )
    result = await engine.run("partial")
    assert isinstance(result, Completed)
    assert result.final_node == "end_human"
    # Issue #29: the terminate disposition now matches the verdict card —
    # a human-handoff reports `needs_human`, not the misleading `failed`.
    assert result.disposition == "needs_human"

    # NO closeout written, NO state transition.
    assert fs.files == {}
    assert twig.state_transitions == []
    assert fs.write_calls == []

    # The needs_human gate carried the gap context.
    events = list(replay(log_dir / "partial.events.jsonl"))
    gates = [e for e in events if e["kind"] == "gate_opened"]
    assert any(g["node_id"] == "route_verdict" for g in gates)
    route_gate = [g for g in gates if g["node_id"] == "route_verdict"][0]
    ctx = route_gate["payload"]["context"]
    assert ctx["overall"] == "partial"
    assert ctx["unmet_count"] == 1
    assert ctx["met_count"] == 2
    gap_strings = [g["gap"] for g in ctx["unmet"]]
    assert any("cancellation handling" in g for g in gap_strings)

    # CloseOutResult surfaces gaps + verdict="needs_human".
    completed = _completed_projection(log_dir / "partial.events.jsonl")
    cor = close_out_result(completed, result.final_node)
    assert cor.verdict == "needs_human"
    assert cor.closeout_path is None
    assert any("cancellation handling" in g for g in cor.gaps)

    # Verdict card includes the gap and the resume hint.
    card = verdict_card(completed)
    assert card is not None
    assert "Needs human" in card
    assert "2/3 met" in card
    assert "AB#33003" in card
    assert "requiem resume" in card


# ---- PR not merged → NeedsHuman immediately ------------------------


async def test_pr_not_merged_raises_needs_human_immediately(tmp_path: Path):
    log_dir = tmp_path / "runs"
    parent = make_twig_item(item_id=55, title="Half-baked feature",
                            linked_prs=[{"repo": "acme/widgets", "number": 700}])
    twig = FakeTwigClient(items={55: parent})
    pr = make_pr(number=700, merged=False, state="OPEN")
    gh = FakeGhClient(pr_by_number={("acme/widgets", 700): pr})
    fs = FakeFilesystemClient()

    engine = _engine(
        log_dir, item_id=55, pr_number=700,
        twig=twig, gh=gh, fs=fs,
        gate_handler=_abort_handler,
    )
    result = await engine.run("not-merged")
    assert isinstance(result, Completed)
    assert result.final_node == "end_human"

    # Verifier was never invoked, fetch_criteria was never reached.
    events = list(replay(log_dir / "not-merged.events.jsonl"))
    visited = [e["node_id"] for e in events if e["kind"] == "node_entered"]
    assert "fetch_criteria" not in visited
    assert "verifier_agent" not in visited
    # And the gate that fired was the pr_not_merged one.
    gates = [e for e in events if e["kind"] == "gate_opened"]
    assert any(
        g["node_id"] == "fetch_pr"
        and g["payload"]["context"].get("state") == "open"
        for g in gates
    )

    assert twig.state_transitions == []
    assert fs.files == {}


# ---- PR not linked → NeedsHuman ("PR not linked") ------------------


async def test_pr_not_linked_raises_needs_human(tmp_path: Path):
    """No explicit pr_number, no linked PR, and a gh search that finds
    nothing → NeedsHuman from resolve_pr. Issue #30: the search fallback is
    attempted first (real twig often omits the linked-PR relation), but with
    no hit the run still escalates to the operator."""
    log_dir = tmp_path / "runs"
    parent = make_twig_item(item_id=88, title="Orphan item", linked_prs=[])
    twig = FakeTwigClient(items={88: parent})
    gh = FakeGhClient()  # search returns [] → still escalates
    fs = FakeFilesystemClient()

    engine = _engine(
        log_dir, item_id=88, pr_number=None,
        twig=twig, gh=gh, fs=fs,
        gate_handler=_abort_handler,
    )
    result = await engine.run("no-pr")
    assert isinstance(result, Completed)
    assert result.final_node == "end_human"

    events = list(replay(log_dir / "no-pr.events.jsonl"))
    gates = [e for e in events if e["kind"] == "gate_opened"]
    assert any(
        g["node_id"] == "resolve_pr"
        and "PR linked" in g["payload"]["prompt"]
        for g in gates
    )
    # Issue #30: gh WAS searched by the ADO link syntax before escalating.
    assert len(gh.search_queries) == 1
    assert gh.search_queries[0]["query"] == "in:body AB#88"
    assert twig.state_transitions == []


async def test_pr_resolved_via_gh_search_fallback(tmp_path: Path):
    """No linked PR on the item, but a gh search by branch convention finds
    exactly one — resolve_pr adopts it (source=gh_search) and close-out
    proceeds to end_success without operator intervention (issue #30)."""
    log_dir = tmp_path / "runs"
    closeout_dir = tmp_path / "docs" / "closeouts"
    parent = make_twig_item(
        item_id=91, title="Item w/ unlinked PR",
        state="In Review", linked_prs=[],
    )
    crit1 = make_criterion(9101, "behaviour observed", parent_id=91)
    twig = FakeTwigClient(
        items={91: parent, 9101: crit1},
        children_by_parent={91: [9101]},
    )
    found = make_pr(number=515, merged=True)
    gh = FakeGhClient(
        prs_by_repo={"acme/widgets": [found]},
        pr_by_number={("acme/widgets", 515): make_pr(number=515, merged=True)},
    )
    fs = FakeFilesystemClient()
    provider = _scripted_verifier(
        overall="all_met", met=[9101], unmet=[], notes="ok",
    )

    engine = _engine(
        log_dir, item_id=91, pr_number=None,
        twig=twig, gh=gh, fs=fs, provider=provider,
        closeout_dir=closeout_dir,
    )
    result = await engine.run("gh-search-hit")
    assert isinstance(result, Completed)
    assert result.final_node == "end_success"

    completed = _completed_projection(log_dir / "gh-search-hit.events.jsonl")
    rp = completed["resolve_pr"]["value"]
    assert rp["pr_number"] == 515
    assert rp["source"] == "gh_search"
    assert gh.search_queries[0]["query"] == "in:body AB#91"


# ---- Verifier BadOutput → NeedsHuman, NO auto-retry ----------------


async def test_verifier_bad_output_raises_needs_human_without_retry(tmp_path: Path):
    log_dir = tmp_path / "runs"
    parent = make_twig_item(item_id=42, title="Bad-output case",
                            linked_prs=[{"repo": "acme/widgets", "number": 420}])
    c1 = make_criterion(91001, "x")
    twig = FakeTwigClient(
        items={42: parent, 91001: c1},
        children_by_parent={42: [91001]},
    )
    pr = make_pr(number=420, merged=True)
    gh = FakeGhClient(pr_by_number={("acme/widgets", 420): pr})
    fs = FakeFilesystemClient()

    provider = _scripted_verifier(met=[], bad=True)

    engine = _engine(
        log_dir, item_id=42, pr_number=420,
        twig=twig, gh=gh, fs=fs,
        provider=provider,
        gate_handler=_abort_handler,
    )
    result = await engine.run("bad-output")
    assert isinstance(result, Completed)
    assert result.final_node == "end_human"

    events = list(replay(log_dir / "bad-output.events.jsonl"))

    # The verifier produced BadOutput exactly once — no auto-retry.
    verifier_completed = [
        e for e in events
        if e["kind"] == "verb_completed" and e["node_id"] == "verifier_agent"
    ]
    assert len(verifier_completed) == 1
    assert verifier_completed[0]["payload"]["outcome"]["kind"] == "bad_output"

    # No retry_attempted ever landed.
    retries = [e for e in events if e["kind"] == "retry_attempted"]
    assert retries == [], "Ravel L-1 violation: BadOutput auto-retried"

    # A needs-human gate from `verifier_bad_output` was opened.
    gates = [e for e in events if e["kind"] == "gate_opened"]
    assert any(g["node_id"] == "verifier_bad_output" for g in gates)

    # No mutations happened.
    assert twig.state_transitions == []
    assert fs.files == {}


# ---- INV-RESTART: kill mid-run, resume → identical terminal --------


async def test_inv_restart_resume_produces_identical_terminal_and_result(tmp_path: Path):
    log_dir = tmp_path / "runs"
    closeout_dir = tmp_path / "docs" / "closeouts"

    # ---- first run: complete, capture closeout + summary
    parent = make_twig_item(item_id=77, title="Restart subject",
                            linked_prs=[{"repo": "acme/widgets", "number": 770}])
    c1 = make_criterion(60001, "criterion one")
    c2 = make_criterion(60002, "criterion two")
    twig1 = FakeTwigClient(
        items={77: parent, 60001: c1, 60002: c2},
        children_by_parent={77: [60001, 60002]},
    )
    pr = make_pr(number=770, merged=True)
    gh1 = FakeGhClient(pr_by_number={("acme/widgets", 770): pr})
    fs1 = FakeFilesystemClient()
    provider1 = _scripted_verifier(met=[60001, 60002], notes="ok")

    engine1 = _engine(
        log_dir, item_id=77, pr_number=770,
        twig=twig1, gh=gh1, fs=fs1, provider=provider1,
        closeout_dir=closeout_dir,
    )
    result1 = await engine1.run("restart")
    assert isinstance(result1, Completed)
    assert result1.final_node == "end_success"

    log_path = log_dir / "restart.events.jsonl"
    completed1 = _completed_projection(log_path)
    cor1 = close_out_result(completed1, result1.final_node)
    transitions1 = list(twig1.state_transitions)
    writes1 = list(fs1.write_calls)

    # ---- truncate log to just-after close_item completed
    lines = log_path.read_text(encoding="utf-8").splitlines()
    keep: list[str] = []
    for raw in lines:
        keep.append(raw)
        ev = json.loads(raw)
        if ev["kind"] == "verb_completed" and ev.get("node_id") == "close_item":
            break
    log_path.write_text("\n".join(keep) + "\n", encoding="utf-8")

    # ---- second engine: fresh fakes (we want to prove the resume reads
    # state from the log, not from in-memory).
    twig2 = FakeTwigClient(
        items={77: make_twig_item(item_id=77, state="Closed",
                                  linked_prs=[{"repo": "acme/widgets", "number": 770}])},
    )
    gh2 = FakeGhClient()
    fs2 = FakeFilesystemClient()
    provider2 = FakeProvider(scripts={})  # nothing should call verifier

    engine2 = _engine(
        log_dir, item_id=77, pr_number=770,
        twig=twig2, gh=gh2, fs=fs2, provider=provider2,
        closeout_dir=closeout_dir,
    )
    result2 = await engine2.run("restart")
    assert isinstance(result2, Completed)
    assert result2.final_node == "end_success"
    assert result2.disposition == "completed"

    completed2 = _completed_projection(log_path)
    cor2 = close_out_result(completed2, result2.final_node)

    # The CloseOutResult is IDENTICAL across the kill/resume.
    assert cor1 == cor2, f"INV-RESTART violation: {cor1!r} != {cor2!r}"

    # Mutations were not re-executed on resume.
    assert twig2.state_transitions == [], (
        "close_item re-ran on resume — INV-RESTART violation"
    )
    assert fs2.write_calls == [], (
        "write_closeout re-ran on resume — INV-RESTART violation"
    )

    # Sanity: the original mutations happened exactly once.
    assert len(transitions1) == 1
    assert len(writes1) == 1


# ---- Dry-run: no fs.write, no twig.set_state -----------------------


async def test_dry_run_makes_no_mutations(tmp_path: Path):
    log_dir = tmp_path / "runs"
    closeout_dir = tmp_path / "docs" / "closeouts"
    parent = make_twig_item(item_id=999, title="Dry feature",
                            linked_prs=[{"repo": "acme/widgets", "number": 99}])
    c1 = make_criterion(80001, "a")
    c2 = make_criterion(80002, "b")
    twig = FakeTwigClient(
        items={999: parent, 80001: c1, 80002: c2},
        children_by_parent={999: [80001, 80002]},
    )
    pr = make_pr(number=99, merged=True)
    gh = FakeGhClient(pr_by_number={("acme/widgets", 99): pr})
    fs = FakeFilesystemClient()
    provider = _scripted_verifier(met=[80001, 80002])

    engine = _engine(
        log_dir, item_id=999, pr_number=99, dry_run=True,
        twig=twig, gh=gh, fs=fs, provider=provider,
        closeout_dir=closeout_dir,
    )
    result = await engine.run("dryrun")
    assert isinstance(result, Completed)
    assert result.final_node == "end_success"

    # Hard contract.
    assert fs.write_calls == []
    assert fs.files == {}
    assert twig.state_transitions == []

    # CloseOutResult still says verdict=closed, dry_run=True.
    completed = _completed_projection(log_dir / "dryrun.events.jsonl")
    cor = close_out_result(completed, result.final_node)
    assert cor.verdict == "closed"
    assert cor.dry_run is True
    # closeout_path is the path we *would* have written.
    assert cor.closeout_path == closeout_dir / "AB-999.md"

    # Verdict card flips to the dry-run shape.
    card = verdict_card(completed)
    assert card is not None
    assert "Dry run" in card
    assert "(dry-run)" in card


# ---- live-run smoke gate (skipped without env var) -----------------


@pytest.mark.skipif(
    "RUN_CLOSE_OUT_LIVE" not in __import__("os").environ,
    reason="real-tool smoke test gated by RUN_CLOSE_OUT_LIVE=1",
)
def test_live_smoke_placeholder(tmp_path: Path):
    assert "RUN_CLOSE_OUT_LIVE" in __import__("os").environ


# ---- CLI smoke: --dry-run end-to-end via the standalone main() -----


def test_standalone_main_dry_run_smoke(tmp_path: Path, capsys):
    """The brief calls for `requiem run requiem.workflows.close_out
    --item 99999 --pr 1 --repo owner/name --dry-run` with mocked
    clients. The standalone `main()` honours those flags directly and
    uses the canned `_demo_toolbelt` for off-line runs."""
    from requiem.workflows.close_out import main

    rc = main([
        "--item", "99999",
        "--pr", "1",
        "--repo", "owner/name",
        "--dry-run",
        "--log-dir", str(tmp_path),
        "--run-id", "cli-smoke",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "requiem.workflows.close_out" in out
    assert "AB#99999" in out
    assert "Dry run" in out
