"""External launcher that fences cleanup and a Scenario process together."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence

from requiem.lease import FencedRootLease, LeaseError, LeaseLostError
from requiem.pre_run_cleanup import default_manifest_path


def _cleanup_program() -> list[str]:
    return [sys.executable, "-m", "requiem.cli"]


def build_cleanup_command(
    args: argparse.Namespace,
    lease: FencedRootLease,
    *,
    manifest_path: Path,
) -> list[str]:
    identity = lease.identity
    return [
        *_cleanup_program(),
        "pre-run-cleanup",
        "--item",
        str(args.item),
        "--ado-repo",
        args.ado_repo,
        "--repo-path",
        str(Path(args.repo_path).resolve()),
        "--remote",
        args.remote,
        "--log-dir",
        str(Path(args.log_dir).resolve()),
        "--manifest",
        str(manifest_path),
        "--limit",
        str(args.limit),
        "--apply",
        "--lease-record",
        str(identity.record_path),
        "--lease-token",
        str(identity.token),
        "--lease-holder",
        identity.holder,
    ]


def _normalise_command(command: Sequence[str]) -> list[str]:
    result = list(command)
    if result and result[0] == "--":
        result = result[1:]
    if not result:
        raise ValueError("a Scenario command is required after '--'")
    return result


def _terminate(child: subprocess.Popen[object]) -> None:
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=10)


def run_launcher(args: argparse.Namespace) -> int:
    command = _normalise_command(args.command)
    scenario_cwd = (
        Path(args.scenario_cwd).resolve()
        if args.scenario_cwd is not None
        else Path.cwd().resolve()
    )
    if not scenario_cwd.is_dir():
        raise ValueError(f"Scenario working directory does not exist: {scenario_cwd}")
    manifest_path = (
        Path(args.manifest).resolve()
        if args.manifest
        else default_manifest_path(Path(args.log_dir).resolve(), args.item).resolve()
    )
    lease = FencedRootLease(
        lease_dir=Path(args.lease_dir),
        repo=args.ado_repo,
        root_item=args.item,
        ttl_seconds=args.lease_ttl,
        heartbeat_seconds=args.lease_heartbeat,
        acquire_timeout_seconds=args.lease_timeout,
    )

    with lease:
        cleanup = subprocess.run(
            build_cleanup_command(args, lease, manifest_path=manifest_path),
            cwd=Path(args.repo_path).resolve(),
            check=False,
        )
        if cleanup.returncode != 0:
            return int(cleanup.returncode or 1)
        lease.assert_current()

        identity = lease.identity
        env = dict(os.environ)
        env.update({
            "REQUIEM_LEASE_RECORD": str(identity.record_path),
            "REQUIEM_LEASE_TOKEN": str(identity.token),
            "REQUIEM_LEASE_HOLDER": identity.holder,
            "REQUIEM_LEASE_REPO": identity.repo,
            "REQUIEM_LEASE_ROOT_ITEM": str(identity.root_item),
            "REQUIEM_CLEANUP_MANIFEST": str(manifest_path),
        })
        child = subprocess.Popen(
            command,
            cwd=scenario_cwd,
            env=env,
        )
        try:
            while child.poll() is None:
                time.sleep(0.25)
                lease.assert_current()
        except (KeyboardInterrupt, LeaseLostError):
            _terminate(child)
            if sys.exc_info()[0] is KeyboardInterrupt:
                return 130
            raise
        lease.assert_current()
        return int(child.returncode or 0)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="requiem-launch",
        description=(
            "Acquire a shared fenced root lease, apply pre-run cleanup, then "
            "hold the lease until the supplied Scenario process exits."
        ),
    )
    parser.add_argument("--item", type=int, required=True)
    parser.add_argument("--ado-repo", required=True)
    parser.add_argument("--repo-path", type=Path, required=True)
    parser.add_argument(
        "--scenario-cwd",
        type=Path,
        default=None,
        help=(
            "Working directory for the Scenario command. Defaults to the "
            "directory from which requiem-launch was invoked."
        ),
    )
    parser.add_argument(
        "--lease-dir",
        type=Path,
        required=True,
        help="Shared storage visible to every launcher for this repository.",
    )
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--log-dir", type=Path, default=Path(".runs"))
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--lease-ttl", type=float, default=30.0)
    parser.add_argument("--lease-heartbeat", type=float, default=10.0)
    parser.add_argument("--lease-timeout", type=float, default=0.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return run_launcher(args)
    except (LeaseError, ValueError) as error:
        print(f"requiem-launch: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
