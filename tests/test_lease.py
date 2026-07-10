from __future__ import annotations

import json
from pathlib import Path

import pytest

from requiem.lease import (
    FencedRootLease,
    LeaseBusyError,
    LeaseLostError,
    validate_lease_record,
)


def _lease(tmp_path: Path, *, holder: str) -> FencedRootLease:
    return FencedRootLease(
        lease_dir=tmp_path,
        repo="microsoft/CloudVault/cloudvault-service-api",
        root_item=42,
        ttl_seconds=5,
        heartbeat_seconds=1,
        holder=holder,
    )


def test_lease_is_exclusive_and_tokens_increase(tmp_path: Path) -> None:
    first = _lease(tmp_path, holder="first")
    with first:
        identity = first.identity
        validate_lease_record(
            identity.record_path,
            token=identity.token,
            holder=identity.holder,
        )
        second = _lease(tmp_path, holder="second")
        with pytest.raises(LeaseBusyError):
            second.acquire()

    third = _lease(tmp_path, holder="third")
    with third:
        assert third.identity.token == identity.token + 1


def test_lease_detects_fencing_record_tampering(tmp_path: Path) -> None:
    lease = _lease(tmp_path, holder="owner")
    lease.acquire()
    identity = lease.identity
    payload = json.loads(identity.record_path.read_text(encoding="utf-8"))
    payload["token"] = identity.token + 1
    identity.record_path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        with pytest.raises(LeaseLostError, match="no longer matches"):
            lease.assert_current()
    finally:
        lease.release()


def test_validate_rejects_released_lease(tmp_path: Path) -> None:
    lease = _lease(tmp_path, holder="owner")
    with lease:
        identity = lease.identity
    with pytest.raises(LeaseLostError, match="not active"):
        validate_lease_record(
            identity.record_path,
            token=identity.token,
            holder=identity.holder,
        )


def test_validate_binds_token_to_repo_and_root(tmp_path: Path) -> None:
    lease = _lease(tmp_path, holder="owner")
    with lease:
        identity = lease.identity
        with pytest.raises(LeaseLostError, match="repo"):
            validate_lease_record(
                identity.record_path,
                token=identity.token,
                holder=identity.holder,
                repo="microsoft/OtherProject/other-repo",
                root_item=42,
            )
        with pytest.raises(LeaseLostError, match="root item"):
            validate_lease_record(
                identity.record_path,
                token=identity.token,
                holder=identity.holder,
                repo=identity.repo,
                root_item=43,
            )
