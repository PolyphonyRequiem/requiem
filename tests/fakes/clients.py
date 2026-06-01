"""In-process fakes for the Toolbelt's Phase B clients.

Why these exist
---------------
The real ``TwigClient`` / ``GhClient`` / ``FilesystemClient`` shell out to
external binaries (``twig``, ``gh``, ``git``). Workflow tests need to drive
the verbs end-to-end without touching the network, ADO, or even a real
``git`` install, so we ship small fakes that match the public **method
shape** of each client and can be scripted by the test.

Design rules
------------
* Match the real client's async method names and signatures exactly. Verbs
  can't tell the difference at call time.
* Raise the **same typed exception hierarchies** the real client raises
  (``TwigItemNotFoundError``, ``GhNotFoundError``, ``FsGitError``, ...) so
  the verb's outcome-translation arms exercise the production paths.
* Record every call so tests can assert "exactly one mutation happened"
  (the dry-run test depends on this).
* No pretend "delays" or "rate-limit waves" — scripted behaviour only.
  If a test needs flakiness, push it into the script.

These fakes are intentionally test-only (under ``tests/fakes/``). They are
**not** part of the public ``requiem`` package — workflow authors writing
their own tests are welcome to copy them, but the fake is not a shipping
contract.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from requiem.clients.fs import (
    FsGitError,
    FsNotFoundError,
)
from requiem.clients.gh import (
    GhNotFoundError,
    GhPullRequest,
)
from requiem.clients.twig import (
    TwigItem,
    TwigItemNotFoundError,
    TwigUnknownError,
)


# ---- twig -------------------------------------------------------------


@dataclass
class FakeTwigClient:
    """Scripted ``TwigClient`` for tests.

    ``items`` maps ``item_id`` to either a ``TwigItem`` (returned on
    ``show``) or a ``BaseException`` (raised on ``show``). ``state_calls``
    records every ``set_state`` invocation so a test can assert which
    mutations happened (or didn't, in dry-run).

    ``children_by_parent`` maps ``parent_id`` → list of child ``item_id``s
    so ``list_children_async`` can return concrete children without
    requiring the real twig CLI's "look in raw.children" convention.
    A child whose id is not in ``items`` is silently skipped.
    """

    items: dict[int, TwigItem | BaseException] = field(default_factory=dict)
    children_by_parent: dict[int, list[int]] = field(default_factory=dict)
    state_transitions: list[dict[str, Any]] = field(default_factory=list)
    show_calls: list[int] = field(default_factory=list)
    list_children_calls: list[int] = field(default_factory=list)

    async def show_async(self, item_id: int) -> TwigItem:
        self.show_calls.append(item_id)
        entry = self.items.get(item_id)
        if entry is None:
            raise TwigItemNotFoundError(f"no fake item for id={item_id}")
        if isinstance(entry, BaseException):
            raise entry
        return entry

    def show(self, item_id: int) -> TwigItem:
        """Sync mirror of ``show_async`` for sync verbs.

        Added so this fake also satisfies
        ``requiem.workflows.root_dispatch.TwigClientProto``, whose
        ``fetch_item`` verb calls ``twig.show(item_id)`` synchronously
        (see Wave 5 / Schoenberg drift sentinel).
        """
        self.show_calls.append(item_id)
        entry = self.items.get(item_id)
        if entry is None:
            raise TwigItemNotFoundError(f"no fake item for id={item_id}")
        if isinstance(entry, BaseException):
            raise entry
        return entry

    async def set_state_async(self, item_id: int, new_state: str) -> TwigItem:
        entry = self.items.get(item_id)
        if entry is None or isinstance(entry, BaseException):
            raise TwigItemNotFoundError(f"no fake item for id={item_id}")
        self.state_transitions.append(
            {"item_id": item_id, "from": entry.state, "to": new_state}
        )
        updated = TwigItem(
            id=entry.id,
            title=entry.title,
            state=new_state,
            area_path=entry.area_path,
            work_item_type=entry.work_item_type,
            parent_id=entry.parent_id,
            raw=entry.raw,
        )
        self.items[item_id] = updated
        return updated

    async def list_children_async(self, parent_id: int) -> list[TwigItem]:
        self.list_children_calls.append(parent_id)
        child_ids = list(self.children_by_parent.get(parent_id, []))
        out: list[TwigItem] = []
        for cid in child_ids:
            entry = self.items.get(cid)
            if entry is None or isinstance(entry, BaseException):
                continue
            out.append(entry)
        return out


def make_twig_item(
    item_id: int = 12345,
    title: str = "Refactor outcome dispatch in kernel",
    state: str = "In Review",
    area_path: str = "Requiem\\Phase B",
    work_item_type: str = "Task",
    parent_id: int | None = None,
    linked_prs: list[dict[str, Any]] | None = None,
) -> TwigItem:
    raw: dict[str, Any] = {"id": item_id, "title": title, "state": state}
    if linked_prs is not None:
        raw["pullRequests"] = list(linked_prs)
    return TwigItem(
        id=item_id,
        title=title,
        state=state,
        area_path=area_path,
        work_item_type=work_item_type,
        parent_id=parent_id,
        raw=raw,
    )


def make_criterion(
    item_id: int,
    title: str,
    state: str = "Resolved",
    parent_id: int | None = None,
) -> TwigItem:
    """Sugar for building an Acceptance-Criteria child of a work item."""
    return TwigItem(
        id=item_id,
        title=title,
        state=state,
        area_path="Requiem\\Phase B",
        work_item_type="Acceptance Criteria",
        parent_id=parent_id,
        raw={"id": item_id, "title": title, "state": state},
    )


# ---- gh ---------------------------------------------------------------


@dataclass
class FakeGhClient:
    """Scripted ``GhClient`` for tests.

    ``prs_by_repo`` maps ``repo`` → ``list[GhPullRequest]`` returned on
    ``pr_search``. ``pr_by_number`` maps ``(repo, number)`` →
    ``GhPullRequest`` or ``BaseException`` for ``pr_view``. ``search_queries``
    records the ``query`` string passed to each ``pr_search`` so tests
    can assert the verb formed the right query.
    """

    prs_by_repo: dict[str, list[GhPullRequest]] = field(default_factory=dict)
    pr_by_number: dict[tuple[str, int], GhPullRequest | BaseException] = field(
        default_factory=dict
    )
    search_queries: list[dict[str, Any]] = field(default_factory=list)

    async def pr_search(
        self, repo: str, query: str, limit: int = 30
    ) -> list[GhPullRequest]:
        self.search_queries.append({"repo": repo, "query": query, "limit": limit})
        return list(self.prs_by_repo.get(repo, []))

    async def pr_view(self, repo: str, number: int) -> GhPullRequest:
        entry = self.pr_by_number.get((repo, number))
        if entry is None:
            raise GhNotFoundError(f"fake: no PR {number} in {repo}")
        if isinstance(entry, BaseException):
            raise entry
        return entry


def make_pr(
    number: int = 347,
    title: str = "Refactor outcome dispatch in kernel",
    merged: bool = True,
    state: str | None = None,
    merged_at: datetime | None = None,
    head: str = "feature/refactor-outcomes",
    base: str = "main",
    url: str | None = None,
    merge_sha: str | None = "a3f9c7e1234567890abcdef0123456789abcdef0",
) -> GhPullRequest:
    s = state if state is not None else ("MERGED" if merged else "OPEN")
    if merged and merged_at is None:
        merged_at = datetime(2026, 5, 31, 14, 22, 0, tzinfo=timezone.utc)
    if not merged:
        merged_at = None
        merge_sha = None
    raw: dict[str, Any] = {"number": number, "title": title, "state": s}
    if merge_sha:
        raw["mergeCommit"] = {"oid": merge_sha}
    return GhPullRequest(
        number=number,
        title=title,
        state=s,
        merged=merged,
        merged_at=merged_at,
        head=head,
        base=base,
        url=url or f"https://github.com/acme/widgets/pull/{number}",
        raw=raw,
    )


# ---- fs ---------------------------------------------------------------


@dataclass
class FakeFilesystemClient:
    """Scripted ``FilesystemClient`` for tests.

    Backed by a tiny in-memory file-table ``{path: content}``. ``git_mv``
    moves the entry and records the move; if ``mv_should_fail`` is set,
    the next ``git_mv`` raises ``FsGitError``. Tests use ``git_mv_calls``
    to assert dry-run did not move anything.
    """

    files: dict[Path, str] = field(default_factory=dict)
    git_mv_calls: list[dict[str, Path]] = field(default_factory=list)
    write_calls: list[dict[str, Any]] = field(default_factory=list)
    mv_should_fail: BaseException | None = None
    repo_root: Path = field(default_factory=lambda: Path("."))

    def exists(self, path: Path) -> bool:
        return Path(path) in self.files

    def read_text(self, path: Path) -> str:
        p = Path(path)
        if p not in self.files:
            raise FsNotFoundError(p)
        return self.files[p]

    def write_text(self, path: Path, content: str) -> None:
        p = Path(path)
        self.files[p] = content
        self.write_calls.append({"path": p, "bytes": len(content.encode("utf-8"))})

    async def git_mv(self, src: Path, dst: Path) -> None:
        src = Path(src)
        dst = Path(dst)
        if self.mv_should_fail is not None:
            err = self.mv_should_fail
            self.mv_should_fail = None
            raise err
        if src not in self.files:
            raise FsNotFoundError(src)
        self.files[dst] = self.files.pop(src)
        self.git_mv_calls.append({"src": src, "dst": dst})


__all__ = [
    "FakeTwigClient",
    "FakeGhClient",
    "FakeFilesystemClient",
    "make_twig_item",
    "make_criterion",
    "make_pr",
    # Re-export the typed-error classes so tests don't need to know which
    # client module owns each one.
    "TwigItemNotFoundError",
    "TwigUnknownError",
    "GhNotFoundError",
    "FsGitError",
    "FsNotFoundError",
]
