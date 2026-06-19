"""Read the Azure CLI MSAL token cache as a bootstrap source.

On Windows the cache file is DPAPI-encrypted at
``~/.azure/msal_token_cache.bin``. Elsewhere it's plaintext JSON at
``~/.azure/msal_token_cache.json``. Both contain the same shape:

    {
      "RefreshToken": {<key>: {secret, client_id, home_account_id, environment}},
      "Account":      {<key>: {home_account_id, realm, environment}},
      "AccessToken":  {<key>: {secret, target, expires_on}},
      ...
    }

Note: a cache may have ``RefreshToken: {}`` and still be a valid
session-only artifact (the situation on Daniel's host today). In that
case this module returns ``None`` and the chain falls through to the
twig-bootstrap path.

DPAPI decryption uses ``ctypes`` against ``crypt32.dll`` — no extra
package (``pywin32`` not required). Pure stdlib.
"""

from __future__ import annotations

import ctypes
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from requiem.clients.auth.stores import RefreshTokenStoreEntry

DEFAULT_MSAL_BIN_PATH = Path.home() / ".azure" / "msal_token_cache.bin"
DEFAULT_MSAL_JSON_PATH = Path.home() / ".azure" / "msal_token_cache.json"


def try_bootstrap_from_msal(
    cache_path: Path | None = None,
) -> RefreshTokenStoreEntry | None:
    """Read the Azure CLI MSAL cache and lift a refresh token into a
    requiem-shaped entry. Returns ``None`` if:

    * Cache file doesn't exist
    * Cache is DPAPI-encrypted and decryption fails
    * Cache is corrupt / not JSON after decrypt
    * Cache has no refresh tokens (common on access-token-only setups)

    Caller (the orchestrator) is responsible for falling through to the
    next bootstrap source on ``None``.
    """
    if cache_path is not None:
        plaintext = _read_cache(cache_path)
        if plaintext is None:
            return None
    else:
        # Try the standard locations in order. On Windows we expect .bin;
        # on Linux/macOS we expect .json.
        plaintext = (
            _read_cache(DEFAULT_MSAL_BIN_PATH)
            or _read_cache(DEFAULT_MSAL_JSON_PATH)
        )
        if plaintext is None:
            return None

    try:
        cache = json.loads(plaintext)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(cache, dict):
        return None

    return _find_refresh_context(cache)


# ---- cache file reading ------------------------------------------------


def _read_cache(path: Path) -> str | None:
    """Read the cache and return decrypted UTF-8 text, or ``None``."""
    try:
        if not path.exists():
            return None
        raw = path.read_bytes()
    except OSError:
        return None

    if not raw:
        return None

    # Try plaintext JSON first (Linux/macOS, and older Windows az versions).
    if raw[:1] == b"{":
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

    # Otherwise assume DPAPI-encrypted blob. The DPAPI header is always
    # ``\x01\x00\x00\x00`` followed by the provider GUID — not JSON.
    if sys.platform != "win32":
        # Encrypted on a non-Windows host means we can't decrypt — fall through.
        return None

    try:
        decrypted = _dpapi_decrypt(raw)
        return decrypted.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _dpapi_decrypt(encrypted: bytes) -> bytes:
    """``CryptUnprotectData`` via ctypes. Raises ``OSError`` on failure."""
    from ctypes import wintypes, byref, c_void_p

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    in_blob = DATA_BLOB(
        len(encrypted),
        ctypes.cast(ctypes.c_char_p(encrypted), ctypes.POINTER(ctypes.c_ubyte)),
    )
    out_blob = DATA_BLOB()

    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        c_void_p, c_void_p, c_void_p, c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL

    ok = crypt32.CryptUnprotectData(
        byref(in_blob), None, None, None, None, 0, byref(out_blob),
    )
    if not ok:
        err = kernel32.GetLastError()
        raise OSError(f"CryptUnprotectData failed: 0x{err:08x}")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


# ---- MSAL cache parsing ------------------------------------------------


def _find_refresh_context(cache: dict) -> RefreshTokenStoreEntry | None:
    """Walk the MSAL cache to find a usable refresh token. Mirrors
    twig's ``MsalTokenRefresher.FindRefreshContext`` two-pass strategy:

    1. Prefer RTs that have a matching Account record (gives us tenant +
       authority host with full confidence).
    2. Fall back to parsing ``home_account_id`` (``{oid}.{tenantId}``)
       directly for RTs without a paired Account.
    """
    refresh_tokens = cache.get("RefreshToken")
    if not isinstance(refresh_tokens, dict) or not refresh_tokens:
        return None

    accounts = cache.get("Account") or {}
    # Build a lookup: home_account_id → (realm, environment).
    account_lookup: dict[str, tuple[str, str]] = {}
    if isinstance(accounts, dict):
        for acc in accounts.values():
            if not isinstance(acc, dict):
                continue
            hai = acc.get("home_account_id")
            realm = acc.get("realm")
            env = acc.get("environment")
            if isinstance(hai, str) and isinstance(realm, str) and isinstance(env, str):
                account_lookup[hai.lower()] = (realm, env)

    now = datetime.now(tz=timezone.utc).isoformat()

    # Pass 1: matched accounts.
    for rt in refresh_tokens.values():
        ctx = _try_rt_with_account(rt, account_lookup, now)
        if ctx is not None:
            return ctx

    # Pass 2: parse tenant from home_account_id.
    for rt in refresh_tokens.values():
        ctx = _try_rt_from_home_account(rt, now)
        if ctx is not None:
            return ctx

    return None


def _try_rt_with_account(
    rt: object,
    account_lookup: dict,
    now: str,
) -> RefreshTokenStoreEntry | None:
    if not isinstance(rt, dict):
        return None
    secret = rt.get("secret")
    client_id = rt.get("client_id")
    hai = rt.get("home_account_id")
    if not (isinstance(secret, str) and secret
            and isinstance(client_id, str) and client_id
            and isinstance(hai, str) and hai):
        return None

    account_info = account_lookup.get(hai.lower())
    if account_info is None:
        return None

    realm, env = account_info
    return RefreshTokenStoreEntry(
        refresh_token=secret,
        client_id=client_id,
        tenant_id=realm,
        authority_host=env,
        bootstrapped_at=now,
        source="azcli",
    )


def _try_rt_from_home_account(rt: object, now: str) -> RefreshTokenStoreEntry | None:
    if not isinstance(rt, dict):
        return None
    secret = rt.get("secret")
    client_id = rt.get("client_id")
    hai = rt.get("home_account_id")
    if not (isinstance(secret, str) and secret
            and isinstance(client_id, str) and client_id
            and isinstance(hai, str) and hai):
        return None

    dot = hai.find(".")
    if dot <= 0 or dot >= len(hai) - 1:
        return None

    tenant_id = hai[dot + 1:]
    env = rt.get("environment")
    authority_host = env if isinstance(env, str) and env else "login.microsoftonline.com"

    return RefreshTokenStoreEntry(
        refresh_token=secret,
        client_id=client_id,
        tenant_id=tenant_id,
        authority_host=authority_host,
        bootstrapped_at=now,
        source="azcli",
    )
