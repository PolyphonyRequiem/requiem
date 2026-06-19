"""ADO token provider — the orchestrator that ties the chain together.

Implements ADR-0028. Three-tier cache, JWT audience validation on every
read, bootstrap-once with one re-bootstrap on ``invalid_grant``.

Public surface:

* :class:`AdoAuthError` — raised when no path can produce a token.
* :class:`AdoTokenProvider` — async API (``get_access_token()``); the
  internals used by requiem's async transport code.
* :class:`MsalRefreshCredential` — synchronous ``get_token(*scopes)``
  matching :class:`azure.identity.TokenCredential` so existing
  ``credential=`` kwargs accept it as a drop-in.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from requiem.clients.auth import msal_cache, twig_bootstrap
from requiem.clients.auth.jwt_inspector import (
    ADO_RESOURCE_ID,
    has_valid_ado_audience,
    try_decode,
)
from requiem.clients.auth.stores import (
    RefreshTokenStore,
    RefreshTokenStoreEntry,
    TokenFileCache,
)
from requiem.clients.auth.token_refresher import (
    DEFAULT_REFRESH_TIMEOUT_SECONDS,
    RefreshResult,
    try_refresh,
)

# How long we trust an in-memory access token before re-checking the file
# cache / re-refreshing. Mirrors twig: 50 minutes. Matches the typical
# 1-hour AAD access token lifetime minus a safety buffer.
TOKEN_TTL = timedelta(minutes=50)

# How long before the token's actual expiry we treat it as "must refresh".
# Belt-and-braces vs clock skew between the AAD issuer and our local clock.
EXPIRY_BUFFER = timedelta(minutes=5)


class AdoAuthError(Exception):
    """Raised when no path in the credential chain can produce a token."""


@dataclass(frozen=True)
class _AccessToken:
    """Minimal ``azure.identity.AccessToken`` shape (``.token``, ``.expires_on``).

    ``expires_on`` is a Unix epoch *seconds* int — that's what callers
    that lifted this from ``azure-identity`` expect.
    """
    token: str
    expires_on: int


class AdoTokenProvider:
    """Async ADO access-token provider with 3-tier cache + AAD refresh.

    Construct once, share across the process. All operations are
    coroutine-safe (one in-flight refresh at a time via the lock).
    """

    def __init__(
        self,
        *,
        refresh_store: RefreshTokenStore | None = None,
        file_cache: TokenFileCache | None = None,
        refresh_timeout_seconds: float = DEFAULT_REFRESH_TIMEOUT_SECONDS,
        clock=None,  # () -> datetime, for tests
        # Test seams: override the bootstrap and refresh strategies.
        _bootstrap_for_test=None,
        _refresher_for_test=None,
    ) -> None:
        self._refresh_store = refresh_store or RefreshTokenStore()
        self._file_cache = file_cache or TokenFileCache()
        self._refresh_timeout = refresh_timeout_seconds
        self._clock = clock or (lambda: datetime.now(tz=timezone.utc))
        self._bootstrap = _bootstrap_for_test or _default_bootstrap
        self._refresher = _refresher_for_test or try_refresh

        # Process-wide state.
        self._lock = asyncio.Lock()
        self._cached_token: str | None = None
        self._cache_expiry: datetime | None = None

    async def get_access_token(self) -> _AccessToken:
        """Return a fresh ADO bearer token (audience-validated, not expired).

        Raises :class:`AdoAuthError` with actionable text if no path can
        produce one.
        """
        async with self._lock:
            now = self._clock()

            # Tier 1: in-memory cache — audience already validated when stored.
            if self._cached_token and self._cache_expiry and now < self._cache_expiry:
                return self._to_access_token(self._cached_token, self._cache_expiry)

            # Tier 2: cross-process file cache — re-validate audience before trusting.
            file_token, file_expiry = self._file_cache.try_read()
            if (
                file_token
                and file_expiry
                and now + EXPIRY_BUFFER < file_expiry
                and has_valid_ado_audience(file_token)
            ):
                self._cached_token = file_token
                self._cache_expiry = file_expiry
                return self._to_access_token(file_token, file_expiry)

            # File-cache hit but wrong audience or expired? Wipe it to
            # avoid poisoning other processes that read after us.
            if file_token:
                self._file_cache.try_delete()

            # Tier 3: refresh from our own RT store. Bootstrap-once if missing.
            minted = await self._mint_from_refresh_store(now)
            if minted is not None:
                return minted

            raise AdoAuthError(
                "Could not acquire an Azure DevOps access token. "
                "Try one of:\n"
                "  - twig auth login (refreshes ~/.twig/.refresh-token; "
                "requiem will bootstrap from it)\n"
                "  - az login --scope "
                f"{ADO_RESOURCE_ID}/.default\n"
                "  - export ADO_PAT=<your_pat>  (legacy fallback)\n"
                "Then re-run requiem; no extra commands needed."
            )

    def invalidate(self) -> None:
        """Drop the cached access token. Forces a refresh on the next call.

        The refresh-token store is *not* dropped — only the access token.
        Call this from auth-failure handlers when a 401 looks like a stale
        access token rather than a revoked grant.
        """
        self._cached_token = None
        self._cache_expiry = None
        self._file_cache.try_delete()

    # ---- internal -------------------------------------------------------

    async def _mint_from_refresh_store(self, now: datetime) -> _AccessToken | None:
        entry = self._refresh_store.try_read()
        had_stored_entry = entry is not None
        if entry is None:
            entry = self._bootstrap()
            if entry is None:
                return None
            self._refresh_store.try_write(entry)

        minted, is_invalid_grant = await self._refresh_and_store(entry, now)
        if minted is not None:
            return minted

        # Only re-bootstrap when a *previously stored* entry was rejected
        # by AAD. Plain transient failures must not silently re-bootstrap
        # (would mask real problems), and a failure on a fresh entry must
        # not loop (would mean the upstream source is also broken).
        if had_stored_entry and is_invalid_grant:
            rebooted = self._bootstrap()
            if rebooted is None:
                return None
            self._refresh_store.try_write(rebooted)
            retry_minted, _ = await self._refresh_and_store(rebooted, now)
            return retry_minted

        return None

    async def _refresh_and_store(
        self, entry: RefreshTokenStoreEntry, now: datetime,
    ) -> tuple[_AccessToken | None, bool]:
        result: RefreshResult = await self._refresher(
            entry.refresh_token,
            entry.client_id,
            entry.tenant_id,
            entry.authority_host,
            timeout_seconds=self._refresh_timeout,
        )

        if result.access_token is None:
            if result.is_invalid_grant:
                # RT was revoked — drop everything cached so the
                # re-bootstrap path doesn't reuse the dead RT.
                self._cached_token = None
                self._cache_expiry = None
                self._refresh_store.try_delete()
            return (None, result.is_invalid_grant)

        if not has_valid_ado_audience(result.access_token):
            # AAD gave us a token for the wrong audience (shouldn't
            # happen given our scope, but the audience guard is what
            # makes the wrong-audience bug class structurally impossible).
            return (None, False)

        # Capture rotated RT if AAD issued one. Critical for keeping the
        # 90-day sliding inactivity window alive — without writing this
        # back, our stored RT slowly ages out even for active users.
        if (
            result.rotated_refresh_token
            and result.rotated_refresh_token != entry.refresh_token
        ):
            updated = RefreshTokenStoreEntry(
                refresh_token=result.rotated_refresh_token,
                client_id=entry.client_id,
                tenant_id=entry.tenant_id,
                authority_host=entry.authority_host,
                user_principal_name=entry.user_principal_name,
                object_id=entry.object_id,
                bootstrapped_at=entry.bootstrapped_at,
                source=entry.source,
            )
            self._refresh_store.try_write(updated)

        # Use the earlier of our standard TTL or the JWT's exp - buffer.
        token_expiry = self._resolve_jwt_expiry(result.access_token, now)
        self._cached_token = result.access_token
        self._cache_expiry = (
            token_expiry - EXPIRY_BUFFER
            if token_expiry < now + TOKEN_TTL
            else now + TOKEN_TTL
        )
        self._file_cache.try_write(self._cached_token, self._cache_expiry)
        return (self._to_access_token(result.access_token, self._cache_expiry), False)

    def _resolve_jwt_expiry(self, token: str, now: datetime) -> datetime:
        info = try_decode(token)
        if info and info.expires_at:
            return info.expires_at
        return now + TOKEN_TTL

    @staticmethod
    def _to_access_token(token: str, expiry: datetime) -> _AccessToken:
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return _AccessToken(token=token, expires_on=int(expiry.timestamp()))


def _default_bootstrap() -> RefreshTokenStoreEntry | None:
    """Bootstrap-source resolution: twig → polyphony → MSAL cache."""
    sibling = twig_bootstrap.try_bootstrap_from_siblings()
    if sibling is not None:
        return sibling
    return msal_cache.try_bootstrap_from_msal()


# ---- azure.identity.TokenCredential drop-in ----------------------------


class MsalRefreshCredential:
    """Synchronous credential matching :class:`azure.identity.TokenCredential`.

    Use this as a drop-in for ``AzureCliCredential`` in any constructor
    that takes a ``credential=`` kwarg:

    .. code-block:: python

        from requiem.clients.auth import MsalRefreshCredential
        from requiem.clients.azuredevops import AdoClient
        client = AdoClient(credential=MsalRefreshCredential())

    The ``get_token(*scopes, **kwargs)`` signature matches the
    azure-identity Protocol exactly, so callers can hold this behind a
    ``TokenCredential`` type hint without importing ``azure-identity``
    at all.

    Internally this credential only ever serves ADO-scoped tokens
    (``499b84ac-1321-427f-aa17-267ca6975798/.default``). Asking for any
    other scope raises ``AdoAuthError`` — this is an ADO-specific
    credential, not a general AAD credential.
    """

    def __init__(self, provider: AdoTokenProvider | None = None) -> None:
        # Default to a shared module-singleton so multiple call sites
        # in one process share the in-memory cache.
        self._provider = provider or _shared_provider()
        # Synchronous get_token is called from sync code paths; we need
        # to drive the async provider from a private event loop or a
        # one-shot thread.
        self._sync_lock = threading.Lock()

    def get_token(self, *scopes: str, **kwargs) -> _AccessToken:
        """Synchronous facade. Matches azure-identity's TokenCredential."""
        if not scopes:
            raise AdoAuthError("get_token requires at least one scope")
        _validate_ado_scope(scopes)
        with self._sync_lock:
            return _run_sync(self._provider.get_access_token())

    async def get_token_async(self, *scopes: str) -> _AccessToken:
        """Async variant — preferred when the caller is already in an
        event loop (avoids the thread round-trip)."""
        if not scopes:
            raise AdoAuthError("get_token_async requires at least one scope")
        _validate_ado_scope(scopes)
        return await self._provider.get_access_token()

    def invalidate(self) -> None:
        """Drop the cached access token. Useful from a 401 retry handler."""
        self._provider.invalidate()

    # azure-identity exposes a no-op close() for symmetry with confidential
    # client credentials; we don't hold any sockets but match the shape.
    def close(self) -> None:  # noqa: D401
        """No-op; this credential holds no long-lived resources."""


def _validate_ado_scope(scopes: Sequence[str]) -> None:
    """Every requested scope must target the ADO API resource. Anything
    else is an out-of-scope ask (Graph, ARM, etc.) and should fail
    loudly rather than mint an ADO token under a wrong contract.
    """
    for scope in scopes:
        if not isinstance(scope, str):
            raise AdoAuthError(f"non-string scope: {scope!r}")
        # Accept either the GUID form, the URI form, or the canonical
        # ``<resource>/.default`` form for either.
        normalized = scope.lower().rstrip("/")
        if normalized.endswith("/.default"):
            normalized = normalized[: -len("/.default")]
        if normalized in (
            ADO_RESOURCE_ID.lower(),
            "https://app.vssps.visualstudio.com",
        ):
            continue
        raise AdoAuthError(
            f"MsalRefreshCredential only mints ADO-scoped tokens; got {scope!r}"
        )


# ---- sync/async bridge -------------------------------------------------


def _shared_provider() -> AdoTokenProvider:
    """Lazy module-singleton so all credentials in one process share cache."""
    global _SHARED
    if _SHARED is None:
        _SHARED = AdoTokenProvider()
    return _SHARED


_SHARED: Optional[AdoTokenProvider] = None


def _run_sync(coro):
    """Run an async coroutine to completion from sync code.

    If there's an existing running loop in *this* thread (rare, but
    happens if someone calls get_token from inside an async handler),
    we offload to a fresh thread to avoid reentrancy issues. Otherwise
    we use ``asyncio.run`` directly for simplicity.
    """
    try:
        loop = asyncio.get_event_loop_policy().get_event_loop()
        is_running = loop.is_running()
    except RuntimeError:
        is_running = False

    if not is_running:
        return asyncio.run(coro)

    # Existing running loop — punt to a worker thread with its own loop.
    box: list = []
    err: list = []

    def _runner():
        try:
            box.append(asyncio.run(coro))
        except BaseException as e:  # noqa: BLE001 — propagate via err
            err.append(e)

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join()
    if err:
        raise err[0]
    return box[0]
