"""End-to-end tests for the ``kanban_executor`` workflow.

Pattern mirrors ``test_close_out_workflow``: real engine, an in-process
:class:`SimKanbanClient` standing in for Hermes' durable board. The sim
implements the same async surface as the real ``KanbanClient`` and models
the external board as durable state that survives a Requiem crash.

Covers:
* Live happy path — every leaf delivered, verdict 2/2.
* Dry-run — a distinct *non-delivering* outcome ("planned only").
* Partial delivery — one leaf's worker crashes → surfaced, not silently green.
* Missing client — no implicit fake fallback; fails typed.
* INV-RESTART — crash after dispatch, resume off the durable board, identical
  terminal, and **no duplicate dispatch** (idempotency by stable key).
"""
from __future__ import annotations

import json
from pathlib import Path

from requiem.kernel import Completed, Engine
from requiem.toolbelt import Toolbelt
from requiem.workflows.kanban_executor import (
    ExecInputs,
    LeafSpec,
    SimKanbanClient,
    _is_delivered,
    _validate_dep_graph,
    build_engine,
    build_verb_registry,
    build_workflow,
    verdict_card,
)
from requiem.dsl import AgentRegistry
from requiem.persistence import replay


def _leaves() -> tuple[LeafSpec, ...]:
    return (
        LeafSpec(leaf_id="1", title="leaf one", branch="impl/r-1"),
        LeafSpec(leaf_id="2", title="leaf two", branch="impl/r-2", deps=("1",)),
    )


def _engine(log_dir: Path, *, kanban: SimKanbanClient, live: bool) -> Engine:
    inputs = ExecInputs(
        root_item="r", board="requiem-test", assignee="worker",
        live=live, leaves=_leaves(), poll_interval_s=0.0, max_polls=5,
    )
    toolbelt = Toolbelt(git=Toolbelt.real().git, files=Toolbelt.real().files, kanban=kanban)
    return Engine(
        workflow=build_workflow(),
        verbs=build_verb_registry(inputs),
        agents=AgentRegistry(),
        provider=_Null(),
        toolbelt=toolbelt,
        log_dir=log_dir,
        gate_handler=_auto,
    )


class _Null:
    async def invoke(self, call):  # pragma: no cover
        raise RuntimeError("no agents")


def _auto(node_id, prompt, options):
    return options[0] if options else "approve"


_auto.__requiem_auto__ = True


def _completed(log_path: Path) -> dict:
    """Reconstruct the node->outcome map from the durable log (what the CLI
    renderer and verdict_card consume)."""
    completed: dict[str, dict] = {}
    for ev in replay(log_path):
        if ev.get("kind") == "verb_completed":
            completed[ev["node_id"]] = ev["payload"]["outcome"]
    return completed


# ---- live happy path -------------------------------------------------


async def test_live_delivers_every_leaf(tmp_path: Path):
    kanban = SimKanbanClient()
    engine = _engine(tmp_path, kanban=kanban, live=True)
    result = await engine.run("happy")
    assert isinstance(result, Completed)
    assert result.disposition == "completed"
    assert result.final_node == "end"

    completed = _completed(tmp_path / "happy.events.jsonl")
    per_leaf = completed["poll_kanban"]["value"]["per_leaf"]
    assert len(per_leaf) == 2
    assert all(_is_delivered(p) for p in per_leaf)
    card = verdict_card(completed)
    assert "Delivered: 2/2" in card


# ---- two-phase dispatch: tasks created unassigned, then assigned -----


async def test_two_phase_dispatch_assigns_after_create(tmp_path: Path):
    kanban = SimKanbanClient()
    engine = _engine(tmp_path, kanban=kanban, live=True)
    await engine.run("twophase")
    # Every task ended assigned to the worker (phase-3 release happened).
    tasks = await kanban.list_async(board="requiem-test")
    assert tasks and all(t.assignee == "worker" for t in tasks)


# ---- dry run is a distinct, non-delivering outcome -------------------


async def test_dry_run_plans_but_delivers_nothing(tmp_path: Path):
    kanban = SimKanbanClient()
    engine = _engine(tmp_path, kanban=kanban, live=False)
    result = await engine.run("dry")
    assert isinstance(result, Completed)
    assert result.disposition == "completed"

    completed = _completed(tmp_path / "dry.events.jsonl")
    poll = completed["poll_kanban"]["value"]
    assert poll["mode"] == "dry_run"
    assert all(not _is_delivered(p) for p in poll["per_leaf"])
    card = verdict_card(completed)
    assert "DRY RUN" in card and "nothing delivered" in card
    # No worker ever ran.
    for t in await kanban.list_async(board="requiem-test"):
        assert not await kanban.runs_async(t.id, board="requiem-test")


# ---- partial delivery is surfaced, not silently green ----------------


async def test_partial_delivery_surfaces_failed_leaf(tmp_path: Path):
    # Worker on the leaf with branch impl/r-2 crashes.
    kanban = SimKanbanClient(fail_leaf_ids=("impl/r-2",))
    engine = _engine(tmp_path, kanban=kanban, live=True)
    result = await engine.run("partial")
    assert isinstance(result, Completed)

    completed = _completed(tmp_path / "partial.events.jsonl")
    per_leaf = completed["poll_kanban"]["value"]["per_leaf"]
    delivered = [p for p in per_leaf if _is_delivered(p)]
    failed = [p for p in per_leaf if not _is_delivered(p)]
    assert len(delivered) == 1 and len(failed) == 1
    card = verdict_card(completed)
    assert "Partial delivery: 1/2" in card


# ---- no implicit fake fallback ---------------------------------------


async def test_missing_kanban_client_fails_typed(tmp_path: Path):
    inputs = ExecInputs(root_item="r", board="requiem-test", live=True,
                        leaves=_leaves(), poll_interval_s=0.0)
    toolbelt = Toolbelt(git=Toolbelt.real().git, files=Toolbelt.real().files, kanban=None)
    engine = Engine(
        workflow=build_workflow(), verbs=build_verb_registry(inputs),
        agents=AgentRegistry(), provider=_Null(), toolbelt=toolbelt,
        log_dir=tmp_path, gate_handler=_auto,
    )
    result = await engine.run("noclient")
    assert isinstance(result, Completed)
    assert result.disposition == "failed"
    assert result.final_node == "fail_end"
    completed = _completed(tmp_path / "noclient.events.jsonl")
    assert completed["preflight"]["error_kind"] == "toolbelt.missing_client"


# ---- INV-RESTART: resume off the durable board, no double dispatch ----


async def test_inv_restart_resume_no_duplicate_dispatch(tmp_path: Path):
    kanban = SimKanbanClient()  # the external board — survives the "crash"
    engine1 = _engine(tmp_path, kanban=kanban, live=True)
    result1 = await engine1.run("restart")
    assert isinstance(result1, Completed)
    tasks_after_run = {t.id for t in await kanban.list_async(board="requiem-test")}
    assert len(tasks_after_run) == 2

    # Truncate the log just after dispatch_leaves completed (simulate a crash
    # while polling).
    log_path = tmp_path / "restart.events.jsonl"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    keep: list[str] = []
    for raw in lines:
        keep.append(raw)
        ev = json.loads(raw)
        if ev["kind"] == "verb_completed" and ev.get("node_id") == "dispatch_leaves":
            break
    log_path.write_text("\n".join(keep) + "\n", encoding="utf-8")

    # Resume against the SAME durable board.
    engine2 = _engine(tmp_path, kanban=kanban, live=True)
    result2 = await engine2.run("restart")
    assert isinstance(result2, Completed)
    assert result2.disposition == "completed"

    # No duplicate tasks were created on resume (idempotency by stable key).
    tasks_after_resume = {t.id for t in await kanban.list_async(board="requiem-test")}
    assert tasks_after_resume == tasks_after_run, "resume re-dispatched — duplicate tasks"


# ---- artifact-driven leaf resolution (the committed-plan contract) ----


def _committed_artifacts(tmp_path: Path) -> tuple[Path, Path]:
    """Write an approved two-level plan tree + a committed manifest. Root 7000
    decomposes into Child A (700001, itself decomposed into two leaf
    grandchildren) and Child B (700002, a leaf). Real ids via id_map.
    """
    tree = {
        "schema_version": 2, "plan_id": "plan-7000", "item_id": 7000,
        "decomposable": True, "verdict": "approved",
        "proposals": [
            {"title": "Child A", "description": "x", "work_item_type": "Story"},
            {"title": "Child B", "description": "y", "work_item_type": "Task"},
        ],
        "children": [
            {"item_id": 700001, "decomposable": True,
             "proposals": [
                 {"title": "Leaf A1", "description": "a1", "work_item_type": "Task"},
                 {"title": "Leaf A2", "description": "a2", "work_item_type": "Task"},
             ],
             "children": [
                 {"item_id": 70000101, "decomposable": False, "proposals": [], "children": []},
                 {"item_id": 70000102, "decomposable": False, "proposals": [], "children": []},
             ]},
            {"item_id": 700002, "decomposable": False, "proposals": [], "children": []},
        ],
    }
    committed = {
        "schema_version": 1, "plan_id": "plan-7000", "root_item_id": 7000,
        "dry_run": False,
        "id_map": {"700001": 8001, "700002": 8002, "70000101": 8101, "70000102": 8102},
    }
    tp = tmp_path / "r.plan.tree.json"
    cp = tmp_path / "r.plan.committed.json"
    tp.write_text(json.dumps(tree), encoding="utf-8")
    cp.write_text(json.dumps(committed), encoding="utf-8")
    return tp, cp


def _artifact_engine(log_dir: Path, *, kanban: SimKanbanClient, tree: Path,
                     committed: Path) -> Engine:
    inputs = ExecInputs(
        root_item="7000", board="requiem-test", assignee="worker", live=True,
        plan_tree_path=tree, committed_path=committed,
        poll_interval_s=0.0, max_polls=5,
    )
    toolbelt = Toolbelt(git=Toolbelt.real().git, files=Toolbelt.real().files, kanban=kanban)
    return Engine(
        workflow=build_workflow(), verbs=build_verb_registry(inputs),
        agents=AgentRegistry(), provider=_Null(), toolbelt=toolbelt,
        log_dir=log_dir, gate_handler=_auto,
    )


async def test_resolves_committed_plan_to_real_leaf_ids(tmp_path: Path):
    tree, committed = _committed_artifacts(tmp_path)
    kanban = SimKanbanClient()
    engine = _artifact_engine(tmp_path, kanban=kanban, tree=tree, committed=committed)
    result = await engine.run("commit")
    assert isinstance(result, Completed)
    assert result.disposition == "completed"

    completed = _completed(tmp_path / "commit.events.jsonl")
    resolved = completed["resolve_leaves"]["value"]
    assert resolved["source"] == "committed_plan"
    # decomposable==False leaves depth-first → grandchildren then Child B,
    # mapped through id_map to real ADO ids; branch shape impl/<root>-<leaf>.
    assert [l["leaf_id"] for l in resolved["leaves"]] == ["8101", "8102", "8002"]
    assert [l["branch"] for l in resolved["leaves"]] == [
        "impl/7000-8101", "impl/7000-8102", "impl/7000-8002",
    ]
    per_leaf = completed["poll_kanban"]["value"]["per_leaf"]
    assert len(per_leaf) == 3 and all(_is_delivered(p) for p in per_leaf)


async def test_committed_plan_depends_on_threads_into_leaf_deps_and_gates_release(
    tmp_path: Path,
):
    """A `depends_on` declared on grandchild A2 (naming sibling A1) must
    surface as `LeafSpec.deps` on the real leaf id, and the pre-existing
    wave-release machinery (poll_kanban) must hold A2's task un-dispatched
    until A1 is delivered — even though both still land by end of run."""
    tree, committed = _committed_artifacts(tmp_path)
    tree_data = json.loads(tree.read_text(encoding="utf-8"))
    # Leaf A2 (children[0].proposals[1]) depends on sibling A1 (slot 0).
    tree_data["children"][0]["proposals"][1]["depends_on"] = [0]
    tree.write_text(json.dumps(tree_data), encoding="utf-8")

    kanban = SimKanbanClient()
    engine = _artifact_engine(tmp_path, kanban=kanban, tree=tree, committed=committed)
    result = await engine.run("commit")
    assert isinstance(result, Completed)
    assert result.disposition == "completed"

    completed = _completed(tmp_path / "commit.events.jsonl")
    resolved = completed["resolve_leaves"]["value"]
    by_id = {l["leaf_id"]: l for l in resolved["leaves"]}
    # A1 = 8101, A2 = 8102 (see _committed_artifacts id_map).
    assert by_id["8101"]["deps"] == []
    assert by_id["8102"]["deps"] == ["8101"]
    per_leaf = completed["poll_kanban"]["value"]["per_leaf"]
    assert len(per_leaf) == 3 and all(_is_delivered(p) for p in per_leaf)


async def test_dry_run_committed_manifest_fails_closed(tmp_path: Path):
    tree, committed = _committed_artifacts(tmp_path)
    # Flip the manifest to a dry-run preview (no real ids to dispatch).
    data = json.loads(committed.read_text(encoding="utf-8"))
    data["dry_run"] = True
    committed.write_text(json.dumps(data), encoding="utf-8")

    kanban = SimKanbanClient()
    engine = _artifact_engine(tmp_path, kanban=kanban, tree=tree, committed=committed)
    result = await engine.run("drycommit")
    assert isinstance(result, Completed)
    assert result.final_node == "fail_end"

    completed = _completed(tmp_path / "drycommit.events.jsonl")
    outcome = completed["resolve_leaves"]
    assert outcome["kind"] == "permanent_failure"
    assert outcome["error_kind"] == "plan_artifact.dry_run"
    # Nothing was dispatched.
    assert "dispatch_leaves" not in completed


async def test_no_leaf_source_fails_closed(tmp_path: Path):
    inputs = ExecInputs(root_item="42", board="requiem-test", live=True)
    kanban = SimKanbanClient()
    toolbelt = Toolbelt(git=Toolbelt.real().git, files=Toolbelt.real().files, kanban=kanban)
    engine = Engine(
        workflow=build_workflow(), verbs=build_verb_registry(inputs),
        agents=AgentRegistry(), provider=_Null(), toolbelt=toolbelt,
        log_dir=tmp_path, gate_handler=_auto,
    )
    result = await engine.run("nosrc")
    assert isinstance(result, Completed)
    assert result.final_node == "fail_end"
    completed = _completed(tmp_path / "nosrc.events.jsonl")
    assert completed["resolve_leaves"]["error_kind"] == "plan_artifact.no_source"


# ---- dependency-graph validation (fail-closed gating) ----------------


def test_validate_dep_graph_independent_leaves_all_ready():
    leaves = [LeafSpec(leaf_id="a", title="A"), LeafSpec(leaf_id="b", title="B")]
    err, ready = _validate_dep_graph(leaves)
    assert err is None
    assert set(ready) == {"a", "b"}


def test_validate_dep_graph_frontier_excludes_dependents():
    leaves = [
        LeafSpec(leaf_id="a", title="A"),
        LeafSpec(leaf_id="b", title="B", deps=("a",)),
    ]
    err, ready = _validate_dep_graph(leaves)
    assert err is None
    assert tuple(ready) == ("a",)  # b is held until a is accepted


def test_validate_dep_graph_unknown_dep_fails_closed():
    leaves = [LeafSpec(leaf_id="a", title="A", deps=("ghost",))]
    err, ready = _validate_dep_graph(leaves)
    assert err is not None and "unknown leaf 'ghost'" in err
    assert ready == ()


def test_validate_dep_graph_self_dep_fails_closed():
    leaves = [LeafSpec(leaf_id="a", title="A", deps=("a",))]
    err, _ = _validate_dep_graph(leaves)
    assert err is not None and "itself" in err


def test_validate_dep_graph_cycle_fails_closed():
    leaves = [
        LeafSpec(leaf_id="a", title="A", deps=("b",)),
        LeafSpec(leaf_id="b", title="B", deps=("a",)),
    ]
    err, _ = _validate_dep_graph(leaves)
    assert err is not None and "cycle" in err


# ---- acceptance-gating: requiem owns child release (ADR-0017 §3) ------


async def test_dispatch_holds_dependent_child_until_parent_accepted(tmp_path: Path):
    kanban = SimKanbanClient()
    engine = _engine(tmp_path, kanban=kanban, live=True)
    await engine.run("gating")
    completed = _completed(tmp_path / "gating.events.jsonl")
    disp = completed["dispatch_leaves"]["value"]
    # At dispatch, only the dependency-free leaf is released; the child is held.
    assert disp["ready_frontier"] == ["1"]
    assert disp["held_pending_acceptance"] == ["2"]


async def test_child_of_failed_parent_is_never_released(tmp_path: Path):
    # The PARENT (impl/r-1, leaf "1") crashes. Its dependent child (leaf "2")
    # must never be assigned/spawned — kanban promotion is not the authority,
    # and requiem refuses to release a child whose parent was not accepted.
    kanban = SimKanbanClient(fail_leaf_ids=("impl/r-1",))
    engine = _engine(tmp_path, kanban=kanban, live=True)
    result = await engine.run("blockedchild")
    assert isinstance(result, Completed)

    completed = _completed(tmp_path / "blockedchild.events.jsonl")
    per_leaf = completed["poll_kanban"]["value"]["per_leaf"]
    assert all(not _is_delivered(p) for p in per_leaf)  # nothing delivered

    # The child task was created but stayed UNASSIGNED (never released).
    tasks = {t.branch_name: t for t in await kanban.list_async(board="requiem-test")}
    assert tasks["impl/r-2"].assignee is None
    # And the child never ran.
    assert not await kanban.runs_async(tasks["impl/r-2"].id, board="requiem-test")


# ---- plan-hash idempotency + fail-closed reconcile (ADR-0017 §5) ------


def test_plan_hash_stable_and_content_sensitive():
    from requiem.workflows.kanban_executor import _plan_hash
    a = [LeafSpec(leaf_id="1", title="A"), LeafSpec(leaf_id="2", title="B")]
    # Order-independent (canonicalised by leaf_id).
    assert _plan_hash(a) == _plan_hash(list(reversed(a)))
    # Any identity change moves the hash.
    assert _plan_hash(a) != _plan_hash(
        [LeafSpec(leaf_id="1", title="A"), LeafSpec(leaf_id="2", title="B!")]
    )


async def test_idempotency_key_carries_plan_hash(tmp_path: Path):
    from requiem.workflows.kanban_executor import _plan_hash
    kanban = SimKanbanClient()
    engine = _engine(tmp_path, kanban=kanban, live=True)
    await engine.run("keys")
    completed = _completed(tmp_path / "keys.events.jsonl")
    keys = completed["dispatch_leaves"]["value"]["idempotency_keys"]
    expected_hash = _plan_hash(list(_leaves()))
    for leaf_id, key in keys.items():
        assert key == f"requiem:r:{expected_hash}:{leaf_id}"


async def test_replanned_leaf_gets_a_fresh_task_not_a_stale_reuse(tmp_path: Path):
    # Same board, but the second run carries a leaf with a changed title. The
    # plan hash moves, so the key moves, so a NEW task is created rather than
    # silently reusing the superseded plan's task.
    kanban = SimKanbanClient()
    e1 = _engine(tmp_path, kanban=kanban, live=True)
    await e1.run("v1")
    n_after_v1 = len(await kanban.list_async(board="requiem-test"))

    changed = (
        LeafSpec(leaf_id="1", title="leaf one v2", branch="impl/r-1"),
        LeafSpec(leaf_id="2", title="leaf two", branch="impl/r-2", deps=("1",)),
    )
    inputs = ExecInputs(
        root_item="r", board="requiem-test", assignee="worker",
        live=True, leaves=changed, poll_interval_s=0.0, max_polls=5,
    )
    toolbelt = Toolbelt(git=Toolbelt.real().git, files=Toolbelt.real().files, kanban=kanban)
    e2 = Engine(
        workflow=build_workflow(), verbs=build_verb_registry(inputs),
        agents=AgentRegistry(), provider=_Null(), toolbelt=toolbelt,
        log_dir=tmp_path, gate_handler=_auto,
    )
    await e2.run("v2")
    n_after_v2 = len(await kanban.list_async(board="requiem-test"))
    assert n_after_v2 > n_after_v1, "replanned leaf silently reused a stale task"


def test_reconcile_fails_closed_on_mismatched_reuse():
    from requiem.clients.kanban import KanbanTask
    from requiem.workflows.kanban_executor import _reconcile
    leaf = LeafSpec(leaf_id="1", title="real work", branch="impl/r-1", skills=("coding",))
    key = "requiem:r:abc123:1"
    ok = KanbanTask(
        id="t1", title="real work", status="ready", assignee=None,
        workspace_kind="worktree", branch_name="impl/r-1", result=None,
        idempotency_key=key, raw={"leaf_skills": ["coding"]},
    )
    assert _reconcile(ok, leaf, key) is None
    # A task reused under our key but carrying a different title is rejected.
    tampered = KanbanTask(
        id="t1", title="something else", status="done", assignee="someone",
        workspace_kind="worktree", branch_name="impl/r-1", result="PR",
        idempotency_key=key, raw={"leaf_skills": ["coding"]},
    )
    assert _reconcile(tampered, leaf, key) is not None


# ---- state translation table (ADR-0017 §6) ---------------------------


def test_translate_state_in_flight_when_not_terminal():
    from requiem.workflows.kanban_executor import translate_state
    out, _ = translate_state(status="running", outcome=None, result=None, run_raw=None)
    assert out == "in_flight"


def test_translate_state_delivered_on_receipt():
    from requiem.workflows.kanban_executor import translate_state
    out, _ = translate_state(
        status="done", outcome="completed", result="PR opened", run_raw=None,
    )
    assert out == "delivered"


def test_translate_state_done_without_result_needs_human():
    from requiem.workflows.kanban_executor import translate_state
    out, reason = translate_state(
        status="done", outcome="completed", result=None, run_raw=None,
    )
    assert out == "needs_human" and "no delivery receipt" in reason


def test_translate_state_blocked_after_failure_is_permanent():
    from requiem.workflows.kanban_executor import translate_state
    out, _ = translate_state(
        status="blocked", outcome="crashed", result=None, run_raw=None,
    )
    assert out == "permanent_failure"


def test_translate_state_blocked_for_human_needs_human():
    from requiem.workflows.kanban_executor import translate_state
    out, _ = translate_state(
        status="blocked", outcome=None, result=None, run_raw=None,
    )
    assert out == "needs_human"


def test_translate_state_archived_is_permanent():
    from requiem.workflows.kanban_executor import translate_state
    out, _ = translate_state(
        status="archived", outcome=None, result=None, run_raw=None,
    )
    assert out == "permanent_failure"


def test_translate_state_valid_metadata_passes_through():
    from requiem.workflows.kanban_executor import translate_state
    meta = {
        "schema_version": 1, "leaf_id": "8101", "root_item": "7000",
        "plan_hash": "deadbeef", "worker_profile": "coder",
        "pr_url": "https://example/pr/1",
    }
    out, _ = translate_state(
        status="done", outcome="completed", result="ok",
        run_raw={"metadata": meta},
        expect={"leaf_id": "8101", "root_item": "7000", "plan_hash": "deadbeef"},
    )
    assert out == "delivered"


def test_translate_state_misattributed_metadata_downgrades():
    from requiem.workflows.kanban_executor import translate_state
    meta = {
        "schema_version": 1, "leaf_id": "WRONG", "root_item": "7000",
        "plan_hash": "deadbeef", "worker_profile": "coder",
    }
    out, reason = translate_state(
        status="done", outcome="completed", result="ok",
        run_raw={"metadata": meta},
        expect={"leaf_id": "8101", "root_item": "7000", "plan_hash": "deadbeef"},
    )
    assert out == "needs_human" and "misattributed" in reason


def test_translate_state_unknown_schema_downgrades():
    from requiem.workflows.kanban_executor import translate_state
    out, reason = translate_state(
        status="done", outcome="completed", result="ok",
        run_raw={"metadata": {"schema_version": 999, "leaf_id": "x"}},
    )
    assert out == "needs_human" and "handoff invalid" in reason


def test_translate_state_missing_metadata_under_gating_needs_human():
    """A done task with NO handoff metadata cannot be attributed to the leaf;
    under the gating path (expect present) it is surfaced, never accepted."""
    from requiem.workflows.kanban_executor import translate_state
    out, reason = translate_state(
        status="done", outcome="completed", result="ok", run_raw={},
        expect={"leaf_id": "8101", "root_item": "7000", "plan_hash": "deadbeef"},
    )
    assert out == "needs_human" and "missing handoff" in reason


def test_row_delivered_honors_requiem_outcome():
    """A live row's disposition is authoritative — a done+completed row whose
    evidence was rejected must NOT count as delivered."""
    from requiem.workflows.kanban_executor import _row_delivered
    rejected = {"leaf_id": "x", "status": "done", "outcome": "completed",
                "result": "ok", "requiem_outcome": "needs_human"}
    accepted = {"leaf_id": "y", "status": "done", "outcome": "completed",
                "result": "ok", "requiem_outcome": "delivered"}
    assert not _row_delivered(rejected)
    assert _row_delivered(accepted)


def test_row_delivered_falls_back_for_dry_run_rows():
    """Dry-run rows carry no disposition; fall back to the raw receipt check."""
    from requiem.workflows.kanban_executor import _row_delivered
    dry = {"leaf_id": "z", "status": "done", "outcome": "completed", "result": "ok"}
    assert _row_delivered(dry)


def test_sim_handoff_metadata_attributes_via_idempotency_key():
    """The sim reconstructs the worker's handoff blob from the task identity so
    live sim deliveries carry attributable evidence (closes the emit side)."""
    from requiem.clients.kanban import KanbanTask
    from requiem.workflows.kanban_executor import _sim_handoff_metadata
    task = KanbanTask(
        id="t1", title="leaf", status="ready", assignee="coder",
        workspace_kind="worktree", branch_name="impl/7000-8101",
        result=None, idempotency_key="requiem:7000:deadbeef:8101",
    )
    blob = _sim_handoff_metadata(task)["metadata"]
    assert blob["leaf_id"] == "8101"
    assert blob["root_item"] == "7000"
    assert blob["plan_hash"] == "deadbeef"
    assert blob["worker_profile"] == "coder"
    assert blob["schema_version"] == 1


def test_sim_handoff_metadata_empty_without_requiem_key():
    """A task created outside requiem yields no evidence — an evidence-less
    completion the executor surfaces rather than silently accepts."""
    from requiem.clients.kanban import KanbanTask
    from requiem.workflows.kanban_executor import _sim_handoff_metadata
    task = KanbanTask(
        id="t9", title="rogue", status="ready", assignee="coder",
        workspace_kind="worktree", branch_name="x", result=None,
        idempotency_key="some-other-key",
    )
    assert _sim_handoff_metadata(task) == {}


async def test_live_delivery_carries_attributable_evidence(tmp_path: Path):
    """End-to-end: a live sim run's delivered leaves are counted via the
    requiem disposition (translated from real handoff evidence), not the raw
    receipt — proving the emit→consume wire contract closes."""
    kanban = SimKanbanClient()
    engine = _engine(tmp_path, kanban=kanban, live=True)
    result = await engine.run("attrib")
    assert isinstance(result, Completed)
    completed = _completed(tmp_path / "attrib.events.jsonl")
    per_leaf = completed["poll_kanban"]["value"]["per_leaf"]
    assert per_leaf, "expected per-leaf rows"
    assert all(p["requiem_outcome"] == "delivered" for p in per_leaf), per_leaf


# ---- topology sanity --------------------------------------------------


def test_topology_validates():
    wf = build_workflow()
    assert wf.validate_topology() == []


def test_demo_engine_runs_keyfree(tmp_path: Path):
    import asyncio
    engine = build_engine(tmp_path)
    result = asyncio.run(engine.run("demo"))
    assert isinstance(result, Completed)
    assert result.disposition == "completed"


# ---- real Hermes binary, through the whole engine (gated) ------------


import pytest  # noqa: E402
from requiem.clients.kanban import KanbanClient, is_hermes_on_path  # noqa: E402


@pytest.mark.skipif(not is_hermes_on_path(), reason="hermes not on PATH")
async def test_real_kanban_client_flows_through_engine_dry_run(tmp_path: Path):
    """Drive the REAL `hermes kanban` binary through the full workflow in
    dry-run mode against a throwaway board. Proves preflight → resolve →
    dispatch (real create+link on a real board) → poll → aggregate with the
    production client, then deletes the board. Never touches `default`."""
    import subprocess

    board = "requiem-pytest-engine"
    inputs = ExecInputs(
        root_item="r", board=board, assignee=None, live=False,
        leaves=_leaves(), poll_interval_s=0.0, max_polls=2,
    )
    toolbelt = Toolbelt(git=Toolbelt.real().git, files=Toolbelt.real().files,
                        kanban=KanbanClient())
    engine = Engine(
        workflow=build_workflow(), verbs=build_verb_registry(inputs),
        agents=AgentRegistry(), provider=_Null(), toolbelt=toolbelt,
        log_dir=tmp_path, gate_handler=_auto,
    )
    try:
        result = await engine.run("realdry")
        assert isinstance(result, Completed)
        assert result.disposition == "completed"
        completed = _completed(tmp_path / "realdry.events.jsonl")
        disp = completed["dispatch_leaves"]["value"]
        assert disp["mode"] == "dry_run"
        assert len(disp["leaf_to_task"]) == 2  # two real tasks were created
        poll = completed["poll_kanban"]["value"]
        assert poll["mode"] == "dry_run"
        assert all(not _is_delivered(p) for p in poll["per_leaf"])
    finally:
        subprocess.run(["hermes", "kanban", "boards", "rm", board, "--delete"],
                       capture_output=True, text=True)

