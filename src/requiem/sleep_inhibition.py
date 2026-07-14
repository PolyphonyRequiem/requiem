"""Mandatory system-sleep inhibition for fenced Scenario launches."""
from __future__ import annotations

import ctypes
import sys
import threading
from collections.abc import Callable
from types import TracebackType
from typing import Protocol


ES_SYSTEM_REQUIRED = 0x00000001
ES_CONTINUOUS = 0x80000000
_WINDOWS_INHIBIT_FLAGS = ES_CONTINUOUS | ES_SYSTEM_REQUIRED

ExecutionStateSetter = Callable[[int], int]


class SleepInhibitionError(RuntimeError):
    """The launcher cannot prove that automatic system sleep is inhibited."""


class SystemSleepInhibitor(Protocol):
    def __enter__(self) -> SystemSleepInhibitor: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


def _load_windows_execution_state_setter() -> ExecutionStateSetter:
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError) as error:
        raise SleepInhibitionError(
            "Windows SetThreadExecutionState is unavailable"
        ) from error
    setter = kernel32.SetThreadExecutionState
    setter.argtypes = [ctypes.c_uint]
    setter.restype = ctypes.c_uint
    return setter


class WindowsSystemSleepInhibitor:
    """Hold a thread-scoped Windows SYSTEM execution-state request."""

    def __init__(
        self,
        *,
        set_execution_state: ExecutionStateSetter | None = None,
    ) -> None:
        self._set_execution_state = (
            set_execution_state or _load_windows_execution_state_setter()
        )
        self._thread_id: int | None = None

    def _set_state(self, flags: int, action: str) -> None:
        try:
            result = self._set_execution_state(flags)
        except OSError as error:
            raise SleepInhibitionError(
                f"SetThreadExecutionState failed to {action}: {error}"
            ) from error
        if result == 0:
            raise SleepInhibitionError(
                f"SetThreadExecutionState failed to {action}"
            )

    def __enter__(self) -> WindowsSystemSleepInhibitor:
        if self._thread_id is not None:
            raise SleepInhibitionError("system-sleep inhibition is already active")
        self._set_state(
            _WINDOWS_INHIBIT_FLAGS,
            "establish ES_CONTINUOUS | ES_SYSTEM_REQUIRED",
        )
        self._thread_id = threading.get_ident()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        thread_id = self._thread_id
        if thread_id is None:
            return
        if threading.get_ident() != thread_id:
            raise SleepInhibitionError(
                "system-sleep inhibition must be cleared on its controlling thread"
            )
        self._set_state(ES_CONTINUOUS, "clear ES_SYSTEM_REQUIRED")
        self._thread_id = None


def create_system_sleep_inhibitor(
    *,
    platform: str | None = None,
) -> SystemSleepInhibitor:
    current_platform = platform or sys.platform
    if current_platform != "win32":
        raise SleepInhibitionError(
            "fenced launching is unsupported on "
            f"{current_platform!r}: a mandatory system-sleep inhibitor is required"
        )
    return WindowsSystemSleepInhibitor()
