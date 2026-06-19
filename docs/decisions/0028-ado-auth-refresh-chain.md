# ADR 0028 — ADO auth: own the refresh-token chain instead of trusting `az`

**Status:** Proposed (2026-06-19)
**Date:** 2026-06-19
**Relates to:**
ADR-0007 Q4 (chose `AzureCliCredential` for ADO auth — superseded here),
ADR-0024 (RepoPlatform + ADO support — uses the credential chain this ADR replaces),
ADR-0025 (dogfood delivery path — every `--commit` run has been blocked at
least once by the failure mode this ADR fixes).

## Context

Every dogfood attempt against live ADO eventually dies with the same error:

```
AzureCliCredential.get_token failed: Account has previously been signed
out of this application. Status: Response_Status.Status_AccountUnusable
```

This is **not** "the access token expired." `AzureCliCredential` would
silently refresh in that case. `Status_AccountUnusable` (`Tag: 540940121`)
means MSAL has marked the whole *account* as unusable — Microsoft's tenant
fires CAE (Continuous Access Evaluation) revocation events for: new IP /
Wi-Fi, password change, admin policy refresh, group membership change,
device compliance signal change. Corp tenant is aggressive about this.

Recovery today requires the human-in-the-loop:

```
az logout
az login --tenant 72f988bf-... --scope 499b84ac-.../.default
```

…then a browser flow. Dogfood runs that finally crawl through 30+ minutes
of planning + reviewer iteration get killed in the last second by a CAE
event that fires while requiem was running. This is **the single largest
source of dogfood interruption** — 5 distinct occurrences in session
history (2026-06-02, 06-05, 06-06×2, 06-19).

### What twig and polyphony already did

Both projects hit this exact problem and built the same solution:

- **twig:** `Twig.Infrastructure.Auth` — 12+ commits since April 2026 building
  a layered cache with direct AAD HTTP refresh. Closed it as "bug class
  structurally impossible" (twig issue #164).
- **polyphony:** PR #430 (`feat(ado): port twig's MSAL refresh-token chain for
  ADO auth`) — straight port to `Polyphony.Infrastructure.AzureDevOps.Auth`
  with `~/.polyphony/` instead of `~/.twig/`.

The C# implementation header literally says:
> "Methodology ported from twig's `AdoAccessTokenProvider`. Polyphony owns
> its own cache files under `~/.polyphony/`."

requiem has done none of this. Both `requiem.clients.azuredevops.AdoClient`
and `requiem.workflows.ado_pr.RealAdoPrToolkit` use vanilla
`AzureCliCredential` with no fallback — the entire dogfood pipeline
inherits az's full fragility.

### Bootstrap-source reality (verified on this host)

The twig design assumes `~/.azure/msal_token_cache.json` (plaintext JSON)
holds the refresh token to bootstrap from. **On Windows the reality is
different on two counts:**

1. The cache is **DPAPI-encrypted** at `~/.azure/msal_token_cache.bin`
   (not plaintext). Decryption requires `CryptUnprotectData` via ctypes
   (`crypt32.dll`) — no extra package needed.
2. After decryption the cache on this host has **`RefreshToken: {}` empty**
   — only `AccessToken`, `Account`, `IdToken`, `AppMetadata`. `az` on this
   machine is operating in access-token-only mode, which is exactly why
   CAE eviction is catastrophic (no RT to refresh from).
3. **Twig has a working RT at `~/.twig/.refresh-token`** (`source:
   "login-pkce"`, client_id `04b07795-...` = Azure CLI well-known) that
   it minted via its own loopback PKCE flow. This RT is the immediate
   bootstrap source available to requiem.

So requiem's bootstrap-source resolution must be:

1. **`~/.twig/.refresh-token`** (preferred — already exists, the same
   AAD/Microsoft Corp identity twig uses; same scope = ADO)
2. **`~/.polyphony/.refresh-token`** (if polyphony has been used on this
   host — same shape as twig's)
3. **`~/.azure/msal_token_cache.{bin,json}`** (legacy fallback, requires
   DPAPI decrypt on Windows; may have zero RTs)
4. **Fail with actionable guidance** (run `twig auth login` to mint an RT,
   then re-run requiem)

PAT path via `ADO_PAT` env var stays as a backstop for locked-down
runners — never the default.

## Decision

Port the twig/polyphony refresh-token chain to requiem. Stdlib-only (no
new runtime dependencies: `urllib`, `json`, `pathlib`, `ctypes` on
Windows). Live behind a `MsalRefreshCredential` class that satisfies
the `azure.identity.TokenCredential` shape (`get_token(*scopes,
**kwargs) -> AccessToken`) so existing call sites that pass
`credential=` keep working without code change.

### Architecture (mirrors twig 1:1, modulo Python idioms)

```
requiem/clients/auth/
    __init__.py                  # public API: MsalRefreshCredential
    jwt_inspector.py             # JWT decode, ADO audience validation (stdlib only)
    token_refresher.py           # POST to AAD token endpoint, capture rotated RT
    stores.py                    # RefreshTokenStore + TokenFileCache under ~/.requiem/
    msal_cache.py                # DPAPI decrypt (Win) / plaintext (other), RT extraction
    twig_bootstrap.py            # Read ~/.twig/.refresh-token as bootstrap source
    ado_token_provider.py        # 3-tier cache + invalid_grant re-bootstrap orchestrator
```

### Three-tier cache (same TTL constants as twig/polyphony)

1. **In-memory** (50-min TTL, pre-validated). Per-process.
2. **Cross-process file** at `~/.requiem/.token-cache` (audience-validated
   on read; `expiry-ticks\n<jwt>\n` two-line format). Atomic write
   (tmp + rename), chmod 600 on Unix.
3. **`~/.requiem/.refresh-token`** (twig-shape JSON: `refresh_token`,
   `client_id`, `tenant_id`, `authority_host`, `upn`, `oid`,
   `bootstrapped_at`, `source`). Bootstrapped **once** from twig /
   polyphony / MSAL — then we never read the upstream caches again.

### Refresh path

`MsalTokenRefresher.try_refresh(refresh_token, client_id, tenant_id,
authority_host, *, timeout=5s)` → POSTs `grant_type=refresh_token` with
`scope="499b84ac-.../.default offline_access"` (the `offline_access`
scope is what makes AAD return a rotated RT — without it our stored RT
slowly ages out).

Returns `(access_token | None, rotated_rt | None, is_invalid_grant: bool)`.
`is_invalid_grant=True` means the RT has been revoked → caller drops the
stored entry and tries one re-bootstrap from upstream.

### Audience guard

Every cached token is JWT-decoded and the `aud` claim must match either
`499b84ac-1321-427f-aa17-267ca6975798` (ADO resource ID) or
`https://app.vssps.visualstudio.com/` (URI form). Wrong-audience tokens
are wiped on read, never returned. This makes the "az minted a token for
the wrong scope" bug structurally impossible — the same guard that twig
PR #164 introduced.

### TokenCredential surface

```python
class MsalRefreshCredential:
    """Drop-in replacement for AzureCliCredential.

    Sync API matches azure.identity.TokenCredential.get_token signature
    so existing call sites (AdoClient, RealAdoPrToolkit) work
    unchanged. The async variant get_token_async() is exposed for
    callers that want to skip the thread-pool round-trip.
    """
    def get_token(self, *scopes: str, **kwargs) -> AccessToken: ...
    async def get_token_async(self, *scopes: str) -> AccessToken: ...
```

Internally the implementation only ever requests the ADO scope —
`scopes` argument is validated but ignored beyond that. Asking
`MsalRefreshCredential` for a Graph token raises immediately; this is
an ADO-scoped credential.

### Resolution order in `_resolve_default_credential()`

After this ADR ships, the default chain inside `ado_pr.py` becomes:

1. Explicit `credential=` kwarg (any `TokenCredential`-shaped object) — unchanged.
2. Explicit `pat=` kwarg — unchanged.
3. `ADO_PAT` env var — unchanged.
4. **`MsalRefreshCredential()`** — NEW. Bootstrap-and-refresh chain. Used
   by default for almost every caller.
5. `AzureCliCredential()` — kept as final fallback for environments
   without a twig/MSAL RT (e.g. fresh CI runners) where the user explicitly
   ran `az login`. The exact path that was the default before this ADR.

### Bootstrap-once semantics

The MSAL/twig/polyphony caches are **consulted exactly once** — when
`~/.requiem/.refresh-token` doesn't exist. After bootstrap, we
exclusively read our own store. If the stored RT eventually rolls into
`invalid_grant` (e.g. user explicitly logged out), we attempt **exactly
one** re-bootstrap from upstream and then propagate the failure with
actionable text:

```
Could not acquire an Azure DevOps access token.
Try one of:
  - twig auth login (refreshes ~/.twig/.refresh-token; requiem will re-bootstrap)
  - az login --scope 499b84ac-1321-427f-aa17-267ca6975798/.default
  - export ADO_PAT=<your_pat> (legacy fallback)
Then re-run requiem; no extra commands needed.
```

## Consequences

**Positive:**

- Dogfood runs survive CAE events that previously killed them.
- `requiem --commit` becomes safe to walk away from for ~hours instead of
  minutes (RT validity is hours-to-days; access token refresh is silent).
- The `~/.requiem/.refresh-token` is portable across machines if the user
  wants to copy it (same as twig).
- No new runtime dependencies. Tests can mock the HTTP refresher and the
  store independently.

**Negative:**

- One new code surface (~600 lines across 6 files) to maintain. Mitigated
  by the fact that twig/polyphony have lived with the equivalent for
  6+ months without incident, and the Python port preserves the same
  test seams.
- Windows DPAPI requires `ctypes` — slightly more delicate than reading
  a plaintext file. Mitigated by a clear `try/except` and falling through
  to twig-bootstrap (which is the actually-likely source on this host).
- The first request after a process start always pays a ~200-500ms
  refresh latency (was ~5-15s with `az` subprocess; net improvement).

**Out of scope (deferred):**

- The PR-lifecycle path (`ado_pr.py:complete_pr`) currently silently
  retries on auth failures. Not changing that here — it's a separate
  concern from credential acquisition.
- Service-principal / OIDC federation for CI. Will land separately if
  required for the eventual GitHub Actions / ADO Pipelines fleet (none
  exists today).
- The `kanban` backend worker pool would need its own copy of the
  refresh-token store. Not part of this ADR — Gap C in ADR-0025.

## STATUS log

- **2026-06-19 PROPOSED.** Plan written. Implementation order:
  1. `jwt_inspector` + `stores` + `token_refresher` (no I/O dependencies)
  2. `msal_cache` + `twig_bootstrap` (bootstrap sources)
  3. `ado_token_provider` orchestrator
  4. `MsalRefreshCredential` TokenCredential surface
  5. Wire into `_resolve_default_credential()`
  6. Hermetic tests
  7. Live smoke (verify bootstrap from twig + a fresh ADO call works)
