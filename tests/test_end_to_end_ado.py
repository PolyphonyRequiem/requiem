"""ADR-0024 step 5: end-to-end driver wires --ado-repo through the full pipeline.

Step 4 proved the trunk-topology workflows are platform-neutral when wired
via toolbelt.repo. Step 5 proves the driver itself does the right thing
when an operator passes ado_repo= instead of github_repo=: it resolves an
AdoClient (or accepts an injected stub), threads it through the topology
phases via _topology_toolbelt, and projects the operator's choice back
out in PipelineResult.ado_repo / IntegrationResult.ado_repo.

These tests stub the engine factories (no real ADO calls, no Hermes
workers spawned). The clients/test_azuredevops.py + the
test_trunk_topology_against_ado.py modules already cover the real-client
and workflow-level behaviour. Here we're testing the driver's choreography.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from requiem.clients.azuredevops import FakeAdoClient
from requiem.end_to_end import (
    IntegrationResult,
    _resolve_repo_target,
    _topology_toolbelt,
    integrate_pipeline,
    run_pipeline,
)
from requiem.kernel import Completed
from requiem.workflows.feature_pr import ItemDisposition


# ---- _resolve_repo_target ----------------------------------------------


def test_resolve_repo_target_neither_returns_none_pair():
    """Executor-only path: no repo wired."""
    assert _resolve_repo_target(github_repo=None, ado_repo=None, gh=None) == (None, None)


def test_resolve_repo_target_mutually_exclusive_raises():
    """Operator passed both → fail closed, never silently route to one."""
    with pytest.raises(ValueError) as exc:
        _resolve_repo_target(
            github_repo="Acme/Widget", ado_repo="Contoso/P/widget", gh=None,
        )
    assert "mutually exclusive" in str(exc.value)


def test_resolve_repo_target_ado_constructs_ado_client():
    """ADO path: lazy-imports and constructs AdoClient. Confirms the import
    works (azure-identity is available) and the client satisfies the
    RepoPlatform Protocol it was advertised as."""
    from requiem.clients.azuredevops import AdoClient
    from requiem.clients.repo import RepoPlatform
    repo_id, client = _resolve_repo_target(
        github_repo=None, ado_repo="Contoso/P/widgets", gh=None,
    )
    assert repo_id == "Contoso/P/widgets"
    assert isinstance(client, AdoClient)
    assert isinstance(client, RepoPlatform)


def test_resolve_repo_target_github_reuses_injected_gh():
    """When the caller injects a gh stub, the resolver should pass it
    through rather than constructing a fresh GhClient."""

    class _StubGh:
        pass

    stub = _StubGh()
    repo_id, client = _resolve_repo_target(
        github_repo="Acme/Widget", ado_repo=None, gh=stub,
    )
    assert repo_id == "Acme/Widget"
    assert client is stub


# ---- _topology_toolbelt -------------------------------------------------


def test_topology_toolbelt_wires_repo_client_at_toolbelt_repo():
    """The new helper installs the repo client at toolbelt.repo (ADR-0024
    step 4 invariant). gh remains the real GhClient from Toolbelt.real()
    so non-trunk workflows that read it directly still work."""
    ado = FakeAdoClient()
    tb = _topology_toolbelt(twig=None, repo_client=ado)
    assert tb.repo is ado
    assert tb.gh is not None       # legacy field still populated
    assert tb.gh is not ado        # but distinct from the ADO impl


# ---- run_pipeline with ado_repo (driver routes through FakeAdoClient) --


def _write_log(log_dir: Path, run_id: str, events: list[tuple[str, dict]]) -> None:
    path = log_dir / f"{run_id}.events.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for node_id, outcome in events:
            fh.write(json.dumps({
                "kind": "verb_completed", "node_id": node_id,
                "payload": {"outcome": outcome},
            }) + "\n")


def _record_plan(item_id: int = 500) -> dict:
    return {
        "kind": "success",
        "value": {
            "item_id": item_id, "item_title": f"item {item_id}",
            "summary": "do the thing", "decomposable": False,
            "final_verdict": "approved",
            "plan_artifact": f"/logs/plan-{item_id}.plan.tree.json",
        },
    }


def _stub_factories(*, captured: dict):
    """Build stub factories that the driver chains through. They write
    the minimum durable events the driver reads from each stage. The
    `captured` dict accumulates the toolbelts they were given — that's
    how we assert the ADO-vs-GitHub plumbing actually happened."""

    def planning_factory(log_dir, *, item_id, twig=None, provider=None,
                         gate_handler=None, process_config=None):
        class _E:
            async def run(self, run_id):
                _write_log(log_dir, run_id, [
                    ("record_plan", _record_plan(item_id=item_id)),
                ])
                return Completed(run_id, "completed", "end", {})
        return _E()

    def executor_factory(log_dir, *, inputs=None, toolbelt=None, gate_handler=None):
        captured.setdefault("executor_toolbelts", []).append(toolbelt)
        leaves = [{"leaf_id": leaf.leaf_id, "branch": leaf.branch}
                  for leaf in inputs.leaves]

        class _E:
            async def run(self, run_id):
                _write_log(log_dir, run_id, [
                    ("resolve_leaves", {"kind": "success",
                                        "value": {"leaves": leaves}}),
                ])
                return Completed(run_id, "completed", "end", {})
        return _E()

    def trunk_bootstrap_factory(log_dir, *, inputs=None, toolbelt=None,
                                gate_handler=None):
        captured.setdefault("trunk_toolbelts", []).append(toolbelt)
        captured["trunk_inputs"] = inputs

        class _E:
            async def run(self, run_id):
                _write_log(log_dir, run_id, [
                    ("start", {
                        "kind": "success",
                        "value": {
                            "intent": "trunk_bootstrap",
                            "root_item_id": inputs.root_item_id,
                            "repo": inputs.repo,
                            "trunk_branch": inputs.trunk_branch,
                            "base_branch": inputs.base_branch,
                            "dry_run": inputs.dry_run,
                        },
                    }),
                    ("ensure_trunk", {
                        "kind": "success",
                        "value": {
                            "trunk_branch": inputs.trunk_branch,
                            "base_branch": inputs.base_branch,
                            "base_sha": "abc123",
                            "created": True, "exists": False,
                            "dry_run": inputs.dry_run,
                        },
                    }),
                ])
                return Completed(run_id, "completed", "end_success", {})
        return _E()

    def leaf_pr_factory(log_dir, *, inputs=None, toolbelt=None,
                        gate_handler=None):
        captured.setdefault("leaf_pr_toolbelts", []).append(toolbelt)
        leaves = [{"leaf_id": lid, "pr_number": 8000 + i}
                  for i, lid in enumerate(inputs.leaf_ids)]

        class _E:
            async def run(self, run_id):
                _write_log(log_dir, run_id, [
                    ("open_leaf_prs", {
                        "kind": "success",
                        "value": {
                            "trunk_branch": inputs.trunk_branch,
                            "verdict": "opened",
                            "leaves": leaves,
                            "dry_run": inputs.dry_run,
                        },
                    }),
                ])
                return Completed(run_id, "completed", "end_success", {})
        return _E()

    return planning_factory, executor_factory, trunk_bootstrap_factory, leaf_pr_factory


async def test_ado_repo_routes_topology_through_ado_client(tmp_path):
    """The load-bearing test: run_pipeline(ado_repo=...) wires the
    AdoClient (here: a Fake) into both the trunk_bootstrap and leaf_pr
    toolbelts at toolbelt.repo, and projects ado_repo back in the
    PipelineResult."""
    captured: dict = {}
    pf, ef, tbf, lpf = _stub_factories(captured=captured)
    ado = FakeAdoClient(refs={("Contoso/P/widgets", "main"): "abc"})
    result = await run_pipeline(
        500,
        log_dir=tmp_path,
        board="requiem-500",
        assignee="w",
        live=True,
        ado_repo="Contoso/P/widgets",
        repo_client=ado,
        base_branch="main",   # skip the platform probe path
        planning_factory=pf,
        executor_factory=ef,
        trunk_bootstrap_factory=tbf,
        leaf_pr_factory=lpf,
    )
    # Result projects the ADO choice back out.
    assert result.ado_repo == "Contoso/P/widgets"
    assert result.github_repo is None
    assert result.trunk_branch == "feature/500"
    assert result.trunk_verdict == "created"
    # Both topology phases ran with FakeAdoClient wired at toolbelt.repo.
    trunk_tb = captured["trunk_toolbelts"][0]
    leaf_tb = captured["leaf_pr_toolbelts"][0]
    assert trunk_tb.repo is ado
    assert leaf_tb.repo is ado
    # Sanity: the driver passed the ADO repo string through to the inputs.
    assert captured["trunk_inputs"].repo == "Contoso/P/widgets"


async def test_ado_repo_threads_repo_client_to_executor_toolbelt(tmp_path):
    """ADR-0025 Gap B follow-up: the executor stage must also receive
    ``repo=repo_client`` on its toolbelt so future kanban/in-process
    workers can do ADO-side work (open per-leaf PRs, query branches)
    without falling back to a fake GhClient or crashing.

    Without this, the executor's `exec_toolbelt` was built from
    `Toolbelt.real()` (GitHub-only `repo` field) regardless of whether
    the driver was passed `ado_repo`. The kanban backend's eventual
    real workers — and the in-process fanout path that already runs
    per-leaf `implementation` engines — both need the ADO `repo` to
    propagate through.
    """
    captured: dict = {}
    pf, ef, tbf, lpf = _stub_factories(captured=captured)
    ado = FakeAdoClient(refs={("Contoso/P/widgets", "main"): "abc"})
    await run_pipeline(
        500,
        log_dir=tmp_path,
        board="requiem-500",
        assignee="w",
        live=True,
        ado_repo="Contoso/P/widgets",
        repo_client=ado,
        base_branch="main",
        planning_factory=pf,
        executor_factory=ef,
        trunk_bootstrap_factory=tbf,
        leaf_pr_factory=lpf,
    )
    # The executor's toolbelt carries the same FakeAdoClient at .repo —
    # the same identity the trunk_bootstrap + leaf_pr stages received.
    exec_tbs = captured.get("executor_toolbelts", [])
    assert exec_tbs, "executor_factory was never invoked"
    exec_tb = exec_tbs[0]
    assert exec_tb.repo is ado, (
        "executor toolbelt missing repo=AdoClient — workers would fall "
        "back to Toolbelt.real()'s GhClient and crash against an ADO repo"
    )


async def test_github_repo_path_still_projects_github_field(tmp_path):
    """Back-compat: the original github_repo path still populates
    PipelineResult.github_repo and leaves ado_repo None."""
    captured: dict = {}
    pf, ef, tbf, lpf = _stub_factories(captured=captured)
    # Inject a stub gh client so we don't need the real `gh` binary.
    from requiem.clients.repo import RepoPullRequest

    class _StubGh:
        async def branch_sha(self, repo, branch): return "abc"

        async def ensure_branch_ref(self, repo, branch, source_sha): return True

        async def find_open_pr_for_branch(self, repo, *, head, limit=30):
            return []

        async def pr_view(self, repo, number):
            return RepoPullRequest(
                number=number, title="t", state="open", merged_at=None,
                head="h", base="b", url="u",
            )

        async def pr_create(self, repo, *, title, body, head, base):
            return RepoPullRequest(
                number=1, title=title, state="open", merged_at=None,
                head=head, base=base, url="u",
            )

        async def default_branch(self, repo): return "main"

    stub_gh = _StubGh()
    result = await run_pipeline(
        500,
        log_dir=tmp_path,
        board="requiem-500",
        assignee="w",
        live=True,
        github_repo="Acme/Widget",
        repo_client=stub_gh,
        base_branch="main",
        planning_factory=pf,
        executor_factory=ef,
        trunk_bootstrap_factory=tbf,
        leaf_pr_factory=lpf,
    )
    assert result.github_repo == "Acme/Widget"
    assert result.ado_repo is None


async def test_passing_both_github_and_ado_raises(tmp_path):
    """Driver-level fail-closed for the mutually-exclusive contract."""
    captured: dict = {}
    pf, ef, tbf, lpf = _stub_factories(captured=captured)
    with pytest.raises(ValueError) as exc:
        await run_pipeline(
            500, log_dir=tmp_path, board="requiem-500", assignee="w",
            live=True,
            github_repo="Acme/Widget",
            ado_repo="Contoso/P/widgets",
            planning_factory=pf, executor_factory=ef,
            trunk_bootstrap_factory=tbf, leaf_pr_factory=lpf,
        )
    assert "mutually exclusive" in str(exc.value)


async def test_executor_only_path_unaffected(tmp_path):
    """Neither github_repo nor ado_repo set → executor-only, no topology.
    PipelineResult has both repo fields as None."""
    captured: dict = {}
    pf, ef, tbf, lpf = _stub_factories(captured=captured)
    result = await run_pipeline(
        500, log_dir=tmp_path, board="requiem-500", assignee="w",
        live=True,
        planning_factory=pf, executor_factory=ef,
        trunk_bootstrap_factory=tbf, leaf_pr_factory=lpf,
    )
    assert result.status == "delivered"
    assert result.github_repo is None
    assert result.ado_repo is None
    # Topology stages NEVER ran.
    assert captured.get("trunk_toolbelts") is None
    assert captured.get("leaf_pr_toolbelts") is None


# ---- integrate_pipeline with ado_repo -----------------------------------


async def test_integrate_pipeline_ado_repo_projects_back(tmp_path):
    """integrate_pipeline(ado_repo=...) routes feature_pr through the
    ADO client and surfaces ado_repo on the IntegrationResult."""
    captured: dict = {}
    ado = FakeAdoClient()

    def feature_pr_factory(log_dir, *, inputs=None, toolbelt=None,
                           gate_handler=None):
        captured["feature_toolbelt"] = toolbelt
        captured["feature_inputs"] = inputs

        class _E:
            async def run(self, run_id):
                _write_log(log_dir, run_id, [
                    ("verify_readiness", {
                        "kind": "success",
                        "value": {
                            "trunk_branch": inputs.trunk_branch,
                            "leaves_ready": len(inputs.leaves),
                            "leaves_total": len(inputs.leaves),
                            "dispositions_total": len(inputs.dispositions),
                            "dispositions_satisfied": len(inputs.dispositions),
                        },
                    }),
                    ("open_pr", {
                        "kind": "success",
                        "value": {
                            "pr_number": 9999,
                            "pr_url": "https://dev.azure.com/.../9999",
                            "title": "trunk PR",
                            "reused_existing": False,
                        },
                    }),
                ])
                return Completed(run_id, "completed", "end_success", {})
        return _E()

    from requiem.workflows.feature_pr import LeafPr
    result = await integrate_pipeline(
        500,
        log_dir=tmp_path,
        ado_repo="Contoso/P/widgets",
        repo_client=ado,
        leaves=(LeafPr(leaf_id="L1", pr_number=4242),),
        base_branch="main",
        live=True,
        dispositions=(ItemDisposition(item_id=500, state="Done", satisfied=True),),
        feature_pr_factory=feature_pr_factory,
    )
    assert isinstance(result, IntegrationResult)
    assert result.ado_repo == "Contoso/P/widgets"
    assert result.github_repo is None
    assert result.status == "opened"
    assert result.feature_pr_number == 9999
    # Toolbelt wired through with the FakeAdoClient at .repo.
    assert captured["feature_toolbelt"].repo is ado
    assert captured["feature_inputs"].repo == "Contoso/P/widgets"
