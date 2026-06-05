"""KanbanClient — wraps Hermes' ``hermes kanban`` work-delivery CLI.

Architecture mirrors :mod:`requiem.clients.twig` (Liszt B+C hybrid, ADR
0002): a per-tool typed client that verbs receive via the ``Toolbelt`` and
whose typed errors verbs translate into the discriminated ``Outcome`` union.
The client returns plain dataclasses on success and raises a typed
``KanbanClientError`` hierarchy on failure.

## Why this client exists

Hermes is a *real*, external agent runtime (Daniel's local Copilot-CLI-style
tool). Its kanban board is a durable SQLite work-delivery substrate: tasks are
atomically claimed, can depend on one another, and are executed by named
profiles in isolated git worktrees. Requiem uses it as the **external fan-out
executor**: instead of dispatching the ``implementation`` sub-workflow
in-process (which ADR-0013 §B1 shows falls back to *fake* providers and only
*looks* successful), Requiem creates one kanban task per implementable leaf and
lets a real Hermes worker deliver it. See ADR-0014.

## Exit-code / failure posture (Ravel's L-1 caveat applies)

Same cardinal rule as ``twig``: an unclassified failure is **never**
auto-retried. It raises ``KanbanUnknownError`` which verbs MUST convert to
``NeedsHuman``. Auto-retrying an unclassified failure is an
INV-NO-CORRUPT-FORWARD violation.

| signal                                   | raises                     | verb maps to                       |
|------------------------------------------|----------------------------|------------------------------------|
| exit 0                                   | (returns value)            | ``Success``                        |
| stderr "board ... does not exist"        | ``KanbanBoardMissingError``| ``NeedsHuman``                     |
| stderr "database is locked" / "busy"     | ``KanbanBusyError``        | ``RetryableFailure``               |
| stderr "task ... not found"              | ``KanbanTaskNotFoundError``| ``NeedsHuman``                     |
| anything else / bad JSON / timeout       | ``KanbanUnknownError``     | ``NeedsHuman`` — DO NOT auto-retry |

## Board safety

Every call is board-scoped via the global ``--board <slug>`` flag (which Hermes
requires *before* the subcommand). Requiem always targets a dedicated board, never
Hermes' ``default`` board, so a Requiem run can never collide with the operator's
own task queue.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any


# ---- public dataclasses ------------------------------------------------


@dataclass(frozen=True, slots=True)
class KanbanTask:
    """Subset of a kanban task row the executor verbs care about.

    ``raw`` preserves the full JSON payload so callers needing more fields
    don't have to re-shell.
    """

    id: str
    title: str
    status: str
    assignee: str | None
    workspace_kind: str
    branch_name: str | None
    result: str | None
    idempotency_key: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class KanbanRun:
    """One attempt row from ``hermes kanban runs --json``."""

    id: int
    status: str
    outcome: str | None
    summary: str | None
    profile: str | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Outcome of one ``hermes kanban dispatch`` pass."""

    spawned: tuple[str, ...]
    skipped_unassigned: tuple[str, ...]
    promoted: int
    reclaimed: int
    auto_blocked: tuple[str, ...]
    dry_run: bool
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BoardInfo:
    slug: str
    name: str
    db_path: str
    total: int
    raw: dict[str, Any] = field(default_factory=dict)


# ---- error hierarchy ---------------------------------------------------


class KanbanClientError(Exception):
    """Base class. Verbs match on the concrete subclass to pick an outcome."""


class KanbanBoardMissingError(KanbanClientError):
    """Target board does not exist. Verb -> ``NeedsHuman``."""


class KanbanTaskNotFoundError(KanbanClientError):
    """A task referenced by id is gone. Verb -> ``NeedsHuman``."""


class KanbanBusyError(KanbanClientError):
    """SQLite busy / transient lock. Verb -> ``RetryableFailure``."""

    def __init__(self, message: str, retry_after: timedelta | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class KanbanUnknownError(KanbanClientError):
    """Anything we couldn't classify. Verb -> ``NeedsHuman`` (Ravel's L-1).

    Carries ``exit_code`` and ``stderr`` so the human gate has receipts.
    """

    def __init__(self, message: str, *, exit_code: int, stderr: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr


# ---- classification ----------------------------------------------------


_BOARD_MISSING_PAT = re.compile(r"board .* does not exist|no such board", re.IGNORECASE)
_BUSY_PAT = re.compile(r"database is locked|database is busy|\bbusy\b", re.IGNORECASE)
_TASK_NOT_FOUND_PAT = re.compile(r"task .* not found|no such task|unknown task", re.IGNORECASE)


def _classify_failure(exit_code: int, stderr: str) -> KanbanClientError:
    """Map a non-zero ``hermes kanban`` exit to the typed hierarchy.

    Pure function so the table above is auditable from one site.
    """
    if _BOARD_MISSING_PAT.search(stderr):
        return KanbanBoardMissingError(stderr.strip() or "kanban board missing")
    if _BUSY_PAT.search(stderr):
        return KanbanBusyError(stderr.strip() or "kanban DB busy")
    if _TASK_NOT_FOUND_PAT.search(stderr):
        return KanbanTaskNotFoundError(stderr.strip() or "kanban task not found")
    return KanbanUnknownError(
        stderr.strip() or f"hermes kanban exited {exit_code} with no stderr",
        exit_code=exit_code,
        stderr=stderr,
    )


# ---- JSON -> dataclass -------------------------------------------------


def _coerce_task(payload: dict[str, Any]) -> KanbanTask:
    try:
        return KanbanTask(
            id=str(payload["id"]),
            title=str(payload.get("title", "")),
            status=str(payload.get("status", "")),
            assignee=payload.get("assignee"),
            workspace_kind=str(payload.get("workspace_kind", "scratch")),
            branch_name=payload.get("branch_name"),
            result=payload.get("result"),
            idempotency_key=payload.get("idempotency_key"),
            raw=payload,
        )
    except (KeyError, TypeError, ValueError) as e:
        raise KanbanUnknownError(
            f"kanban task JSON missing/invalid required field: {e!r}",
            exit_code=0,
            stderr=json.dumps(payload)[:500],
        ) from e


def _coerce_run(payload: dict[str, Any]) -> KanbanRun:
    try:
        return KanbanRun(
            id=int(payload["id"]),
            status=str(payload.get("status", "")),
            outcome=payload.get("outcome"),
            summary=payload.get("summary"),
            profile=payload.get("profile"),
            raw=payload,
        )
    except (KeyError, TypeError, ValueError) as e:
        raise KanbanUnknownError(
            f"kanban run JSON missing/invalid required field: {e!r}",
            exit_code=0,
            stderr=json.dumps(payload)[:500],
        ) from e


# ---- client ------------------------------------------------------------


class KanbanClient:
    """Read/write wrapper over ``hermes kanban``.

    ``executable`` defaults to ``"hermes"`` (resolved via PATH). ``timeout_s``
    caps each call so a hung subprocess never wedges the engine.
    """

    def __init__(
        self,
        *,
        executable: str = "hermes",
        timeout_s: float = 60.0,
    ) -> None:
        self._executable = executable
        self._timeout_s = timeout_s

    # -- async surface ---------------------------------------------------

    async def version_async(self) -> str:
        stdout, _ = await self._run(["version"])
        return stdout.strip().splitlines()[0] if stdout.strip() else ""

    async def ensure_board_async(self, slug: str) -> None:
        """Create the board if missing (``boards create`` is idempotent)."""
        await self._run(["kanban", "boards", "create", slug])

    async def list_boards_async(self) -> list[BoardInfo]:
        stdout, _ = await self._run(["kanban", "boards", "list", "--json"])
        data = _parse_json_array(stdout)
        return [
            BoardInfo(
                slug=str(b.get("slug", "")),
                name=str(b.get("name", "")),
                db_path=str(b.get("db_path", "")),
                total=int(b.get("total", 0) or 0),
                raw=b,
            )
            for b in data
        ]

    async def create_async(
        self,
        title: str,
        *,
        board: str,
        body: str | None = None,
        idempotency_key: str | None = None,
        workspace: str | None = None,
        branch: str | None = None,
        assignee: str | None = None,
        skills: tuple[str, ...] = (),
        max_runtime: str | None = None,
        created_by: str = "requiem",
    ) -> KanbanTask:
        # No `--model`: verified against hermes v0.15.1 `kanban create --help`,
        # the model is a property of the *assignee profile*, not the task
        # (set via `hermes -p <profile> config set model.default`). Per-task
        # model selection is intentionally absent (ADR-0017 role→profile routing).
        argv = ["kanban", "--board", board, "create", title, "--json", "--created-by", created_by]
        if body is not None:
            argv += ["--body", body]
        if idempotency_key is not None:
            argv += ["--idempotency-key", idempotency_key]
        if workspace is not None:
            argv += ["--workspace", workspace]
        if branch is not None:
            argv += ["--branch", branch]
        if assignee is not None:
            argv += ["--assignee", assignee]
        for s in skills:
            argv += ["--skill", s]
        if max_runtime is not None:
            argv += ["--max-runtime", max_runtime]
        stdout, _ = await self._run(argv)
        return _coerce_task(_parse_json_object(stdout))

    async def link_async(self, parent_id: str, child_id: str, *, board: str) -> None:
        await self._run(["kanban", "--board", board, "link", parent_id, child_id])

    async def assign_async(self, task_id: str, assignee: str, *, board: str) -> None:
        await self._run(["kanban", "--board", board, "assign", task_id, assignee])

    async def list_async(
        self, *, board: str, status: str | None = None
    ) -> list[KanbanTask]:
        argv = ["kanban", "--board", board, "list", "--json"]
        if status is not None:
            argv += ["--status", status]
        stdout, _ = await self._run(argv)
        return [_coerce_task(t) for t in _parse_json_array(stdout)]

    async def runs_async(self, task_id: str, *, board: str) -> list[KanbanRun]:
        stdout, _ = await self._run(
            ["kanban", "--board", board, "runs", task_id, "--json"]
        )
        return [_coerce_run(r) for r in _parse_json_array(stdout)]

    async def dispatch_async(
        self, *, board: str, dry_run: bool = False, max_spawns: int | None = None
    ) -> DispatchResult:
        argv = ["kanban", "--board", board, "dispatch", "--json"]
        if dry_run:
            argv += ["--dry-run"]
        if max_spawns is not None:
            argv += ["--max", str(max_spawns)]
        stdout, _ = await self._run(argv)
        d = _parse_json_object(stdout)
        return DispatchResult(
            spawned=tuple(str(x) for x in (d.get("spawned") or [])),
            skipped_unassigned=tuple(str(x) for x in (d.get("skipped_unassigned") or [])),
            promoted=int(d.get("promoted", 0) or 0),
            reclaimed=int(d.get("reclaimed", 0) or 0),
            auto_blocked=tuple(str(x) for x in (d.get("auto_blocked") or [])),
            dry_run=dry_run,
            raw=d,
        )

    # -- sync sugar ------------------------------------------------------

    def version(self) -> str:
        return asyncio.run(self.version_async())

    def ensure_board(self, slug: str) -> None:
        asyncio.run(self.ensure_board_async(slug))

    def list_boards(self) -> list[BoardInfo]:
        return asyncio.run(self.list_boards_async())

    def create(self, title: str, **kw: Any) -> KanbanTask:
        return asyncio.run(self.create_async(title, **kw))

    def link(self, parent_id: str, child_id: str, *, board: str) -> None:
        asyncio.run(self.link_async(parent_id, child_id, board=board))

    def assign(self, task_id: str, assignee: str, *, board: str) -> None:
        asyncio.run(self.assign_async(task_id, assignee, board=board))

    def list(self, *, board: str, status: str | None = None) -> list[KanbanTask]:
        return asyncio.run(self.list_async(board=board, status=status))

    def runs(self, task_id: str, *, board: str) -> list[KanbanRun]:
        return asyncio.run(self.runs_async(task_id, board=board))

    def dispatch(self, **kw: Any) -> DispatchResult:
        return asyncio.run(self.dispatch_async(**kw))

    # -- runner ----------------------------------------------------------

    async def _run(self, argv: list[str]) -> tuple[str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._executable,
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, NotADirectoryError) as e:
            raise KanbanClientError(
                f"hermes executable not found: exe={self._executable!r}: {e}"
            ) from e

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout_s
            )
        except asyncio.TimeoutError as e:
            proc.kill()
            await proc.wait()
            raise KanbanUnknownError(
                f"hermes kanban timed out after {self._timeout_s}s",
                exit_code=-1,
                stderr="",
            ) from e

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        rc = proc.returncode if proc.returncode is not None else -1
        if rc == 0:
            return stdout, stderr
        raise _classify_failure(rc, stderr)


def _parse_json_object(stdout: str) -> dict[str, Any]:
    data = _loads(stdout)
    if not isinstance(data, dict):
        raise KanbanUnknownError(
            f"kanban JSON root was {type(data).__name__}, expected object",
            exit_code=0,
            stderr=stdout[:500],
        )
    return data


def _parse_json_array(stdout: str) -> list[dict[str, Any]]:
    data = _loads(stdout)
    if not isinstance(data, list):
        raise KanbanUnknownError(
            f"kanban JSON root was {type(data).__name__}, expected array",
            exit_code=0,
            stderr=stdout[:500],
        )
    return [d for d in data if isinstance(d, dict)]


def _loads(stdout: str) -> Any:
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as e:
        raise KanbanUnknownError(
            f"hermes kanban stdout was not valid JSON: {e}",
            exit_code=0,
            stderr=stdout[:500],
        ) from e


def is_hermes_on_path(executable: str = "hermes") -> bool:
    """Smoke-test helper for ``Toolbelt.real()`` preflight."""
    return shutil.which(executable) is not None
