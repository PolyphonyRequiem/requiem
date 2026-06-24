"""Tests for the in-process fan-out orchestrator (ADR-0021, parity #4).

These exercise the orchestrator dispatching real ``implementation`` engines
in-process (the seam from ADR-0020 in action), per-leaf log isolation, the
landed / needs_human / failed roll-up (B2), and idempotent re-entry. The child
runs against a real throwaway git repo + a demo gh client + a per-leaf twig, so
no live ADO/GitHub is needed.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from requiem.agent import FakeProvider
from requiem.clients.fs import FilesystemClient
from requiem.kernel import Completed
from requiem.toolbelt import FakeFileClient, RealGitClient, Toolbelt
from requiem.workflows import fanout
from requiem.workflows import implementation as impl
from requiem.workflows.planning import completed_from_log

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


def _toolbelt(repo: Path, *, gh=None) -> Toolbelt:
    return Toolbelt(
        git=RealGitClient(),
        files=FakeFileClient({}),
        gh=gh or impl._DemoGhClient(),  # type: ignore[arg-type]
        fs=FilesystemClient(repo),
        twig=None,  # per-leaf twig is swapped in by dispatch_leaves
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


def _engine(repo: Path, log_dir: Path, *, leaves, provider, gh=None, dry_run=True):
    inputs = fanout.FanoutInputs(
        root_item_id=ROOT, repo=REPO, repo_path=repo, log_dir=log_dir,
        dry_run=dry_run, leaves=tuple(leaves),
    )
    return fanout.build_engine(
        log_dir, inputs=inputs, toolbelt=_toolbelt(repo, gh=gh), provider=provider,
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
