"""trunk_bootstrap workflow tests — ensure feature/<root> (ADR-0018 step 1).

trunk_bootstrap touches only ``gh`` (branch_sha / ensure_branch_ref) — no
git/fs/twig — so these tests run against an in-memory refs store. They assert
the contract: idempotent create (never force-moves an existing trunk),
fail-closed on a missing base, and read-only dry-run probing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

import requiem.workflows.trunk_bootstrap as tb
from requiem import branch_model
from requiem.clients.gh import GhNotFoundError, GhUnknownError
from requiem.kernel import Completed
from requiem.toolbelt import FakeFileClient, RealGitClient, Toolbelt
from requiem.workflows.planning import completed_from_log
from requiem.workflows.trunk_bootstrap import TrunkBootstrapInputs

ROOT = 900
REPO = "Owner/Repo"


# ---- fake ---------------------------------------------------------------


@dataclass
class FakeGh:
    refs: dict[str, str] = field(default_factory=lambda: {"main": "basesha000"})
    created: list[str] = field(default_factory=list)
    raise_on_sha: Exception | None = None
    raise_on_ensure: Exception | None = None

    async def branch_sha(self, repo: str, branch: str) -> str:
        if self.raise_on_sha is not None:
            raise self.raise_on_sha
        try:
            return self.refs[branch]
        except KeyError as e:
            raise GhNotFoundError(
                f"no such branch {branch}", exit_code=1, stderr="404", argv=(),
            ) from e

    async def ensure_branch_ref(self, repo: str, branch: str, source_sha: str) -> bool:
        if self.raise_on_ensure is not None:
            raise self.raise_on_ensure
        if branch in self.refs:
            return False
        self.refs[branch] = source_sha
        self.created.append(branch)
        return True


def _toolbelt(*, gh: FakeGh) -> Toolbelt:
    return Toolbelt(
        git=RealGitClient(),
        files=FakeFileClient({}),
        gh=gh,  # type: ignore[arg-type]
        fs=None,
        twig=None,
    )


def _engine(log_dir, *, gh, dry_run=True, base="main"):
    inputs = TrunkBootstrapInputs(
        root_item_id=ROOT, repo=REPO, base_branch=base, dry_run=dry_run,
    )
    return tb.build_engine(log_dir, inputs=inputs, toolbelt=_toolbelt(gh=gh))


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs"
    d.mkdir()
    return d


def _result(engine, run_id, final_node):
    return tb.trunk_bootstrap_result(completed_from_log(engine.log_path(run_id)), final_node)


TRUNK = branch_model.feature_trunk(ROOT)


# ---- dry-run (read-only) ------------------------------------------------


async def test_dry_run_previews_would_create(log_dir):
    gh = FakeGh()  # trunk absent
    engine = _engine(log_dir, gh=gh)
    result = await engine.run("dry")
    assert isinstance(result, Completed)
    assert result.final_node == "end_success"
    res = _result(engine, "dry", result.final_node)
    assert res.verdict == "previewed"
    assert res.trunk_branch == TRUNK
    assert res.base_sha == "basesha000"
    # read-only: no ref created.
    assert gh.created == []
    assert TRUNK not in gh.refs


async def test_dry_run_reports_existing_trunk(log_dir):
    gh = FakeGh(refs={"main": "basesha000", TRUNK: "oldsha"})
    engine = _engine(log_dir, gh=gh)
    await engine.run("dry-exists")
    completed = completed_from_log(engine.log_path("dry-exists"))
    ensure = completed["ensure_trunk"]["value"]
    assert ensure["exists"] is True
    assert gh.created == []


# ---- real create --------------------------------------------------------


async def test_real_create_makes_trunk(log_dir):
    gh = FakeGh()
    engine = _engine(log_dir, gh=gh, dry_run=False)
    result = await engine.run("create")
    assert result.final_node == "end_success"
    res = _result(engine, "create", result.final_node)
    assert res.verdict == "created"
    assert gh.refs[TRUNK] == "basesha000"
    assert gh.created == [TRUNK]


async def test_idempotent_rerun_does_not_recreate(log_dir):
    gh = FakeGh()
    await _engine(log_dir, gh=gh, dry_run=False).run("first")
    assert gh.created == [TRUNK]
    # advance the trunk as if leaves merged into it...
    gh.refs[TRUNK] = "advancedsha"
    engine2 = _engine(log_dir, gh=gh, dry_run=False)
    result = await engine2.run("second")
    res = _result(engine2, "second", result.final_node)
    assert res.verdict == "exists"
    # never force-moved: the advanced SHA is preserved.
    assert gh.refs[TRUNK] == "advancedsha"
    assert gh.created == [TRUNK]


# ---- fail-closed --------------------------------------------------------


async def test_missing_base_fails_closed(log_dir):
    gh = FakeGh(refs={})  # no base branch at all
    engine = _engine(log_dir, gh=gh, dry_run=False)
    result = await engine.run("nobase")
    assert result.final_node == "end_failed"
    res = _result(engine, "nobase", result.final_node)
    assert res.verdict == "failed"
    assert gh.created == []


async def test_ensure_failure_is_permanent(log_dir):
    gh = FakeGh(raise_on_ensure=GhUnknownError(
        "boom", exit_code=1, stderr="boom", argv=(),
    ))
    engine = _engine(log_dir, gh=gh, dry_run=False)
    result = await engine.run("ensurefail")
    assert result.final_node == "end_failed"


# ---- branch_model wiring ------------------------------------------------


def test_inputs_use_branch_model_trunk():
    inputs = TrunkBootstrapInputs(root_item_id=ROOT, repo=REPO)
    assert inputs.trunk_branch == branch_model.feature_trunk(ROOT)


# ---- demo engine smoke --------------------------------------------------


async def test_demo_engine_runs_dry(log_dir):
    engine = tb.build_engine(log_dir)
    result = await engine.run("demo")
    assert result.final_node == "end_success"
    res = tb.trunk_bootstrap_result(
        completed_from_log(engine.log_path("demo")), result.final_node,
    )
    assert res.verdict == "previewed"
