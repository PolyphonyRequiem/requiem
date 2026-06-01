"""Scripted fakes — the LLM seam and the external-process seam.

`FakeAgent` is a thin wrapper around the existing `requiem.agent.FakeProvider`
that accepts the harness's two normalization affordances:

* a scalar entry (``dict`` / `Outcome`) → one call expected; second call
  errors with ``fake.exhausted``.
* a list entry → N calls replayed in order (mirrors Mahler's contract).

`FakeToolbelt` builds a `Toolbelt` whose every client is scripted from a
single dict keyed by ``(tool, method, *args)``. Unknown calls raise a
clear ``HarnessUnscriptedError`` rather than falling through to a real
subprocess — the harness is for hermetic tests; live calls belong in
``pytest -k live``.

The tool key scheme is uniform across every client:

    ("files", "read_text", path_str)                 → str | FileMissing | FileRead
    ("git",   "show",      repo_str, ref, path_str)  → str | GitShowOk | GitShowMissing | GitNotARepo
    ("gh",    "pr_view",   repo_str, number)         → dict | GhPullRequest | GhClientError
    ("gh",    "pr_search", repo_str, query, limit)   → list | GhClientError
    ("gh",    "api",       endpoint, method)         → dict | GhClientError
    ("twig",  "show",      item_id)                  → dict | TwigItem | TwigClientError
    ("twig",  "set_state", item_id, new_state)       → dict | TwigItem | TwigClientError
    ("twig",  "list_children", item_id)              → list | TwigClientError

For convenience, scalar shortcuts (``str`` for file content, ``dict`` for
gh/twig payload) are lifted into the appropriate typed dataclass. To
script an error, pass the exception instance directly — fakes raise it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from requiem.agent import AgentCall, AgentProvider, FakeProvider
from requiem.clients.gh import (
    GhClient,
    GhClientError,
    GhPullRequest,
    GhUnknownError,
)
from requiem.outcomes import Outcome
from requiem.toolbelt import (
    FileMissing,
    FileRead,
    GitNotARepo,
    GitShowMissing,
    GitShowOk,
    Toolbelt,
)

try:  # twig is recent (Mendelssohn Phase B); ride graceful if absent.
    from requiem.clients.twig import (
        TwigClient,
        TwigClientError,
        TwigItem,
        TwigItemNotFoundError,
    )

    _HAS_TWIG = True
except ImportError:  # pragma: no cover — defensive: keep harness import-safe.
    TwigClient = None  # type: ignore[assignment,misc]
    TwigClientError = Exception  # type: ignore[misc,assignment]
    TwigItem = None  # type: ignore[assignment,misc]
    TwigItemNotFoundError = Exception  # type: ignore[misc,assignment]
    _HAS_TWIG = False


class HarnessUnscriptedError(RuntimeError):
    """Raised when a fake client receives a call with no matching script.

    Fail loud so missing fixtures show up as test-author errors, not as
    silent fall-throughs to live subprocesses.
    """


# ---- FakeAgent -------------------------------------------------------


@dataclass
class FakeAgent:
    """Scripted AgentProvider, keyed by `agent.name`.

    Wraps `requiem.agent.FakeProvider` after normalizing the two input
    shapes: a scalar entry becomes ``[entry]``; a list entry passes
    through. The provider's own ``fake.exhausted`` / ``fake.unscripted``
    failure surfaces are preserved so the harness can detect missing
    scripts without inventing its own error variant.
    """

    scripts: dict[str, list[Any]] = field(default_factory=dict)
    _provider: FakeProvider = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._provider = FakeProvider(scripts=self.scripts)

    @classmethod
    def from_outputs(cls, outputs: dict[str, Any]) -> "FakeAgent":
        normalized: dict[str, list[Any]] = {}
        for name, entry in outputs.items():
            normalized[name] = list(entry) if isinstance(entry, list) else [entry]
        return cls(scripts=normalized)

    async def invoke(self, call: AgentCall) -> Outcome:
        return await self._provider.invoke(call)

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self._provider.calls


# Structural-subtype confirmation (the kernel needs `AgentProvider` only).
assert isinstance(FakeAgent(scripts={}), AgentProvider)


# ---- shared lookup ---------------------------------------------------


def _normalize_path(value: Any) -> Any:
    """Coerce Path-shaped values to ``str`` so dict keys compare cleanly."""
    if isinstance(value, Path):
        return str(value)
    return value


def _lookup(
    scripts: dict[tuple, Any], tool: str, method: str, *args: Any
) -> Any:
    key = (tool, method, *(_normalize_path(a) for a in args))
    if key not in scripts:
        available = sorted(
            k for k in scripts if isinstance(k, tuple) and k[:2] == (tool, method)
        )
        raise HarnessUnscriptedError(
            f"no fake script for {key!r}; nearby keys: {available!r}"
        )
    return scripts[key]


# ---- files -----------------------------------------------------------


class _FakeFileClient:
    def __init__(self, scripts: dict[tuple, Any]) -> None:
        self._scripts = scripts

    def read_text(self, path: Path):
        try:
            entry = _lookup(self._scripts, "files", "read_text", path)
        except HarnessUnscriptedError:
            return FileMissing(path=path)
        if isinstance(entry, (FileRead, FileMissing)):
            return entry
        if isinstance(entry, str):
            return FileRead(path=path, content=entry)
        if entry is None:
            return FileMissing(path=path)
        raise TypeError(
            f"files.read_text fake got unsupported entry {type(entry).__name__}"
        )


# ---- git -------------------------------------------------------------


class FakeGitClient:
    """Scripted ``GitClient`` for the harness."""

    def __init__(self, scripts: dict[tuple, Any]) -> None:
        self._scripts = scripts

    def show(
        self, repo: Path, ref: str, path: Path, *, timeout_s: float = 5.0
    ):
        try:
            entry = _lookup(self._scripts, "git", "show", repo, ref, path)
        except HarnessUnscriptedError:
            return GitShowMissing(ref=ref, path=path, stderr="no fake script")
        if isinstance(entry, (GitShowOk, GitShowMissing, GitNotARepo)):
            return entry
        if isinstance(entry, str):
            return GitShowOk(ref=ref, path=path, content=entry)
        raise TypeError(
            f"git.show fake got unsupported entry {type(entry).__name__}"
        )


# ---- gh --------------------------------------------------------------


class FakeGhClient:
    """Scripted ``GhClient`` for the harness.

    Implements the read-only surface (`pr_view`, `pr_search`, `api`)
    against the same script dict. Subclasses the real client only to
    pick up the type identity verbs may match on; no superclass methods
    run because every async surface is overridden.
    """

    def __init__(self, scripts: dict[tuple, Any]) -> None:
        self._scripts = scripts

    async def pr_view(self, repo: str, number: int) -> GhPullRequest:
        entry = _lookup(self._scripts, "gh", "pr_view", repo, number)
        if isinstance(entry, GhClientError):
            raise entry
        if isinstance(entry, GhPullRequest):
            return entry
        if isinstance(entry, dict):
            return _gh_pr_from_dict(entry)
        raise TypeError(
            f"gh.pr_view fake got unsupported entry {type(entry).__name__}"
        )

    async def pr_search(
        self, repo: str, query: str, limit: int = 30
    ) -> list[GhPullRequest]:
        entry = _lookup(self._scripts, "gh", "pr_search", repo, query, limit)
        if isinstance(entry, GhClientError):
            raise entry
        if isinstance(entry, list):
            return [
                e if isinstance(e, GhPullRequest) else _gh_pr_from_dict(e)
                for e in entry
            ]
        raise TypeError(
            f"gh.pr_search fake got unsupported entry {type(entry).__name__}"
        )

    async def api(
        self,
        endpoint: str,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        entry = _lookup(self._scripts, "gh", "api", endpoint, method)
        if isinstance(entry, GhClientError):
            raise entry
        if isinstance(entry, dict):
            return entry
        raise TypeError(
            f"gh.api fake got unsupported entry {type(entry).__name__}"
        )


def _gh_pr_from_dict(payload: dict[str, Any]) -> GhPullRequest:
    from datetime import datetime

    merged_at_s = payload.get("mergedAt") or payload.get("merged_at")
    merged_at = None
    if merged_at_s:
        merged_at = datetime.fromisoformat(merged_at_s.replace("Z", "+00:00"))
    return GhPullRequest(
        number=int(payload.get("number", 0)),
        title=str(payload.get("title", "")),
        state=str(payload.get("state", "")),
        merged=bool(payload.get("merged", False)),
        merged_at=merged_at,
        head=str(payload.get("headRefName", payload.get("head", ""))),
        base=str(payload.get("baseRefName", payload.get("base", ""))),
        url=str(payload.get("url", "")),
        raw=dict(payload),
    )


# ---- twig (optional — only attached if `requiem.clients.twig` ships) ----


class FakeTwigClient:
    """Scripted ``TwigClient`` for the harness.

    Mirrors the real client's async surface (``show_async``, ``set_state_async``,
    ``list_children_async``) plus the sync sugar (``show``, ``set_state``,
    ``list_children``). Raises typed ``TwigClientError`` subclasses when the
    scripted entry is an exception.
    """

    def __init__(self, scripts: dict[tuple, Any]) -> None:
        if not _HAS_TWIG:
            raise RuntimeError(
                "requiem.clients.twig is not importable; FakeTwigClient "
                "requires the Mendelssohn Phase B seat to be merged."
            )
        self._scripts = scripts

    async def show_async(self, item_id: int):
        return self._resolve("show", item_id)

    async def set_state_async(self, item_id: int, new_state: str):
        return self._resolve("set_state", item_id, new_state)

    async def list_children_async(self, parent_id: int):
        entry = _lookup(self._scripts, "twig", "list_children", parent_id)
        if isinstance(entry, TwigClientError):
            raise entry
        if isinstance(entry, list):
            return [
                e if isinstance(e, TwigItem) else _twig_item_from_dict(e)
                for e in entry
            ]
        raise TypeError(
            f"twig.list_children fake got unsupported entry {type(entry).__name__}"
        )

    def show(self, item_id: int):
        import asyncio

        return asyncio.run(self.show_async(item_id))

    def set_state(self, item_id: int, new_state: str):
        import asyncio

        return asyncio.run(self.set_state_async(item_id, new_state))

    def list_children(self, parent_id: int):
        import asyncio

        return asyncio.run(self.list_children_async(parent_id))

    def _resolve(self, method: str, *args: Any):
        entry = _lookup(self._scripts, "twig", method, *args)
        if isinstance(entry, TwigClientError):
            raise entry
        if isinstance(entry, TwigItem):
            return entry
        if isinstance(entry, dict):
            return _twig_item_from_dict(entry)
        raise TypeError(
            f"twig.{method} fake got unsupported entry {type(entry).__name__}"
        )


def _twig_item_from_dict(payload: dict[str, Any]):
    if not _HAS_TWIG:  # pragma: no cover — guarded at construction
        raise RuntimeError("requiem.clients.twig not importable")
    return TwigItem(
        id=int(payload["id"]),
        title=str(payload.get("title", "")),
        state=str(payload.get("state", "")),
        area_path=str(payload.get("areaPath", payload.get("area_path", ""))),
        work_item_type=str(payload.get("type", payload.get("work_item_type", ""))),
        parent_id=(
            int(payload["parentId"])
            if payload.get("parentId") is not None
            else payload.get("parent_id")
        ),
        raw=payload,
    )


# ---- FakeToolbelt ----------------------------------------------------


@dataclass
class FakeToolbelt:
    """Build a `Toolbelt` whose clients are scripted by a single dict.

    Pass the same `tool_outputs` dict the `Scenario` carries; the
    factory wires up only the per-tool fakes the dict mentions. The
    `Toolbelt` v0 has fixed named slots (`git`, `files`, `gh`); when
    Sibelius's close-out work extends the slot set (e.g. adds `twig`),
    update the conditional below — the fake itself is already here.
    """

    scripts: dict[tuple, Any] = field(default_factory=dict)
    extra_clients: dict[str, Any] = field(default_factory=dict)
    """Extra named attributes to attach to the built `Toolbelt`.

    Escape hatch for non-frozen-slot tools (workflows that read
    ``ctx.toolbelt.twig`` before the Toolbelt grows a typed slot).
    """

    def build(self) -> Toolbelt:
        tb = Toolbelt(
            git=FakeGitClient(self.scripts),
            files=_FakeFileClient(self.scripts),
            gh=FakeGhClient(self.scripts) if self._has("gh") else None,
        )
        # `Toolbelt` is frozen slots; sidecar attrs go via object.__setattr__
        # which is only available pre-`slots=True`. Hand back the typed
        # instance unchanged when no extras requested.
        if not self.extra_clients and not self._has("twig"):
            return tb
        # Build a thin proxy that forwards the typed slots and exposes
        # the extras as attributes (so `ctx.toolbelt.twig` works without
        # mutating the frozen base).
        extras = dict(self.extra_clients)
        if _HAS_TWIG and self._has("twig") and "twig" not in extras:
            extras["twig"] = FakeTwigClient(self.scripts)
        return _ToolbeltWithExtras(base=tb, extras=extras)  # type: ignore[return-value]

    def _has(self, tool: str) -> bool:
        return any(isinstance(k, tuple) and k and k[0] == tool for k in self.scripts)


class _ToolbeltWithExtras:
    """Thin attribute-forwarding proxy used when the harness needs to
    attach tools that ``Toolbelt`` does not yet have typed slots for.

    Keep the type name distinct from `Toolbelt` so `isinstance` checks
    are unambiguous; verbs that pattern-match on `Toolbelt` already
    should fall through to the real slots via `__getattr__`.
    """

    __slots__ = ("_base", "_extras")

    def __init__(self, base: Toolbelt, extras: dict[str, Any]) -> None:
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_extras", extras)

    def __getattr__(self, name: str) -> Any:
        extras = self._extras
        if name in extras:
            return extras[name]
        return getattr(self._base, name)

    def __repr__(self) -> str:  # pragma: no cover — diagnostic only
        return f"_ToolbeltWithExtras(base={self._base!r}, extras={sorted(self._extras)})"
