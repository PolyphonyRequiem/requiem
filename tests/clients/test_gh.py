"""Tests for `requiem.clients.gh.GhClient`.

Subprocess is mocked at `asyncio.create_subprocess_exec`. Each test
scripts one (stdout, stderr, exit_code) tuple and asserts on the typed
outcome / typed error. One smoke test is gated by `RUN_REAL_GH=1` and
calls the real binary.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from requiem.clients.gh import (
    GhAuthError,
    GhClient,
    GhClientError,
    GhNotFoundError,
    GhPullRequest,
    GhRateLimitedError,
    GhServerError,
    GhUnknownError,
    _PR_FIELDS,
)


# ---- subprocess mock harness ------------------------------------------


class _FakeProc:
    """Minimal stand-in for asyncio's Process — only what GhClient uses."""

    def __init__(
        self, stdout: bytes, stderr: bytes, returncode: int
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.received_stdin: bytes | None = None

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        self.received_stdin = input
        return self._stdout, self._stderr


def _fake_subprocess(
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> tuple[Any, list[dict[str, Any]]]:
    """Build a `create_subprocess_exec` replacement + call-log."""
    calls: list[dict[str, Any]] = []
    proc = _FakeProc(stdout.encode("utf-8"), stderr.encode("utf-8"), returncode)

    async def fake(*argv: str, **kwargs: Any) -> _FakeProc:
        calls.append({"argv": argv, "kwargs": kwargs})
        return proc

    return fake, calls


def _patch_subprocess(stdout: str = "", stderr: str = "", returncode: int = 0):
    fake, calls = _fake_subprocess(stdout, stderr, returncode)
    return patch(
        "requiem.clients.gh.asyncio.create_subprocess_exec",
        side_effect=fake,
    ), calls


# ---- fixtures ---------------------------------------------------------


def _pr_payload(**overrides: Any) -> dict[str, Any]:
    base = {
        "number": 13,
        "title": "Promote: walking-skeleton engine",
        "state": "MERGED",
        "mergedAt": "2026-06-01T04:34:17Z",
        "headRefName": "promote/engine-v0",
        "baseRefName": "main",
        "url": "https://github.com/PolyphonyRequiem/requiem/pull/13",
    }
    base.update(overrides)
    return base


# ---- happy paths ------------------------------------------------------


def test_pr_view_happy_path_parses_all_fields() -> None:
    payload = _pr_payload()
    p, calls = _patch_subprocess(stdout=json.dumps(payload))
    with p:
        pr = asyncio.run(GhClient().pr_view("PolyphonyRequiem/requiem", 13))

    assert isinstance(pr, GhPullRequest)
    assert pr.number == 13
    assert pr.title.startswith("Promote")
    assert pr.state == "merged"
    assert pr.merged is True
    assert pr.merged_at == datetime(2026, 6, 1, 4, 34, 17, tzinfo=timezone.utc)
    assert pr.head == "promote/engine-v0"
    assert pr.base == "main"
    assert pr.url.endswith("/pull/13")
    assert pr.raw == payload

    argv = calls[0]["argv"]
    # We pass every requested field explicitly — no `*`.
    assert "--json" in argv
    json_idx = argv.index("--json")
    assert argv[json_idx + 1] == ",".join(_PR_FIELDS)
    assert "--repo" in argv
    assert "PolyphonyRequiem/requiem" in argv


def test_pr_view_open_pr_is_not_merged() -> None:
    payload = _pr_payload(state="OPEN", mergedAt=None)
    p, _ = _patch_subprocess(stdout=json.dumps(payload))
    with p:
        pr = asyncio.run(GhClient().pr_view("repo/x", 1))
    assert pr.state == "open"
    assert pr.merged is False
    assert pr.merged_at is None


def test_pr_search_returns_list_of_prs() -> None:
    payloads = [_pr_payload(number=10), _pr_payload(number=11, state="OPEN")]
    p, calls = _patch_subprocess(stdout=json.dumps(payloads))
    with p:
        prs = asyncio.run(
            GhClient().pr_search("repo/x", "label:close-out", limit=5)
        )

    assert [pr.number for pr in prs] == [10, 11]
    argv = calls[0]["argv"]
    assert "--search" in argv and "label:close-out" in argv
    assert "--limit" in argv and "5" in argv


def test_pr_search_empty_list_is_a_valid_result() -> None:
    p, _ = _patch_subprocess(stdout="[]")
    with p:
        prs = asyncio.run(GhClient().pr_search("repo/x", "nope"))
    assert prs == []


def test_pr_search_rejects_zero_limit() -> None:
    with pytest.raises(ValueError):
        asyncio.run(GhClient().pr_search("repo/x", "q", limit=0))


# ---- RepoPlatform additions (ADR-0024 step 2) ---------------------------


def test_find_open_pr_for_branch_translates_to_pr_list_search() -> None:
    """find_open_pr_for_branch is the RepoPlatform wrapper around pr_search.
    GitHub: turns ``head=feature/x`` into ``--search "head:feature/x state:open"``.
    """
    payloads = [_pr_payload(number=42, state="OPEN")]
    p, calls = _patch_subprocess(stdout=json.dumps(payloads))
    with p:
        prs = asyncio.run(
            GhClient().find_open_pr_for_branch(
                "repo/x", head="feature/widget", limit=10
            )
        )
    assert [pr.number for pr in prs] == [42]
    argv = calls[0]["argv"]
    assert "--search" in argv
    # The query gets stitched together as one positional argv element.
    assert any("head:feature/widget" in a and "state:open" in a for a in argv), (
        f"expected head:feature/widget state:open in argv, got {argv}"
    )
    assert "--limit" in argv and "10" in argv


def test_default_branch_reads_from_repos_api() -> None:
    """default_branch resolves via ``gh api repos/<repo>`` and pulls the
    ``default_branch`` field — used by end_to_end to discover whether the
    trunk integrates back into main/master/develop."""
    payload = {"default_branch": "master", "id": 1234, "name": "x"}
    p, calls = _patch_subprocess(stdout=json.dumps(payload))
    with p:
        branch = asyncio.run(GhClient().default_branch("acme/widgets"))
    assert branch == "master"
    argv = calls[0]["argv"]
    assert "api" in argv and "repos/acme/widgets" in argv


def test_default_branch_raises_on_missing_field() -> None:
    """Per Ravel L-1: an unknown shape from gh is NeedsHuman, not silent
    retry. If the API payload lacks default_branch (forbidden but worth
    defending), raise GhUnknownError rather than returning empty string."""
    from requiem.clients.gh import GhUnknownError
    payload = {"id": 1234, "name": "x"}  # default_branch missing
    p, _ = _patch_subprocess(stdout=json.dumps(payload))
    with p:
        with pytest.raises(GhUnknownError):
            asyncio.run(GhClient().default_branch("acme/widgets"))


def test_api_get_returns_parsed_dict() -> None:
    body = {"login": "PolyphonyRequiem"}
    p, calls = _patch_subprocess(stdout=json.dumps(body))
    with p:
        out = asyncio.run(GhClient().api("/user"))
    assert out == body
    argv = calls[0]["argv"]
    assert argv[:4] == ("gh", "api", "/user", "--method")
    assert argv[4] == "GET"
    # No body → no --input flag.
    assert "--input" not in argv


def test_api_post_streams_body_on_stdin() -> None:
    payload = {"ok": True}
    fake, calls = _fake_subprocess(stdout=json.dumps(payload))
    proc_holder: dict[str, _FakeProc] = {}

    async def capture(*argv: str, **kwargs: Any) -> _FakeProc:
        proc = await fake(*argv, **kwargs)
        proc_holder["proc"] = proc
        return proc

    with patch(
        "requiem.clients.gh.asyncio.create_subprocess_exec", side_effect=capture
    ):
        out = asyncio.run(
            GhClient().api("/repos/x/y/issues", method="post", body={"title": "hi"})
        )

    assert out == payload
    argv = calls[0]["argv"]
    assert argv[4] == "POST"
    assert argv[-2:] == ("--input", "-")
    assert proc_holder["proc"].received_stdin == json.dumps({"title": "hi"}).encode()


# ---- error mapping ----------------------------------------------------


def test_rate_limit_with_reset_header_parses_retry_after() -> None:
    future = int(time.time()) + 120
    stderr = (
        "gh: API rate limit exceeded for user ID 1234.\n"
        f"X-RateLimit-Reset: {future}\n"
    )
    p, _ = _patch_subprocess(stderr=stderr, returncode=1)
    with p, pytest.raises(GhRateLimitedError) as ei:
        asyncio.run(GhClient().pr_view("r/x", 1))

    err = ei.value
    assert err.retry_after is not None
    # Allow a small slack for the second tick between time.time() calls.
    assert timedelta(seconds=115) <= err.retry_after <= timedelta(seconds=125)
    assert err.exit_code == 1


def test_rate_limit_without_reset_header_has_none_retry_after() -> None:
    p, _ = _patch_subprocess(stderr="secondary rate limit hit\n", returncode=1)
    with p, pytest.raises(GhRateLimitedError) as ei:
        asyncio.run(GhClient().pr_view("r/x", 1))
    assert ei.value.retry_after is None


def test_rate_limit_via_http_429_status() -> None:
    p, _ = _patch_subprocess(
        stderr="gh: HTTP 429: Too Many Requests\n", returncode=1
    )
    with p, pytest.raises(GhRateLimitedError):
        asyncio.run(GhClient().pr_view("r/x", 1))


def test_not_found_via_could_not_resolve() -> None:
    p, _ = _patch_subprocess(
        stderr="GraphQL: Could not resolve to a PullRequest with the number of 999.\n",
        returncode=1,
    )
    with p, pytest.raises(GhNotFoundError):
        asyncio.run(GhClient().pr_view("r/x", 999))


def test_not_found_via_http_404() -> None:
    p, _ = _patch_subprocess(
        stderr="gh: Not Found (HTTP 404)\n", returncode=1
    )
    with p, pytest.raises(GhNotFoundError):
        asyncio.run(GhClient().pr_view("r/x", 999))


def test_auth_error_via_401() -> None:
    p, _ = _patch_subprocess(
        stderr="HTTP 401: Bad credentials. Try gh auth login.\n", returncode=1
    )
    with p, pytest.raises(GhAuthError):
        asyncio.run(GhClient().pr_view("r/x", 1))


def test_auth_error_via_textual_hint() -> None:
    p, _ = _patch_subprocess(
        stderr="authentication required\n", returncode=1
    )
    with p, pytest.raises(GhAuthError):
        asyncio.run(GhClient().pr_view("r/x", 1))


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_server_error_is_distinct(status: int) -> None:
    p, _ = _patch_subprocess(
        stderr=f"gh: GitHub had a fit (HTTP {status})\n", returncode=1
    )
    with p, pytest.raises(GhServerError) as ei:
        asyncio.run(GhClient().pr_view("r/x", 1))
    assert ei.value.status == status


def test_unclassified_exit_1_is_unknown_not_retryable() -> None:
    # Ravel's L-1 caveat: if we don't recognize the stderr, it is NEVER
    # a retryable failure. Verbs convert this to NeedsHuman.
    p, _ = _patch_subprocess(
        stderr="some brand-new gh failure mode we have never seen\n",
        returncode=1,
    )
    with p, pytest.raises(GhUnknownError) as ei:
        asyncio.run(GhClient().pr_view("r/x", 1))
    # And specifically NOT one of the retryable subclasses.
    assert not isinstance(ei.value, (GhRateLimitedError, GhServerError))


def test_exit_code_2_is_unknown() -> None:
    p, _ = _patch_subprocess(stderr="usage: gh ...\n", returncode=2)
    with p, pytest.raises(GhUnknownError) as ei:
        asyncio.run(GhClient().pr_view("r/x", 1))
    assert ei.value.exit_code == 2


def test_json_parse_failure_on_zero_exit_is_unknown_error() -> None:
    p, _ = _patch_subprocess(stdout="not valid json{{{", returncode=0)
    with p, pytest.raises(GhUnknownError):
        asyncio.run(GhClient().pr_view("r/x", 1))


def test_pr_view_with_non_object_top_level_is_unknown_error() -> None:
    p, _ = _patch_subprocess(stdout="[1, 2, 3]", returncode=0)
    with p, pytest.raises(GhUnknownError):
        asyncio.run(GhClient().pr_view("r/x", 1))


def test_pr_search_with_non_array_top_level_is_unknown_error() -> None:
    p, _ = _patch_subprocess(stdout='{"oops": true}', returncode=0)
    with p, pytest.raises(GhUnknownError):
        asyncio.run(GhClient().pr_search("r/x", "q"))


# ---- spawn-time failures (cross-platform) -----------------------------


def test_bad_cwd_raises_unknown_cross_platform() -> None:
    # Linux/macOS: FileNotFoundError. Windows: NotADirectoryError.
    # We test both by monkey-patching create_subprocess_exec to raise
    # the platform-appropriate error, so the test passes on every OS.
    expected_exc = (
        NotADirectoryError("nope") if sys.platform == "win32"
        else FileNotFoundError("nope")
    )

    async def boom(*args: Any, **kwargs: Any) -> None:
        raise expected_exc

    with patch(
        "requiem.clients.gh.asyncio.create_subprocess_exec", side_effect=boom
    ):
        with pytest.raises(GhUnknownError) as ei:
            asyncio.run(
                GhClient(cwd=Path("Z:/definitely/not/here")).pr_view("r/x", 1)
            )
    assert ei.value.exit_code == -1


def test_missing_gh_binary_raises_unknown() -> None:
    async def boom(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError("[WinError 2] gh not on PATH")

    with patch(
        "requiem.clients.gh.asyncio.create_subprocess_exec", side_effect=boom
    ):
        with pytest.raises(GhUnknownError) as ei:
            asyncio.run(GhClient().pr_view("r/x", 1))
    assert "gh" in str(ei.value).lower()


# ---- base-class invariant --------------------------------------------


def test_all_errors_subclass_gh_client_error() -> None:
    for cls in (
        GhRateLimitedError, GhNotFoundError, GhAuthError,
        GhServerError, GhUnknownError,
    ):
        assert issubclass(cls, GhClientError)


# ---- subprocess kwarg hygiene (Schumann's note) -----------------------


def test_stdin_is_devnull_when_no_body_pytest_win_314_safe() -> None:
    """pytest's captured stdin isn't inheritable on Windows + Python 3.14.

    Every spawn that does not need a stdin body must pass DEVNULL
    explicitly. This test pins the convention so a refactor that drops
    back to the inheriting default fails loudly.
    """
    p, calls = _patch_subprocess(stdout=json.dumps(_pr_payload()))
    with p:
        asyncio.run(GhClient().pr_view("r/x", 1))
    assert calls[0]["kwargs"]["stdin"] is asyncio.subprocess.DEVNULL


def test_stdin_is_pipe_when_api_body_is_present() -> None:
    p, calls = _patch_subprocess(stdout="{}")
    with p:
        asyncio.run(GhClient().api("/x", method="POST", body={"a": 1}))
    assert calls[0]["kwargs"]["stdin"] is asyncio.subprocess.PIPE


# ---- real-tool smoke (opt-in) ----------------------------------------


@pytest.mark.skipif(
    os.environ.get("RUN_REAL_GH") != "1",
    reason="RUN_REAL_GH=1 not set; skipping live gh smoke test.",
)
def test_real_gh_binary_is_invokable() -> None:
    """Bare-minimum smoke: prove we can spawn the real binary.

    Calls `gh --version` via raw subprocess (the GhClient's public
    methods all invoke JSON-returning subcommands; --version is enough
    to prove the PATH/exec wiring works without depending on auth).
    """
    if shutil.which("gh") is None:
        pytest.skip("gh not on PATH on this machine")

    async def go() -> tuple[int, str]:
        # stdin=DEVNULL is mandatory under pytest on Win+3.14
        # (Schumann's note); also harmless everywhere else.
        proc = await asyncio.create_subprocess_exec(
            "gh", "--version",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        return proc.returncode or 0, out.decode()

    rc, out = asyncio.run(go())
    assert rc == 0
    assert "gh version" in out
