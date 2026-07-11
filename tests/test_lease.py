from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from requiem.lease import (
    FencedRootLease,
    LeaseBusyError,
    LeaseLostError,
    validate_lease_record,
)


def _lease(
    tmp_path: Path,
    *,
    holder: str,
    heartbeat_seconds: float = 1,
) -> FencedRootLease:
    return FencedRootLease(
        lease_dir=tmp_path,
        repo="microsoft/CloudVault/cloudvault-service-api",
        root_item=42,
        ttl_seconds=5,
        heartbeat_seconds=heartbeat_seconds,
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


def test_public_validation_is_serialized_with_heartbeat_renewal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heartbeat_seconds = 0.05
    lease = _lease(
        tmp_path,
        holder="owner",
        heartbeat_seconds=heartbeat_seconds,
    )
    lease.acquire()
    identity = lease.identity
    original_read_text = Path.read_text
    initial_record = json.loads(original_read_text(identity.record_path))
    validation_open = threading.Event()
    release_validation = threading.Event()
    validation_errors: list[BaseException] = []

    def blocking_read_text(
        path: Path,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        if threading.current_thread().name == "launcher-validation":
            with path.open("r", encoding=encoding, errors=errors) as handle:
                contents = handle.read()
                validation_open.set()
                assert release_validation.wait(timeout=2)
                return contents
        return original_read_text(path, encoding=encoding, errors=errors)

    def validate_from_launcher() -> None:
        try:
            lease.assert_current()
        except BaseException as error:
            validation_errors.append(error)

    monkeypatch.setattr(Path, "read_text", blocking_read_text)
    validator = threading.Thread(
        target=validate_from_launcher,
        name="launcher-validation",
    )
    validator.start()
    try:
        assert validation_open.wait(timeout=1)
        lease.start_heartbeat()
        time.sleep(heartbeat_seconds * 3)

        during_validation = json.loads(original_read_text(identity.record_path))
        assert during_validation["renewed_at"] == initial_record["renewed_at"]

        release_validation.set()
        validator.join(timeout=1)
        assert not validator.is_alive()
        assert validation_errors == []

        time.sleep(heartbeat_seconds * 2)
        lease.assert_current()
        renewed_record = json.loads(original_read_text(identity.record_path))
        assert renewed_record["renewed_at"] != initial_record["renewed_at"]
    finally:
        release_validation.set()
        validator.join(timeout=1)
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
