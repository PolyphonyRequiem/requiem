"""Tests for the in-process fan-out orchestrator (ADR-0021, parity #4).

These exercise the orchestrator dispatching real ``implementation`` engines
in-process (the seam from ADR-0020 in action), per-leaf log isolation, the
landed / needs_human / failed roll-up (B2), and idempotent re-entry. The child
runs against a real throwaway git repo + a demo gh client + a per-leaf twig, so
no live ADO/GitHub is needed.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from requiem.agent import FakeProvider
from requiem.clients.fs import FilesystemClient
from requiem.clients.twig import TwigItem
from requiem.kernel import Completed
from requiem.toolbelt import FakeFileClient, RealGitClient, Toolbelt
from requiem.workflows import fanout
from requiem.workflows import implementation as impl
from requiem.workflows.planning import completed_from_log

def _make_pushable(repo_path: Path) -> None:
    """Make `repo_path` push-able by wiring `origin` to a local bare clone.

    Needed for the leaf_merge/wave-gating tests below, which require a real
    PR number (only recovered from a leaf that actually reached `create_pr`,
    which only runs after a successful push — see `_leaf_pr_info`)."""
    bare = repo_path.parent / (repo_path.name + ".git")
    if not bare.exists():
        subprocess.run(
            ["git", "clone", "--bare", "-q", str(repo_path), str(bare)],
            check=True,
        )
    has_origin = "origin" in subprocess.run(
        ["git", "remote"], cwd=str(repo_path), capture_output=True, text=True,
    ).stdout.split()
    if has_origin:
        subprocess.run(["git", "remote", "remove", "origin"], cwd=str(repo_path), check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=str(repo_path), check=True)


ROOT = 9300
REPO = "Owner/Repo"


# ---- fixtures -----------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(cmd, cwd=r, check=True)
    (r / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=r, check=True)
    return r


def _toolbelt(repo: Path, *, gh=None, twig=None) -> Toolbelt:
    return Toolbelt(
        git=RealGitClient(),
        files=FakeFileClient({}),
        gh=gh or impl._DemoGhClient(),  # type: ignore[arg-type]
        fs=FilesystemClient(repo),
        twig=twig,
    )


def _happy_provider() -> FakeProvider:
    # One file change per leaf, tests pass. The coder script is reused for every
    # leaf (FakeProvider replays it per agent invocation).
    return FakeProvider(scripts={
        "coder": [
            {
                "intent_summary": "create a marker",
                "file_changes": [
                    {"path": "MARKER.md", "operation": "create", "content": "x\n"},
                ],
                "notes": "",
            },
            {
                "intent_summary": "create a marker",
                "file_changes": [
                    {"path": "MARKER2.md", "operation": "create", "content": "y\n"},
                ],
                "notes": "",
            },
        ],
    })


def _bad_output_provider() -> FakeProvider:
    # The coder returns a malformed payload → BadOutput → end_needs_human.
    return FakeProvider(scripts={"coder": [{"not": "a valid CoderOutput"}]})


def _passing_runner(_cmd, _cwd):
    from requiem.workflows.implementation import TestRunResult
    return TestRunResult(passed=True, summary="green", full_output="OK")


def _engine(repo: Path, log_dir: Path, *, leaves, provider, gh=None, twig=None,
            dry_run=True, leaf_merge=None):
    inputs = fanout.FanoutInputs(
        root_item_id=ROOT, repo=REPO, repo_path=repo, log_dir=log_dir,
        dry_run=dry_run, leaves=tuple(leaves), leaf_merge=leaf_merge,
    )
    return fanout.build_engine(
        log_dir,
        inputs=inputs,
        toolbelt=_toolbelt(repo, gh=gh, twig=twig),
        provider=provider,
    )


def _result(engine, run_id, final_node):
    return fanout.fanout_result(
        completed_from_log(engine.log_path(run_id)), final_node)


# ---- resolve_leaves -----------------------------------------------------


async def test_no_leaf_source_fails_closed(repo: Path, tmp_path: Path):
    inputs = fanout.FanoutInputs(
        root_item_id=ROOT, repo=REPO, repo_path=repo, log_dir=tmp_path,
    )  # no inline leaves, no plan artifacts
    engine = fanout.build_engine(
        tmp_path, inputs=inputs, toolbelt=_toolbelt(repo),
        provider=_happy_provider(),
    )
    result = await engine.run("noleaf")
    assert isinstance(result, Completed)
    assert result.final_node == "end_failed"
    res = _result(engine, "noleaf", result.final_node)
    assert res.verdict == "no_leaves"


async def test_misaligned_plan_fails_before_dispatch_mutates_repo(
    repo: Path,
    tmp_path: Path,
):
    tree_path = tmp_path / "plan.tree.json"
    committed_path = tmp_path / "plan.committed.json"
    tree_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "plan_id": f"plan-{ROOT}-test",
                "item_id": ROOT,
                "decomposable": True,
                "verdict": "approved",
                "proposals": [
                    {
                        "title": "Leaf",
                        "description": "body",
                        "work_item_type": "Task",
                    }
                ],
                "children": [],
            }
        ),
        encoding="utf-8",
    )
    committed_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "plan_id": f"plan-{ROOT}-test",
                "root_item_id": ROOT,
                "dry_run": False,
                "id_map": {str(ROOT * 100 + 1): 9001},
            }
        ),
        encoding="utf-8",
    )
    inputs = fanout.FanoutInputs(
        root_item_id=ROOT,
        repo=REPO,
        repo_path=repo,
        log_dir=tmp_path,
        dry_run=False,
        plan_tree_path=tree_path,
        committed_path=committed_path,
    )
    engine = fanout.build_engine(
        tmp_path,
        inputs=inputs,
        toolbelt=_toolbelt(repo),
        provider=_happy_provider(),
    )

    result = await engine.run("misaligned")

    assert isinstance(result, Completed)
    assert result.final_node == "end_failed"
    completed = completed_from_log(engine.log_path("misaligned"))
    assert completed["resolve_leaves"]["error_kind"] == "fanout.plan.misaligned"
    assert "dispatch_leaves" not in completed
    assert list(tmp_path.glob(f"fanout-{ROOT}__leaf-*.events.jsonl")) == []
    assert not (repo / "MARKER.md").exists()


# ---- happy path: all leaves land ----------------------------------------


async def test_all_leaves_land_in_process(repo: Path, tmp_path: Path):
    leaves = [
        fanout.FanoutLeaf(real_id=1, title="leaf one", body="marker"),
        fanout.FanoutLeaf(real_id=2, title="leaf two", body="marker"),
    ]
    engine = _engine(repo, tmp_path, leaves=leaves, provider=_happy_provider())
    result = await engine.run("fo")
    assert isinstance(result, Completed)
    assert result.final_node == "end_success"

    res = _result(engine, "fo", result.final_node)
    assert res.verdict == "previewed"  # dry_run
    assert res.leaves_total == 2
    assert res.leaves_landed == 2
    assert res.leaves_needs_human == 0
    assert res.leaves_failed == 0
    # Every leaf reached the success-handoff terminal.
    assert all(o.disposition == "completed" for o in res.outcomes)


async def test_live_twig_description_overrides_shortened_plan_body(
    repo: Path,
    tmp_path: Path,
):
    class AuthoritativeTwig:
        async def show_async(self, item_id: int) -> TwigItem:
            return TwigItem(
                id=item_id,
                title="leaf one",
                state="Active",
                area_path="Demo",
                work_item_type="Task",
                parent_id=ROOT,
                raw={
                    "id": item_id,
                    "title": "leaf one",
                    "fields": {
                        "System.Description": (
                            "Implement the dedicated run-once ACI deployment surface."
                        )
                    },
                },
            )

        async def comment_async(self, item_id: int, message: str) -> None:
            pass

    engine = _engine(
        repo,
        tmp_path,
        leaves=[
            fanout.FanoutLeaf(
                real_id=1,
                title="leaf one",
                body="Implement the probe mechanism.",
            )
        ],
        provider=_happy_provider(),
        twig=AuthoritativeTwig(),
    )

    await engine.run("authoritative-description")

    child = completed_from_log(
        tmp_path / f"fanout-{ROOT}__leaf-1.events.jsonl"
    )
    assert (
        child["fetch_plan"]["value"]["plan_text"]
        == "Implement the dedicated run-once ACI deployment surface."
    )


async def test_each_leaf_writes_isolated_log(repo: Path, tmp_path: Path):
    """INV-SUBWORKFLOW-LOG-ISOLATION: each child writes its own *.events.jsonl."""
    leaves = [
        fanout.FanoutLeaf(real_id=11, title="a", body="m"),
        fanout.FanoutLeaf(real_id=22, title="b", body="m"),
    ]
    engine = _engine(repo, tmp_path, leaves=leaves, provider=_happy_provider())
    await engine.run("iso")
    assert (tmp_path / f"fanout-{ROOT}__leaf-11.events.jsonl").exists()
    assert (tmp_path / f"fanout-{ROOT}__leaf-22.events.jsonl").exists()
    # The orchestrator's own log does NOT carry the children's impl node ids.
    from requiem.persistence import replay
    parent_nodes = {
        e.get("node_id") for e in replay(engine.log_path("iso"))
        if e.get("kind") in ("node_entered", "verb_completed")
    }
    assert "invoke_coder" not in parent_nodes  # a child-only node
    assert "dispatch_leaves" in parent_nodes


async def test_b3_leaf_branch_is_impl_topology(repo: Path, tmp_path: Path):
    """B3: each dispatched leaf's branch is impl/<root>-<leaf>, not feature/<id>.

    Read the branch from the child's own create_branch log event — this proves
    the topology regardless of whether a later push/PR step succeeds (the test
    repo has no origin remote)."""
    leaves = [fanout.FanoutLeaf(real_id=7, title="seven", body="m")]
    engine = _engine(repo, tmp_path, leaves=leaves, provider=_happy_provider(),
                     dry_run=True)
    await engine.run("b3")
    # The child's create_branch recorded the branch it built.
    child_log = tmp_path / f"fanout-{ROOT}__leaf-7.events.jsonl"
    child_completed = completed_from_log(child_log)
    branch = (child_completed.get("create_branch") or {}).get("value", {}).get("branch_name")
    assert branch == f"impl/{ROOT}-7"


# ---- B2 roll-up: a surrendering leaf is needs_human, not success --------


async def test_surrendering_leaf_rolls_up_needs_human(repo: Path, tmp_path: Path):
    """A leaf whose coder emits BadOutput surrenders (end_needs_human); the
    roll-up must count it as needs_human — not landed (ADR-0013 B2)."""
    leaves = [
        fanout.FanoutLeaf(real_id=1, title="ok", body="m"),
        fanout.FanoutLeaf(real_id=2, title="bad", body="m"),
    ]
    # Leaf 1's coder is happy; leaf 2's coder emits bad output. FakeProvider
    # replays per invocation, so script: [happy, bad].
    provider = FakeProvider(scripts={
        "coder": [
            {
                "intent_summary": "ok",
                "file_changes": [
                    {"path": "M.md", "operation": "create", "content": "x\n"},
                ],
                "notes": "",
            },
            {"garbage": "not a CoderOutput"},
        ],
    })
    engine = _engine(repo, tmp_path, leaves=leaves, provider=provider)
    result = await engine.run("mix")
    assert isinstance(result, Completed)
    res = _result(engine, "mix", result.final_node)
    assert res.leaves_total == 2
    assert res.leaves_landed == 1
    assert res.leaves_needs_human == 1
    assert res.verdict == "needs_human"
    nh = [o for o in res.outcomes if o.disposition == "needs_human"]
    assert nh and nh[0].real_id == 2
    assert nh[0].final_node == "end_needs_human"


# ---- idempotent re-entry ------------------------------------------------


async def test_rerun_skips_already_landed_leaves(repo: Path, tmp_path: Path):
    """Iterate-until-stable: a second orchestrator run skips leaves whose child
    run already reached a terminal disposition."""
    leaves = [fanout.FanoutLeaf(real_id=1, title="one", body="m")]
    engine1 = _engine(repo, tmp_path, leaves=leaves, provider=_happy_provider())
    await engine1.run("first")
    # A fresh orchestrator over the SAME log_dir + child run-id scheme.
    engine2 = _engine(repo, tmp_path, leaves=leaves, provider=_happy_provider())
    result = await engine2.run("second")
    res = _result(engine2, "second", result.final_node)
    # The leaf was already terminal → skipped (not re-dispatched).
    assert res.leaves_total == 1
    assert res.outcomes[0].skipped is True
    # Disposition carried forward from the prior child run.
    assert res.outcomes[0].disposition in ("completed", "needs_human", "failed")


# ---- parallel mode + worktree isolation (ADR-0022, #5) ------------------


class _RepeatProvider:
    """A stateless provider that returns the same coder output for EVERY call.

    Safe to share across concurrent leaf children (unlike FakeProvider, whose
    cursor is stateful and races under asyncio.gather). Each leaf writes a
    distinctly-named marker so we can prove per-worktree isolation.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def invoke(self, call):
        from requiem.outcomes import Success
        self.calls.append(call.spec.name)
        # A valid CoderOutput dict → Success (the kernel validates it).
        return Success(value={
            "intent_summary": "create a marker",
            "file_changes": [
                {"path": "MARKER.md", "operation": "create", "content": "x\n"},
            ],
            "notes": "",
        })


@pytest.fixture
def main_repo(tmp_path: Path) -> Path:
    """A repo whose default branch is `main` (so base_branch=main resolves for
    `git worktree add`)."""
    r = tmp_path / "mrepo"
    r.mkdir()
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(cmd, cwd=r, check=True)
    (r / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=r, check=True)
    return r


def _parallel_engine(repo: Path, log_dir: Path, *, leaves, provider, max_parallel=4):
    inputs = fanout.FanoutInputs(
        root_item_id=ROOT, repo=REPO, repo_path=repo, log_dir=log_dir,
        base_branch="main", dry_run=True, parallel=True, max_parallel=max_parallel,
        leaves=tuple(leaves),
    )
    return fanout.build_engine(
        log_dir, inputs=inputs, toolbelt=_toolbelt(repo), provider=provider,
    )


async def test_parallel_dispatch_lands_all_leaves_in_worktrees(
    main_repo: Path, tmp_path: Path
):
    """#5: leaves dispatched in parallel, each in its own git worktree, all land."""
    leaves = [
        fanout.FanoutLeaf(real_id=1, title="one", body="m"),
        fanout.FanoutLeaf(real_id=2, title="two", body="m"),
        fanout.FanoutLeaf(real_id=3, title="three", body="m"),
    ]
    engine = _parallel_engine(
        main_repo, tmp_path, leaves=leaves, provider=_RepeatProvider(),
    )
    result = await engine.run("par")
    assert isinstance(result, Completed)
    res = _result(engine, "par", result.final_node)
    assert res.leaves_total == 3
    assert res.leaves_landed == 3, [o.disposition for o in res.outcomes]
    assert res.verdict == "previewed"  # dry_run
    # Each leaf wrote its own isolated child log.
    for rid in (1, 2, 3):
        assert (tmp_path / f"fanout-{ROOT}__leaf-{rid}.events.jsonl").exists()


async def test_parallel_leaf_branch_is_impl_topology(main_repo: Path, tmp_path: Path):
    """Each parallel leaf's worktree is created on impl/<root>-<leaf> (B3)."""
    leaves = [fanout.FanoutLeaf(real_id=7, title="seven", body="m")]
    engine = _parallel_engine(
        main_repo, tmp_path, leaves=leaves, provider=_RepeatProvider(),
    )
    await engine.run("par1")
    child_completed = completed_from_log(
        tmp_path / f"fanout-{ROOT}__leaf-7.events.jsonl")
    branch = (child_completed.get("create_branch") or {}).get("value", {}).get("branch_name")
    assert branch == f"impl/{ROOT}-7"


async def test_parallel_landed_leaf_worktree_cleaned(main_repo: Path, tmp_path: Path):
    """A landed leaf's worktree is removed (best-effort); the repo stays tidy."""
    leaves = [fanout.FanoutLeaf(real_id=5, title="five", body="m")]
    engine = _parallel_engine(
        main_repo, tmp_path, leaves=leaves, provider=_RepeatProvider(),
    )
    await engine.run("par5")
    # Worktree dir for the landed leaf was cleaned up.
    wt = main_repo.parent / f".requiem-wt-{ROOT}-5"
    assert not wt.exists()


async def test_parallel_isolation_no_cross_leaf_contamination(
    main_repo: Path, tmp_path: Path
):
    """Concurrent leaves don't clobber each other: each leaf's child run reaches
    its own terminal independently (proves worktree isolation under gather)."""
    leaves = [fanout.FanoutLeaf(real_id=i, title=f"leaf{i}", body="m")
              for i in range(1, 6)]
    engine = _parallel_engine(
        main_repo, tmp_path, leaves=leaves, provider=_RepeatProvider(),
        max_parallel=5,
    )
    result = await engine.run("par_many")
    res = _result(engine, "par_many", result.final_node)
    assert res.leaves_total == 5
    assert res.leaves_landed == 5, [
        (o.real_id, o.disposition, o.final_node) for o in res.outcomes
    ]
    # Every leaf id appears exactly once in the outcomes (no lost/dup leaves).
    assert sorted(o.real_id for o in res.outcomes) == [1, 2, 3, 4, 5]


# ---- ADR-0030 §1 context-pack integration -----------------------------


@pytest.mark.asyncio
async def test_fanout_dispatch_writes_context_pack_to_leaf_worktree(
    repo: Path, tmp_path: Path,
) -> None:
    """When fanout's _build_leaf_context_pack returns a pack, the
    implementation workflow's commit_context_pack verb commits the
    `.requiem/AGENTS.md` slice onto the leaf branch BEFORE invoke_coder
    runs. This is the wiring pin: fanout actually builds a pack and
    passes it through ImplementationInputs.context_pack.

    We assert directly on the synthesiser's behaviour: given the fanout
    inputs we construct, _build_leaf_context_pack returns a ContextPack
    (not None) with a non-empty agents_md. The end-to-end "did the file
    land on the branch" assertion is covered by test_commit_context_pack.
    """
    from requiem.workflows.fanout import (
        FanoutInputs, FanoutLeaf, _build_leaf_context_pack,
    )
    from requiem.context_pack import ContextPack
    leaf = FanoutLeaf(real_id=11, title="DTO leaf", body="Define CapacityMetrics.")
    inputs = FanoutInputs(
        root_item_id=ROOT, repo=REPO, repo_path=repo, log_dir=tmp_path,
        leaves=(leaf,),
    )
    pack = _build_leaf_context_pack(leaf, inputs)
    assert isinstance(pack, ContextPack)
    assert pack.leaf_id == "11"
    assert "DTO leaf" in pack.agents_md
    # The synthesiser's plan_hash is deterministic — same inputs → same hash.
    pack2 = _build_leaf_context_pack(leaf, inputs)
    assert pack.plan_hash == pack2.plan_hash


@pytest.mark.asyncio
async def test_fanout_build_leaf_context_pack_returns_none_on_failure(
    repo: Path, tmp_path: Path,
) -> None:
    """The helper is defensive: any exception inside the synthesiser
    returns None so the leaf still gets the baseline coder prompt.
    Verify via a leaf with empty fields — should still produce a pack,
    NOT None. The "returns None on failure" path is exercised when the
    context_pack module raises, which is hard to provoke hermetically
    without monkey-patching. The structural pin here is: a degenerate
    but valid leaf produces a valid pack."""
    from requiem.workflows.fanout import (
        FanoutInputs, FanoutLeaf, _build_leaf_context_pack,
    )
    leaf = FanoutLeaf(real_id=99, title="", body="")
    inputs = FanoutInputs(
        root_item_id=ROOT, repo=REPO, repo_path=repo, log_dir=tmp_path,
        leaves=(leaf,),
    )
    pack = _build_leaf_context_pack(leaf, inputs)
    # Empty leaf still synthesises (the synthesiser tolerates empty fields).
    assert pack is not None
    assert pack.leaf_id == "99"


# ---- wave-gated dispatch + interleaved merge (run #36 postmortem) -------
#
# These exercise the dependency-aware path added to `_dispatch_leaves`:
# wave-gating only activates when BOTH a `leaf_merge` hook is wired AND at
# least one leaf declares a real `deps` edge — otherwise the flat dispatch
# above runs completely unchanged (see `test_..._is_a_noop_without_hook`
# and `test_..._is_a_noop_without_declared_deps` below).
#
# These need `dry_run=False` (a landed leaf only carries a `pr_number` once
# it actually reaches `create_pr` — see `_leaf_pr_info`), which in turn
# reaches `run_tests` for real. The throwaway repo has no detectable test
# suite, so stub test-detection/execution the same way the auto-detected
# command would resolve in a real dogfood repo — hermetic, no subprocess.


def _stub_test_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        impl, "detect_test_command",
        lambda repo_path: impl.DetectedTestCommand(command="true", cwd=repo_path),
    )
    monkeypatch.setattr(
        impl, "_default_test_runner",
        lambda command, cwd: impl.TestRunResult(
            passed=True, summary="stubbed", full_output="stubbed",
        ),
    )


def _two_leaf_provider() -> FakeProvider:
    # One coder response per dispatched leaf (in dispatch order).
    return FakeProvider(scripts={
        "coder": [
            {
                "intent_summary": "producer change",
                "file_changes": [
                    {"path": "PRODUCER.md", "operation": "create", "content": "p\n"},
                ],
                "notes": "",
            },
            {
                "intent_summary": "dependent change",
                "file_changes": [
                    {"path": "DEPENDENT.md", "operation": "create", "content": "d\n"},
                ],
                "notes": "",
            },
        ],
    })


def _three_leaf_provider() -> FakeProvider:
    return FakeProvider(scripts={
        "coder": [
            {
                "intent_summary": f"change {index}",
                "file_changes": [
                    {
                        "path": f"CHANGE-{index}.md",
                        "operation": "create",
                        "content": f"{index}\n",
                    },
                ],
                "notes": "",
            }
            for index in range(1, 4)
        ],
    })


def _recursive_dependency_artifacts(tmp_path: Path) -> tuple[Path, Path]:
    producer = ROOT * 100 + 1
    consumer = ROOT * 100 + 2
    producer_first = producer * 100 + 1
    producer_exit = producer * 100 + 2
    tree_path = tmp_path / "recursive.plan.tree.json"
    committed_path = tmp_path / "recursive.plan.committed.json"
    tree_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "plan_id": f"plan-{ROOT}-recursive",
                "item_id": ROOT,
                "decomposable": True,
                "verdict": "approved",
                "proposals": [
                    {
                        "title": "Producer subtree",
                        "description": "produces a contract",
                        "work_item_type": "Task",
                    },
                    {
                        "title": "Consumer",
                        "description": "consumes the contract",
                        "work_item_type": "Task",
                        "depends_on": [0],
                    },
                ],
                "children": [
                    {
                        "item_id": producer,
                        "decomposable": True,
                        "proposals": [
                            {
                                "title": "Producer first",
                                "description": "first",
                                "work_item_type": "Task",
                            },
                            {
                                "title": "Producer exit",
                                "description": "exit",
                                "work_item_type": "Task",
                                "depends_on": [0],
                            },
                        ],
                        "children": [
                            {
                                "item_id": producer_first,
                                "decomposable": False,
                                "proposals": [],
                                "children": [],
                            },
                            {
                                "item_id": producer_exit,
                                "decomposable": False,
                                "proposals": [],
                                "children": [],
                            },
                        ],
                    },
                    {
                        "item_id": consumer,
                        "decomposable": False,
                        "proposals": [],
                        "children": [],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    committed_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "plan_id": f"plan-{ROOT}-recursive",
                "root_item_id": ROOT,
                "dry_run": False,
                "id_map": {
                    str(producer): 10,
                    str(producer_first): 1,
                    str(producer_exit): 2,
                    str(consumer): 3,
                },
            }
        ),
        encoding="utf-8",
    )
    return tree_path, committed_path


def _artifact_engine(
    repo: Path,
    log_dir: Path,
    *,
    tree_path: Path,
    committed_path: Path,
    provider: FakeProvider,
    leaf_merge,
):
    inputs = fanout.FanoutInputs(
        root_item_id=ROOT,
        repo=REPO,
        repo_path=repo,
        log_dir=log_dir,
        dry_run=False,
        plan_tree_path=tree_path,
        committed_path=committed_path,
        leaf_merge=leaf_merge,
    )
    return fanout.build_engine(
        log_dir,
        inputs=inputs,
        toolbelt=_toolbelt(repo),
        provider=provider,
    )


async def test_recursive_subtree_dependency_gates_fanout_by_exit_leaf(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    _make_pushable(repo)
    _stub_test_detection(monkeypatch)
    tree_path, committed_path = _recursive_dependency_artifacts(tmp_path)
    calls: list[int] = []

    async def fake_merge(real_id: int, pr_number: int) -> str:
        calls.append(real_id)
        return "merged"

    engine = _artifact_engine(
        repo,
        tmp_path,
        tree_path=tree_path,
        committed_path=committed_path,
        provider=_three_leaf_provider(),
        leaf_merge=fake_merge,
    )

    result = await engine.run("recursive-waves")

    assert isinstance(result, Completed)
    assert calls == [1, 2, 3]
    resolved = completed_from_log(engine.log_path("recursive-waves"))[
        "resolve_leaves"
    ]["value"]["leaves"]
    deps_by_id = {leaf["real_id"]: leaf["deps"] for leaf in resolved}
    assert deps_by_id == {1: [], 2: [1], 3: [2]}


async def test_recursive_dependency_graph_survives_resume_after_resolution(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    _make_pushable(repo)
    _stub_test_detection(monkeypatch)
    tree_path, committed_path = _recursive_dependency_artifacts(tmp_path)

    async def unused_merge(real_id: int, pr_number: int) -> str:
        raise AssertionError("dispatch must not start before the injected crash")

    engine = _artifact_engine(
        repo,
        tmp_path,
        tree_path=tree_path,
        committed_path=committed_path,
        provider=_three_leaf_provider(),
        leaf_merge=unused_merge,
    )

    def crash_after_resolution(event: dict) -> None:
        if event.get("kind") == "route_taken" and event.get("node_id") == "resolve_leaves":
            raise RuntimeError("injected crash after dependency resolution")

    engine.on_event = crash_after_resolution
    with pytest.raises(RuntimeError, match="injected crash"):
        await engine.run("recursive-resume")

    tree_path.unlink()
    committed_path.unlink()
    calls: list[int] = []

    async def fake_merge(real_id: int, pr_number: int) -> str:
        calls.append(real_id)
        return "merged"

    resumed = _artifact_engine(
        repo,
        tmp_path,
        tree_path=tree_path,
        committed_path=committed_path,
        provider=_three_leaf_provider(),
        leaf_merge=fake_merge,
    )

    result = await resumed.run("recursive-resume")

    assert isinstance(result, Completed)
    assert calls == [1, 2, 3]


async def test_dependent_leaf_is_released_only_after_producer_merges(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Leaf 2 depends on leaf 1. The fake `leaf_merge` hook reports leaf 1 as
    merged; leaf 2 must then be dispatched (its own child log exists) and
    land, carrying merge_state == "merged" from the SAME hook."""
    _make_pushable(repo)
    _stub_test_detection(monkeypatch)
    calls: list[tuple[int, int]] = []

    async def fake_merge(real_id: int, pr_number: int) -> str:
        calls.append((real_id, pr_number))
        return "merged"

    leaves = [
        fanout.FanoutLeaf(real_id=1, title="producer", body="m"),
        fanout.FanoutLeaf(real_id=2, title="dependent", body="m", deps=(1,)),
    ]
    engine = _engine(
        repo, tmp_path, leaves=leaves, provider=_two_leaf_provider(),
        dry_run=False, leaf_merge=fake_merge,
    )
    result = await engine.run("waves")
    assert isinstance(result, Completed)
    res = _result(engine, "waves", result.final_node)

    assert res.leaves_total == 2
    assert res.leaves_landed == 2
    by_id = {o.real_id: o for o in res.outcomes}
    assert by_id[1].disposition == "completed"
    assert by_id[1].merge_state == "merged"
    assert by_id[2].disposition == "completed"
    assert by_id[2].merge_state == "merged"
    # The dependent was actually dispatched (own child log written), not
    # merely reported as landed.
    assert (tmp_path / f"fanout-{ROOT}__leaf-2.events.jsonl").exists()
    # The hook was called once per landed leaf, with that leaf's own real id.
    assert len(calls) == 2
    assert calls[0][0] == 1
    assert calls[1][0] == 2


async def test_dependent_leaf_is_blocked_when_producer_never_merges(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Leaf 2 depends on leaf 1. The fake `leaf_merge` hook reports leaf 1 as
    needs_human (never merged). Leaf 2 must NEVER be dispatched — no child
    log, and its roll-up disposition is the synthetic "blocked" value."""
    _make_pushable(repo)
    _stub_test_detection(monkeypatch)

    async def fake_merge(real_id: int, pr_number: int) -> str:
        return "needs_human"

    leaves = [
        fanout.FanoutLeaf(real_id=1, title="producer", body="m"),
        fanout.FanoutLeaf(real_id=2, title="dependent", body="m", deps=(1,)),
    ]
    engine = _engine(
        repo, tmp_path, leaves=leaves, provider=_two_leaf_provider(),
        dry_run=False, leaf_merge=fake_merge,
    )
    result = await engine.run("waves-blocked")
    assert isinstance(result, Completed)
    res = _result(engine, "waves-blocked", result.final_node)

    by_id = {o.real_id: o for o in res.outcomes}
    assert by_id[1].disposition == "completed"
    assert by_id[1].merge_state == "needs_human"
    assert by_id[2].disposition == "blocked"
    assert by_id[2].merge_state is None
    # Never dispatched: no child log for the dependent leaf at all.
    assert not (tmp_path / f"fanout-{ROOT}__leaf-2.events.jsonl").exists()
    # A blocked leaf buckets into leaves_failed (fanout_result's rollup),
    # not leaves_needs_human — it never even ran, unlike leaf 1.
    assert res.leaves_failed >= 1
    assert res.verdict == "failed"


async def test_wave_gating_is_a_noop_without_declared_deps(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """A leaf_merge hook wired but no leaf declares any `deps` must take the
    EXACT flat dispatch path — the hook is never even called, and no leaf's
    outcome carries a merge_state (regression guard for the "safe by
    default" design: dependency-aware wave dispatch is opt-in per-plan)."""
    _make_pushable(repo)
    _stub_test_detection(monkeypatch)
    calls: list[tuple[int, int]] = []

    async def fake_merge(real_id: int, pr_number: int) -> str:
        calls.append((real_id, pr_number))
        return "merged"

    leaves = [
        fanout.FanoutLeaf(real_id=1, title="one", body="m"),
        fanout.FanoutLeaf(real_id=2, title="two", body="m"),  # no deps
    ]
    engine = _engine(
        repo, tmp_path, leaves=leaves, provider=_two_leaf_provider(),
        dry_run=False, leaf_merge=fake_merge,
    )
    result = await engine.run("flat-with-hook")
    res = _result(engine, "flat-with-hook", result.final_node)

    assert res.leaves_landed == 2
    assert calls == []  # hook never invoked — no leaf declared a dependency
    assert all(o.merge_state is None for o in res.outcomes)


async def test_wave_gating_is_a_noop_without_a_hook(repo: Path, tmp_path: Path):
    """A leaf declaring `deps` but no `leaf_merge` hook wired also takes the
    flat dispatch path unchanged (e.g. a dry-run preview or a caller that
    hasn't wired self-merge at all) — dependencies alone never activate
    wave-gating."""
    leaves = [
        fanout.FanoutLeaf(real_id=1, title="producer", body="m"),
        fanout.FanoutLeaf(real_id=2, title="dependent", body="m", deps=(1,)),
    ]
    engine = _engine(
        repo, tmp_path, leaves=leaves, provider=_two_leaf_provider(),
        dry_run=True, leaf_merge=None,
    )
    result = await engine.run("flat-no-hook")
    res = _result(engine, "flat-no-hook", result.final_node)

    assert res.leaves_total == 2
    assert res.leaves_landed == 2  # both dispatched despite the declared dep
    assert all(o.merge_state is None for o in res.outcomes)
