from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from requiem.launcher import build_cleanup_command, run_launcher
from requiem.lease import LeaseIdentity


def _args(tmp_path: Path, command: list[str]) -> argparse.Namespace:
    return argparse.Namespace(
        item=42,
        ado_repo="microsoft/CloudVault/cloudvault-service-api",
        repo_path=tmp_path,
        scenario_cwd=None,
        lease_dir=tmp_path / "leases",
        remote="origin",
        log_dir=tmp_path / "runs",
        manifest=None,
        limit=1000,
        lease_ttl=30.0,
        lease_heartbeat=10.0,
        lease_timeout=0.0,
        command=command,
    )


class FakeLease:
    instances: list["FakeLease"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.identity = LeaseIdentity(
            lock_path=Path(kwargs["lease_dir"]) / "root.lock",
            record_path=Path(kwargs["lease_dir"]) / "root.json",
            repo=kwargs["repo"],
            root_item=kwargs["root_item"],
            token=7,
            holder="holder-7",
        )
        self.assertions = 0
        self.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def assert_current(self) -> None:
        self.assertions += 1


def test_cleanup_command_carries_apply_and_fencing_identity(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path, ["python", "run.py"])
    lease = FakeLease(
        lease_dir=args.lease_dir,
        repo=args.ado_repo,
        root_item=args.item,
    )
    command = build_cleanup_command(
        args,
        lease,
        manifest_path=tmp_path / "manifest.json",
    )
    assert "pre-run-cleanup" in command
    assert "--apply" in command
    assert command[command.index("--lease-token") + 1] == "7"
    assert command[command.index("--lease-holder") + 1] == "holder-7"
    assert command[command.index("--item") + 1] == "42"


def test_cleanup_failure_prevents_scenario_launch(tmp_path: Path) -> None:
    args = _args(tmp_path, ["--", "python", "run.py"])
    with (
        patch("requiem.launcher.FencedRootLease", FakeLease),
        patch(
            "requiem.launcher.subprocess.run",
            return_value=subprocess.CompletedProcess([], 3),
        ),
        patch("requiem.launcher.subprocess.Popen") as popen,
    ):
        assert run_launcher(args) == 3
    popen.assert_not_called()


def test_launcher_holds_lease_through_child_exit(tmp_path: Path) -> None:
    args = _args(tmp_path, ["--", "python", "run.py"])
    child = SimpleNamespace(returncode=0)
    poll_results = iter([None, 0])
    child.poll = lambda: next(poll_results)

    with (
        patch("requiem.launcher.FencedRootLease", FakeLease),
        patch(
            "requiem.launcher.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0),
        ),
        patch("requiem.launcher.subprocess.Popen", return_value=child) as popen,
        patch("requiem.launcher.time.sleep"),
    ):
        assert run_launcher(args) == 0

    env = popen.call_args.kwargs["env"]
    assert env["REQUIEM_LEASE_TOKEN"] == "7"
    assert env["REQUIEM_LEASE_ROOT_ITEM"] == "42"
    assert FakeLease.instances[-1].assertions >= 2


def test_launcher_separates_cleanup_repo_from_scenario_cwd(
    tmp_path: Path,
) -> None:
    cleanup_repo = tmp_path / "cleanup-repo"
    scenario_cwd = tmp_path / "scenario-workspace"
    cleanup_repo.mkdir()
    scenario_cwd.mkdir()
    args = _args(cleanup_repo, ["--", "python", "run.py"])
    args.scenario_cwd = scenario_cwd
    child = SimpleNamespace(returncode=0)
    child.poll = lambda: 0

    with (
        patch("requiem.launcher.FencedRootLease", FakeLease),
        patch(
            "requiem.launcher.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0),
        ) as cleanup,
        patch("requiem.launcher.subprocess.Popen", return_value=child) as popen,
    ):
        assert run_launcher(args) == 0

    assert cleanup.call_args.kwargs["cwd"] == cleanup_repo.resolve()
    assert popen.call_args.kwargs["cwd"] == scenario_cwd.resolve()


def test_launcher_defaults_scenario_cwd_to_callers_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cleanup_repo = tmp_path / "cleanup-repo"
    caller_cwd = tmp_path / "caller-workspace"
    cleanup_repo.mkdir()
    caller_cwd.mkdir()
    args = _args(cleanup_repo, ["--", "python", "run.py"])
    child = SimpleNamespace(returncode=0)
    child.poll = lambda: 0
    monkeypatch.chdir(caller_cwd)

    with (
        patch("requiem.launcher.FencedRootLease", FakeLease),
        patch(
            "requiem.launcher.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0),
        ),
        patch("requiem.launcher.subprocess.Popen", return_value=child) as popen,
    ):
        assert run_launcher(args) == 0

    assert popen.call_args.kwargs["cwd"] == caller_cwd.resolve()
