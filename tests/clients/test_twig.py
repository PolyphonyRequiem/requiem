"""TwigClient tests.

Two kinds of test in this module:

1. **Hermetic** -- patch `asyncio.create_subprocess_exec` at the module
   boundary inside `requiem.clients.twig` and inject scripted (stdout,
   stderr, exit) tuples. These cover the classification table and the
   JSON->dataclass lift. They always run.

2. **Real-tool smoke** -- gated by `RUN_REAL_TWIG=1`. Calls `twig
   --version` as the lightest signal that the subprocess seam wires up
   to a real binary. Skips with a clear message if twig isn't on PATH.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from requiem.clients.twig import (
    TwigClient,
    TwigClientError,
    TwigItem,
    TwigItemNotFoundError,
    TwigRateLimitedError,
    TwigUnknownError,
    _classify_failure,
    _parse_retry_after,
    is_twig_on_path,
)


# ---- subprocess fake ---------------------------------------------------


class _FakeProc:
    """Mimics enough of `asyncio.subprocess.Process` for the runner."""

    def __init__(self, stdout: bytes, stderr: bytes, returncode: int) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:  # pragma: no cover -- only called in timeout test
        pass

    async def wait(self) -> int:  # pragma: no cover
        return self.returncode


def _scripted(stdout: bytes | str, stderr: bytes | str = b"", returncode: int = 0):
    """Return a coroutine factory suitable for patching `create_subprocess_exec`.

    The patched callable must be a *coroutine function* because the
    production code awaits it.
    """
    if isinstance(stdout, str):
        stdout = stdout.encode("utf-8")
    if isinstance(stderr, str):
        stderr = stderr.encode("utf-8")
    captured: dict = {}

    async def _factory(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeProc(stdout, stderr, returncode)

    _factory.captured = captured  # type: ignore[attr-defined]
    return _factory


# ---- sample payloads ---------------------------------------------------


_ITEM_JSON = json.dumps(
    {
        "id": 1234,
        "title": "Wire close-out",
        "type": "Task",
        "state": "Active",
        "areaPath": "Requiem\\PhaseB",
        "parentId": 1200,
        "children": [
            {"id": 1235, "title": "child A", "type": "Task", "state": "To Do"},
            {"id": 1236, "title": "child B", "type": "Task", "state": "Done"},
        ],
    }
)


_CHILD_A_JSON = json.dumps(
    {
        "id": 1235,
        "title": "child A",
        "type": "Task",
        "state": "To Do",
        "areaPath": "Requiem\\PhaseB",
        "parentId": 1234,
    }
)


_CHILD_B_JSON = json.dumps(
    {
        "id": 1236,
        "title": "child B",
        "type": "Task",
        "state": "Done",
        "areaPath": "Requiem\\PhaseB",
        "parentId": 1234,
    }
)


# ---- classifier unit tests ---------------------------------------------


class TestClassify:
    def test_exit_1_rate_limit_phrase(self):
        err = _classify_failure(1, "Request was rate limited; retry after 30s")
        assert isinstance(err, TwigRateLimitedError)
        assert err.retry_after == timedelta(seconds=30)

    def test_exit_1_throttled_phrase(self):
        err = _classify_failure(1, "ADO returned 429 Throttled")
        assert isinstance(err, TwigRateLimitedError)
        assert err.retry_after is None

    def test_exit_1_not_found_phrase(self):
        err = _classify_failure(1, "Work item #42 not found in local cache")
        assert isinstance(err, TwigItemNotFoundError)

    def test_exit_1_tf40001(self):
        err = _classify_failure(1, "TF40001: work item does not exist")
        assert isinstance(err, TwigItemNotFoundError)

    def test_exit_1_unknown_goes_to_unknown_not_retryable(self):
        """Ravel's L-1 caveat: unclassified exit 1 must be UnknownError."""
        err = _classify_failure(1, "something weird happened with the wire")
        assert isinstance(err, TwigUnknownError)
        assert not isinstance(err, TwigRateLimitedError)
        assert err.exit_code == 1

    def test_exit_2_is_unknown_even_with_rate_limit_phrase(self):
        """Only exit 1 gets pattern-matched. Higher exits are always unknown."""
        err = _classify_failure(2, "rate limit") 
        assert isinstance(err, TwigUnknownError)
        assert not isinstance(err, TwigRateLimitedError)

    def test_exit_5_is_unknown(self):
        err = _classify_failure(5, "corruption suspected")
        assert isinstance(err, TwigUnknownError)
        assert err.exit_code == 5

    def test_empty_stderr_still_yields_unknown_with_message(self):
        err = _classify_failure(1, "")
        assert isinstance(err, TwigUnknownError)
        assert str(err)

    @pytest.mark.parametrize(
        "stderr,expected_s",
        [
            ("retry after 5s", 5),
            ("Retry-After: 12", 12),
            ("try again in 90 seconds", 90),
            ("nothing here", None),
        ],
    )
    def test_parse_retry_after(self, stderr, expected_s):
        out = _parse_retry_after(stderr)
        if expected_s is None:
            assert out is None
        else:
            assert out == timedelta(seconds=expected_s)


# ---- runner / show / set_state / list_children -------------------------


_PATCH_TARGET = "requiem.clients.twig.asyncio.create_subprocess_exec"


class TestShow:
    def test_happy_show_returns_typed_item(self):
        fake = _scripted(_ITEM_JSON, returncode=0)
        with patch(_PATCH_TARGET, fake):
            item = asyncio.run(TwigClient().show_async(1234))
        assert isinstance(item, TwigItem)
        assert item.id == 1234
        assert item.title == "Wire close-out"
        assert item.state == "Active"
        assert item.area_path == "Requiem\\PhaseB"
        assert item.work_item_type == "Task"
        assert item.parent_id == 1200
        assert item.raw["children"][0]["id"] == 1235

    def test_show_passes_expected_argv(self):
        fake = _scripted(_ITEM_JSON, returncode=0)
        with patch(_PATCH_TARGET, fake):
            asyncio.run(TwigClient().show_async(7))
        args = fake.captured["args"]
        assert args[0] == "twig"
        assert list(args[1:]) == ["show", "7", "--output", "json"]

    def test_show_no_parent_yields_none(self):
        payload = json.loads(_ITEM_JSON)
        payload["parentId"] = None
        fake = _scripted(json.dumps(payload), returncode=0)
        with patch(_PATCH_TARGET, fake):
            item = asyncio.run(TwigClient().show_async(1234))
        assert item.parent_id is None

    def test_show_rate_limited_raises_typed_error(self):
        fake = _scripted("", b"Rate limit exceeded; retry after 15s", returncode=1)
        with patch(_PATCH_TARGET, fake):
            with pytest.raises(TwigRateLimitedError) as exc:
                asyncio.run(TwigClient().show_async(1234))
        assert exc.value.retry_after == timedelta(seconds=15)

    def test_show_not_found_raises_typed_error(self):
        fake = _scripted("", b"error: Work item #99 not found in local cache", returncode=1)
        with patch(_PATCH_TARGET, fake):
            with pytest.raises(TwigItemNotFoundError):
                asyncio.run(TwigClient().show_async(99))

    def test_show_unknown_failure_raises_unknown_error(self):
        """Unknown exit-1 stays unknown -- the verb will route to NeedsHuman."""
        fake = _scripted("", b"some unexpected diagnostic", returncode=1)
        with patch(_PATCH_TARGET, fake):
            with pytest.raises(TwigUnknownError) as exc:
                asyncio.run(TwigClient().show_async(1234))
        assert exc.value.exit_code == 1
        # Critically: not the retry/notfound subclasses.
        assert not isinstance(exc.value, (TwigRateLimitedError, TwigItemNotFoundError))

    def test_show_invalid_json_raises_unknown(self):
        fake = _scripted("not json at all", returncode=0)
        with patch(_PATCH_TARGET, fake):
            with pytest.raises(TwigUnknownError):
                asyncio.run(TwigClient().show_async(1234))

    def test_show_missing_required_field_raises_unknown(self):
        fake = _scripted(json.dumps({"title": "no id field"}), returncode=0)
        with patch(_PATCH_TARGET, fake):
            with pytest.raises(TwigUnknownError):
                asyncio.run(TwigClient().show_async(1234))

    def test_show_decodes_non_utf8_bytes_leniently(self):
        # 0xff is invalid UTF-8; replace should kick in and the parser then
        # rejects the garbled JSON as TwigUnknownError. The test asserts we
        # do not crash with a UnicodeDecodeError up the stack.
        fake = _scripted(b"\xffnot json", returncode=0)
        with patch(_PATCH_TARGET, fake):
            with pytest.raises(TwigUnknownError):
                asyncio.run(TwigClient().show_async(1234))


class TestSetState:
    def test_happy_set_state_returns_item(self):
        payload = json.loads(_ITEM_JSON)
        payload["state"] = "Done"
        fake = _scripted(json.dumps(payload), returncode=0)
        with patch(_PATCH_TARGET, fake):
            item = asyncio.run(TwigClient().set_state_async(1234, "Done"))
        assert item.state == "Done"

    def test_set_state_passes_argv_in_twig_order(self):
        fake = _scripted(_ITEM_JSON, returncode=0)
        with patch(_PATCH_TARGET, fake):
            asyncio.run(TwigClient().set_state_async(1234, "Active"))
        args = fake.captured["args"]
        assert list(args[1:]) == [
            "state",
            "Active",
            "--id",
            "1234",
            "--output",
            "json",
        ]

    def test_set_state_refetches_when_payload_is_thin(self):
        """If `state` returns a non-work-item payload, fall back to `show`."""
        calls: list[list[str]] = []

        async def factory(*args, **kwargs):
            calls.append(list(args))
            # First call: state returns a minimal ack; second: show returns the item.
            if "state" in args:
                return _FakeProc(b'{"ok": true}', b"", 0)
            return _FakeProc(_ITEM_JSON.encode(), b"", 0)

        with patch(_PATCH_TARGET, factory):
            item = asyncio.run(TwigClient().set_state_async(1234, "Done"))
        assert item.id == 1234
        # We expect two subprocess calls: state, then show.
        assert len(calls) == 2
        assert "state" in calls[0]
        assert "show" in calls[1]


class TestListChildren:
    def test_list_children_returns_hydrated_items(self):
        responses = {
            ("show", "1234"): _ITEM_JSON,
            ("show", "1235"): _CHILD_A_JSON,
            ("show", "1236"): _CHILD_B_JSON,
        }

        async def factory(*args, **kwargs):
            key = (args[1], args[2])
            return _FakeProc(responses[key].encode(), b"", 0)

        with patch(_PATCH_TARGET, factory):
            children = asyncio.run(TwigClient().list_children_async(1234))
        assert [c.id for c in children] == [1235, 1236]
        assert all(c.parent_id == 1234 for c in children)

    def test_list_children_empty_returns_empty(self):
        payload = json.loads(_ITEM_JSON)
        payload["children"] = []
        fake = _scripted(json.dumps(payload), returncode=0)
        with patch(_PATCH_TARGET, fake):
            children = asyncio.run(TwigClient().list_children_async(1234))
        assert children == []


# ---- create_child (Wave 6 / Liszt) -------------------------------------


_NEW_CHILD_JSON = json.dumps(
    {
        "id": 4242,
        "title": "New child seeded by recursive plan",
        "type": "Task",
        "state": "New",
        "areaPath": "PolyphonyRequiem\\v0",
        "parentId": 1234,
    }
)


class TestCreateChild:
    """Coverage for the create_child seam added for Mahler-3 §2.4.

    Mirrors the other client-method test classes: scripted subprocess
    fakes, an argv-shape assertion, the same error taxonomy as the rest
    of the table, and a thin-payload guard that delegates to
    ``show_async`` (analogous to ``set_state_async``).
    """

    def test_happy_create_returns_typed_item(self):
        fake = _scripted(_NEW_CHILD_JSON, returncode=0)
        with patch(_PATCH_TARGET, fake):
            item = asyncio.run(
                TwigClient().create_child_async(
                    parent_id=1234,
                    title="New child seeded by recursive plan",
                    work_item_type="Task",
                )
            )
        assert isinstance(item, TwigItem)
        assert item.id == 4242
        assert item.parent_id == 1234
        assert item.work_item_type == "Task"
        assert item.title == "New child seeded by recursive plan"

    def test_create_passes_expected_argv(self):
        """Polyphony's twig contract: --parent / --title / --work-item-type."""
        fake = _scripted(_NEW_CHILD_JSON, returncode=0)
        with patch(_PATCH_TARGET, fake):
            asyncio.run(
                TwigClient().create_child_async(
                    parent_id=1234,
                    title="New child",
                    work_item_type="Task",
                )
            )
        args = list(fake.captured["args"])
        assert args[0] == "twig"
        assert args[1:] == [
            "create-child",
            "--parent", "1234",
            "--title", "New child",
            "--work-item-type", "Task",
            "--output", "json",
        ]

    def test_create_includes_optional_area_and_description(self):
        fake = _scripted(_NEW_CHILD_JSON, returncode=0)
        with patch(_PATCH_TARGET, fake):
            asyncio.run(
                TwigClient().create_child_async(
                    parent_id=1234,
                    title="New child",
                    work_item_type="Task",
                    area_path="PolyphonyRequiem\\v0",
                    description="Spawned by recursive planning.",
                )
            )
        args = list(fake.captured["args"])
        # Order: positional verb, parent, title, type, optionals, output.
        assert args[1:] == [
            "create-child",
            "--parent", "1234",
            "--title", "New child",
            "--work-item-type", "Task",
            "--area-path", "PolyphonyRequiem\\v0",
            "--description", "Spawned by recursive planning.",
            "--output", "json",
        ]

    def test_create_omits_optionals_when_none(self):
        """Defaults must not become empty-string flags (twig would reject)."""
        fake = _scripted(_NEW_CHILD_JSON, returncode=0)
        with patch(_PATCH_TARGET, fake):
            asyncio.run(
                TwigClient().create_child_async(
                    parent_id=1234,
                    title="New child",
                    work_item_type="Task",
                )
            )
        args = list(fake.captured["args"])
        assert "--area-path" not in args
        assert "--description" not in args

    def test_create_refetches_when_payload_is_thin(self):
        """If create-child returns just `{id: N}`, fall back to `show N`.

        Mirrors ``set_state_async``'s belt-and-brace so callers always
        get a complete ``TwigItem`` regardless of twig version drift in
        the create response shape.
        """
        calls: list[list[str]] = []

        async def factory(*args, **kwargs):
            calls.append(list(args))
            if "create-child" in args:
                return _FakeProc(b'{"id": 4242}', b"", 0)
            return _FakeProc(_NEW_CHILD_JSON.encode(), b"", 0)

        with patch(_PATCH_TARGET, factory):
            item = asyncio.run(
                TwigClient().create_child_async(
                    parent_id=1234,
                    title="New child",
                    work_item_type="Task",
                )
            )
        assert item.id == 4242
        assert item.title == "New child seeded by recursive plan"
        # We expect two subprocess calls: create-child, then show.
        assert len(calls) == 2
        assert "create-child" in calls[0]
        assert "show" in calls[1]
        assert "4242" in calls[1]

    def test_create_thin_payload_missing_id_raises_unknown(self):
        """A create response with no `id` field is a schema drift signal."""
        fake = _scripted(b'{"ok": true}', returncode=0)
        with patch(_PATCH_TARGET, fake):
            with pytest.raises(TwigUnknownError):
                asyncio.run(
                    TwigClient().create_child_async(
                        parent_id=1234,
                        title="New child",
                        work_item_type="Task",
                    )
                )

    def test_create_invalid_json_raises_unknown(self):
        fake = _scripted("not json at all", returncode=0)
        with patch(_PATCH_TARGET, fake):
            with pytest.raises(TwigUnknownError):
                asyncio.run(
                    TwigClient().create_child_async(
                        parent_id=1234,
                        title="New child",
                        work_item_type="Task",
                    )
                )

    def test_create_rate_limited_raises_typed_error(self):
        fake = _scripted("", b"Rate limit exceeded; retry after 30s", returncode=1)
        with patch(_PATCH_TARGET, fake):
            with pytest.raises(TwigRateLimitedError) as exc:
                asyncio.run(
                    TwigClient().create_child_async(
                        parent_id=1234,
                        title="New child",
                        work_item_type="Task",
                    )
                )
        from datetime import timedelta as _td
        assert exc.value.retry_after == _td(seconds=30)

    def test_create_parent_not_found_raises_typed_error(self):
        """If the parent doesn't exist, twig returns not_found on exit 1."""
        fake = _scripted("", b"error: parent work item not found", returncode=1)
        with patch(_PATCH_TARGET, fake):
            with pytest.raises(TwigItemNotFoundError):
                asyncio.run(
                    TwigClient().create_child_async(
                        parent_id=999999,
                        title="New child",
                        work_item_type="Task",
                    )
                )

    def test_create_unknown_failure_raises_unknown_not_retryable(self):
        """Ravel's L-1 caveat: unclassified exit 1 must be UnknownError."""
        fake = _scripted("", b"workflow validation failed for ItemType=Task", returncode=1)
        with patch(_PATCH_TARGET, fake):
            with pytest.raises(TwigUnknownError) as exc:
                asyncio.run(
                    TwigClient().create_child_async(
                        parent_id=1234,
                        title="New child",
                        work_item_type="Task",
                    )
                )
        assert exc.value.exit_code == 1
        assert not isinstance(exc.value, (TwigRateLimitedError, TwigItemNotFoundError))

    def test_create_stdin_is_devnull(self):
        """Schumann's caveat: every subprocess call must pin stdin=DEVNULL."""
        fake = _scripted(_NEW_CHILD_JSON, returncode=0)
        with patch(_PATCH_TARGET, fake):
            asyncio.run(
                TwigClient().create_child_async(
                    parent_id=1234,
                    title="New child",
                    work_item_type="Task",
                )
            )
        assert fake.captured["kwargs"]["stdin"] == asyncio.subprocess.DEVNULL

    def test_sync_create_child_wraps_async(self):
        fake = _scripted(_NEW_CHILD_JSON, returncode=0)
        with patch(_PATCH_TARGET, fake):
            item = TwigClient().create_child(
                parent_id=1234,
                title="New child",
                work_item_type="Task",
            )
        assert isinstance(item, TwigItem)
        assert item.id == 4242


# ---- cwd / executable failure modes ------------------------------------


class TestRunnerFailureModes:
    def test_missing_executable_raises_client_error(self):
        async def factory(*args, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", args[0])

        with patch(_PATCH_TARGET, factory):
            with pytest.raises(TwigClientError) as exc:
                asyncio.run(TwigClient(executable="not-a-real-binary").show_async(1))
        msg = str(exc.value)
        assert "invalid cwd or executable" in msg

    def test_bad_cwd_windows_style_raises_client_error(self):
        """On Windows, an invalid cwd raises NotADirectoryError -- catch it."""

        async def factory(*args, **kwargs):
            raise NotADirectoryError(20, "Not a directory", kwargs.get("cwd"))

        with patch(_PATCH_TARGET, factory):
            with pytest.raises(TwigClientError):
                asyncio.run(
                    TwigClient(cwd=Path(r"C:\nope\not-a-dir")).show_async(1)
                )

    def test_bad_cwd_posix_style_raises_client_error(self):
        """On POSIX, the same condition raises FileNotFoundError."""

        async def factory(*args, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", kwargs.get("cwd"))

        with patch(_PATCH_TARGET, factory):
            with pytest.raises(TwigClientError):
                asyncio.run(TwigClient(cwd=Path("/definitely/not/here")).show_async(1))

    def test_cwd_is_forwarded_to_subprocess(self):
        fake = _scripted(_ITEM_JSON, returncode=0)
        with patch(_PATCH_TARGET, fake):
            asyncio.run(TwigClient(cwd=Path(r"C:\some\repo")).show_async(1))
        assert fake.captured["kwargs"]["cwd"] == r"C:\some\repo"

    def test_no_cwd_passes_none(self):
        fake = _scripted(_ITEM_JSON, returncode=0)
        with patch(_PATCH_TARGET, fake):
            asyncio.run(TwigClient().show_async(1))
        assert fake.captured["kwargs"]["cwd"] is None

    def test_stdin_is_devnull(self):
        """Schumann's caveat: on Py3.14 + Windows + pytest, captured stdin
        is not inheritable. Every subprocess call MUST pin stdin=DEVNULL."""
        fake = _scripted(_ITEM_JSON, returncode=0)
        with patch(_PATCH_TARGET, fake):
            asyncio.run(TwigClient().show_async(1))
        assert fake.captured["kwargs"]["stdin"] == asyncio.subprocess.DEVNULL


# ---- sync sugar --------------------------------------------------------


def test_sync_show_wraps_async():
    fake = _scripted(_ITEM_JSON, returncode=0)
    with patch(_PATCH_TARGET, fake):
        item = TwigClient().show(1234)
    assert item.id == 1234


# ---- real-tool smoke (opt in) ------------------------------------------


@pytest.mark.skipif(
    os.environ.get("RUN_REAL_TWIG") != "1",
    reason="set RUN_REAL_TWIG=1 to run the real twig --version smoke test",
)
def test_real_twig_version_smoke():
    if not is_twig_on_path():
        pytest.skip("twig binary not on PATH")
    # `twig --version` exits 0 with the version string on stdout. We don't
    # parse it -- the point is that the subprocess seam wires up end-to-end.
    import subprocess

    r = subprocess.run(
        ["twig", "--version"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode == 0, f"twig --version failed: {r.stderr!r}"
    assert r.stdout.strip(), "twig --version produced no output"


# ---- Python 3.11+ asyncio.run interaction sanity -----------------------


def test_python_version_is_supported():
    """Guardrail: the asyncio surface assumes 3.11+ semantics."""
    assert sys.version_info >= (3, 11)
