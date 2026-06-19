"""JWT decode + ADO audience validation — stdlib only, no PyJWT dep.

Mirrors twig's ``JwtAccessTokenInspector.cs`` 1:1. Used by the cache layer
to reject any token whose ``aud`` claim doesn't match the Azure DevOps
resource id. Pure utility, no I/O, never throws — malformed input returns
``None``.

Why we don't use PyJWT: we never *validate* a signature here (the token
was already validated by AAD when it minted it for us). All we need is
decoding the payload to check ``aud`` and ``exp``. Adding PyJWT for
``base64.urlsafe_b64decode + json.loads`` would be silly.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone

# The Azure DevOps API resource ID (audience claim format). Stable across
# tenants; this is the canonical id every ADO call uses. Verified against
# twig's live ``twig auth status`` and the AzureCliCredential docs.
ADO_RESOURCE_ID = "499b84ac-1321-427f-aa17-267ca6975798"

# Some token issuers use the URI form for the audience claim. AAD has used
# both forms over the years; accept either as valid ADO audience.
ADO_RESOURCE_URI = "https://app.vssps.visualstudio.com/"


@dataclass(frozen=True)
class JwtTokenInfo:
    """Snapshot of an inspected JWT — the parsed payload plus computed fields."""

    audience: str | None
    app_id: str | None
    expires_at: datetime | None
    issued_at: datetime | None
    tenant_id: str | None
    user_principal_name: str | None
    object_id: str | None
    issuer: str | None

    @property
    def is_valid_ado_audience(self) -> bool:
        """True iff the audience claim matches the ADO API resource."""
        if not self.audience:
            return False
        aud_lower = self.audience.lower()
        return aud_lower == ADO_RESOURCE_ID.lower() or aud_lower == ADO_RESOURCE_URI.lower()

    def is_not_expired(self, now: datetime, buffer_seconds: int = 0) -> bool:
        """True iff the token has not expired (with a small buffer)."""
        if self.expires_at is None:
            return False
        # buffer is applied to ``now`` so the token must outlive (now + buffer).
        from datetime import timedelta
        return self.expires_at > now + timedelta(seconds=buffer_seconds)


def try_decode(token: str | None) -> JwtTokenInfo | None:
    """Attempt to decode the JWT payload. Returns ``None`` for any non-JWT
    input (PAT strings, opaque tokens, malformed JWTs, etc.) — never raises.
    """
    if not token or not token.strip():
        return None

    # Strip optional "Bearer " prefix; the credential surface never stores
    # it but defensive parsing makes this usable from diagnostics paths.
    if token.lower().startswith("bearer "):
        token = token[len("bearer "):].strip()

    # PAT / Basic auth values are not JWTs — bail early.
    if token.lower().startswith("basic "):
        return None

    # A JWT has exactly three Base64Url segments separated by '.'.
    parts = token.split(".")
    if len(parts) != 3:
        return None
    if not parts[0] or not parts[1] or not parts[2]:
        return None

    try:
        payload_bytes = _decode_base64url(parts[1])
    except (ValueError, TypeError):
        return None
    if payload_bytes is None:
        return None

    try:
        payload = json.loads(payload_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    if not isinstance(payload, dict):
        return None

    return JwtTokenInfo(
        audience=_str_or_none(payload.get("aud")),
        app_id=_str_or_none(payload.get("appid")),
        expires_at=_unix_to_dt(payload.get("exp")),
        issued_at=_unix_to_dt(payload.get("iat")),
        tenant_id=_str_or_none(payload.get("tid")),
        user_principal_name=_str_or_none(payload.get("upn")),
        object_id=_str_or_none(payload.get("oid")),
        issuer=_str_or_none(payload.get("iss")),
    )


def has_valid_ado_audience(token: str | None) -> bool:
    """Convenience: ``True`` iff the token is a JWT whose audience is the ADO API.

    Returns ``False`` for non-JWT tokens (PATs, malformed strings). Used as
    a filter on REST refresher and bootstrap sources — neither of which can
    return a PAT, so a non-JWT result there always indicates corruption.
    """
    info = try_decode(token)
    return info is not None and info.is_valid_ado_audience


def describe_for_diagnostics(info: JwtTokenInfo, now: datetime) -> str:
    """Privacy-safe one-line summary suitable for diagnostic output.

    Deliberately omits the token itself; only includes claim metadata.
    Used by ``requiem auth status`` (when that lands) and error messages
    that want to show *why* a cached token was rejected.
    """
    if info.audience is None:
        aud_label = "(none)"
    elif info.audience.lower() == ADO_RESOURCE_ID.lower():
        aud_label = f"{ADO_RESOURCE_ID} (ADO OK)"
    elif info.audience.lower() == ADO_RESOURCE_URI.lower():
        aud_label = f"{ADO_RESOURCE_URI} (ADO OK)"
    else:
        aud_label = f"{info.audience} (NOT ADO)"

    if info.expires_at is None:
        exp_label = "(unknown)"
    else:
        delta = info.expires_at - now
        exp_label = (
            f"{info.expires_at.strftime('%Y-%m-%dT%H:%M:%SZ')} "
            f"({_format_relative(delta)})"
        )

    return (
        f"audience: {aud_label}\n"
        f"expires:  {exp_label}\n"
        f"tenant:   {info.tenant_id or '(unknown)'}\n"
        f"upn:      {info.user_principal_name or '(none)'}\n"
        f"appid:    {info.app_id or '(unknown)'}"
    )


# ---- internal helpers --------------------------------------------------


def _decode_base64url(segment: str) -> bytes | None:
    """Decode a Base64Url segment (no padding, '-'/'_' instead of '+'/'/')."""
    padding = (4 - len(segment) % 4) % 4
    padded = segment + ("=" * padding)
    try:
        return base64.urlsafe_b64decode(padded)
    except (ValueError, TypeError):
        return None


def _str_or_none(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _unix_to_dt(value: object) -> datetime | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    return None


def _format_relative(delta) -> str:
    """Render a timedelta as 'expired Nm ago' / 'in Nh Mm' / 'in Ns'."""
    total = delta.total_seconds()
    if total < 0:
        return f"expired {_format_duration(-total)} ago"
    return f"in {_format_duration(total)}"


def _format_duration(seconds: float) -> str:
    """Render seconds as a short, human-friendly duration."""
    if seconds >= 86400:
        days = int(seconds // 86400)
        hours = int((seconds % 86400) // 3600)
        return f"{days}d {hours}h"
    if seconds >= 3600:
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        return f"{hours}h {mins}m"
    if seconds >= 60:
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{mins}m {secs}s"
    return f"{int(seconds)}s"
