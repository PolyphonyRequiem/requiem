"""Shared file-backed fenced leases for external Requiem launchers."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, Callable


class LeaseError(RuntimeError):
    """Base error for lease acquisition, renewal, or validation."""


class LeaseBusyError(LeaseError):
    """Another launcher currently holds the root lease."""


class LeaseLostError(LeaseError):
    """The current holder can no longer prove ownership of its token."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise LeaseLostError("lease record is missing a timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise LeaseLostError(f"lease record has invalid timestamp {value!r}") from error
    if parsed.tzinfo is None:
        raise LeaseLostError("lease record timestamp must include a timezone")
    return parsed


def _read_record(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise LeaseLostError(f"lease record disappeared: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise LeaseLostError(f"lease record is unreadable: {path}: {error}") from error
    if not isinstance(payload, dict):
        raise LeaseLostError(f"lease record must contain a JSON object: {path}")
    return payload


def _write_record(
    path: Path,
    payload: dict[str, object],
    *,
    before_replace: Callable[[], None] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if before_replace is not None:
            before_replace()
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def validate_lease_record(
    path: Path,
    *,
    token: int,
    holder: str,
    repo: str | None = None,
    root_item: int | None = None,
) -> None:
    """Fail unless ``path`` proves the supplied active fencing identity."""
    payload = _read_record(Path(path))
    _validate_record_payload(
        payload,
        token=token,
        holder=holder,
        repo=repo,
        root_item=root_item,
    )


def _validate_record_payload(
    payload: dict[str, object],
    *,
    token: int,
    holder: str,
    repo: str | None = None,
    root_item: int | None = None,
) -> None:
    if payload.get("status") not in {"active", "renewing"}:
        raise LeaseLostError(
            f"lease is not active (status={payload.get('status')!r})"
        )
    if payload.get("token") != token or payload.get("holder") != holder:
        raise LeaseLostError(
            "lease fencing identity no longer matches the current record"
        )
    if repo is not None and payload.get("repo") != repo:
        raise LeaseLostError(
            f"lease repo {payload.get('repo')!r} does not match {repo!r}"
        )
    if root_item is not None and payload.get("root_item") != root_item:
        raise LeaseLostError(
            f"lease root item {payload.get('root_item')!r} "
            f"does not match {root_item!r}"
        )
    if _parse_time(payload.get("expires_at")) <= _now():
        raise LeaseLostError("lease record has expired")


def _try_lock(handle: BinaryIO) -> bool:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True, slots=True)
class LeaseIdentity:
    lock_path: Path
    record_path: Path
    repo: str
    root_item: int
    token: int
    holder: str

    def as_manifest(self) -> dict[str, object]:
        return {
            "lock": str(self.lock_path),
            "record": str(self.record_path),
            "repo": self.repo,
            "root_item": self.root_item,
            "token": self.token,
            "holder": self.holder,
        }


class FencedRootLease:
    """Exclusive ``(repo, root-item)`` lease with a renewable fencing token.

    ``lease_dir`` must be on storage shared by every launcher that can target
    the same repository. The OS byte-range lock provides exclusivity; the
    durable JSON record supplies the monotonically increasing fencing token
    consumed by cleanup subprocesses. The heartbeat exclusively validates and
    renews that record; callers inspect its durable result through
    ``assert_current`` without contending on record I/O.
    """

    def __init__(
        self,
        *,
        lease_dir: Path,
        repo: str,
        root_item: int,
        ttl_seconds: float = 120.0,
        heartbeat_seconds: float = 10.0,
        acquire_timeout_seconds: float = 0.0,
        holder: str | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if heartbeat_seconds <= 0 or heartbeat_seconds >= ttl_seconds:
            raise ValueError(
                "heartbeat_seconds must be positive and less than ttl_seconds"
            )
        if acquire_timeout_seconds < 0:
            raise ValueError("acquire_timeout_seconds cannot be negative")
        self.lease_dir = Path(lease_dir).resolve()
        self.repo = repo
        self.root_item = root_item
        self.ttl_seconds = ttl_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.acquire_timeout_seconds = acquire_timeout_seconds
        self.holder = holder or f"{os.getpid()}-{uuid.uuid4().hex}"

        digest = hashlib.sha256(
            f"{repo.casefold()}\0{root_item}".encode("utf-8")
        ).hexdigest()[:20]
        stem = f"root-{root_item}-{digest}"
        self.lock_path = self.lease_dir / f"{stem}.lock"
        self.record_path = self.lease_dir / f"{stem}.json"

        self._handle: BinaryIO | None = None
        self._identity: LeaseIdentity | None = None
        self._mutex = threading.Lock()
        self._state_mutex = threading.Lock()
        self._stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_error: BaseException | None = None
        self._renewal_deadline: float | None = None
        self._record_expires_at: datetime | None = None

    @property
    def identity(self) -> LeaseIdentity:
        if self._identity is None:
            raise LeaseError("lease has not been acquired")
        return self._identity

    def acquire(self) -> LeaseIdentity:
        if self._handle is not None:
            raise LeaseError("lease is already acquired")
        self.lease_dir.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()

        deadline = time.monotonic() + self.acquire_timeout_seconds
        while not _try_lock(handle):
            if time.monotonic() >= deadline:
                handle.close()
                raise LeaseBusyError(
                    f"root lease is already held for {self.repo} item {self.root_item}"
                )
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

        try:
            previous_token = 0
            if self.record_path.exists():
                previous = _read_record(self.record_path)
                token_value = previous.get("token", 0)
                if not isinstance(token_value, int) or token_value < 0:
                    raise LeaseError(
                        f"lease record has invalid fencing token: {token_value!r}"
                    )
                previous_token = token_value
            token = previous_token + 1
            identity = LeaseIdentity(
                lock_path=self.lock_path,
                record_path=self.record_path,
                repo=self.repo,
                root_item=self.root_item,
                token=token,
                holder=self.holder,
            )
            self._handle = handle
            self._identity = identity
            with self._state_mutex:
                self._heartbeat_error = None
                self._renewal_deadline = None
                self._record_expires_at = None
            acquired_at = _now()
            expires_at = self._write_active_record(acquired_at=acquired_at)
            self._record_successful_renewal(expires_at)
            return identity
        except BaseException:
            self._handle = None
            self._identity = None
            with self._state_mutex:
                self._renewal_deadline = None
                self._record_expires_at = None
            _unlock(handle)
            handle.close()
            raise

    def _write_active_record(
        self,
        *,
        acquired_at: datetime,
        renewed_at: datetime | None = None,
    ) -> datetime:
        identity = self.identity
        renewed_at = renewed_at or _now()
        expires_at = renewed_at + timedelta(seconds=self.ttl_seconds)
        payload: dict[str, object] = {
            "schema_version": 1,
            "status": "active",
            "repo": identity.repo,
            "root_item": identity.root_item,
            "token": identity.token,
            "holder": identity.holder,
            "renewed_at": renewed_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "acquired_at": acquired_at.isoformat(),
        }
        _write_record(
            identity.record_path,
            payload,
            before_replace=lambda: self._assert_renewal_window_open(expires_at),
        )
        return expires_at

    def _write_renewing_record(
        self,
        payload: dict[str, object],
        *,
        expires_at: datetime,
    ) -> None:
        renewing = dict(payload)
        renewing["status"] = "renewing"
        renewing["renewal_started_at"] = _now().isoformat()
        _write_record(
            self.identity.record_path,
            renewing,
            before_replace=lambda: self._assert_renewal_window_open(expires_at),
        )

    def _assert_renewal_window_open(self, expires_at: datetime) -> None:
        now = _now()
        monotonic_now = time.monotonic()
        with self._state_mutex:
            self._assert_renewal_window_open_locked(
                expires_at,
                now=now,
                monotonic_now=monotonic_now,
            )

    def _record_successful_renewal(self, expires_at: datetime) -> None:
        now = _now()
        monotonic_now = time.monotonic()
        with self._state_mutex:
            self._assert_renewal_window_open_locked(
                expires_at,
                now=now,
                monotonic_now=monotonic_now,
            )
            remaining_seconds = (expires_at - now).total_seconds()
            self._renewal_deadline = monotonic_now + remaining_seconds
            self._record_expires_at = expires_at

    def _expire_late_renewal(self, expires_at: datetime) -> None:
        identity = self.identity
        payload = _read_record(identity.record_path)
        if (
            payload.get("status") != "active"
            or payload.get("token") != identity.token
            or payload.get("holder") != identity.holder
            or payload.get("expires_at") != expires_at.isoformat()
        ):
            raise LeaseLostError(
                "late lease renewal no longer matches the current record"
            )
        payload["status"] = "expired"
        payload["expired_at"] = _now().isoformat()
        _write_record(identity.record_path, payload)

    def _assert_renewal_window_open_locked(
        self,
        expires_at: datetime,
        *,
        now: datetime,
        monotonic_now: float,
    ) -> None:
        if (
            self._renewal_deadline is not None
            and monotonic_now >= self._renewal_deadline
        ) or (
            self._record_expires_at is not None
            and now >= self._record_expires_at
        ):
            raise LeaseLostError(
                "lease renewal completed after the previous lease expired"
            )
        if expires_at <= now:
            raise LeaseLostError("lease record has expired")

    def start_heartbeat(self) -> None:
        if self._handle is None:
            raise LeaseError("lease must be acquired before heartbeat starts")
        if self._heartbeat_thread is not None:
            raise LeaseError("lease heartbeat is already running")
        self._stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"requiem-lease-{self.root_item}",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            try:
                with self._mutex:
                    payload = self._assert_record_current_locked()
                    previous_expires_at = _parse_time(payload.get("expires_at"))
                    self._write_renewing_record(
                        payload,
                        expires_at=previous_expires_at,
                    )
                    renewed_at = _now()
                    expires_at = renewed_at + timedelta(seconds=self.ttl_seconds)
                    self._write_active_record(
                        acquired_at=_parse_time(payload.get("acquired_at")),
                        renewed_at=renewed_at,
                    )
                    try:
                        self._record_successful_renewal(expires_at)
                    except LeaseLostError:
                        self._expire_late_renewal(expires_at)
                        raise
            except BaseException as error:
                with self._state_mutex:
                    self._heartbeat_error = error
                self._stop.set()
                return

    def assert_current(self) -> None:
        now = _now()
        monotonic_now = time.monotonic()
        with self._state_mutex:
            deadline = self._renewal_deadline
            expires_at = self._record_expires_at
            heartbeat_error = self._heartbeat_error
        if deadline is None or expires_at is None:
            raise LeaseLostError("lease is not held")
        if heartbeat_error is not None:
            raise LeaseLostError(
                f"lease renewal failed: {heartbeat_error}"
            ) from heartbeat_error
        if monotonic_now >= deadline or now >= expires_at:
            raise LeaseLostError("lease record has expired")

    def _assert_record_current_locked(self) -> dict[str, object]:
        if self._handle is None:
            raise LeaseLostError("lease is not held")
        identity = self.identity
        payload = _read_record(identity.record_path)
        _validate_record_payload(
            payload,
            token=identity.token,
            holder=identity.holder,
            repo=identity.repo,
            root_item=identity.root_item,
        )
        return payload

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._stop.set()
        thread = self._heartbeat_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.heartbeat_seconds * 2))
        identity = self.identity
        with self._mutex:
            try:
                payload = _read_record(identity.record_path)
                if (
                    payload.get("token") == identity.token
                    and payload.get("holder") == identity.holder
                ):
                    payload["status"] = "released"
                    payload["released_at"] = _now().isoformat()
                    _write_record(identity.record_path, payload)
            finally:
                _unlock(handle)
                handle.close()
                self._handle = None
                self._heartbeat_thread = None
                with self._state_mutex:
                    self._renewal_deadline = None
                    self._record_expires_at = None

    def __enter__(self) -> FencedRootLease:
        self.acquire()
        self.start_heartbeat()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
