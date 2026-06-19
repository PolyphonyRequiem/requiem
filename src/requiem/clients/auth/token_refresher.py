"""Direct HTTP refresh-token grant against the AAD v2.0 token endpoint.

Mirrors twig's ``MsalTokenRefresher.cs``. Replaces the
``az account get-access-token`` subprocess (5-15s cold start, sometimes
just fails with ``Status_AccountUnusable``) with a direct HTTP exchange
(200-500ms, plain JSON over TLS).

We deliberately don't use ``msal-python`` here: MSAL caches state, has
its own broker negotiation, and would bring us back to the same
"trust the library's lifecycle decisions" hazard surface we're trying
to escape. Direct HTTP is ~80 lines, has zero state, and lets us own
the rotated-RT capture explicitly.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from requiem.clients.auth.jwt_inspector import ADO_RESOURCE_ID

# Default total timeout for a single refresh attempt. Refresh exchanges
# typically take 200-500ms on a healthy connection; anything > 5s means
# the AAD endpoint is degraded and we should fall through rather than
# block requiem's main loop on it.
DEFAULT_REFRESH_TIMEOUT_SECONDS = 5

# Scope we always request:
#   - ``offline_access`` is *required* — without it, AAD may omit the
#     ``refresh_token`` field from the response even on a refresh-token
#     grant. Our stored RT would then slowly age out even for active
#     users. Same gotcha twig hit in commit 33995bf9.
#   - The ADO API resource id, ``/.default`` form. Same audience
#     ``AzureCliCredential`` would request.
ADO_REFRESH_SCOPE = f"{ADO_RESOURCE_ID}/.default offline_access"


@dataclass(frozen=True)
class RefreshResult:
    """Discriminated result of a single refresh attempt.

    Three useful states:

    * Success: ``access_token`` is set; ``rotated_refresh_token`` may be
      set or ``None`` (server reused the existing RT — keep what we have).
    * Invalid grant: ``is_invalid_grant=True`` — the stored RT has been
      revoked; caller should drop it and try one re-bootstrap.
    * Soft failure: every field falsy — network error, server-side 5xx,
      DNS hiccup, etc. Caller can try a different bootstrap source or
      fall through to the next credential in the chain.
    """

    access_token: str | None
    rotated_refresh_token: str | None
    is_invalid_grant: bool


async def try_refresh(
    refresh_token: str,
    client_id: str,
    tenant_id: str,
    authority_host: str = "login.microsoftonline.com",
    *,
    timeout_seconds: float = DEFAULT_REFRESH_TIMEOUT_SECONDS,
    _urlopen_for_test=None,
) -> RefreshResult:
    """Exchange a refresh token for a fresh ADO-scoped access token.

    Never raises (except ``asyncio.CancelledError`` from a parent task).
    Network errors, JSON parse failures, malformed responses — all map to
    a soft-failure ``RefreshResult`` so the caller can fall through.

    The HTTP I/O runs on a worker thread (``asyncio.to_thread``) so we
    don't block the event loop on TLS handshake / connect latency.

    ``_urlopen_for_test`` is a backdoor for hermetic tests; production
    code MUST NOT pass it.
    """
    token_endpoint = (
        f"https://{authority_host}/{urllib.parse.quote(tenant_id, safe='')}"
        "/oauth2/v2.0/token"
    )
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": ADO_REFRESH_SCOPE,
    }).encode("utf-8")

    request = urllib.request.Request(
        token_endpoint,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )

    urlopen = _urlopen_for_test if _urlopen_for_test is not None else _urlopen_sync

    try:
        response_body, http_status = await asyncio.to_thread(
            urlopen, request, timeout_seconds,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        # Any low-level transport error — fall through.
        return RefreshResult(None, None, is_invalid_grant=False)

    return _parse_token_response(response_body, http_status)


def _urlopen_sync(request: urllib.request.Request, timeout: float) -> tuple[str, int]:
    """Issue the request and return (body, status). Returns the body even on
    non-2xx so the caller can parse ``error=invalid_grant`` from a 400.
    """
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace"), resp.status
    except urllib.error.HTTPError as e:
        # AAD returns the structured error body on 4xx — read it so we
        # can detect ``invalid_grant`` and trigger a re-bootstrap.
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return body, e.code


def _parse_token_response(body: str, status: int) -> RefreshResult:
    """Parse the AAD response body. Tolerates malformed JSON / missing fields."""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return RefreshResult(None, None, is_invalid_grant=False)

    if not isinstance(payload, dict):
        return RefreshResult(None, None, is_invalid_grant=False)

    access_token = payload.get("access_token")
    if isinstance(access_token, str) and access_token:
        rotated = payload.get("refresh_token")
        rotated_rt = rotated if isinstance(rotated, str) and rotated else None
        return RefreshResult(access_token, rotated_rt, is_invalid_grant=False)

    # No access token — check whether AAD told us the grant was revoked.
    error = payload.get("error")
    is_invalid_grant = (
        isinstance(error, str)
        and error.lower() == "invalid_grant"
    )
    return RefreshResult(None, None, is_invalid_grant=is_invalid_grant)
