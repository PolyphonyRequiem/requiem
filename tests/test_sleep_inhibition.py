from __future__ import annotations

import pytest

from requiem.sleep_inhibition import (
    ES_CONTINUOUS,
    ES_SYSTEM_REQUIRED,
    SleepInhibitionError,
    WindowsSystemSleepInhibitor,
    create_system_sleep_inhibitor,
)


def test_windows_inhibitor_uses_system_only_flags_and_clears() -> None:
    calls: list[int] = []

    def set_execution_state(flags: int) -> int:
        calls.append(flags)
        return ES_CONTINUOUS

    with WindowsSystemSleepInhibitor(
        set_execution_state=set_execution_state,
    ):
        assert calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED]

    assert calls == [
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED,
        ES_CONTINUOUS,
    ]


def test_windows_inhibitor_clears_after_body_failure() -> None:
    calls: list[int] = []

    def set_execution_state(flags: int) -> int:
        calls.append(flags)
        return ES_CONTINUOUS

    with (
        pytest.raises(RuntimeError, match="body failed"),
        WindowsSystemSleepInhibitor(
            set_execution_state=set_execution_state,
        ),
    ):
        raise RuntimeError("body failed")

    assert calls[-1] == ES_CONTINUOUS


def test_windows_inhibitor_reports_preflight_failure() -> None:
    inhibitor = WindowsSystemSleepInhibitor(
        set_execution_state=lambda flags: 0,
    )

    with pytest.raises(
        SleepInhibitionError,
        match=r"establish ES_CONTINUOUS \| ES_SYSTEM_REQUIRED",
    ):
        with inhibitor:
            pass


def test_unsupported_platform_fails_explicitly() -> None:
    with pytest.raises(
        SleepInhibitionError,
        match="unsupported on 'linux'",
    ):
        create_system_sleep_inhibitor(platform="linux")
