from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

import requiem.lease as lease_module
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


def test_external_validation_retries_transient_permission_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _lease(tmp_path, holder="owner")
    lease.acquire()
    identity = lease.identity
    original_read_text = Path.read_text
    attempts = 0

    def transient_permission_error(
        path: Path,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        nonlocal attempts
        if path == identity.record_path:
            attempts += 1
            if attempts < 3:
                raise PermissionError(13, "Permission denied", str(path))
        return original_read_text(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", transient_permission_error)
    try:
        validate_lease_record(
            identity.record_path,
            token=identity.token,
            holder=identity.holder,
            repo=identity.repo,
            root_item=identity.root_item,
        )
    finally:
        monkeypatch.setattr(Path, "read_text", original_read_text)
        lease.release()

    assert attempts == 3


def test_external_validation_fails_after_persistent_permission_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _lease(tmp_path, holder="owner")
    lease.acquire()
    identity = lease.identity
    original_read_text = Path.read_text
    attempts = 0

    def persistent_permission_error(
        path: Path,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        nonlocal attempts
        if path == identity.record_path:
            attempts += 1
            raise PermissionError(13, "Permission denied", str(path))
        return original_read_text(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", persistent_permission_error)
    try:
        with pytest.raises(LeaseLostError, match="lease record is unreadable"):
            validate_lease_record(
                identity.record_path,
                token=identity.token,
                holder=identity.holder,
                repo=identity.repo,
                root_item=identity.root_item,
            )
    finally:
        monkeypatch.setattr(Path, "read_text", original_read_text)
        lease.release()

    assert attempts == 3


def test_lease_detects_fencing_record_tampering(tmp_path: Path) -> None:
    lease = _lease(tmp_path, holder="owner", heartbeat_seconds=0.05)
    lease.acquire()
    lease.start_heartbeat()
    identity = lease.identity
    payload = json.loads(identity.record_path.read_text(encoding="utf-8"))
    payload["token"] = identity.token + 1
    identity.record_path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        deadline = time.monotonic() + 1
        while True:
            try:
                lease.assert_current()
            except LeaseLostError as error:
                assert "no longer matches" in str(error)
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        lease.release()


def test_slow_public_validation_cannot_block_heartbeat_renewal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heartbeat_seconds = 0.05
    lease = FencedRootLease(
        lease_dir=tmp_path,
        repo="microsoft/CloudVault/cloudvault-service-api",
        root_item=42,
        ttl_seconds=0.2,
        holder="owner",
        heartbeat_seconds=heartbeat_seconds,
    )
    lease.acquire()
    identity = lease.identity
    original_read_text = Path.read_text
    public_disk_read = threading.Event()
    validation_errors: list[BaseException] = []

    def slow_read_text(
        path: Path,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        if threading.current_thread().name == "launcher-validation":
            public_disk_read.set()
            time.sleep(0.3)
        return original_read_text(path, encoding=encoding, errors=errors)

    def validate_from_launcher() -> None:
        try:
            lease.assert_current()
        except BaseException as error:
            validation_errors.append(error)

    monkeypatch.setattr(Path, "read_text", slow_read_text)
    lease.start_heartbeat()
    validator = threading.Thread(
        target=validate_from_launcher,
        name="launcher-validation",
    )
    validator.start()
    try:
        validator.join(timeout=1)
        assert not validator.is_alive()
        time.sleep(heartbeat_seconds * 3)
        lease.assert_current()
        assert validation_errors == []
        assert not public_disk_read.is_set()
    finally:
        validator.join(timeout=1)
        lease.release()


def test_blocked_heartbeat_expires_without_recovering_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = FencedRootLease(
        lease_dir=tmp_path,
        repo="microsoft/CloudVault/cloudvault-service-api",
        root_item=42,
        ttl_seconds=0.2,
        heartbeat_seconds=0.05,
        holder="owner",
    )
    lease.acquire()
    identity = lease.identity
    original_read_text = Path.read_text
    initial_record = json.loads(original_read_text(identity.record_path))
    heartbeat_read_started = threading.Event()
    release_heartbeat_read = threading.Event()
    validation_errors: list[BaseException] = []

    def blocking_read_text(
        path: Path,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        if threading.current_thread().name.startswith("requiem-lease-"):
            heartbeat_read_started.set()
            assert release_heartbeat_read.wait(timeout=2)
        return original_read_text(path, encoding=encoding, errors=errors)

    def validate_after_expiry() -> None:
        try:
            lease.assert_current()
        except BaseException as error:
            validation_errors.append(error)

    monkeypatch.setattr(Path, "read_text", blocking_read_text)
    lease.start_heartbeat()
    assert heartbeat_read_started.wait(timeout=1)
    time.sleep(0.2)
    validator = threading.Thread(target=validate_after_expiry)
    validator.start()
    validator.join(timeout=0.1)
    validation_was_prompt = not validator.is_alive()
    release_heartbeat_read.set()
    validator.join(timeout=1)
    lease.release()

    final_record = json.loads(original_read_text(identity.record_path))
    assert validation_was_prompt
    assert len(validation_errors) == 1
    assert "expired" in str(validation_errors[0])
    assert final_record["renewed_at"] == initial_record["renewed_at"]


def test_late_renewal_intent_cannot_publish_a_future_active_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = FencedRootLease(
        lease_dir=tmp_path,
        repo="microsoft/CloudVault/cloudvault-service-api",
        root_item=42,
        ttl_seconds=0.3,
        heartbeat_seconds=0.05,
        holder="owner",
    )
    lease.acquire()
    identity = lease.identity
    initial_record = json.loads(identity.record_path.read_text(encoding="utf-8"))
    original_replace = lease_module.os.replace
    intent_published = threading.Event()
    release_replace = threading.Event()
    delayed = False

    def delayed_replace(source: Path, destination: Path) -> None:
        nonlocal delayed
        if (
            not delayed
            and threading.current_thread().name.startswith("requiem-lease-")
        ):
            delayed = True
            original_replace(source, destination)
            intent_published.set()
            assert release_replace.wait(timeout=2)
            return
        original_replace(source, destination)

    monkeypatch.setattr(lease_module.os, "replace", delayed_replace)
    lease.start_heartbeat()
    assert intent_published.wait(timeout=1)

    renewing_record = json.loads(identity.record_path.read_text(encoding="utf-8"))
    assert renewing_record["status"] == "renewing"
    assert renewing_record["expires_at"] == initial_record["expires_at"]

    time.sleep(0.3)
    with pytest.raises(LeaseLostError, match="expired"):
        validate_lease_record(
            identity.record_path,
            token=identity.token,
            holder=identity.holder,
        )

    release_replace.set()
    assert lease._heartbeat_thread is not None
    lease._heartbeat_thread.join(timeout=1)
    try:
        with pytest.raises(LeaseLostError, match="renewal failed"):
            lease.assert_current()
    finally:
        lease.release()


def test_local_health_waits_for_active_renewal_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = FencedRootLease(
        lease_dir=tmp_path,
        repo="microsoft/CloudVault/cloudvault-service-api",
        root_item=42,
        ttl_seconds=0.3,
        heartbeat_seconds=0.05,
        holder="owner",
    )
    lease.acquire()
    identity = lease.identity
    original_replace = lease_module.os.replace
    active_replace_started = threading.Event()
    release_active_replace = threading.Event()
    heartbeat_replaces = 0

    def blocking_second_replace(source: Path, destination: Path) -> None:
        nonlocal heartbeat_replaces
        if threading.current_thread().name.startswith("requiem-lease-"):
            heartbeat_replaces += 1
            if heartbeat_replaces == 2:
                active_replace_started.set()
                assert release_active_replace.wait(timeout=2)
        original_replace(source, destination)

    monkeypatch.setattr(lease_module.os, "replace", blocking_second_replace)
    lease.start_heartbeat()
    assert active_replace_started.wait(timeout=1)
    time.sleep(0.3)

    renewing_record = json.loads(identity.record_path.read_text(encoding="utf-8"))
    assert renewing_record["status"] == "renewing"
    with pytest.raises(LeaseLostError, match="expired"):
        lease.assert_current()

    release_active_replace.set()
    assert lease._heartbeat_thread is not None
    lease._heartbeat_thread.join(timeout=1)
    late_record = json.loads(identity.record_path.read_text(encoding="utf-8"))
    assert late_record["status"] == "expired"
    try:
        with pytest.raises(LeaseLostError, match="renewal failed"):
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
