"""Contract tests for AdoClient (ADR-0024 step 3).

Two layers of coverage:

1. **Protocol shape** — AdoClient and FakeAdoClient both satisfy
   :class:`RepoPlatform` (Protocol surface, async-ness, return types).
2. **Behaviour against a mocked HTTP transport** — every Protocol method
   issues the right ADO REST call and translates the response into the
   neutral :class:`RepoPullRequest` correctly. Auth is exercised
   independently via the AzureCliCredential / PAT / explicit-credential
   resolution order; we monkeypatch the auth header for these tests so
   the focus is the wire shape, not the bearer flow.

Live ADO validation is a deploy-time step (operator's ``az login`` +
reachable org/project) — the same discipline ``RealAdoPrToolkit`` uses.
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from requiem.clients.azuredevops import (
    AdoClient,
    AdoClientError,
    AdoNotFoundError,
    AdoUnknownError,
    FakeAdoClient,
)
from requiem.clients.repo import MergeCapableRepoPlatform, RepoPlatform, RepoPullRequest


# ---- protocol shape -----------------------------------------------------


def test_ado_client_satisfies_repoplatform_at_runtime():
    """The @runtime_checkable Protocol must accept a bare AdoClient
    instance — same property GhClient was tested for in step 2."""
    assert isinstance(AdoClient(), RepoPlatform)


def test_fake_ado_client_satisfies_repoplatform_at_runtime():
    """The Fake is also a contract-bound impl per the faithful-fake
    discipline (tests/test_fake_surface_contract.py); the Protocol
    check belongs in BOTH places."""
    assert isinstance(FakeAdoClient(), RepoPlatform)


def test_ado_client_satisfies_merge_capable_platform_at_runtime():
    assert isinstance(AdoClient(), MergeCapableRepoPlatform)


def test_fake_ado_client_satisfies_merge_capable_platform_at_runtime():
    assert isinstance(FakeAdoClient(), MergeCapableRepoPlatform)


def test_ado_client_implements_every_repoplatform_method_async():
    required = (
        "branch_sha", "ensure_branch_ref",
        "find_open_pr_for_branch", "pr_view", "pr_create",
        "default_branch",
    )
    for name in required:
        for cls in (AdoClient, FakeAdoClient):
            method = getattr(cls, name, None)
            assert method is not None, (
                f"{cls.__name__} missing required method {name!r}"
            )
            assert inspect.iscoroutinefunction(method), (
                f"{cls.__name__}.{name} must be async (Protocol contract)"
            )


def test_ado_clients_implement_every_merge_capable_method_async():
    required = ("pr_mergeability", "pr_complete")
    for name in required:
        for cls in (AdoClient, FakeAdoClient):
            method = getattr(cls, name, None)
            assert method is not None, f"{cls.__name__} missing required method {name!r}"
            assert inspect.iscoroutinefunction(method), (
                f"{cls.__name__}.{name} must be async (Protocol contract)"
            )


# ---- AdoClient repo parsing ---------------------------------------------


def test_split_repo_accepts_canonical_form():
    org, project, repo = AdoClient._split_repo("Contoso/Polyphony/requiem")
    assert (org, project, repo) == ("Contoso", "Polyphony", "requiem")


def test_split_repo_rejects_two_segment_form():
    with pytest.raises(AdoClientError):
        AdoClient._split_repo("Owner/Repo")


def test_repo_base_builds_canonical_ado_url():
    client = AdoClient()
    base = client._repo_base("Contoso/Polyphony/requiem")
    assert base == (
        "https://dev.azure.com/Contoso/Polyphony"
        "/_apis/git/repositories/requiem"
    )


# ---- mocked-transport tests for the wire protocol -----------------------
#
# We monkeypatch _request to a stub that records the (method, url, body,
# params) tuple and returns canned payloads. This lets us assert the wire
# shape without making real HTTP calls.


def _stub_client(responses: list[Any]):
    """Build an AdoClient whose _request iterates through ``responses``,
    recording every call. Each response is returned in order; raising an
    Exception (passed in instead of a payload) is re-raised."""
    client = AdoClient()
    calls: list[dict[str, Any]] = []
    iter_responses = iter(responses)

    async def fake_request(method, url, *, body=None, params=None):
        calls.append({
            "method": method, "url": url, "body": body, "params": params,
        })
        nxt = next(iter_responses)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    client._request = fake_request  # type: ignore[method-assign]
    return client, calls


def test_branch_sha_uses_filter_and_returns_object_id():
    client, calls = _stub_client([
        {"value": [{"name": "refs/heads/main", "objectId": "abc123"}]}
    ])
    sha = asyncio.run(client.branch_sha("Contoso/P/repo", "main"))
    assert sha == "abc123"
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"].endswith("/refs")
    assert calls[0]["params"] == {"filter": "heads/main"}


def test_branch_sha_raises_not_found_on_empty_value_list():
    """ADO's refs?filter= returns {value: []} on a missing branch (not 404).
    The client normalises this to AdoNotFoundError so callers don't have
    to know about the API quirk."""
    client, _ = _stub_client([{"value": []}])
    with pytest.raises(AdoNotFoundError):
        asyncio.run(client.branch_sha("Contoso/P/repo", "missing"))


def test_ensure_branch_ref_creates_with_null_old_object_id():
    """Per ADR-0018: ensure_branch_ref MUST NOT force-move. The null
    oldObjectId precondition is exactly that guarantee — ADO will reject
    the update if the ref already exists at a different SHA."""
    client, calls = _stub_client([
        {"value": [{"success": True, "name": "refs/heads/feature/42"}]}
    ])
    created = asyncio.run(client.ensure_branch_ref(
        "Contoso/P/repo", "feature/42", "newsha000"
    ))
    assert created is True
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/refs")
    body = calls[0]["body"]
    assert body == [{
        "name": "refs/heads/feature/42",
        "oldObjectId": AdoClient._NULL_OBJECT_ID,
        "newObjectId": "newsha000",
    }]


def test_ensure_branch_ref_returns_false_when_already_present():
    """The 'already exists' case: POST returns success=False; the client
    falls back to a branch_sha probe to confirm the ref is there, and
    returns False (idempotent)."""
    client, _ = _stub_client([
        {"value": [{"success": False, "customMessage": "ref exists"}]},
        {"value": [{"objectId": "existingsha"}]},  # branch_sha probe finds it
    ])
    created = asyncio.run(client.ensure_branch_ref(
        "Contoso/P/repo", "feature/42", "newsha000"
    ))
    assert created is False


def test_ensure_branch_ref_raises_unknown_when_post_fails_and_ref_absent():
    """If POST fails AND the follow-up probe also says missing, something
    we don't understand happened (auth, validation, network race) — Ravel
    L-1 says NeedsHuman, not silent retry."""
    client, _ = _stub_client([
        {"value": [{"success": False, "customMessage": "validation failed"}]},
        {"value": []},  # probe says ref still missing
    ])
    with pytest.raises(AdoUnknownError):
        asyncio.run(client.ensure_branch_ref(
            "Contoso/P/repo", "feature/42", "newsha000"
        ))


def test_find_open_pr_for_branch_passes_refs_heads_prefix_and_status_active():
    """ADO's searchCriteria.sourceRefName requires the refs/heads/ prefix
    on the wire. The Protocol contract is bare branch names; the client
    adds the prefix once at the boundary."""
    client, calls = _stub_client([{"value": []}])
    asyncio.run(client.find_open_pr_for_branch(
        "Contoso/P/repo", head="impl/100-1", limit=5,
    ))
    assert calls[0]["params"] == {
        "searchCriteria.sourceRefName": "refs/heads/impl/100-1",
        "searchCriteria.status": "active",
        "$top": "5",
    }


def test_find_open_pr_for_branch_strips_refs_heads_in_results():
    """ADO PR responses include refs/heads/ prefixed branches; the
    neutral RepoPullRequest must always carry bare names so workflows
    can compare directly to what they construct."""
    client, _ = _stub_client([{"value": [{
        "pullRequestId": 42,
        "title": "Add reset",
        "status": "active",
        "sourceRefName": "refs/heads/impl/100-1",
        "targetRefName": "refs/heads/feature/100",
    }]}])
    prs = asyncio.run(client.find_open_pr_for_branch(
        "Contoso/P/repo", head="impl/100-1",
    ))
    assert len(prs) == 1
    assert prs[0].head == "impl/100-1"
    assert prs[0].base == "feature/100"
    assert prs[0].state == "open"
    assert prs[0].number == 42


def test_pr_view_translates_completed_status_to_merged_with_closed_date():
    client, calls = _stub_client([{
        "pullRequestId": 99, "title": "leaf",
        "status": "completed",
        "closedDate": "2026-06-16T10:00:00Z",
        "sourceRefName": "refs/heads/impl/100-1",
        "targetRefName": "refs/heads/feature/100",
    }])
    pr = asyncio.run(client.pr_view("Contoso/P/repo", 99))
    assert pr.state == "merged"
    assert pr.merged is True
    assert pr.merged_at is not None
    assert pr.merged_at.year == 2026
    assert calls[0]["url"].endswith("/pullrequests/99")


def test_pr_view_translates_abandoned_to_closed():
    client, _ = _stub_client([{
        "pullRequestId": 99, "title": "abandoned PR",
        "status": "abandoned",
        "sourceRefName": "refs/heads/x", "targetRefName": "refs/heads/main",
    }])
    pr = asyncio.run(client.pr_view("Contoso/P/repo", 99))
    assert pr.state == "closed"
    assert pr.merged is False


def test_pr_create_adds_refs_heads_prefix_on_the_wire():
    client, calls = _stub_client([{
        "pullRequestId": 500, "title": "new leaf",
        "status": "active",
        "sourceRefName": "refs/heads/impl/100-2",
        "targetRefName": "refs/heads/feature/100",
    }])
    pr = asyncio.run(client.pr_create(
        "Contoso/P/repo",
        title="new leaf", body="…",
        head="impl/100-2", base="feature/100",
    ))
    body = calls[0]["body"]
    assert body["sourceRefName"] == "refs/heads/impl/100-2"
    assert body["targetRefName"] == "refs/heads/feature/100"
    # Returned PR is the neutral shape (bare branches).
    assert pr.head == "impl/100-2"
    assert pr.base == "feature/100"
    assert pr.number == 500


def test_default_branch_strips_refs_heads_prefix():
    """ADO's repo metadata returns defaultBranch as refs/heads/main; the
    Protocol contract is bare branch names."""
    client, calls = _stub_client([{"defaultBranch": "refs/heads/master"}])
    branch = asyncio.run(client.default_branch("Contoso/P/repo"))
    assert branch == "master"
    # The repo metadata URL is exactly the repo base — no trailing path.
    assert calls[0]["url"] == client._repo_base("Contoso/P/repo")


def test_default_branch_raises_on_missing_field():
    """Per Ravel L-1: an unknown shape from ADO is NeedsHuman, not silent
    retry."""
    client, _ = _stub_client([{"id": "abc", "name": "repo"}])
    with pytest.raises(AdoUnknownError):
        asyncio.run(client.default_branch("Contoso/P/repo"))


def test_pr_mergeability_maps_succeeded_to_green() -> None:
    client, calls = _stub_client([
        {
            "mergeStatus": "succeeded",
            "sourceRefName": "refs/heads/impl/700-1",
        },
        {"value": [{"name": "refs/heads/impl/700-1", "objectId": "headsha42"}]},
        {"value": [{"state": "succeeded"}]},
    ])
    report = asyncio.run(client.pr_mergeability("Contoso/P/repo", 42))
    assert calls[0]["url"].endswith("/pullrequests/42")
    assert calls[1]["url"].endswith("/refs")
    assert calls[2]["url"].endswith("/commits/headsha42/statuses")
    assert report.mergeable is True
    assert report.mergeable_state == "succeeded"
    assert report.checks_state == "success"
    assert report.conflicts is False
    assert report.policies_satisfied is True


def test_pr_mergeability_with_no_commit_statuses_reports_unknown() -> None:
    client, _ = _stub_client([
        {
            "mergeStatus": "succeeded",
            "sourceRefName": "refs/heads/impl/700-1",
        },
        {"value": [{"name": "refs/heads/impl/700-1", "objectId": "headsha42"}]},
        {"value": []},
    ])
    report = asyncio.run(client.pr_mergeability("Contoso/P/repo", 42))
    assert report.checks_state == "unknown"


def test_pr_complete_refuses_to_patch_on_expected_base_mismatch() -> None:
    client, calls = _stub_client([{
        "status": "active",
        "sourceRefName": "refs/heads/impl/700-1",
        "targetRefName": "refs/heads/feature/other",
    }])
    with pytest.raises(AdoUnknownError):
        asyncio.run(client.pr_complete(
            "Contoso/P/repo",
            42,
            strategy="squash",
            expected_head="impl/700-1",
            expected_base="feature/700",
        ))
    assert len(calls) == 1
    assert calls[0]["method"] == "GET"


def test_pr_complete_patches_completion_options_with_strategy() -> None:
    client, calls = _stub_client([
        {
            "status": "active",
            "sourceRefName": "refs/heads/impl/700-1",
            "targetRefName": "refs/heads/feature/700",
        },
        {
            "pullRequestId": 42,
            "status": "completed",
            "lastMergeCommit": {"commitId": "merge-ado-42"},
        },
    ])
    result = asyncio.run(client.pr_complete(
        "Contoso/P/repo",
        42,
        strategy="squash",
        expected_head="impl/700-1",
        expected_base="feature/700",
    ))
    assert calls[1]["method"] == "PATCH"
    assert calls[1]["url"].endswith("/pullrequests/42")
    assert calls[1]["body"] == {
        "status": "completed",
        "completionOptions": {"mergeStrategy": "squash"},
    }
    assert result.number == 42
    assert result.merged is True
    assert result.merge_sha == "merge-ado-42"
    assert result.strategy == "squash"


# ---- FakeAdoClient in-memory behaviour ----------------------------------


def test_fake_branch_sha_returns_seeded_sha():
    fake = FakeAdoClient(refs={("acme/p/repo", "main"): "main-sha"})
    sha = asyncio.run(fake.branch_sha("acme/p/repo", "main"))
    assert sha == "main-sha"


def test_fake_branch_sha_raises_not_found_for_unseeded():
    fake = FakeAdoClient()
    with pytest.raises(AdoNotFoundError):
        asyncio.run(fake.branch_sha("acme/p/repo", "missing"))


def test_fake_ensure_branch_ref_is_idempotent():
    fake = FakeAdoClient()
    first = asyncio.run(fake.ensure_branch_ref(
        "acme/p/repo", "feature/42", "sha000"
    ))
    second = asyncio.run(fake.ensure_branch_ref(
        "acme/p/repo", "feature/42", "sha000"
    ))
    assert first is True
    assert second is False  # second call no-ops
    assert fake.created_refs == [("acme/p/repo", "feature/42", "sha000")]


def test_fake_pr_create_assigns_incrementing_numbers_and_persists():
    fake = FakeAdoClient(next_pr_number=8000)
    pr1 = asyncio.run(fake.pr_create(
        "acme/p/repo", title="A", body="", head="x", base="main",
    ))
    pr2 = asyncio.run(fake.pr_create(
        "acme/p/repo", title="B", body="", head="y", base="main",
    ))
    assert (pr1.number, pr2.number) == (8000, 8001)
    # Both reachable via pr_view.
    fetched = asyncio.run(fake.pr_view("acme/p/repo", 8001))
    assert fetched.title == "B"


def test_fake_find_open_pr_for_branch_filters_by_head_and_state():
    open_pr = RepoPullRequest(
        number=1, title="open", state="open", merged_at=None,
        head="impl/x", base="feature/x", url="u",
    )
    merged_pr = RepoPullRequest(
        number=2, title="merged", state="merged", merged_at=None,
        head="impl/x", base="feature/x", url="u",
    )
    other_head = RepoPullRequest(
        number=3, title="other", state="open", merged_at=None,
        head="impl/y", base="feature/x", url="u",
    )
    fake = FakeAdoClient(open_prs=[open_pr, merged_pr, other_head])
    results = asyncio.run(fake.find_open_pr_for_branch(
        "acme/p/repo", head="impl/x"
    ))
    assert [pr.number for pr in results] == [1]


def test_fake_default_branch_falls_back_to_main_when_unset():
    """Tests that don't care about the default branch should not have to
    seed every repo."""
    fake = FakeAdoClient()
    assert asyncio.run(fake.default_branch("any/repo/here")) == "main"


def test_fake_raise_on_search_propagates():
    """Test fakes match the real client's failure-injection surface so a
    test can exercise the verb's error-handling path without a real ADO."""
    err = AdoUnknownError("simulated", status=503)
    fake = FakeAdoClient(raise_on_search=err)
    with pytest.raises(AdoUnknownError):
        asyncio.run(fake.find_open_pr_for_branch("a/b/c", head="x"))


# ---- ado_status_to_neutral ----------------------------------------------


@pytest.mark.parametrize("ado_status,expected", [
    ("active", "open"),
    ("Active", "open"),       # case-insensitive
    ("abandoned", "closed"),
    ("completed", "merged"),
    ("unknown-status", "open"),  # garbage defaults to open
])
def test_ado_status_to_neutral(ado_status, expected):
    assert AdoClient._ado_status_to_neutral(ado_status) == expected
