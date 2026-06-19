"""Persistent stores for requiem's ADO credential chain.

Two files live under ``~/.requiem/``:

* ``.refresh-token`` — JSON. The bootstrapped refresh-token entry plus the
  metadata needed to exchange it (``client_id``, ``tenant_id``,
  ``authority_host``). Written once at bootstrap; rewritten when AAD
  rotates the RT.
* ``.token-cache`` — two-line text. ``<expiry-utc-isoformat>\\n<jwt>\\n``.
  Cross-process cache so multiple ``requiem`` invocations can share an
  in-flight access token rather than each minting their own.

Both writers are atomic (tmp + rename), both files are chmod 600 on Unix.
Failures are silent (best-effort) — the in-memory cache + next refresh
attempt cover any persistence hiccup.

Mirrors twig's ``TwigRefreshTokenStore`` + ``TwigTokenFileCache`` 1:1,
modulo the JSON-vs-DataContract difference and Python's ``os.replace``
instead of ``File.Move(overwrite=true)``.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Where our files live. Override via the constructor in tests (the default
# is the only sane choice for real users).
DEFAULT_REQUIEM_DIR = Path.home() / ".requiem"
DEFAULT_REFRESH_TOKEN_PATH = DEFAULT_REQUIEM_DIR / ".refresh-token"
DEFAULT_TOKEN_CACHE_PATH = DEFAULT_REQUIEM_DIR / ".token-cache"


@dataclass
class RefreshTokenStoreEntry:
    """On-disk schema for ``~/.requiem/.refresh-token``.

    Owned exclusively by requiem — never read by ``az`` or any other
    process. Schema is intentionally identical to twig's
    ``TwigRefreshTokenStoreEntry`` so the format is portable: a user
    could ``cp ~/.twig/.refresh-token ~/.requiem/.refresh-token`` and
    requiem would pick it up without code change.
    """

    refresh_token: str
    client_id: str
    tenant_id: str
    authority_host: str = "login.microsoftonline.com"
    user_principal_name: str | None = None
    object_id: str | None = None
    bootstrapped_at: str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )
    source: str = "unknown"  # "twig" | "polyphony" | "azcli" | "explicit"

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "RefreshTokenStoreEntry | None":
        """Parse the on-disk JSON. Returns ``None`` for any unrecoverable
        corruption (missing required fields, malformed JSON, wrong types).
        """
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None

        # Required fields — bail if any are missing or non-string.
        rt = data.get("refresh_token")
        cid = data.get("client_id")
        tid = data.get("tenant_id")
        if not (isinstance(rt, str) and rt
                and isinstance(cid, str) and cid
                and isinstance(tid, str) and tid):
            return None

        return cls(
            refresh_token=rt,
            client_id=cid,
            tenant_id=tid,
            authority_host=_opt_str(data.get("authority_host")) or "login.microsoftonline.com",
            user_principal_name=_opt_str(data.get("user_principal_name") or data.get("upn")),
            object_id=_opt_str(data.get("object_id") or data.get("oid")),
            bootstrapped_at=_opt_str(data.get("bootstrapped_at")) or datetime.now(tz=timezone.utc).isoformat(),
            source=_opt_str(data.get("source")) or "unknown",
        )


class RefreshTokenStore:
    """Owns ``~/.requiem/.refresh-token``. All operations are best-effort."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or DEFAULT_REFRESH_TOKEN_PATH

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.exists()

    def try_read(self) -> RefreshTokenStoreEntry | None:
        """Read the stored entry. Returns ``None`` on any failure — never raises."""
        try:
            if not self._path.exists():
                return None
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return None
        return RefreshTokenStoreEntry.from_json(raw)

    def try_write(self, entry: RefreshTokenStoreEntry) -> None:
        """Atomic write (tmp + rename). chmod 600 on Unix. Silent on failure."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp_path.write_text(entry.to_json(), encoding="utf-8")
            os.replace(tmp_path, self._path)
            _maybe_chmod_600(self._path)
        except OSError:
            # Best effort — caller falls back to bootstrap-on-next-call.
            pass

    def try_delete(self) -> None:
        try:
            if self._path.exists():
                self._path.unlink()
        except OSError:
            pass


class TokenFileCache:
    """Cross-process cache for the most recent valid access token.

    Two-line format keeps it dead simple and lets a human inspect with
    ``head -c 20 ~/.requiem/.token-cache`` without revealing the token
    body. ISO-8601 timestamp is human-readable (twig used a .NET ticks
    integer — gain readability, lose a few bytes).
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or DEFAULT_TOKEN_CACHE_PATH

    @property
    def path(self) -> Path:
        return self._path

    def try_read(self) -> tuple[str | None, datetime | None]:
        """Read the cached token. Returns (None, None) on any failure."""
        try:
            if not self._path.exists():
                return (None, None)
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return (None, None)

        if len(lines) < 2:
            return (None, None)

        token = lines[1].strip()
        if not token:
            return (None, None)

        try:
            expiry = datetime.fromisoformat(lines[0].strip())
            # Normalize to UTC tz-aware so caller can compare against now().
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
        except ValueError:
            return (None, None)

        return (token, expiry)

    def try_write(self, token: str, expiry: datetime) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
            # Ensure tz-aware ISO format so try_read can round-trip cleanly.
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            content = f"{expiry.isoformat()}\n{token}\n"
            tmp_path.write_text(content, encoding="utf-8")
            os.replace(tmp_path, self._path)
            _maybe_chmod_600(self._path)
        except OSError:
            pass

    def try_delete(self) -> None:
        try:
            if self._path.exists():
                self._path.unlink()
        except OSError:
            pass


# ---- internal helpers --------------------------------------------------


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _maybe_chmod_600(path: Path) -> None:
    """On Unix, restrict the file to owner read/write. No-op on Windows
    (ACLs work differently; the user-profile directory is already
    ACL-protected by the OS)."""
    if sys.platform == "win32":
        return
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
