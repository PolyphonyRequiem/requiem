"""Bootstrap from twig's or polyphony's own refresh-token store.

The preferred bootstrap source for requiem in practice: both tools store
their RT under ``~/.<name>/.refresh-token`` in the exact JSON shape we
use, so we can lift the entry straight into requiem's store and start
exchanging with AAD on the next call.

This is what we hit when ``~/.azure/msal_token_cache.bin`` is either
DPAPI-locked, plaintext-but-empty (no RTs), or absent entirely — all
three of which are realities on developer machines that don't run
``az login`` regularly.

Why we don't *share* the upstream store directly: ADR-0028 invariant —
bootstrap-once. After requiem reads twig's RT, AAD will rotate it (and
twig also rotates *its* RT independently). Sharing the file would race
both tools onto the same line and one of us would lose the rotated RT.
Each tool owns its own copy after bootstrap.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from requiem.clients.auth.stores import RefreshTokenStoreEntry

# Standard locations twig and polyphony use. Verified on this host:
#   ~/.twig/.refresh-token  → source: "login-pkce", client_id Azure CLI
#   ~/.polyphony/.refresh-token  → same shape (port of twig)
DEFAULT_TWIG_REFRESH_PATH = Path.home() / ".twig" / ".refresh-token"
DEFAULT_POLYPHONY_REFRESH_PATH = Path.home() / ".polyphony" / ".refresh-token"


def try_read_external_refresh(path: Path) -> RefreshTokenStoreEntry | None:
    """Read a sibling tool's refresh-token JSON, returning an entry
    re-tagged with our source label. ``None`` on any failure.

    Tolerates legacy field-naming (twig writes ``upn`` and ``oid``; our
    canonical names are ``user_principal_name`` and ``object_id``,
    accepted by the entry parser).
    """
    try:
        if not path.exists():
            return None
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    rt = data.get("refresh_token")
    cid = data.get("client_id")
    tid = data.get("tenant_id")
    if not (isinstance(rt, str) and rt
            and isinstance(cid, str) and cid
            and isinstance(tid, str) and tid):
        return None

    # Stamp the source so ``requiem auth status`` (future) can tell us
    # where the bootstrap came from. The original ``source`` field
    # (e.g. "login-pkce") is preserved as a suffix for forensics.
    upstream_source = data.get("source") or "unknown"
    tool_name = _infer_tool_name(path)
    source = f"{tool_name}({upstream_source})"

    return RefreshTokenStoreEntry(
        refresh_token=rt,
        client_id=cid,
        tenant_id=tid,
        authority_host=(
            data.get("authority_host")
            or "login.microsoftonline.com"
        ),
        user_principal_name=(
            data.get("user_principal_name")
            or data.get("upn")
        ),
        object_id=(
            data.get("object_id")
            or data.get("oid")
        ),
        bootstrapped_at=datetime.now(tz=timezone.utc).isoformat(),
        source=source,
    )


def _infer_tool_name(path: Path) -> str:
    """``~/.twig/.refresh-token`` → ``twig``; same for polyphony."""
    parent = path.parent.name
    if parent.startswith("."):
        return parent[1:]
    return parent or "external"


def try_bootstrap_from_siblings() -> RefreshTokenStoreEntry | None:
    """Try the standard sibling-tool locations in priority order:
    twig first (most likely to be fresh on this host), then polyphony.
    """
    for path in (DEFAULT_TWIG_REFRESH_PATH, DEFAULT_POLYPHONY_REFRESH_PATH):
        entry = try_read_external_refresh(path)
        if entry is not None:
            return entry
    return None
