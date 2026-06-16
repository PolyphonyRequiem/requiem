"""ADR-0024 step 4: trunk-topology workflows are platform-neutral.

These tests prove the load-bearing property of step 4: the three
trunk-topology workflows (trunk_bootstrap, leaf_pr, feature_pr) work
unchanged against FakeAdoClient when wired via `toolbelt.repo`. Step 4
is exactly this — the workflows depend on the RepoPlatform Protocol,
not on GhClient as a concrete type.

If these tests pass, the same end-to-end pipeline that drives a GitHub
repo today can drive an ADO repo tomorrow (modulo step 5's
driver-wiring work — the workflows themselves no longer block it).

The tests use the in-memory FakeAdoClient. Live ADO is a deploy-time
validation step (`az login` + a reachable ADO repo) and is the final
gate after step 5 ships.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from requiem import branch_model
from requiem.clients.azuredevops import FakeAdoClient
from requiem.clients.repo import RepoPullRequest
from requiem.kernel import Completed
from requiem.toolbelt import FakeFileClient, RealGitClient, Toolbelt
from requiem.workflows import feature_pr, leaf_pr, trunk_bootstrap
from requiem.workflows.feature_pr import FeaturePrInputs, ItemDisposition, LeafPr
from requiem.workflows.leaf_pr import LeafPrInputs
from requiem.workflows.trunk_bootstrap import TrunkBootstrapInputs


ROOT = 9000
ADO_REPO = "Contoso/Polyphony/widgets"   # <org>/<project>/<repo>
TRUNK = branch_model.feature_trunk(ROOT)


# ---- helpers ------------------------------------------------------------


def _toolbelt(*, ado: FakeAdoClient) -> Toolbelt:
    """Wire FakeAdoClient via the new platform-neutral toolbelt.repo
    field. Crucially, toolbelt.gh is None — the workflows must NOT
    silently fall back to a GitHub client we haven't supplied. This is
    the proof that the workflows truly depend on the Protocol, not on
    the concrete GhClient type."""
    return Toolbelt(
        git=RealGitClient(),
        files=FakeFileClient({}),
        gh=None,
        repo=ado,
        fs=None,
        twig=None,
    )


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs"
    d.mkdir()
    return d


# ---- trunk_bootstrap ----------------------------------------------------


async def test_trunk_bootstrap_creates_trunk_against_ado(log_dir: Path):
    """trunk_bootstrap should be able to create the feature/<root> trunk
    via FakeAdoClient.ensure_branch_ref — same behaviour as GhClient,
    different impl."""
    ado = FakeAdoClient(refs={(ADO_REPO, "main"): "main-sha"})
    inputs = TrunkBootstrapInputs(
        root_item_id=ROOT, repo=ADO_REPO, base_branch="main", dry_run=False,
    )
    engine = trunk_bootstrap.build_engine(
        log_dir, inputs=inputs, toolbelt=_toolbelt(ado=ado),
    )
    result = await engine.run("trunk-create")
    assert isinstance(result, Completed)
    assert result.final_node == "end_success"
    # The trunk ref now exists in the ADO fake at the base SHA.
    assert ado._refs.get((ADO_REPO, TRUNK)) == "main-sha"


async def test_trunk_bootstrap_idempotent_against_ado(log_dir: Path):
    """A re-run when the trunk already exists must NOT create it again
    and MUST return success — same idempotent semantics as GhClient."""
    ado = FakeAdoClient(refs={
        (ADO_REPO, "main"): "main-sha",
        (ADO_REPO, TRUNK): "preexisting-trunk-sha",   # trunk already there
    })
    inputs = TrunkBootstrapInputs(
        root_item_id=ROOT, repo=ADO_REPO, base_branch="main", dry_run=False,
    )
    engine = trunk_bootstrap.build_engine(
        log_dir, inputs=inputs, toolbelt=_toolbelt(ado=ado),
    )
    result = await engine.run("trunk-idempotent")
    assert isinstance(result, Completed)
    assert result.final_node == "end_success"
    # The trunk SHA did NOT change (no force-move per ADR-0018).
    assert ado._refs[(ADO_REPO, TRUNK)] == "preexisting-trunk-sha"


async def test_trunk_bootstrap_fails_closed_when_base_missing(log_dir: Path):
    """Same fail-closed property: no base branch → PermanentFailure with
    EK_BASE_MISSING, no half-created trunk."""
    ado = FakeAdoClient()  # no refs seeded at all
    inputs = TrunkBootstrapInputs(
        root_item_id=ROOT, repo=ADO_REPO, base_branch="main", dry_run=False,
    )
    engine = trunk_bootstrap.build_engine(
        log_dir, inputs=inputs, toolbelt=_toolbelt(ado=ado),
    )
    result = await engine.run("base-missing")
    assert isinstance(result, Completed)
    assert result.final_node == "end_failed"
    # No trunk was created as a side-effect of the failed run.
    assert (ADO_REPO, TRUNK) not in ado._refs


# ---- leaf_pr ------------------------------------------------------------


async def test_leaf_pr_creates_leaf_prs_against_ado(log_dir: Path):
    """leaf_pr opens impl/<root>-<leaf> → feature/<root> PRs via
    FakeAdoClient.pr_create. Verifies the conflict-detection logic
    works against the platform-neutral find_open_pr_for_branch."""
    ado = FakeAdoClient(next_pr_number=5000)
    inputs = LeafPrInputs(
        root_item_id=ROOT, repo=ADO_REPO, leaf_ids=["L1", "L2"], dry_run=False,
    )
    engine = leaf_pr.build_engine(
        log_dir, inputs=inputs, toolbelt=_toolbelt(ado=ado),
    )
    result = await engine.run("leaf-create")
    assert isinstance(result, Completed)
    assert result.final_node == "end_success"
    # Both leaf PRs were created against the ADO fake.
    assert len(ado.created_prs) == 2
    heads = {p["head"] for p in ado.created_prs}
    assert heads == {f"impl/{ROOT}-L1", f"impl/{ROOT}-L2"}
    bases = {p["base"] for p in ado.created_prs}
    assert bases == {TRUNK}


async def test_leaf_pr_reuses_existing_open_pr_against_ado(log_dir: Path):
    """A re-run with an existing open PR for a leaf must reuse it (not
    open a duplicate) — same idempotent semantics."""
    existing_pr = RepoPullRequest(
        number=4242, title="existing", state="open", merged_at=None,
        head=f"impl/{ROOT}-L1", base=TRUNK,
        url="https://dev.azure.com/Contoso/Polyphony/_git/widgets/pullrequest/4242",
    )
    ado = FakeAdoClient(open_prs=[existing_pr], next_pr_number=5000)
    inputs = LeafPrInputs(
        root_item_id=ROOT, repo=ADO_REPO, leaf_ids=["L1"], dry_run=False,
    )
    engine = leaf_pr.build_engine(
        log_dir, inputs=inputs, toolbelt=_toolbelt(ado=ado),
    )
    result = await engine.run("leaf-reuse")
    assert isinstance(result, Completed)
    assert result.final_node == "end_success"
    # No new PR created — the existing #4242 was reused.
    assert len(ado.created_prs) == 0


# ---- feature_pr ---------------------------------------------------------


async def test_feature_pr_opens_trunk_pr_against_ado(log_dir: Path):
    """feature_pr verifies all leaf PRs are merged into the trunk, then
    opens the trunk→main integration PR. Tests the full readiness gate
    + creation against the ADO impl."""
    # Seed: both leaf PRs are "merged" (completed) in the ADO fake.
    leaf1_pr = RepoPullRequest(
        number=4242, title="leaf 1", state="merged", merged_at=None,
        head=f"impl/{ROOT}-L1", base=TRUNK, url="…/4242",
        raw={"_repo": ADO_REPO},
    )
    leaf2_pr = RepoPullRequest(
        number=4243, title="leaf 2", state="merged", merged_at=None,
        head=f"impl/{ROOT}-L2", base=TRUNK, url="…/4243",
        raw={"_repo": ADO_REPO},
    )
    ado = FakeAdoClient(
        open_prs=[leaf1_pr, leaf2_pr],  # also serves pr_view lookups by number
        next_pr_number=9999,
    )
    inputs = FeaturePrInputs(
        root_item_id=ROOT, repo=ADO_REPO, base_branch="main",
        leaves=(
            LeafPr(leaf_id="L1", pr_number=4242),
            LeafPr(leaf_id="L2", pr_number=4243),
        ),
        dispositions=(
            ItemDisposition(item_id=ROOT, state="Done", satisfied=True),
        ),
        dry_run=False,
    )
    engine = feature_pr.build_engine(
        log_dir, inputs=inputs, toolbelt=_toolbelt(ado=ado),
    )
    result = await engine.run("feature-open")
    assert isinstance(result, Completed)
    assert result.final_node == "end_success", (
        f"expected end_success, got {result.final_node}"
    )
    # The trunk→main integration PR was created.
    integration_prs = [
        p for p in ado.created_prs if p["head"] == TRUNK and p["base"] == "main"
    ]
    assert len(integration_prs) == 1


# ---- missing-client fail-closed -----------------------------------------


async def test_trunk_bootstrap_fails_when_neither_repo_nor_gh_set(log_dir: Path):
    """Both toolbelt.repo AND toolbelt.gh are None — workflow must
    fail closed with toolbelt.missing_client, not crash with
    AttributeError on a None deref."""
    toolbelt = Toolbelt(
        git=RealGitClient(), files=FakeFileClient({}),
        gh=None, repo=None, fs=None, twig=None,
    )
    inputs = TrunkBootstrapInputs(
        root_item_id=ROOT, repo=ADO_REPO, base_branch="main", dry_run=False,
    )
    engine = trunk_bootstrap.build_engine(log_dir, inputs=inputs, toolbelt=toolbelt)
    result = await engine.run("no-client")
    assert isinstance(result, Completed)
    assert result.final_node == "end_failed"


async def test_back_compat_gh_only_still_works(log_dir: Path):
    """The back-compat path: toolbelt.gh is set but toolbelt.repo is
    None. The workflow must fall back to toolbelt.gh (GhClient IS a
    RepoPlatform). This guards existing wiring from breaking."""
    # Use FakeAdoClient as a stand-in for "any RepoPlatform passed via
    # the legacy toolbelt.gh field" — proves the fallback works
    # regardless of the underlying impl type.
    ado = FakeAdoClient(refs={(ADO_REPO, "main"): "main-sha"})
    toolbelt = Toolbelt(
        git=RealGitClient(), files=FakeFileClient({}),
        gh=ado,    # back-compat path: client wired via gh
        repo=None, # ← deliberately NOT set
        fs=None, twig=None,
    )
    inputs = TrunkBootstrapInputs(
        root_item_id=ROOT, repo=ADO_REPO, base_branch="main", dry_run=False,
    )
    engine = trunk_bootstrap.build_engine(log_dir, inputs=inputs, toolbelt=toolbelt)
    result = await engine.run("back-compat")
    assert isinstance(result, Completed)
    assert result.final_node == "end_success", (
        f"back-compat path should work — got {result.final_node}"
    )
