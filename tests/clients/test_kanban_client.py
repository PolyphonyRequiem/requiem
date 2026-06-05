"""KanbanClient tests.

Two kinds, mirroring ``test_twig``:

1. **Hermetic** — patch ``asyncio.create_subprocess_exec`` at the
   ``requiem.clients.kanban`` boundary and inject scripted (stdout, stderr,
   exit) tuples. These cover the classification table and the JSON->dataclass
   lift. They always run.
2. **Real-tool smoke** — gated by ``hermes`` being on PATH. Creates a throwaway
   board, exercises create/link/list/runs/dispatch(dry-run), then deletes the
   board. Skips cleanly if hermes is absent. NEVER touches the ``default`` board.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from unittest.mock import patch

import pytest

from requiem.clients.kanban import (
    KanbanBoardMissingError,
    KanbanBusyError,
    KanbanClient,
    KanbanTaskNotFoundError,
    KanbanUnknownError,
    _classify_failure,
    is_hermes_on_path,
)


# ---- subprocess fake ---------------------------------------------------


class _FakeProc:
    def __init__(self, stdout: bytes, stderr: bytes, returncode: int) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:  # pragma: no cover
        pass

    async def wait(self) -> int:  # pragma: no cover
        return self.returncode


def _patch_proc(stdout: str = "", stderr: str = "", rc: int = 0):
    async def _fake_exec(*args, **kwargs):
        return _FakeProc(stdout.encode(), stderr.encode(), rc)

    return patch(
        "requiem.clients.kanban.asyncio.create_subprocess_exec", new=_fake_exec
    )


# ---- classification table ----------------------------------------------


def test_classify_board_missing():
    err = _classify_failure(1, "kanban: board 'x' does not exist.")
    assert isinstance(err, KanbanBoardMissingError)


def test_classify_busy():
    err = _classify_failure(1, "sqlite3.OperationalError: database is locked")
    assert isinstance(err, KanbanBusyError)


def test_classify_task_not_found():
    err = _classify_failure(1, "task t_abc not found")
    assert isinstance(err, KanbanTaskNotFoundError)


def test_classify_unknown_is_default():
    err = _classify_failure(1, "something weird happened")
    assert isinstance(err, KanbanUnknownError)
    assert err.exit_code == 1


# ---- JSON -> dataclass lift --------------------------------------------


async def test_create_lifts_task_json():
    payload = {"id": "t_1", "title": "leaf", "status": "ready", "assignee": None,
               "workspace_kind": "worktree", "branch_name": "impl/x", "result": None}
    with _patch_proc(stdout=json.dumps(payload)):
        client = KanbanClient()
        task = await client.create_async("leaf", board="b", branch="impl/x")
    assert task.id == "t_1"
    assert task.branch_name == "impl/x"
    assert task.workspace_kind == "worktree"


async def test_list_lifts_array():
    payload = [{"id": "t_1", "title": "a", "status": "ready"},
               {"id": "t_2", "title": "b", "status": "done", "result": "PR"}]
    with _patch_proc(stdout=json.dumps(payload)):
        tasks = await KanbanClient().list_async(board="b")
    assert [t.id for t in tasks] == ["t_1", "t_2"]
    assert tasks[1].result == "PR"


async def test_runs_lifts_outcomes():
    payload = [{"id": 1, "status": "done", "outcome": "completed", "summary": "ok"}]
    with _patch_proc(stdout=json.dumps(payload)):
        runs = await KanbanClient().runs_async("t_1", board="b")
    assert runs[0].outcome == "completed"


async def test_dispatch_parses_result():
    payload = {"spawned": ["t_1"], "skipped_unassigned": ["t_2"], "promoted": 1}
    with _patch_proc(stdout=json.dumps(payload)):
        res = await KanbanClient().dispatch_async(board="b", dry_run=True)
    assert res.spawned == ("t_1",)
    assert res.skipped_unassigned == ("t_2",)
    assert res.dry_run is True


async def test_nonzero_exit_raises_classified():
    with _patch_proc(stderr="board 'x' does not exist", rc=1):
        with pytest.raises(KanbanBoardMissingError):
            await KanbanClient().list_async(board="x")


async def test_bad_json_raises_unknown():
    with _patch_proc(stdout="not json"):
        with pytest.raises(KanbanUnknownError):
            await KanbanClient().list_async(board="b")


# ---- real-tool smoke ---------------------------------------------------


@pytest.mark.skipif(not is_hermes_on_path(), reason="hermes not on PATH")
def test_real_kanban_roundtrip_on_throwaway_board():
    """Create a throwaway board, run the full client surface against the real
    `hermes kanban` binary, then delete the board. Never touches `default`."""
    client = KanbanClient()
    board = "requiem-pytest-smoke"
    try:
        client.ensure_board(board)
        boards = {b.slug for b in client.list_boards()}
        assert board in boards

        t1 = client.create("smoke leaf 1", board=board, branch="impl/smoke-1",
                            idempotency_key="requiem:smoke:1", workspace="worktree")
        t2 = client.create("smoke leaf 2", board=board, branch="impl/smoke-2",
                            idempotency_key="requiem:smoke:2", workspace="worktree")
        assert t1.id != t2.id

        # Idempotency: same key returns the same task id.
        t1_again = client.create("smoke leaf 1", board=board,
                                 idempotency_key="requiem:smoke:1")
        assert t1_again.id == t1.id

        client.link(t1.id, t2.id, board=board)
        listed = {t.id for t in client.list(board=board)}
        assert {t1.id, t2.id} <= listed

        # Unassigned tasks are not spawned by a dry-run dispatch.
        disp = client.dispatch(board=board, dry_run=True)
        assert t1.id in disp.skipped_unassigned or t1.id in disp.spawned

        assert client.runs(t1.id, board=board) == []  # no worker ran yet
    finally:
        subprocess.run(["hermes", "kanban", "boards", "rm", board, "--delete"],
                       capture_output=True, text=True)
