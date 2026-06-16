"""Tests for the Azure DevOps PR lifecycle (ADO sibling of pr_lifecycle, #10).

Exercised against FakeAdoPrToolkit + a fake twig — exactly how pr_lifecycle is
tested against FakePrToolkit. Live ADO (a real PAT + org/project) is a deploy-time
validation step; the workflow LOGIC is fully covered here.
"""
from __future__ import annotations

from pathlib import Path

from requiem.kernel import Completed
from requiem.persistence import replay
from requiem.toolbelt import Toolbelt
from requiem.workflows import ado_pr
from requiem.workflows.ado_pr import (
    AdoPrInputs,
    AdoPullRequest,
    FakeAdoPrToolkit,
)


def _pr(status="active", merge_status="succeeded", is_draft=False, wids=(9001,)) -> AdoPullRequest:
    return AdoPullRequest(
        pull_request_id=42, title="leaf PR", status=status,
        source_branch="refs/heads/impl/9000-1",
        target_branch="refs/heads/feature/9000",
        merge_status=merge_status, is_draft=is_draft, work_item_ids=wids,
    )


class _FakeTwig:
    def __init__(self):
        self.states: list[tuple[int, str]] = []

    async def set_state_async(self, item_id: int, new_state: str):
        self.states.append((item_id, new_state))


def _completed(engine, run_id):
    return {
        e["node_id"]: e["payload"]["outcome"]
        for e in replay(engine.log_path(run_id))
        if e["kind"] == "verb_completed"
    }


def _engine(tmp_path: Path, *, inputs: AdoPrInputs, toolkit, twig=None):
    real = Toolbelt.real()
    tb = Toolbelt(git=real.git, files=real.files, gh=real.gh, fs=real.fs, twig=twig)
    return ado_pr.build_engine(tmp_path, inputs=inputs, toolkit=toolkit, toolbelt=tb)


# ---- happy paths --------------------------------------------------------


async def test_ready_pr_completes_dry_run(tmp_path: Path):
    """A ready active PR runs to end_completed; dry-run means no real complete."""
    tk = FakeAdoPrToolkit(pr=_pr())
    inputs = AdoPrInputs(repo="org/proj/repo", pull_request_id=42, dry_run=True)
    engine = _engine(tmp_path, inputs=inputs, toolkit=tk)
    result = await engine.run("dry")
    assert isinstance(result, Completed)
    assert result.final_node == "end_completed"
    r = ado_pr.ado_pr_result(_completed(engine, "dry"), result.final_node)
    assert r.verdict == "completed"
    assert r.merged is False           # dry-run never completes
    assert tk.completed_calls == []    # toolkit.complete_pr not called
    assert r.work_item_ids == (9001,)


async def test_ready_pr_completes_live_and_updates_item(tmp_path: Path):
    """live (dry_run=False): the PR is completed AND the linked work item is
    transitioned to the closed state via twig."""
    tk = FakeAdoPrToolkit(pr=_pr())
    twig = _FakeTwig()
    inputs = AdoPrInputs(repo="org/proj/repo", pull_request_id=42,
                         dry_run=False, closed_state="Closed", merge_strategy="squash")
    engine = _engine(tmp_path, inputs=inputs, toolkit=tk, twig=twig)
    result = await engine.run("live")
    assert result.final_node == "end_completed"
    # The PR was completed via the toolkit with the configured strategy.
    assert len(tk.completed_calls) == 1
    assert tk.completed_calls[0]["strategy"] == "squash"
    # The linked work item was transitioned.
    assert twig.states == [(9001, "Closed")]
    r = ado_pr.ado_pr_result(_completed(engine, "live"), result.final_node)
    assert r.merged is True


async def test_already_completed_short_circuits(tmp_path: Path):
    tk = FakeAdoPrToolkit(pr=_pr(status="completed"))
    inputs = AdoPrInputs(repo="org/proj/repo", pull_request_id=42, dry_run=False)
    engine = _engine(tmp_path, inputs=inputs, toolkit=tk)
    result = await engine.run("already")
    assert result.final_node == "end_already_completed"
    assert result.disposition == "completed"
    r = ado_pr.ado_pr_result(_completed(engine, "already"), result.final_node)
    assert r.verdict == "already_completed"
    assert tk.completed_calls == []    # never re-completes


# ---- surrender paths (needs_human) --------------------------------------


async def test_merge_conflicts_route_to_human(tmp_path: Path):
    tk = FakeAdoPrToolkit(pr=_pr(merge_status="conflicts"), merge_status="conflicts")
    inputs = AdoPrInputs(repo="org/proj/repo", pull_request_id=42, dry_run=False)
    engine = _engine(tmp_path, inputs=inputs, toolkit=tk)
    result = await engine.run("conflict")
    assert result.final_node == "needs_human_end"
    assert result.disposition == "needs_human"
    assert tk.completed_calls == []


async def test_unsatisfied_policies_route_to_human(tmp_path: Path):
    tk = FakeAdoPrToolkit(pr=_pr(), merge_status="queued", policies_satisfied=False)
    inputs = AdoPrInputs(repo="org/proj/repo", pull_request_id=42, dry_run=False)
    engine = _engine(tmp_path, inputs=inputs, toolkit=tk)
    result = await engine.run("blocked")
    assert result.final_node == "needs_human_end"
    assert result.disposition == "needs_human"


async def test_abandoned_pr_routes_to_human(tmp_path: Path):
    tk = FakeAdoPrToolkit(pr=_pr(status="abandoned"))
    inputs = AdoPrInputs(repo="org/proj/repo", pull_request_id=42, dry_run=False)
    engine = _engine(tmp_path, inputs=inputs, toolkit=tk)
    result = await engine.run("abandoned")
    assert result.final_node == "needs_human_end"


async def test_draft_pr_routes_to_human(tmp_path: Path):
    """A draft PR raises a NeedsHuman gate; with no gate_handler the engine
    suspends at check_state (it's the operator's call to publish + proceed)."""
    from requiem.kernel import Suspended
    tk = FakeAdoPrToolkit(pr=_pr(is_draft=True))
    inputs = AdoPrInputs(repo="org/proj/repo", pull_request_id=42, dry_run=False)
    engine = _engine(tmp_path, inputs=inputs, toolkit=tk)
    result = await engine.run("draft")
    assert isinstance(result, Suspended)
    assert result.node_id == "check_state"
    assert tk.completed_calls == []


async def test_fetch_failure_routes_to_human(tmp_path: Path):
    from requiem.workflows.ado_pr import AdoPrError
    tk = FakeAdoPrToolkit(pr=_pr(), raise_on_view=AdoPrError("404 not found", status=404))
    inputs = AdoPrInputs(repo="org/proj/repo", pull_request_id=42, dry_run=False)
    engine = _engine(tmp_path, inputs=inputs, toolkit=tk)
    result = await engine.run("nopr")
    assert result.final_node == "needs_human_end"


# ---- toolkit shape ------------------------------------------------------


def test_real_toolkit_splits_repo_and_builds_url():
    tk = ado_pr.RealAdoPrToolkit(pat="dummy", base_url="https://dev.azure.com")
    org, project, repo = tk._split_repo("Contoso/Polyphony/requiem")
    assert (org, project, repo) == ("Contoso", "Polyphony", "requiem")
    url = tk._pr_url("Contoso/Polyphony/requiem", 42)
    assert "Contoso/Polyphony/_apis/git/repositories/requiem/pullrequests/42" in url
    assert "api-version=" in url


def test_real_toolkit_rejects_malformed_repo():
    import pytest
    tk = ado_pr.RealAdoPrToolkit(pat="dummy")
    with pytest.raises(ado_pr.AdoPrError):
        tk._split_repo("just-a-repo")


# ---- credential surface (ADR-0024 step 1) -------------------------------


class _StubCredential:
    """A minimal TokenCredential stand-in. Records get_token calls so we can
    assert the toolkit asks for the right ADO resource."""

    def __init__(self, token_value: str = "stub-bearer-xyz") -> None:
        self.calls: list[tuple[str, ...]] = []
        self._token_value = token_value

    def get_token(self, *scopes, **kw):
        self.calls.append(scopes)
        # azure.core.credentials.AccessToken duck-typed: only .token used here.
        return _StubAccessToken(self._token_value)


class _StubAccessToken:
    def __init__(self, token: str) -> None:
        self.token = token
        self.expires_on = 0


async def test_explicit_credential_takes_precedence_over_pat_and_env(monkeypatch):
    """Resolution order: explicit credential > explicit pat > env > default.
    An explicit credential beats ADO_PAT in the env (otherwise CI runners
    with a leftover ADO_PAT would silently override the operator's choice)."""
    monkeypatch.setenv("ADO_PAT", "env-pat-should-not-win")
    cred = _StubCredential()
    tk = ado_pr.RealAdoPrToolkit(credential=cred, pat="explicit-pat-also-loses")
    header = await tk._auth_header()
    assert header == "Bearer stub-bearer-xyz"
    # Verify the toolkit asked for the right audience (ADR-0024 ADO_RESOURCE).
    assert cred.calls == [(ado_pr.ADO_RESOURCE,)]


async def test_explicit_pat_wins_over_env(monkeypatch):
    monkeypatch.setenv("ADO_PAT", "env-loses")
    tk = ado_pr.RealAdoPrToolkit(pat="explicit-wins")
    header = await tk._auth_header()
    # Basic + base64(":explicit-wins"). Decode and assert the username:password.
    import base64
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header.removeprefix("Basic ")).decode("ascii")
    assert decoded == ":explicit-wins"


async def test_default_credential_lazy_resolves_to_azure_cli_when_no_pat(monkeypatch):
    """With no explicit credential, no pat arg, and no ADO_PAT env: the
    toolkit must defer to AzureCliCredential (the ADR-0024 v0 default).
    This proves the lazy-import path works AND that az-cli is the default
    rather than a silent Basic-with-empty-pat header (which would 401 in
    confusing ways)."""
    monkeypatch.delenv("ADO_PAT", raising=False)
    tk = ado_pr.RealAdoPrToolkit()
    # The sentinel is in place pre-call (lazy):
    assert tk._credential is ado_pr._LazyDefault
    # Monkey-patch the resolver to avoid an actual az-cli subprocess in CI.
    sentinel_credential = _StubCredential(token_value="from-azurecli")
    monkeypatch.setattr(ado_pr, "_resolve_default_credential",
                        lambda: sentinel_credential)
    header = await tk._auth_header()
    assert header == "Bearer from-azurecli"
    # Sentinel has been replaced with the resolved credential — second call
    # reuses it instead of re-resolving.
    assert tk._credential is sentinel_credential
    header2 = await tk._auth_header()
    assert header2 == "Bearer from-azurecli"
    assert len(sentinel_credential.calls) == 2  # one per _auth_header call


def test_missing_azure_identity_raises_with_actionable_message(monkeypatch):
    """When azure-identity isn't installed, the default-credential path must
    raise an AdoPrError naming the [ado] extra — not a bare ImportError that
    operators have to chase."""
    import sys
    import pytest
    # Simulate the package missing by injecting an ImportError into sys.modules.
    monkeypatch.setitem(sys.modules, "azure.identity", None)
    with pytest.raises(ado_pr.AdoPrError) as exc:
        ado_pr._resolve_default_credential()
    msg = str(exc.value)
    assert "azure-identity" in msg
    assert "requiem[ado]" in msg
    assert "ADO_PAT" in msg  # mentions the backward-compat fallback
