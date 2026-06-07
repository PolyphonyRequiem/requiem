"""Unit tests for GhClient.branch_sha / ensure_branch_ref (ADR-0018 step 1).

These exercise the real client's GET→POST branching and 422-race reconciliation
by stubbing the low-level ``api`` seam — no subprocess, no network. The
workflow-level behaviour lives in test_trunk_bootstrap_workflow.py.
"""
from __future__ import annotations

import pytest

from requiem.clients.gh import (
    GhClient,
    GhNotFoundError,
    GhUnknownError,
)

REPO = "Owner/Repo"


def _ref_endpoint(branch: str) -> str:
    return f"repos/{REPO}/git/ref/heads/{branch}"


class _StubApi:
    """Routes ``api(endpoint, method, body)`` calls against scripted behaviour."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        # endpoint -> value or Exception, for GET (method GET)
        self.get_results: dict[str, object] = {}
        # POST behaviour
        self.post_result: object = {"ref": "refs/heads/x"}
        self.post_error: Exception | None = None

    async def __call__(self, endpoint, method="GET", body=None):
        self.calls.append((endpoint, method.upper()))
        if method.upper() == "POST":
            if self.post_error is not None:
                raise self.post_error
            return self.post_result
        res = self.get_results.get(endpoint)
        if isinstance(res, Exception):
            raise res
        if res is None:
            raise GhNotFoundError(
                f"no such ref {endpoint}", exit_code=1, stderr="404", argv=(),
            )
        return res


def _client_with(stub: _StubApi) -> GhClient:
    c = GhClient()
    c.api = stub  # type: ignore[method-assign]
    return c


# ---- branch_sha ---------------------------------------------------------


async def test_branch_sha_returns_object_sha():
    stub = _StubApi()
    stub.get_results[_ref_endpoint("main")] = {"object": {"sha": "deadbeef"}}
    c = _client_with(stub)
    assert await c.branch_sha(REPO, "main") == "deadbeef"


async def test_branch_sha_missing_object_is_unknown_error():
    stub = _StubApi()
    stub.get_results[_ref_endpoint("main")] = {"ref": "refs/heads/main"}  # no object
    c = _client_with(stub)
    with pytest.raises(GhUnknownError):
        await c.branch_sha(REPO, "main")


async def test_branch_sha_absent_branch_raises_not_found():
    stub = _StubApi()  # nothing scripted → GhNotFoundError
    c = _client_with(stub)
    with pytest.raises(GhNotFoundError):
        await c.branch_sha(REPO, "nope")


# ---- ensure_branch_ref --------------------------------------------------


async def test_ensure_existing_ref_is_noop_false():
    stub = _StubApi()
    stub.get_results[_ref_endpoint("feature/1")] = {"object": {"sha": "abc"}}
    c = _client_with(stub)
    created = await c.ensure_branch_ref(REPO, "feature/1", "src")
    assert created is False
    # never POSTed — we don't force-move an existing trunk.
    assert all(m != "POST" for _, m in stub.calls)


async def test_ensure_missing_ref_creates_true():
    stub = _StubApi()  # GET → not found, POST → ok
    c = _client_with(stub)
    created = await c.ensure_branch_ref(REPO, "feature/1", "src")
    assert created is True
    assert ("repos/Owner/Repo/git/refs", "POST") in stub.calls


async def test_ensure_lost_create_race_reconciles_false():
    # GET #1 → not found; POST → 422 (someone created it first); GET #2 → exists.
    post_error = GhUnknownError(
        "Reference already exists", exit_code=1, stderr="HTTP 422", argv=(),
    )
    gets = {"n": 0}

    async def racey(endpoint, method="GET", body=None):
        if method.upper() == "POST":
            raise post_error
        gets["n"] += 1
        if gets["n"] >= 2 and endpoint == _ref_endpoint("feature/1"):
            return {"object": {"sha": "raced"}}
        raise GhNotFoundError("not yet", exit_code=1, stderr="404", argv=())

    c = GhClient()
    c.api = racey  # type: ignore[method-assign]
    created = await c.ensure_branch_ref(REPO, "feature/1", "src")
    assert created is False


async def test_ensure_genuine_create_failure_reraises():
    boom = GhUnknownError("server exploded", exit_code=1, stderr="HTTP 500-ish", argv=())

    async def always_fail_get_and_post(endpoint, method="GET", body=None):
        if method.upper() == "POST":
            raise boom
        raise GhNotFoundError("absent", exit_code=1, stderr="404", argv=())

    c = GhClient()
    c.api = always_fail_get_and_post  # type: ignore[method-assign]
    with pytest.raises(GhUnknownError):
        await c.ensure_branch_ref(REPO, "feature/1", "src")
