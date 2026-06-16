# ADR 0024 — RepoPlatform Protocol: GitHub + Azure DevOps parity for the trunk topology

**Status:** Accepted (2026-06-16)
**Date:** 2026-06-16 (drafted) / 2026-06-16 (accepted)
**Relates to:** ADR-0006 (merge-group topology), ADR-0007 (PR-lifecycle
architecture — §5.2 / §5.6 / §9.2 closure of Q3), ADR-0014 (Hermes fan-out
executor), ADR-0017 (Hermes delivery fleet), ADR-0018 (trunk integration on
the live path), ADR-0023 (ADO PR lifecycle).
**Supersedes:** the implicit "GitHub-only" assumption embedded in
`trunk_bootstrap.py`, `leaf_pr.py`, `feature_pr.py`, and
`end_to_end.run_pipeline`'s `github_repo` gate.

## Context

The ADR-0018 trunk topology is **the** v0 integration story: a per-run
`feature/<root>` trunk, requiem-opened `impl/<root>-<item>` → trunk leaf
PRs, a readiness-gated trunk → base PR. Three workflows implement it
(`trunk_bootstrap`, `leaf_pr`, `feature_pr`) and the `end_to_end.run_pipeline`
driver wires them in sequence. **All three are hard-coded to `toolbelt.gh`
and to GitHub REST behaviour.** A `github_repo: str | None` arg on the
driver gates the entire topology — when absent, the pipeline degrades to
"executor only, no topology."

This is a real problem because **the primary v0 customer scenario is
Azure DevOps**, not GitHub. Wave 7 META note #1 (`open-questions-wave7.md`):
> *"Requiem's #1 customer scenario is pure ADO. GitHub is supported, not
> the default."*

Concrete evidence the GitHub-only gate is the load-bearing dogfood
blocker: CloudVault's `cloudvault-service-api` and `cloudvault-client`
both live at `dev.azure.com/microsoft/CloudVault/_git/…`. Today
`requiem-end-to-end --item <cv-ado-id> --board requiem-<id> --live`
against either repo would skip the entire ADR-0018 topology (the
`github_repo` arg is absent), and `requiem-end-to-end --github-repo …`
is meaningless because the change has to land in an ADO Repo. Neither
mode runs the topology.

### Why the trunk topology is GitHub-only today

Three sources of coupling, all narrow:

1. **`trunk_bootstrap`, `leaf_pr`, `feature_pr` resolve their client as
   `ctx.toolbelt.gh`** and fail closed with `toolbelt.missing_client` if
   it is absent (`trunk_bootstrap.py:141-146`, `leaf_pr.py:165-170`,
   `feature_pr.py:216-220`). They duck-type a small subset of `GhClient`:
   `branch_sha`, `ensure_branch_ref`, `pr_search`, `pr_view`, `pr_create`,
   and (indirectly via `_resolve_base_branch` in `end_to_end.py`) `api`.
2. **The driver threads `github_repo: str | None`** and only constructs
   the trunk-topology inputs when it is set (`end_to_end.py:453`).
3. **`ado_pr.py` exists** as a parallel ADO PR lifecycle workflow
   (ADR-0023) — but it speaks `AdoPrToolkit` (a Protocol defined inside
   `ado_pr.py`), not the trunk-topology surface. Its method set is
   different (`pr_view`/`mergeability`/`complete_pr`) and it requires
   `ADO_PAT`, which the primary v0 ADO org does not issue
   (ADR-0007 Q4: *"OIDC required; PATs not supported"*).

So there are *two* live problems entangled here: (a) the trunk-topology
workflows assume GitHub by construction, and (b) the ADO PR lifecycle
workflow assumes PAT auth that the primary v0 org cannot supply.

### What ADR-0007 already said about this

ADR-0007 §5.2 closed in favour of:
> *"A single `pr_lifecycle.py` workflow parameterised by a `PrPlatform`
> Protocol. GitHub and ADO each get a concrete implementation living
> next to the existing `GhClient` / `TwigClient`."*

ADR-0007 §5.6 closed in favour of a new `azuredevops.py` REST client
(option (c)), explicitly rejecting (a) "extend `twig`" and (b) "shell
out to `az`."

ADR-0007 §9.2 (the open-questions sidecar that became Bruckner Wave 7 Q3)
**closed** with *"Option (i) — single `PrPlatform` Protocol selects the PR
host."* That closure remains binding for the **PR-lifecycle** workflow.

ADR-0023 then shipped `ado_pr.py` as a **sibling workflow** of
`pr_lifecycle.py` rather than as a `PrPlatform` impl behind it — i.e. the
Protocol abstraction was deferred for the PR-lifecycle surface. That
deferral was a defensible v0 expedient (ship the second platform without
a refactor first), but it does not bind this ADR's scope. The
trunk-topology surface (`trunk_bootstrap`/`leaf_pr`/`feature_pr`) was
authored *after* ADR-0023 and bakes in the same GitHub-only assumption,
which is now the v0 dogfood blocker.

### ADR-0007 Q4 closure that ADR-0023 didn't honour

ADR-0007 Q4 closed with: *"OIDC required. PATs are not supported in
Daniel's primary ADO org … `AdoClient` ships with `azure-identity` +
`DefaultAzureCredential` as the credential-discovery step. … Use
`azure-identity`'s `AzureCliCredential` as the v0 default."*

`ado_pr.py:140` reads `os.environ.get("ADO_PAT", "")`. This is a real
mismatch: the code as shipped cannot authenticate against the primary v0
ADO org. Live ADO dogfood is blocked on **both** the trunk-topology
GitHub assumption AND the `ado_pr.py` PAT assumption.

## The non-negotiables (from rubber-duck pass)

1. **One topology, two backends.** The ADR-0018 trunk shape (per-run
   `feature/<root>`, requiem-opened leaf PRs, readiness-gated trunk PR)
   is a workflow-level decision; it must NOT be re-described per
   platform. Two divergent topology workflows is the Polyphony mistake
   (`github-pr.yaml` vs `ado-pr.yaml` drifted; `ado-feature-pr-parity.md`
   exists to undo the drift). One workflow, two backends.
2. **The trunk-topology surface and the PR-lifecycle surface are
   different Protocols.** `trunk_bootstrap` / `leaf_pr` / `feature_pr`
   need refs + PR create/view/search. `pr_lifecycle` needs reviewer
   threads + iterations + complete. Fusing them into one Protocol
   over-couples; splitting them keeps each backend small.
3. **No PAT in the primary v0 path.** The ADO backend authenticates
   against AAD; the credential surface is a constructor seam so a CI/CD
   manifest can inject a different `TokenCredential` without code
   change. ADR-0007 Q4 is non-negotiable on this point.
4. **Fakes are faithful.** Every `RepoPlatform` impl ships a `Fake*`
   that the existing `tests/test_fake_surface_contract.py` validates —
   methods present on both, async-ness matched. This is the
   Tchaikovsky-class-regression discipline (audit §4.2).
5. **No silent fallback.** If the driver is told `--ado-repo` and the
   ADO backend cannot authenticate, the run fails closed at
   `build_engine` — never falls through to a creds-light executor-only
   path that silently skips the topology. INV-NO-CORRUPT-FORWARD applied
   to construction.

## Options

### A. Per-platform topology workflows (Polyphony shape)

Ship `trunk_bootstrap_ado.py` / `leaf_pr_ado.py` / `feature_pr_ado.py`
alongside the GitHub set. Driver picks the trio at construction time.

**Pros:** zero refactor; clean code paths per platform; the existing
GitHub suite stays untouched.

**Cons:** ~80% LOC duplication across six workflows (the topology logic
— idempotency, fail-closed conflict handling, leaf-id ↔ PR-number
mapping, dispatch ordering — is identical; only the four/five client
calls per workflow change). Drift between the two halves becomes
inevitable; Polyphony's own history demonstrates this. Three new test
files. **Verdict: REJECT** — this is the cost ADR-0007 §6 Option A
specifically rejected at the PR-lifecycle layer, applied one layer up.

### B. RepoPlatform Protocol, two impls (RECOMMENDED)

Extract a `RepoPlatform` Protocol from the method set the three
trunk-topology workflows actually use today on `GhClient`. Make
`GhClient` an impl (already is — only the type signature changes). Add
an `AdoClient` second impl wrapping the ADO REST refs + pull-requests
APIs, AAD-authenticated via `azure-identity`. Switch each workflow's
`_require_gh` helper to `_require_repo_platform` (resolves
`ctx.toolbelt.repo`, a new field; `toolbelt.gh` stays for callers that
want the concrete GitHub client).

**Pros:** the topology logic stays single-sourced. Adding a third
backend later (Gitea, Bitbucket) costs one new client, zero workflow
changes. ADR-0007 §5.2's "PrPlatform Protocol" pattern, applied
correctly to the layer that actually needs it (the trunk topology),
ahead of paying down ADR-0023's deferral on the lifecycle layer.

**Cons:** the refactor touches three workflows + the driver + tests
across the dependency cone. Real upfront cost (~400 LOC client + ~150
LOC refactor + ~50 LOC tests).

### C. Make `RepoPlatform` an abstract base class with default
implementations

ABC with concrete defaults for shared logic (idempotent reuse-or-create,
fail-closed conflict translation), specific overrides per backend.

**Pros:** even less duplication than (B).

**Cons:** Protocol vs ABC is a religious argument in Python; this
codebase has been consistent on Protocol (`PrToolkit`, `AdoPrToolkit`,
the workflow `_E` engine duck-types). Inverting that here for one
client surface introduces a discontinuity. **Verdict: REJECT** —
consistency with the existing Protocol-everywhere idiom is worth more
than the marginal duplication saving.

## Decision

Adopt **Option B**: extract `RepoPlatform` as a Protocol; ship `GhClient`
unchanged (it is already shape-compatible) and a new `AdoClient` second
impl; make the three trunk-topology workflows generic over the Protocol.

### The Protocol shape (grounded in current usage)

```python
class RepoPlatform(Protocol):
    """The narrow ref + PR surface the ADR-0018 trunk topology needs.

    Implementations MUST be safe to call concurrently from different
    workflow runs against the same repo (no shared mutable state).
    Errors are platform-typed exceptions; workflows translate to
    discriminated outcomes per Ravel L-1.
    """

    # Ref ops (trunk_bootstrap)
    async def branch_sha(self, repo: str, branch: str) -> str: ...
    async def ensure_branch_ref(
        self, repo: str, branch: str, source_sha: str
    ) -> bool: ...

    # PR ops (leaf_pr, feature_pr)
    async def pr_search(
        self, repo: str, query: str, limit: int = 30
    ) -> list[RepoPullRequest]: ...
    async def pr_view(self, repo: str, number: int) -> RepoPullRequest: ...
    async def pr_create(
        self, repo: str, *, title: str, body: str, head: str, base: str
    ) -> RepoPullRequest: ...

    # Repo metadata (end_to_end._resolve_base_branch)
    async def default_branch(self, repo: str) -> str: ...
```

Notes:

- **`RepoPullRequest`** is a new dataclass (effectively a rename of the
  existing `GhPullRequest`, with field semantics platform-neutral:
  `number`, `state` ∈ {open, closed, merged}, `merged_at`, `head`,
  `base`, `url`, `raw`). `GhPullRequest` becomes a type alias for
  backward compat; the workflows depend only on `RepoPullRequest`.
- **`default_branch`** is hoisted out of `end_to_end._resolve_base_branch`
  (which currently uses `gh.api("repos/{repo}")` directly). Making it a
  Protocol method moves the GH-specific URL shape into the impl,
  keeping the driver clean.
- **`api` is NOT in the Protocol.** The escape hatch was a GitHub-only
  affordance; making it Protocol-level invites every workflow to bypass
  the typed surface. If a workflow needs a new method, it's added here
  with a typed signature.
- **PR search query syntax** is the one place the abstraction leaks:
  GitHub's `gh pr search` query syntax differs from ADO's `searchCriteria`
  query string. Workflows currently pass a search like
  `"head:impl/<root>-<item> base:feature/<root>"`. The Protocol contract
  is *the impl translates from a small structured idiom into its native
  syntax* — the workflows pass a `head=…, base=…` kwargs pair, not a
  free-form string. (This is a small breaking change to the existing
  `pr_search` signature — addressed in §"Migration steps" below.)

### Two Protocols, not one

The PR-lifecycle workflow's `PrToolkit` (`pr_lifecycle.py:209-229`) and
`ado_pr.py`'s `AdoPrToolkit` (`ado_pr.py:98-110`) deal with a different
domain — reviewer threads, iteration polling, mergeability gates,
completion. These stay as separate Protocols, owned by their respective
workflows. `RepoPlatform` is only the topology surface.

The two Protocols may share implementations on the same `*Client` object
(it is reasonable for `AdoClient` to implement both `RepoPlatform` and
`AdoPrToolkit`), but the workflow types don't know that. This is the
cleanest split — each workflow depends on the narrowest surface it
actually needs.

### Auth model (closes ADR-0007 Q4 properly)

`AdoClient` constructor:

```python
class AdoClient:
    def __init__(
        self,
        *,
        organization: str,                       # "microsoft"
        credential: TokenCredential | None = None,  # azure-identity
        base_url: str = "https://dev.azure.com",
    ) -> None:
        self._cred = credential or AzureCliCredential()
        ...
```

- **Default credential:** `AzureCliCredential` — the user authenticates
  once via `az login` (or `twig auth login`, whose token cache uses the
  same Azure CLI public client and audience — see Daniel's verified
  state in the `microsoft-corp-integration` skill).
- **CI/CD-friendly extension point:** swap in
  `DefaultAzureCredential()` (managed identity, workload identity,
  device-code chain) or a `ChainedTokenCredential` mixing in a PAT for
  locked-down runners — strictly additive, no workflow change.
- **No `ADO_PAT` in code paths the primary v0 user exercises.** The PAT
  remains a possible credential the chain may discover, but never
  required.

### `ado_pr.py`'s `RealAdoPrToolkit` reuses the same credential plumbing

Step 1 of the migration (§"Migration steps" below) is to swap
`RealAdoPrToolkit`'s PAT-only auth for a `TokenCredential`-backed
bearer. This closes the existing ADR-0007 Q4 mismatch in `ado_pr.py`
*and* sets up the credential plumbing `AdoClient` reuses.

### Driver wiring

Replace `github_repo: str | None` on `end_to_end.run_pipeline` with two
new mutually-exclusive args:

```python
async def run_pipeline(
    ...,
    github_repo: str | None = None,         # "Owner/Repo"
    ado_repo: str | None = None,            # "<org>/<project>/<repo>"
    ...,
)
```

- Exactly one may be set (driver raises `ValueError` if both are).
- When set, the matching `RepoPlatform` impl is constructed and
  installed at `toolbelt.repo`; the trunk-topology workflows run.
- When neither is set, the legacy creds-light, executor-only path runs
  (unchanged — preserves ADR-0023's deliberate fallback for runs without
  a topology).
- `github_repo` continues to construct `GhClient` and remains the
  documented GitHub path. `ado_repo` constructs `AdoClient` with the
  default `AzureCliCredential`.

## Migration steps (incremental, each independently shippable)

1. **Auth fix on `ado_pr.RealAdoPrToolkit`** — switch the `__init__`
   from `pat: str | None = None` to
   `credential: TokenCredential | None = None`. Default to
   `AzureCliCredential()`. Keep a PAT fallback (read `ADO_PAT` from env
   only if no credential supplied AND the env is set) for backward
   compat with anyone who's wired it that way locally. Closes the
   ADR-0007 Q4 mismatch in the *existing* code without changing any
   workflow. **~50 LOC + 4 tests.**
   **STATUS: landed 2026-06-16** (`src/requiem/workflows/ado_pr.py`,
   `tests/test_ado_pr_workflow.py`, commit on `main`). Resolution order
   (first non-empty wins): explicit `credential=` → explicit `pat=` →
   `ADO_PAT` env → lazy `AzureCliCredential()`. Lazy default defers the
   `azure.identity` import until first `_auth_header()` call, so non-ADO
   users pay zero dep cost. Missing-package error names `requiem[ado]`
   extra. ADO REST resource id `499b84ac-1321-427f-aa17-267ca6975798`
   (verified against `twig auth status`'s live audience). 4 new tests
   cover the resolution order, the lazy default, the actionable
   missing-package message, and the bearer-vs-basic header semantics;
   10 pre-existing tests still green. Optional `[ado]` extra added to
   `pyproject.toml`.

2. **Extract `RepoPlatform` Protocol + `RepoPullRequest`** in
   `src/requiem/clients/repo.py`. Make `GhClient` an explicit impl
   (no behaviour change; only typing). Move `pr_search` to the
   `head=…, base=…` kwargs API; existing GitHub callers update in
   lockstep. **~100 LOC + protocol-shape tests.**
   **STATUS: landed 2026-06-16** (`src/requiem/clients/repo.py`,
   updated `src/requiem/clients/gh.py`, `tests/clients/test_repo_platform.py`).
   Concrete shape shipped:
   - New `RepoPullRequest` dataclass (`number/title/state/merged_at/
     head/base/url/raw`); `state` typed `Literal["open","closed","merged"]`
     normalised in `__post_init__` (legacy uppercase tolerated for
     back-compat); `.merged` is a derived property.
   - `GhPullRequest = RepoPullRequest` alias preserves every existing
     import path. A constructor shim silently drops the legacy
     `merged=` kwarg so the 20+ existing `GhPullRequest(state="OPEN",
     merged=False, ...)` call sites keep working without per-site edits.
   - Six-method `RepoPlatform` Protocol (`@runtime_checkable`):
     `branch_sha`, `ensure_branch_ref`, `find_open_pr_for_branch`,
     `pr_view`, `pr_create`, `default_branch`. Decision pivot from the
     ADR's original draft: instead of *renaming* `pr_search` to take
     `head=/base=` kwargs (breaking close_out's free-form
     `"in:body AB#<id>"` query), we keep `pr_search` as a GitHub-only
     method on `GhClient` AND add `find_open_pr_for_branch(repo, *,
     head, limit)` as the Protocol-surface method the trunk-topology
     workflows call. Same end state, zero breaking change to close_out.
   - `GhClient` grew two methods to satisfy the Protocol:
     `find_open_pr_for_branch` (thin wrapper around `pr_search`
     translating to `"head:<head> state:open"`); `default_branch`
     (reads `default_branch` field from `gh api repos/<repo>`,
     raises `GhUnknownError` per Ravel L-1 if missing — used by
     `end_to_end._resolve_base_branch`).
   - Tests: 11 new `test_repo_platform.py` covering Protocol shape +
     `RepoPullRequest` normalisation; 3 new `test_gh.py` for
     `find_open_pr_for_branch` + `default_branch` (happy + missing
     field). 345 tests across the affected surface still green
     including the previously-mismatched `test_close_out_workflow.py
     ::test_pr_not_merged_raises_needs_human_immediately` (now asserts
     `state=="open"` instead of `"OPEN"`).

3. **Build `AdoClient`** in `src/requiem/clients/azuredevops.py`
   implementing `RepoPlatform` against ADO REST. Refs API (PUT
   `_apis/git/repositories/{repo}/refs` with refUpdates body),
   pull-requests API (GET/POST), repository GET for `default_branch`.
   Ship `FakeAdoClient` mirroring `GhClient`'s `FakeGhClient` for tests.
   **~300 LOC client + faithful-fake + contract tests.**
   **STATUS: landed 2026-06-16** (`src/requiem/clients/azuredevops.py`,
   `tests/clients/test_azuredevops.py`). Concrete shape shipped:
   - `AdoClient` is a `RepoPlatform` impl over ADO REST (urllib +
     thread-based async — no httpx dep). Auth resolution order is the
     same as `RealAdoPrToolkit` (explicit `credential=` → `pat=` →
     `ADO_PAT` env → lazy `AzureCliCredential`); the shared resolver
     and `ADO_RESOURCE` constant are imported from `ado_pr` to avoid
     two sources of truth (consolidation into a dedicated
     `_AdoSession` helper is noted as a fast-follow).
   - `repo` is `"<org>/<project>/<repo>"` (matches `RealAdoPrToolkit`'s
     shape, so operators see one ADO identifier across the toolset).
   - Typed errors mirror `GhClient`'s taxonomy: `AdoRateLimitedError`,
     `AdoNotFoundError`, `AdoAuthError`, `AdoServerError`,
     `AdoUnknownError` (all inheriting `AdoClientError`). Verbs map
     them to discriminated outcomes per Ravel L-1.
   - Two ADO-specific behaviours documented + tested:
     **(a)** `branch_sha` uses the `?filter=heads/<branch>` form (ADO
     returns `{value: []}` for missing branches rather than 404; the
     client normalises this to `AdoNotFoundError` so callers don't
     need to know the API quirk);
     **(b)** `ensure_branch_ref` uses the nullSha precondition
     (`oldObjectId == "000…0"`) — the ADO equivalent of "create only
     if absent", which honours ADR-0018's never-force-move
     invariant. The fallback path (POST says `success: false`) probes
     `branch_sha` to confirm the ref is present and returns `False`
     idempotently; if the probe also says missing, raises
     `AdoUnknownError` (Ravel L-1).
   - `refs/heads/` prefix translation is the one leaky boundary. The
     Protocol contract is bare branch names; the impl prefixes on the
     wire (in PR-create body, in search criteria) and strips on the
     way back (in `_to_repo_pr`, in `default_branch`). Tests pin
     both directions.
   - `_to_repo_pr` translates ADO's `status` field to the neutral
     `state` vocab: `active → open`, `abandoned → closed`,
     `completed → merged`. `closedDate` becomes `merged_at` iff the
     PR actually merged.
   - `FakeAdoClient` is the in-memory faithful fake — same constructor
     shape (`refs`, `open_prs`, `default_branches`, failure-injection
     hooks). `test_fake_surface_contract.py` automatically discovers
     it and asserts async-shape parity with `AdoClient`.
   - 30 new tests in `tests/clients/test_azuredevops.py`: Protocol
     shape (3), repo parsing (3), 12 mocked-transport tests for the
     wire protocol (covering refs, PR ops, default branch — including
     the not-found / unknown-error paths), 7 `FakeAdoClient` behaviour
     tests, 5 parametrised `_ado_status_to_neutral` cases.
   - 378 tests across the affected surface still green — no
     regressions in the trunk-topology workflows or the existing
     `RealAdoPrToolkit` PAT path.

4. **Make trunk-topology workflows take `RepoPlatform`** — change each
   `_require_gh(ctx)` to `_require_repo_platform(ctx)`; rename the
   `toolbelt.gh` usage to `toolbelt.repo`. `Toolbelt` grows a new field
   `repo: RepoPlatform | None`; existing `gh: GhClient | None` field
   remains for callers that want GitHub-specific behaviour. **~50 LOC
   per workflow + a few new tests per workflow.**
   **STATUS: landed 2026-06-16** (`src/requiem/toolbelt.py`,
   `src/requiem/workflows/{trunk_bootstrap,leaf_pr,feature_pr}.py`,
   `tests/test_trunk_topology_against_ado.py`). Concrete shape shipped:
   - `Toolbelt` grew `repo: RepoPlatform | None = None`; `Toolbelt.real()`
     constructs one `GhClient` and assigns it to BOTH `gh` and `repo`
     (GhClient IS a RepoPlatform — single instance, two type-narrowed
     references).
   - Each of the three trunk-topology workflows
     (`trunk_bootstrap`, `leaf_pr`, `feature_pr`) renamed its
     `_require_gh(ctx)` helper to `_require_repo_platform(ctx)`. New
     resolution order: `ctx.toolbelt.repo or ctx.toolbelt.gh`. **Back-compat
     property:** wiring that only sets `toolbelt.gh` still works — proven
     by a dedicated test (`test_back_compat_gh_only_still_works`).
   - All `await gh.X(...)` callsites in the three workflows refit to
     `await repo_client.X(...)`. The single API change: `pr_search(repo,
     query, limit)` → `find_open_pr_for_branch(repo, *, head, limit)` per
     step 2's pivot.
   - Platform-agnostic exception handling via `_REPO_NOT_FOUND_ERRORS` /
     `_REPO_CLIENT_ERRORS` tuples covering both `(GhNotFoundError,
     AdoNotFoundError)` and `(GhClientError, AdoClientError)`. Each
     workflow defines its own tuple at module top — small duplication
     vs introducing a Protocol-level error hierarchy (which would be a
     separate ADR's worth of design).
   - error_kind strings renamed `gh.* → repo.*` (`gh.base_sha_failed`
     → `repo.base_sha_failed`, etc.). Grep-confirmed nothing in tree
     pinned the old names — these were emitted-only.
   - **Test fakes refit in five places** to add `find_open_pr_for_branch`
     alongside the legacy `pr_search`: `_DemoGhClient` in `leaf_pr.py` +
     `feature_pr.py`, `FakeGh` in `tests/test_leaf_pr_workflow.py` +
     `tests/test_feature_pr_workflow.py`. Both methods coexist so any
     remaining `pr_search`-shaped callers in non-trunk workflows
     (close_out, implementation, plan_pr) keep working unchanged.
   - **New `tests/test_trunk_topology_against_ado.py`** (8 tests) is
     the load-bearing proof step 4 actually unblocks the ADO path: each
     of the three workflows runs end-to-end against `FakeAdoClient`
     via `toolbelt.repo` with `toolbelt.gh=None`. Trunk creation,
     idempotent re-run, fail-closed-on-missing-base, leaf-PR creation,
     leaf-PR idempotent reuse, feature-PR open-after-leaves-merged,
     fail-closed when neither client is set, and the back-compat
     `gh-only` fallback path are all individually pinned.
   - 437 tests (3 skipped) across the affected surface still green —
     no regressions in close_out, pr_lifecycle, implementation,
     plan_pr, kanban_executor, commit_plan, or any of the contract
     suites. Step 5 (driver wiring) is now the last remaining piece;
     after it, `requiem-end-to-end --ado-repo ...` becomes live-runnable.

5. **Driver wiring** — split `github_repo` into the two-arg shape
   above; both go through the same `_resolve_base_branch` helper which
   now takes a `RepoPlatform` parameter (not a `GhClient`). Add tests
   covering the `ado_repo` path with stub clients. **~80 LOC + 5
   tests.**

Each step is shippable on its own; the chain ends with a live ADO
scratch run becoming possible.

## Consequences

**Positive:**
- Unblocks the primary v0 customer scenario (CloudVault and other ADO
  workloads).
- Single-sourced trunk-topology logic across both platforms; no
  Polyphony-style drift.
- Closes ADR-0007 Q4 in code (not just on paper) for the existing
  `ado_pr.py` and pre-builds the credential plumbing the new
  `AdoClient` reuses.
- Makes the eventual extraction of `pr_lifecycle` ↔ `ado_pr` behind a
  `PrLifecyclePlatform` Protocol straightforward — same pattern, second
  surface.

**Negative / open:**
- Real refactor cost across the workflow + client + test surface
  (~600 LOC churn, mostly clean rename + new client + new tests).
- ADO REST has a more verbose path structure than `gh` (org/project/repo
  vs Owner/Repo). The Protocol takes opaque `repo: str`; impls parse
  their native form. Documentation must be clear that `repo` is
  per-impl.
- ADO's PR search by `head`/`base` (`searchCriteria.sourceRefName` /
  `targetRefName`) returns refs with `refs/heads/` prefix; the impl
  normalises before returning. Small footgun if a third backend doesn't
  do the same.
- The PR-lifecycle Protocol extraction (ADR-0023's deferred refactor) is
  still deferred — this ADR does not address it. That's fine: the
  trunk-topology gap is the v0 dogfood blocker; the lifecycle layer can
  be unified later when it has a second concrete customer.
- New dependency: `azure-identity` (and transitively `azure-core`).
  Already a reasonable v0 add since `ado_pr.py` is ADO-bound; the
  alternative (raw HTTP + AAD device-code negotiation) is materially
  worse. Pin a recent stable.

**Why this is recorded as an ADR:** the Protocol shape determines every
future RepoPlatform impl; the auth seam is the load-bearing piece of
the v0 ADO path; and the decision to make the trunk-topology workflows
platform-agnostic while the lifecycle layer stays per-workflow is a
real trade-off (consistency vs incrementalism) that a future reader
will want to understand.
