"""Hermetic tests for the ADO auth chain.

No network. No filesystem outside ``tmp_path``. No real twig/MSAL caches.
Every external dependency (HTTP refresher, bootstrap reader, clock) is
injected via the test-only kwargs the production classes expose.

Covers:

* JWT decoder: valid token, malformed input, audience matcher
* RefreshTokenStore / TokenFileCache: atomicity, round-trip, missing-file
* token_refresher: success, invalid_grant, transport-error, malformed JSON
* AdoTokenProvider 3-tier cache: tier-1 hit, tier-2 hit, tier-3 refresh,
  bootstrap-once, invalid_grant re-bootstrap, rotated-RT capture,
  audience guard rejecting wrong-aud tokens
* MsalRefreshCredential: scope validation, sync facade, drop-in shape
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from requiem.clients.auth.ado_token_provider import (
    AdoAuthError,
    AdoTokenProvider,
    EXPIRY_BUFFER,
    MsalRefreshCredential,
    TOKEN_TTL,
)
from requiem.clients.auth.jwt_inspector import (
    ADO_RESOURCE_ID,
    has_valid_ado_audience,
    try_decode,
)
from requiem.clients.auth.stores import (
    DEFAULT_REFRESH_TOKEN_PATH,
    DEFAULT_TOKEN_CACHE_PATH,
    RefreshTokenStore,
    RefreshTokenStoreEntry,
    TokenFileCache,
)
from requiem.clients.auth.token_refresher import (
    ADO_REFRESH_SCOPE,
    RefreshResult,
    try_refresh,
)


# ---- helpers -----------------------------------------------------------


def _make_jwt(audience: str, exp_seconds_from_now: int = 3600) -> str:
    """Synthesize a 3-segment JWT with the requested audience and expiry.

    Header and signature are placeholders — we never validate the
    signature, only decode the payload.
    """
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    payload = {
        "aud": audience,
        "exp": int(time.time()) + exp_seconds_from_now,
        "iat": int(time.time()),
        "tid": "72f988bf-86f1-41af-91ab-2d7cd011db47",
        "upn": "test@contoso.com",
        "appid": "test-app",
        "iss": "https://sts.windows.net/test/",
    }
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    signature = "ZmFrZS1zaWc"  # base64url of "fake-sig", same length-ish
    return f"{header}.{payload_b64}.{signature}"


def _ado_jwt(exp_seconds_from_now: int = 3600) -> str:
    return _make_jwt(ADO_RESOURCE_ID, exp_seconds_from_now)


def _entry(rt: str = "test-rt", **overrides) -> RefreshTokenStoreEntry:
    return RefreshTokenStoreEntry(
        refresh_token=rt,
        client_id="04b07795-8ddb-461a-bbee-02f9e1bf7b46",
        tenant_id="organizations",
        authority_host="login.microsoftonline.com",
        source="test",
        **overrides,
    )


# ---- jwt_inspector -----------------------------------------------------


class TestJwtInspector:
    def test_decode_valid_jwt(self):
        token = _ado_jwt()
        info = try_decode(token)
        assert info is not None
        assert info.audience == ADO_RESOURCE_ID
        assert info.is_valid_ado_audience
        assert info.tenant_id == "72f988bf-86f1-41af-91ab-2d7cd011db47"
        assert info.user_principal_name == "test@contoso.com"

    def test_decode_strips_bearer_prefix(self):
        token = "Bearer " + _ado_jwt()
        info = try_decode(token)
        assert info is not None
        assert info.is_valid_ado_audience

    def test_decode_rejects_pat(self):
        # PAT looks like an opaque base64-ish string, not 3 segments.
        assert try_decode("Basic dXNlcjpwYXNz") is None
        assert try_decode("abc123def") is None

    def test_decode_rejects_malformed(self):
        assert try_decode("") is None
        assert try_decode(None) is None
        assert try_decode("not.a.jwt.with.too.many.dots") is None
        assert try_decode("only.two") is None
        assert try_decode("..empty.segments..") is None

    def test_audience_check_rejects_graph_token(self):
        # Graph audience is the URI form of Microsoft Graph.
        token = _make_jwt("https://graph.microsoft.com")
        assert not has_valid_ado_audience(token)

    def test_audience_check_accepts_both_ado_forms(self):
        guid_form = _make_jwt(ADO_RESOURCE_ID)
        uri_form = _make_jwt("https://app.vssps.visualstudio.com/")
        assert has_valid_ado_audience(guid_form)
        assert has_valid_ado_audience(uri_form)

    def test_audience_check_returns_false_for_non_jwt(self):
        assert not has_valid_ado_audience(None)
        assert not has_valid_ado_audience("not-a-jwt")


# ---- stores ------------------------------------------------------------


class TestRefreshTokenStore:
    def test_round_trip(self, tmp_path: Path):
        path = tmp_path / ".refresh-token"
        store = RefreshTokenStore(path)
        assert not store.exists()
        assert store.try_read() is None

        entry = _entry()
        store.try_write(entry)
        assert store.exists()

        read = store.try_read()
        assert read is not None
        assert read.refresh_token == entry.refresh_token
        assert read.client_id == entry.client_id
        assert read.tenant_id == entry.tenant_id
        assert read.source == "test"

    def test_missing_required_field_returns_none(self, tmp_path: Path):
        path = tmp_path / ".refresh-token"
        path.write_text('{"client_id":"x","tenant_id":"y"}')  # no refresh_token
        assert RefreshTokenStore(path).try_read() is None

    def test_corrupt_json_returns_none(self, tmp_path: Path):
        path = tmp_path / ".refresh-token"
        path.write_text("not json at all {")
        assert RefreshTokenStore(path).try_read() is None

    def test_atomic_write_no_partial_file_visible(self, tmp_path: Path):
        path = tmp_path / ".refresh-token"
        store = RefreshTokenStore(path)
        store.try_write(_entry())
        # The .tmp file should not linger after the rename.
        assert not (tmp_path / ".refresh-token.tmp").exists()

    def test_delete(self, tmp_path: Path):
        path = tmp_path / ".refresh-token"
        store = RefreshTokenStore(path)
        store.try_write(_entry())
        assert store.exists()
        store.try_delete()
        assert not store.exists()
        # Idempotent — second delete is a no-op, not an error.
        store.try_delete()


class TestTokenFileCache:
    def test_round_trip(self, tmp_path: Path):
        path = tmp_path / ".token-cache"
        cache = TokenFileCache(path)
        token = _ado_jwt()
        expiry = datetime.now(tz=timezone.utc) + timedelta(hours=1)

        cache.try_write(token, expiry)
        read_token, read_expiry = cache.try_read()
        assert read_token == token
        assert read_expiry is not None
        assert abs((read_expiry - expiry).total_seconds()) < 1.0

    def test_missing_file_returns_none(self, tmp_path: Path):
        cache = TokenFileCache(tmp_path / ".token-cache")
        token, expiry = cache.try_read()
        assert token is None and expiry is None

    def test_malformed_returns_none(self, tmp_path: Path):
        path = tmp_path / ".token-cache"
        path.write_text("not-a-valid-isodate\nsome-token\n")
        token, expiry = TokenFileCache(path).try_read()
        assert token is None

    def test_naive_datetime_normalized_to_utc(self, tmp_path: Path):
        path = tmp_path / ".token-cache"
        cache = TokenFileCache(path)
        naive_expiry = datetime(2030, 1, 1, 0, 0, 0)  # no tzinfo
        cache.try_write("token", naive_expiry)
        _, read_expiry = cache.try_read()
        assert read_expiry is not None
        assert read_expiry.tzinfo is not None


# ---- token_refresher ---------------------------------------------------


def _fake_urlopen(body: str, status: int = 200):
    """Build a swap-in replacement for the urlopen call inside try_refresh."""
    def _impl(request, timeout):
        return (body, status)
    return _impl


class TestTokenRefresher:
    @pytest.mark.asyncio
    async def test_success_returns_access_token(self):
        body = json.dumps({"access_token": "fresh-jwt", "refresh_token": "rotated-rt"})
        result = await try_refresh(
            "old-rt", "client-id", "tenant-id",
            _urlopen_for_test=_fake_urlopen(body),
        )
        assert result.access_token == "fresh-jwt"
        assert result.rotated_refresh_token == "rotated-rt"
        assert not result.is_invalid_grant

    @pytest.mark.asyncio
    async def test_rotated_rt_can_be_omitted(self):
        # AAD may reuse the existing RT — caller should keep what they have.
        body = json.dumps({"access_token": "fresh-jwt"})
        result = await try_refresh(
            "old-rt", "client-id", "tenant-id",
            _urlopen_for_test=_fake_urlopen(body),
        )
        assert result.access_token == "fresh-jwt"
        assert result.rotated_refresh_token is None

    @pytest.mark.asyncio
    async def test_invalid_grant_returns_flag(self):
        body = json.dumps({"error": "invalid_grant", "error_description": "RT revoked"})
        result = await try_refresh(
            "dead-rt", "client-id", "tenant-id",
            _urlopen_for_test=_fake_urlopen(body, status=400),
        )
        assert result.access_token is None
        assert result.is_invalid_grant

    @pytest.mark.asyncio
    async def test_transport_error_falls_through(self):
        def _raise(req, timeout):
            raise OSError("network down")
        result = await try_refresh(
            "rt", "client-id", "tenant-id", _urlopen_for_test=_raise,
        )
        assert result.access_token is None
        assert not result.is_invalid_grant

    @pytest.mark.asyncio
    async def test_malformed_response_falls_through(self):
        result = await try_refresh(
            "rt", "client-id", "tenant-id",
            _urlopen_for_test=_fake_urlopen("not json {{"),
        )
        assert result.access_token is None
        assert not result.is_invalid_grant

    @pytest.mark.asyncio
    async def test_offline_access_in_scope(self):
        """Regression: the AAD response only includes a rotated RT when
        ``offline_access`` is in the requested scope. Verify we ask for it.
        """
        captured = {}

        def _capture(request, timeout):
            captured["body"] = request.data.decode("utf-8")
            return (json.dumps({"access_token": "ok"}), 200)

        await try_refresh("rt", "cid", "tid", _urlopen_for_test=_capture)
        assert "offline_access" in captured["body"]
        assert ADO_RESOURCE_ID in captured["body"]


# ---- AdoTokenProvider (the orchestrator) -------------------------------


class _FakeRefresher:
    """Test seam: scriptable refresher for AdoTokenProvider."""

    def __init__(self, results: list[RefreshResult] | None = None):
        self._results = list(results or [])
        self.calls: list[tuple] = []

    def __call__(self, rt, client_id, tenant_id, authority_host, *, timeout_seconds):
        self.calls.append((rt, client_id, tenant_id, authority_host))
        # Return a coroutine so the caller can ``await`` it.
        async def _coro():
            if self._results:
                return self._results.pop(0)
            return RefreshResult(None, None, is_invalid_grant=False)
        return _coro()


class _FakeBootstrap:
    """Scriptable bootstrap source — returns the queued entries in order."""

    def __init__(self, entries: list[RefreshTokenStoreEntry | None]):
        self._entries = list(entries)
        self.calls = 0

    def __call__(self) -> RefreshTokenStoreEntry | None:
        self.calls += 1
        if self._entries:
            return self._entries.pop(0)
        return None


@pytest.fixture
def temp_stores(tmp_path: Path) -> tuple[RefreshTokenStore, TokenFileCache]:
    return (
        RefreshTokenStore(tmp_path / ".refresh-token"),
        TokenFileCache(tmp_path / ".token-cache"),
    )


class TestAdoTokenProviderOrchestrator:
    @pytest.mark.asyncio
    async def test_tier3_first_call_bootstraps_and_refreshes(self, temp_stores):
        store, cache = temp_stores
        bootstrap = _FakeBootstrap([_entry()])
        refresher = _FakeRefresher([RefreshResult(_ado_jwt(), "rotated-rt", False)])

        provider = AdoTokenProvider(
            refresh_store=store, file_cache=cache,
            _bootstrap_for_test=bootstrap, _refresher_for_test=refresher,
        )
        token = await provider.get_access_token()

        assert token.token.startswith("eyJ")  # base64url JWT header
        assert bootstrap.calls == 1
        assert len(refresher.calls) == 1
        # Store now has the rotated RT.
        stored = store.try_read()
        assert stored is not None
        assert stored.refresh_token == "rotated-rt"
        # File cache now has the access token for cross-process sharing.
        cached_token, _ = cache.try_read()
        assert cached_token == token.token

    @pytest.mark.asyncio
    async def test_tier1_cache_hit_skips_io(self, temp_stores):
        store, cache = temp_stores
        bootstrap = _FakeBootstrap([_entry()])
        refresher = _FakeRefresher([RefreshResult(_ado_jwt(), None, False)])

        provider = AdoTokenProvider(
            refresh_store=store, file_cache=cache,
            _bootstrap_for_test=bootstrap, _refresher_for_test=refresher,
        )
        await provider.get_access_token()  # primes the in-memory cache
        await provider.get_access_token()  # should be free
        await provider.get_access_token()  # still free

        # Only one bootstrap, only one refresher hit.
        assert bootstrap.calls == 1
        assert len(refresher.calls) == 1

    @pytest.mark.asyncio
    async def test_tier2_file_cache_promotes_to_memory(self, temp_stores):
        store, cache = temp_stores
        # Prime the file cache directly — simulates another process having
        # minted a token recently.
        token = _ado_jwt()
        future = datetime.now(tz=timezone.utc) + timedelta(minutes=30)
        cache.try_write(token, future)

        bootstrap = _FakeBootstrap([])  # never called — file cache hits
        refresher = _FakeRefresher([])  # ditto

        provider = AdoTokenProvider(
            refresh_store=store, file_cache=cache,
            _bootstrap_for_test=bootstrap, _refresher_for_test=refresher,
        )
        result = await provider.get_access_token()
        assert result.token == token
        assert bootstrap.calls == 0
        assert len(refresher.calls) == 0

    @pytest.mark.asyncio
    async def test_file_cache_wrong_audience_is_wiped(self, temp_stores):
        store, cache = temp_stores
        graph_token = _make_jwt("https://graph.microsoft.com")
        future = datetime.now(tz=timezone.utc) + timedelta(minutes=30)
        cache.try_write(graph_token, future)

        # Bootstrap + refresh fall through and succeed.
        bootstrap = _FakeBootstrap([_entry()])
        refresher = _FakeRefresher([RefreshResult(_ado_jwt(), None, False)])

        provider = AdoTokenProvider(
            refresh_store=store, file_cache=cache,
            _bootstrap_for_test=bootstrap, _refresher_for_test=refresher,
        )
        result = await provider.get_access_token()
        assert result.token != graph_token  # the wrong-audience token was rejected
        # And the bad file-cache entry was wiped + replaced.
        cached, _ = cache.try_read()
        assert cached != graph_token

    @pytest.mark.asyncio
    async def test_invalid_grant_triggers_one_rebootstrap(self, temp_stores):
        store, cache = temp_stores
        # Seed an entry as if we'd bootstrapped previously.
        store.try_write(_entry(rt="dead-rt"))

        # First refresh fails with invalid_grant; second (post-rebootstrap) succeeds.
        bootstrap = _FakeBootstrap([_entry(rt="fresh-rt")])
        refresher = _FakeRefresher([
            RefreshResult(None, None, is_invalid_grant=True),
            RefreshResult(_ado_jwt(), "rotated-rt", False),
        ])

        provider = AdoTokenProvider(
            refresh_store=store, file_cache=cache,
            _bootstrap_for_test=bootstrap, _refresher_for_test=refresher,
        )
        result = await provider.get_access_token()
        assert result.token is not None
        # One re-bootstrap.
        assert bootstrap.calls == 1
        # Two refresher hits — one with the dead RT, one with the fresh one.
        assert len(refresher.calls) == 2
        assert refresher.calls[0][0] == "dead-rt"
        assert refresher.calls[1][0] == "fresh-rt"

    @pytest.mark.asyncio
    async def test_invalid_grant_on_fresh_bootstrap_does_not_loop(self, temp_stores):
        store, cache = temp_stores
        # No pre-existing entry → bootstrap fires first.
        # Bootstrap gives a fresh entry, but AAD rejects it as invalid_grant.
        # Provider must NOT re-bootstrap (would be a loop on a broken upstream).
        bootstrap = _FakeBootstrap([_entry()])
        refresher = _FakeRefresher([
            RefreshResult(None, None, is_invalid_grant=True),
        ])

        provider = AdoTokenProvider(
            refresh_store=store, file_cache=cache,
            _bootstrap_for_test=bootstrap, _refresher_for_test=refresher,
        )
        with pytest.raises(AdoAuthError):
            await provider.get_access_token()
        assert bootstrap.calls == 1
        assert len(refresher.calls) == 1

    @pytest.mark.asyncio
    async def test_rotated_rt_captured_to_store(self, temp_stores):
        store, cache = temp_stores
        store.try_write(_entry(rt="old-rt"))

        bootstrap = _FakeBootstrap([])  # never needed
        refresher = _FakeRefresher([RefreshResult(_ado_jwt(), "rotated-rt", False)])

        provider = AdoTokenProvider(
            refresh_store=store, file_cache=cache,
            _bootstrap_for_test=bootstrap, _refresher_for_test=refresher,
        )
        await provider.get_access_token()

        # The store now has the rotated RT for the next refresh.
        stored = store.try_read()
        assert stored is not None
        assert stored.refresh_token == "rotated-rt"

    @pytest.mark.asyncio
    async def test_no_bootstrap_source_raises_actionable_error(self, temp_stores):
        store, cache = temp_stores
        bootstrap = _FakeBootstrap([None])  # no upstream available
        refresher = _FakeRefresher([])

        provider = AdoTokenProvider(
            refresh_store=store, file_cache=cache,
            _bootstrap_for_test=bootstrap, _refresher_for_test=refresher,
        )
        with pytest.raises(AdoAuthError) as exc_info:
            await provider.get_access_token()
        msg = str(exc_info.value)
        assert "twig auth login" in msg
        assert "az login" in msg
        assert "ADO_PAT" in msg

    @pytest.mark.asyncio
    async def test_refresher_returns_wrong_audience_falls_through(self, temp_stores):
        store, cache = temp_stores
        bootstrap = _FakeBootstrap([_entry()])
        graph_token = _make_jwt("https://graph.microsoft.com")
        refresher = _FakeRefresher([RefreshResult(graph_token, None, False)])

        provider = AdoTokenProvider(
            refresh_store=store, file_cache=cache,
            _bootstrap_for_test=bootstrap, _refresher_for_test=refresher,
        )
        # The token came back from AAD but failed the audience guard.
        # That counts as a refresh failure (not invalid_grant) → error.
        with pytest.raises(AdoAuthError):
            await provider.get_access_token()

    @pytest.mark.asyncio
    async def test_invalidate_drops_cached_token(self, temp_stores):
        store, cache = temp_stores
        bootstrap = _FakeBootstrap([_entry()])
        refresher = _FakeRefresher([
            RefreshResult(_ado_jwt(), "rt1", False),
            RefreshResult(_ado_jwt(), "rt2", False),
        ])

        provider = AdoTokenProvider(
            refresh_store=store, file_cache=cache,
            _bootstrap_for_test=bootstrap, _refresher_for_test=refresher,
        )
        await provider.get_access_token()
        provider.invalidate()
        # File cache should be gone after invalidate.
        cached, _ = cache.try_read()
        assert cached is None
        # Next call refreshes again — refresher gets hit a second time.
        await provider.get_access_token()
        assert len(refresher.calls) == 2


# ---- MsalRefreshCredential ---------------------------------------------


class TestMsalRefreshCredential:
    def test_get_token_requires_scope(self):
        cred = MsalRefreshCredential(provider=_make_provider_with_token())
        with pytest.raises(AdoAuthError):
            cred.get_token()

    def test_get_token_rejects_graph_scope(self):
        cred = MsalRefreshCredential(provider=_make_provider_with_token())
        with pytest.raises(AdoAuthError):
            cred.get_token("https://graph.microsoft.com/.default")

    def test_get_token_accepts_ado_guid_form(self):
        cred = MsalRefreshCredential(provider=_make_provider_with_token())
        token = cred.get_token(f"{ADO_RESOURCE_ID}/.default")
        assert token.token

    def test_get_token_accepts_ado_uri_form(self):
        cred = MsalRefreshCredential(provider=_make_provider_with_token())
        token = cred.get_token("https://app.vssps.visualstudio.com/.default")
        assert token.token

    def test_get_token_returns_azure_identity_shape(self):
        cred = MsalRefreshCredential(provider=_make_provider_with_token())
        token = cred.get_token(f"{ADO_RESOURCE_ID}/.default")
        # azure.identity.AccessToken duck-types as (.token, .expires_on)
        assert isinstance(token.token, str)
        assert isinstance(token.expires_on, int)
        assert token.expires_on > int(time.time())  # not expired


def _make_provider_with_token() -> AdoTokenProvider:
    """Build a provider primed to return a single ADO-audience token."""
    tmp = Path(__import__("tempfile").mkdtemp())
    bootstrap = _FakeBootstrap([_entry()])
    refresher = _FakeRefresher([
        RefreshResult(_ado_jwt(), "rotated-rt", False),
        RefreshResult(_ado_jwt(), "rotated-rt-2", False),
        RefreshResult(_ado_jwt(), "rotated-rt-3", False),
        RefreshResult(_ado_jwt(), "rotated-rt-4", False),
    ])
    return AdoTokenProvider(
        refresh_store=RefreshTokenStore(tmp / ".refresh-token"),
        file_cache=TokenFileCache(tmp / ".token-cache"),
        _bootstrap_for_test=bootstrap,
        _refresher_for_test=refresher,
    )
