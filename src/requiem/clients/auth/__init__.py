"""ADO credential acquisition for requiem — bootstrap-once refresh-token chain.

See ADR-0028 for the design rationale. The short version:

* Vanilla :class:`azure.identity.AzureCliCredential` dies whenever the corp
  tenant fires a CAE revocation event (``Status_AccountUnusable``). That
  has been the single largest source of dogfood interruption in
  ADR-0025's delivery path.
* twig + polyphony both ported the same fix: own the refresh-token chain
  (read the user's RT once from a known cache, then exchange it directly
  with AAD over HTTPS on every refresh). This module is the Python port.
* Public surface is :class:`MsalRefreshCredential`, which satisfies the
  :class:`azure.identity.TokenCredential` shape so existing call sites
  (``AdoClient``, ``RealAdoPrToolkit``) accept it as a drop-in
  ``credential=`` kwarg.

The submodules are internal — only :class:`MsalRefreshCredential` and
the :class:`AdoAuthError` exception are part of the public API.
"""

from __future__ import annotations

from requiem.clients.auth.ado_token_provider import (
    AdoAuthError,
    AdoTokenProvider,
    MsalRefreshCredential,
)

__all__ = [
    "AdoAuthError",
    "AdoTokenProvider",
    "MsalRefreshCredential",
]
