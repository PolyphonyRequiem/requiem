"""Tests for the read-only web dashboard (ADR-0019, v0 non-negotiable #8).

Two layers:
- Pure projection tests (no socket): list_runs / run_detail / pending_gates fold
  the event log the same way the CLI's list-runs does.
- A thin server smoke test: bind an ephemeral port, hit the JSON API + page with
  urllib, assert shapes. No browser, no external deps.
"""
from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from requiem.dashboard import projection
from requiem.dashboard.resolution import GateResolutionError, resolve_gate
from requiem.dashboard.server import build_server


# ---- log fixtures -------------------------------------------------------


def _write_run(log_dir: Path, run_id: str, events: list[dict]) -> Path:
    """Write a minimal *.events.jsonl with auto-assigned event_ids + ts."""
    path = log_dir / f"{run_id}.events.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for i, ev in enumerate(events):
            ev = dict(ev)
            ev.setdefault("event_id", i)
            ev.setdefault("run_id", run_id)
            ev.setdefault("ts", f"2026-06-09T10:00:{i:02d}+00:00")
            ev.setdefault("node_id", ev.get("node_id"))
            ev.setdefault("payload", ev.get("payload", {}))
            fh.write(json.dumps(ev) + "\n")
    return path


def _completed_run(log_dir, run_id="run-done", *, terminal="completed", final="end"):
    return _write_run(log_dir, run_id, [
        {"kind": "run_started", "payload": {"workflow": "demo", "workflow_version": "1"}},
        {"kind": "node_entered", "node_id": "start", "payload": {"attempt": 1}},
        {"kind": "verb_completed", "node_id": "start",
         "payload": {"outcome": {"kind": "success"}}},
        {"kind": "run_completed", "node_id": final,
         "payload": {"terminal": terminal, "final_node": final}},
    ])


def _suspended_run(log_dir, run_id="run-gate"):
    return _write_run(log_dir, run_id, [
        {"kind": "run_started", "payload": {"workflow": "needs-human-wf"}},
        {"kind": "node_entered", "node_id": "gate", "payload": {}},
        {"kind": "gate_opened", "node_id": "gate",
         "payload": {"prompt": "Approve the batch?", "options": ["approve", "abort"],
                     "auto": False}},
    ])


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs"
    d.mkdir()
    return d


# ---- list_runs ----------------------------------------------------------


def test_list_runs_empty_dir_returns_empty(tmp_path):
    assert projection.list_runs(tmp_path / "nope") == []


def test_list_runs_projects_status_and_workflow(log_dir):
    _completed_run(log_dir, "a")
    _suspended_run(log_dir, "b")
    runs = {r.run_id: r for r in projection.list_runs(log_dir)}
    assert runs["a"].status == "Completed"
    assert runs["a"].workflow == "demo"
    assert runs["a"].gate_open is False
    assert runs["b"].status == "Suspended"
    assert runs["b"].gate_open is True


def test_list_runs_status_variants(log_dir):
    _completed_run(log_dir, "ok", terminal="completed")
    _completed_run(log_dir, "bad", terminal="failed")
    _completed_run(log_dir, "cx", terminal="cancelled")
    _completed_run(log_dir, "nh", terminal="needs_human")
    runs = {r.run_id: r.status for r in projection.list_runs(log_dir)}
    assert runs["ok"] == "Completed"
    assert runs["bad"] == "Failed"
    assert runs["cx"] == "Cancelled"
    assert runs["nh"] == "Needs human"


def test_list_runs_sorted_newest_first(log_dir):
    _write_run(log_dir, "old", [
        {"kind": "run_started", "ts": "2026-06-01T00:00:00+00:00",
         "payload": {"workflow": "w"}}])
    _write_run(log_dir, "new", [
        {"kind": "run_started", "ts": "2026-06-09T00:00:00+00:00",
         "payload": {"workflow": "w"}}])
    order = [r.run_id for r in projection.list_runs(log_dir)]
    assert order == ["new", "old"]


# ---- run_detail ---------------------------------------------------------


def test_run_detail_absent_returns_none(log_dir):
    assert projection.run_detail(log_dir, "ghost") is None


def test_run_detail_humanizes_timeline(log_dir):
    _completed_run(log_dir, "a")
    detail = projection.run_detail(log_dir, "a")
    assert detail is not None
    assert detail.status == "Completed"
    assert detail.final_node == "end"
    kinds = [e.kind for e in detail.timeline]
    assert kinds == ["run_started", "node_entered", "verb_completed", "run_completed"]
    # every entry is humanized with a glyph + summary
    assert all(e.glyph for e in detail.timeline)
    assert any("run started" in e.summary for e in detail.timeline)


def test_run_detail_surfaces_process_config_policy(log_dir):
    """#1: the start_run ProcessConfig snapshot is projected as `policy` so the
    dashboard can show the tier policy that classified the run's work."""
    _write_run(log_dir, "withpolicy", [
        {"kind": "run_started", "payload": {"workflow": "planning"}},
        {"kind": "node_entered", "node_id": "start", "payload": {"attempt": 1}},
        {"kind": "verb_completed", "node_id": "start", "payload": {"outcome": {
            "kind": "success",
            "value": {"process_config": {
                "root_parent_types": ["Epic", "Feature"],
                "decomposable_types": ["Feature", "Epic"],
                "implementable_types": ["Task", "Bug"],
                "type_aliases": {"Story": "Feature"},
                "source": ".requiem-config/process.yaml",
                "sha256": "abc123def456",
            }},
        }}},
    ])
    detail = projection.run_detail(log_dir, "withpolicy")
    assert detail is not None
    assert detail.policy is not None
    assert detail.policy["root_parent_types"] == ["Epic", "Feature"]
    assert detail.policy["implementable_types"] == ["Task", "Bug"]
    assert detail.policy["type_aliases"] == {"Story": "Feature"}
    # to_dict (the API shape) carries it too.
    assert detail.to_dict()["policy"]["sha256"] == "abc123def456"


def test_run_detail_no_policy_when_absent(log_dir):
    """A run without a process_config snapshot has policy=None (not an error)."""
    _completed_run(log_dir, "nopolicy")
    detail = projection.run_detail(log_dir, "nopolicy")
    assert detail is not None
    assert detail.policy is None


def test_run_detail_surfaces_open_gate(log_dir):
    _suspended_run(log_dir, "g")
    detail = projection.run_detail(log_dir, "g")
    assert detail.status == "Suspended"
    assert detail.gate is not None
    assert detail.gate["prompt"] == "Approve the batch?"
    assert detail.gate["options"] == ["approve", "abort"]


def test_run_detail_marks_corrupt_log(log_dir):
    path = log_dir / "torn.events.jsonl"
    path.write_text(
        json.dumps({"event_id": 0, "run_id": "torn", "kind": "run_started",
                    "ts": "2026-06-09T10:00:00+00:00", "payload": {"workflow": "w"}}) + "\n"
        + '{"event_id": 1, "kind": "node_ent',  # torn line
        encoding="utf-8",
    )
    detail = projection.run_detail(log_dir, "torn")
    assert detail.status == "Corrupt"
    assert detail.corrupt is not None


# ---- pending_gates ------------------------------------------------------


def test_pending_gates_lists_only_open(log_dir):
    _completed_run(log_dir, "done")
    _suspended_run(log_dir, "waiting")
    gates = projection.pending_gates(log_dir)
    assert [g.run_id for g in gates] == ["waiting"]
    assert gates[0].prompt == "Approve the batch?"
    assert gates[0].options == ["approve", "abort"]


def test_resolved_gate_is_not_pending(log_dir):
    _write_run(log_dir, "resolved", [
        {"kind": "run_started", "payload": {"workflow": "w"}},
        {"kind": "gate_opened", "node_id": "g",
         "payload": {"prompt": "?", "options": ["ok"]}},
        {"kind": "gate_resolved", "node_id": "g", "payload": {"choice": "ok"}},
    ])
    assert projection.pending_gates(log_dir) == []


# ---- server smoke (ephemeral port, urllib, no browser) ------------------


@pytest.fixture
def running_server(log_dir):
    _completed_run(log_dir, "srv-run")
    _suspended_run(log_dir, "srv-gate")
    httpd = build_server(log_dir, host="127.0.0.1", port=0)  # 0 = ephemeral
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    host, port = httpd.socket.getsockname()
    base = f"http://{host}:{port}"
    try:
        yield base
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read()


def test_server_serves_html_page(running_server):
    status, body = _get(running_server + "/")
    assert status == 200
    assert b"requiem dashboard" in body or b"<title>requiem" in body


def test_server_healthz(running_server):
    status, body = _get(running_server + "/healthz")
    assert status == 200
    assert json.loads(body)["ok"] is True


def test_server_api_runs(running_server):
    status, body = _get(running_server + "/api/runs")
    assert status == 200
    runs = json.loads(body)["runs"]
    ids = {r["run_id"]: r for r in runs}
    assert "srv-run" in ids and "srv-gate" in ids
    assert ids["srv-gate"]["gate_open"] is True


def test_server_api_run_detail(running_server):
    status, body = _get(running_server + "/api/runs/srv-run")
    assert status == 200
    detail = json.loads(body)
    assert detail["run_id"] == "srv-run"
    assert detail["status"] == "Completed"
    assert len(detail["timeline"]) == 4


def test_server_api_gates(running_server):
    status, body = _get(running_server + "/api/gates")
    assert status == 200
    gates = json.loads(body)["gates"]
    assert [g["run_id"] for g in gates] == ["srv-gate"]


def test_server_unknown_run_is_404(running_server):
    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as ei:
        _get(running_server + "/api/runs/does-not-exist")
    assert ei.value.code == 404


def test_server_rejects_path_traversal_run_id(running_server):
    import urllib.error
    # a slash in the run id can't reach the handler as a path segment, but a
    # backslash / encoded form must still 404 rather than touch the filesystem.
    with pytest.raises(urllib.error.HTTPError) as ei:
        _get(running_server + "/api/runs/..%5Cevil")
    assert ei.value.code == 404


# ---- phase 2: gate resolution (the guarded write path, ADR-0019 §4) -----


def test_resolve_invalid_choice_refused_and_no_write(log_dir):
    _suspended_run(log_dir, "r")
    before = (log_dir / "r.events.jsonl").read_text(encoding="utf-8")
    with pytest.raises(GateResolutionError) as ei:
        resolve_gate(log_dir, "r", "not-an-option")
    assert ei.value.reason == "invalid_choice"
    # nothing was appended — the refusal is total.
    assert (log_dir / "r.events.jsonl").read_text(encoding="utf-8") == before


def test_resolve_run_not_found(log_dir):
    with pytest.raises(GateResolutionError) as ei:
        resolve_gate(log_dir, "ghost", "approve")
    assert ei.value.reason == "run_not_found"


def test_resolve_not_at_gate_refused(log_dir):
    _completed_run(log_dir, "done")
    with pytest.raises(GateResolutionError) as ei:
        resolve_gate(log_dir, "done", "approve")
    assert ei.value.reason == "not_at_gate"


def test_resolve_valid_choice_appends_and_clears_gate(log_dir):
    _suspended_run(log_dir, "r")
    res = resolve_gate(log_dir, "r", "approve")
    assert res.choice == "approve"
    assert res.node == "gate"
    # the run is no longer pending; status folds to Running.
    assert projection.pending_gates(log_dir) == []
    detail = projection.run_detail(log_dir, "r")
    assert detail.gate is None
    assert detail.status == "Running"
    # exactly one gate_resolved was appended, with the kernel envelope shape.
    events = [json.loads(x) for x in
              (log_dir / "r.events.jsonl").read_text(encoding="utf-8").splitlines() if x]
    resolved = [e for e in events if e["kind"] == "gate_resolved"]
    assert len(resolved) == 1
    assert resolved[0]["payload"]["choice"] == "approve"
    assert resolved[0]["payload"]["auto"] is False
    assert resolved[0]["node_id"] == "gate"


def test_resolve_double_resolve_refused(log_dir):
    _suspended_run(log_dir, "r")
    resolve_gate(log_dir, "r", "approve")
    with pytest.raises(GateResolutionError) as ei:
        resolve_gate(log_dir, "r", "abort")
    assert ei.value.reason == "not_at_gate"


# ---- phase 2: server POST endpoint --------------------------------------


def _post(url, obj):
    import urllib.error
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_server_resolve_happy_path(running_server):
    # srv-gate is suspended on options approve/abort (see _suspended_run).
    status, body = _post(running_server + "/api/gates/srv-gate/resolve",
                         {"choice": "approve"})
    assert status == 200
    assert body["resolved"] is True
    assert body["choice"] == "approve"
    # it now drops out of the pending-gate queue.
    _, gbody = _get(running_server + "/api/gates")
    assert json.loads(gbody)["gates"] == []


def test_server_resolve_invalid_choice_409(running_server):
    status, body = _post(running_server + "/api/gates/srv-gate/resolve",
                         {"choice": "bogus"})
    assert status == 409
    assert body["reason"] == "invalid_choice"


def test_server_resolve_not_at_gate_409(running_server):
    status, body = _post(running_server + "/api/gates/srv-run/resolve",
                         {"choice": "approve"})
    assert status == 409
    assert body["reason"] == "not_at_gate"


def test_server_resolve_unknown_run_404(running_server):
    status, body = _post(running_server + "/api/gates/ghost/resolve",
                         {"choice": "approve"})
    assert status == 404
    assert body["reason"] == "run_not_found"


def test_server_resolve_missing_choice_400(running_server):
    status, body = _post(running_server + "/api/gates/srv-gate/resolve", {})
    assert status == 400


# ---- phase 2: the real-kernel integration proof -------------------------


async def test_kernel_resume_consumes_dashboard_resolution(log_dir):
    """The premise of option (A): a real engine resume routes on the choice the
    dashboard wrote. Without this, the write path would be cosmetic."""
    from requiem.dsl import AgentRegistry, VerbRegistry, WorkflowBuilder
    from requiem.kernel import Completed, Engine, Suspended
    from requiem.outcomes import NeedsHuman, Success
    from requiem.toolbelt import Toolbelt

    def build():
        return (WorkflowBuilder("gatewf", module="t", version="1")
            .entry("start")
            .script("start", verb="start")
                .edge("start", on="success", to="gate")
            .script("gate", verb="gate")
                .edge("gate", on="needs_human:approve", to="end_ok")
                .edge("gate", on="needs_human:abort", to="end_no")
                .edge("gate", on="success", to="end_ok")
            .terminate("end_ok", disposition="completed")
            .terminate("end_no", disposition="failed")
            .humanize({}).build())

    def verbs():
        v = VerbRegistry()

        @v.register("start")
        def _s(ctx):
            return Success(value={})

        @v.register("gate")
        def _g(ctx):
            return NeedsHuman(gate="g", prompt="Approve?", options=("approve", "abort"))
        return v

    def engine():
        return Engine(workflow=build(), verbs=verbs(), agents=AgentRegistry(),
                      provider=None, toolbelt=Toolbelt.real(), log_dir=log_dir,
                      gate_handler=None)

    # 1) no handler ⇒ suspends at the gate
    out = await engine().run("gw")
    assert isinstance(out, Suspended)

    # 2) the dashboard resolves it (append-only)
    resolve_gate(log_dir, "gw", "approve")

    # 3) a fresh engine resumes the same run and routes on the recorded choice
    out2 = await engine().run("gw")
    assert isinstance(out2, Completed)
    assert out2.final_node == "end_ok"

    # 4) the abort branch routes the other way
    await engine().run("gw2")
    resolve_gate(log_dir, "gw2", "abort")
    out3 = await engine().run("gw2")
    assert out3.final_node == "end_no"


# ---- opt-in auto-resume (ADR-0019 ergonomic; default OFF) ---------------


def test_resolve_workflow_module_reads_from_log(log_dir):
    from requiem.dashboard.auto_resume import resolve_workflow_module
    _suspended_run(log_dir, "wfmod")
    assert resolve_workflow_module(log_dir, "wfmod") == "needs-human-wf"
    assert resolve_workflow_module(log_dir, "nope") is None


def test_spawn_resume_builds_resume_argv(log_dir, monkeypatch):
    """spawn_resume fires `requiem resume <module> <run_id>` (mocked Popen)."""
    from requiem.dashboard import auto_resume
    _suspended_run(log_dir, "sp")

    captured = {}

    class _FakePopen:
        def __init__(self, argv, **kw):
            captured["argv"] = argv
            captured["kw"] = kw

    monkeypatch.setattr(auto_resume.subprocess, "Popen", _FakePopen)
    auto_resume.spawn_resume(log_dir, "sp")
    argv = captured["argv"]
    assert "requiem.cli.main" in argv
    assert "resume" in argv
    assert "needs-human-wf" in argv     # the workflow module
    assert "sp" in argv                 # the run id
    assert str(log_dir) in argv


def test_spawn_resume_raises_without_workflow_module(log_dir, monkeypatch):
    from requiem.dashboard import auto_resume
    # A run whose log has no workflow identity.
    _write_run(log_dir, "noident", [
        {"kind": "gate_opened", "node_id": "g",
         "payload": {"prompt": "?", "options": ["approve"]}},
    ])
    monkeypatch.setattr(auto_resume.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
    import pytest
    with pytest.raises(auto_resume.AutoResumeError):
        auto_resume.spawn_resume(log_dir, "noident")


def test_server_default_does_not_auto_resume(log_dir, monkeypatch):
    """Default server (auto_resume off) appends the decision but never spawns."""
    import urllib.request
    from requiem.dashboard import auto_resume
    monkeypatch.setattr(auto_resume.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
    _suspended_run(log_dir, "noauto")
    httpd = build_server(log_dir, host="127.0.0.1", port=0)  # default: off
    import threading
    t = threading.Thread(target=httpd.handle_request, daemon=True)
    t.start()
    port = httpd.socket.getsockname()[1]
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/gates/noauto/resolve",
        data=json.dumps({"choice": "approve"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        payload = json.loads(resp.read())
    httpd.server_close()
    assert payload["auto_resume"] == "disabled"


def test_server_auto_resume_spawns_on_resolve(log_dir, monkeypatch):
    """With auto_resume=True a successful resolve spawns requiem resume."""
    import urllib.request
    from requiem.dashboard import auto_resume
    spawned = {}

    class _FakePopen:
        def __init__(self, argv, **kw):
            spawned["argv"] = argv

    monkeypatch.setattr(auto_resume.subprocess, "Popen", _FakePopen)
    _suspended_run(log_dir, "withauto")
    httpd = build_server(log_dir, host="127.0.0.1", port=0, auto_resume=True)
    import threading
    t = threading.Thread(target=httpd.handle_request, daemon=True)
    t.start()
    port = httpd.socket.getsockname()[1]
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/gates/withauto/resolve",
        data=json.dumps({"choice": "approve"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        payload = json.loads(resp.read())
    httpd.server_close()
    assert payload["auto_resume"] == "spawned"
    assert "resume" in spawned["argv"]
    assert "withauto" in spawned["argv"]


# ---- ADR-0031 / R4: /api/state/<item_id> --------------------------------


def _fake_state_provider(*, item_id: int, log_dir, ado_repo, github_repo):
    """A deterministic in-memory state provider for the dashboard tests.

    Returns a small but structurally complete projection payload. The
    handler is supposed to JSON-encode this verbatim — we assert that
    on the wire.
    """
    if item_id == 555:
        # Simulate the "real twig.show_async raised KeyError" path —
        # the handler maps it to 404 (item not found).
        raise KeyError(f"no item {item_id}")
    if item_id == 666:
        # Any non-KeyError exception → 503 (projection failed).
        raise RuntimeError("simulated provider failure")
    return {
        "root_item_id": item_id,
        "computed_at": "2026-06-22T00:00:00+00:00",
        "tree": {
            "item_id": item_id,
            "title": f"item {item_id}",
            "work_item_type": "Scenario",
            "state": "Active",
            "start_date": "2026-06-01T00:00:00Z",
            "target_date": "2026-06-30T00:00:00Z",
            "finish_date": None,
            "parent_id": None,
            "impl_branch": f"impl/{item_id}/{item_id}",
            "leaf_pr_number": None,
            "leaf_pr_url": None,
            "leaf_pr_state": None,
            "children": [],
        },
        # Surface the query args back so tests can assert pass-through.
        "_test_echo": {
            "ado_repo": ado_repo,
            "github_repo": github_repo,
            "log_dir": str(log_dir),
        },
    }


@pytest.fixture
def state_server(log_dir):
    httpd = build_server(
        log_dir, host="127.0.0.1", port=0,
        state_provider=_fake_state_provider,
    )
    import threading
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    host, port = httpd.socket.getsockname()
    base = f"http://{host}:{port}"
    try:
        yield base
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def test_state_route_returns_projection_json(state_server):
    status, body = _get(state_server + "/api/state/42")
    assert status == 200
    payload = json.loads(body)
    assert payload["root_item_id"] == 42
    assert payload["tree"]["item_id"] == 42
    assert payload["tree"]["title"] == "item 42"
    assert payload["tree"]["state"] == "Active"
    assert payload["tree"]["start_date"] == "2026-06-01T00:00:00Z"
    # Default: no repo arg → echoed as None.
    assert payload["_test_echo"]["ado_repo"] is None
    assert payload["_test_echo"]["github_repo"] is None


def test_state_route_threads_ado_repo_query_arg(state_server):
    status, body = _get(
        state_server + "/api/state/42?ado_repo=org/proj/repo"
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["_test_echo"]["ado_repo"] == "org/proj/repo"


def test_state_route_threads_github_repo_query_arg(state_server):
    status, body = _get(state_server + "/api/state/42?github_repo=Owner/Repo")
    assert status == 200
    payload = json.loads(body)
    assert payload["_test_echo"]["github_repo"] == "Owner/Repo"


def test_state_route_400_on_non_integer_item(state_server):
    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as ei:
        _get(state_server + "/api/state/not-an-int")
    assert ei.value.code == 400
    body = json.loads(ei.value.read())
    assert "not an integer" in body["error"]


def test_state_route_400_when_both_repo_args_set(state_server):
    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as ei:
        _get(
            state_server
            + "/api/state/42?ado_repo=a/b/c&github_repo=X/Y"
        )
    assert ei.value.code == 400
    body = json.loads(ei.value.read())
    assert "mutually exclusive" in body["error"]


def test_state_route_404_on_unknown_item(state_server):
    """Provider raises KeyError → 404 (item genuinely doesn't exist)."""
    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as ei:
        _get(state_server + "/api/state/555")
    assert ei.value.code == 404


def test_state_route_503_on_provider_failure(state_server):
    """Any non-KeyError exception → 503 with the error in the body.

    The point: a missing twig binary / expired token MUST NOT crash
    the dashboard process. The server keeps serving other routes.
    """
    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as ei:
        _get(state_server + "/api/state/666")
    assert ei.value.code == 503
    body = json.loads(ei.value.read())
    assert "projection failed" in body["error"]
    assert "RuntimeError" in body["error"]
    # Verify the server is still alive after the 503.
    status, _ = _get(state_server + "/healthz")
    assert status == 200

