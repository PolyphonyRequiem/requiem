"""Contract tests for the ``RepoPlatform`` Protocol (ADR-0024 step 2).

These prove three load-bearing properties:

1. The Protocol is structurally well-formed (instances of the typed clients
   satisfy ``isinstance(client, RepoPlatform)`` at runtime via the
   ``@runtime_checkable`` decorator).
2. :class:`requiem.clients.gh.GhClient` is a legitimate ``RepoPlatform`` —
   all six required methods are present with the right async-ness.
3. ``RepoPullRequest``'s normalisation guarantees hold: uppercase legacy
   ``state`` values map to the neutral lowercase vocab; the deprecated
   ``merged=`` kwarg is silently accepted; the ``.merged`` property
   derives from the normalised state.

Step 3 (``AdoClient``) will extend the first assertion to cover the ADO
impl alongside ``GhClient``.
"""
from __future__ import annotations

import inspect

import pytest

from requiem.clients.gh import GhClient, GhPullRequest
from requiem.clients.repo import RepoPlatform, RepoPullRequest


# ---- shape ---------------------------------------------------------------


def test_repoplatform_is_runtime_checkable():
    """The Protocol must be ``@runtime_checkable`` so the workflows can
    duck-type via ``isinstance`` if needed. Drift on this would silently
    break the ``_require_repo_platform`` helper that ADR-0024 step 4
    introduces."""
    # A bare GhClient instantiation satisfies the Protocol at runtime
    # because every required method is present.
    gh = GhClient()
    assert isinstance(gh, RepoPlatform)


def test_ghclient_implements_every_repoplatform_method():
    """Each Protocol method must exist on GhClient AND be async — the
    Protocol's whole point is uniform async dispatch."""
    required = (
        "branch_sha", "ensure_branch_ref",
        "find_open_pr_for_branch", "pr_view", "pr_create",
        "default_branch",
    )
    for name in required:
        method = getattr(GhClient, name, None)
        assert method is not None, f"GhClient missing required method {name!r}"
        assert inspect.iscoroutinefunction(method), (
            f"GhClient.{name} must be async (Protocol contract)"
        )


def test_gh_pull_request_is_an_alias_for_repo_pull_request():
    """The legacy ``GhPullRequest`` import path must keep working — many
    workflow modules import it directly. After ADR-0024 it's a re-export."""
    assert GhPullRequest is RepoPullRequest


# ---- RepoPullRequest normalisation --------------------------------------


@pytest.mark.parametrize("legacy_state,neutral", [
    ("OPEN", "open"),
    ("CLOSED", "closed"),
    ("MERGED", "merged"),
    ("Open", "open"),       # mixed-case tolerated
    ("merged", "merged"),   # already-neutral round-trips
])
def test_state_normalisation_accepts_legacy_uppercase(legacy_state, neutral):
    pr = RepoPullRequest(
        number=1, title="t", state=legacy_state, merged_at=None,
        head="h", base="b", url="u",
    )
    assert pr.state == neutral


def test_unknown_state_defaults_to_open():
    """A garbage state value defaults to ``open`` rather than smuggling
    through — defensive against malformed wire payloads."""
    pr = RepoPullRequest(
        number=1, title="t", state="garbage-state", merged_at=None,
        head="h", base="b", url="u",
    )
    assert pr.state == "open"


def test_legacy_merged_kwarg_silently_dropped():
    """Callers across the codebase still construct PRs with
    ``GhPullRequest(state="OPEN", merged=False, ...)``. The shim must
    accept and ignore that kwarg so the migration doesn't require touching
    every constructor site."""
    pr = RepoPullRequest(
        number=1, title="t", state="MERGED", merged=False,  # `merged` ignored
        merged_at=None, head="h", base="b", url="u",
    )
    # merged derives from state, NOT from the legacy kwarg.
    assert pr.merged is True
    assert pr.state == "merged"


def test_merged_property_derives_from_state():
    open_pr = RepoPullRequest(
        number=1, title="t", state="OPEN", merged_at=None,
        head="h", base="b", url="u",
    )
    merged_pr = RepoPullRequest(
        number=2, title="t", state="MERGED", merged_at=None,
        head="h", base="b", url="u",
    )
    assert open_pr.merged is False
    assert merged_pr.merged is True
