# ADR 0007 — PR-lifecycle Architecture (draft)

**Status:** Draft (Wave 6 / Britten seat — design exploration; NOT YET RATIFIED)
**Date:** 2026-06-12
**Supersedes:** none
**Superseded by:** —
**Owners:** Britten (this draft) → Daniel (six open questions) → next implementer seat

---

## TL;DR — recommended position

**Adopt a single `pr_lifecycle.py` workflow parameterised by a `PrPlatform`
Protocol (Option B in §6).** GitHub and ADO each get a concrete
`PrPlatform` implementation living next to the existing `GhClient` /
`TwigClient`. The current `PrToolkit` Protocol in
`src/requiem/workflows/pr_lifecycle.py:209-229` is the seed shape; we
widen it to cover the ADO verb surface, rename it `PrPlatform`, and
inject it at `build_engine` time.

**Do NOT port polyphony's `feature-pr.yaml` aggregator.** Its
remediation-cycle loop is real value, but the *aggregator wrapper itself*
is overhead that exists to bridge the YAML world's compose-by-file
constraint. In Requiem we already compose by `SubWorkflowNode`
(ADR 0005). The aggregator becomes a thin in-workflow loop with a hard
iteration cap and a `NeedsHuman` terminal — no separate `feature-pr`
module.

**Reject the dual-module split** (`pr_lifecycle.py` GH-only +
`ado_pr_lifecycle.py` ADO-only). It would duplicate the ~1167 LOC
topology in
`src/requiem/workflows/pr_lifecycle.py` and drift the moment one platform
adds a behaviour. We have one workflow domain — *drive a PR to merge or
to a human* — and one workflow should encode it.

**Defer the long-poll "drift watcher" split (Option D)** until we have a
concrete operator complaint that requires it. INV-RESTART means a paused
`pr_lifecycle` run is already resumable; we do not need a second
workflow to re-attach to it.

**Decision gate to Daniel:** the six design questions in §5 each
demand a position. The above is Britten's recommendation; sections
§5.1–§5.6 walk each one. Sections §9.1–§9.4 list questions Britten
cannot answer alone.

---

## 1. Context

### 1.1 What we have today

The current PR-lifecycle surface is a single GitHub-only Python workflow:

* `src/requiem/workflows/pr_lifecycle.py` — 1167 LOC. Topology
  `fetch → check_initial_state → request_review → poll_review →
  {synthesize_comments → address_comments → push_addressal → check_progress}* →
  check_can_merge → merge_pr → update_item → end_merged`. Loop cap +
  no-progress detector at `check_progress` (lines 781-824). Single
  `PrToolkit` Protocol (lines 209-229) with `RealPrToolkit` wrapping
  `GhClient` + a `git push` subprocess, and a scriptable `FakePrToolkit`
  for tests.
* `src/requiem/clients/gh.py` — 425 LOC. `pr_view`, `pr_search`,
  `pr_create`, low-level `api` escape hatch. Typed error taxonomy
  (`GhRateLimitedError`, `GhServerError`, `GhNotFoundError`,
  `GhAuthError`, `GhUnknownError`) mapped to Ravel L-1 outcomes.
* `src/requiem/clients/twig.py` — 265 LOC. `show_async`,
  `set_state_async`, `list_children_async`, `comment_async`. **No PR
  helpers; no PR-link surfacing** (Mahler-3 audit issue #30).
* `src/requiem/workflows/implementation.py:818-902` — `create_pr` verb
  uses `GhClient` directly to open the PR and produces a
  `pr_number` / `pr_url` in its `completed` map for downstream stages.
* `src/requiem/workflows/full_sdlc.py:278-313` — the orchestrator stitches
  `implement → pr_lifecycle → close_out` via shim modules; the cross-stage
  `_CROSS_STAGE` cell (line 306) hands the `pr_number` from
  `implementation.create_pr` to `pr_lifecycle.build_engine` and then to
  `close_out.build_engine` at child-construction time.
* `src/requiem/workflows/close_out.py:390-432` — `resolve_pr` reads
  `pr_number` from inputs if given, otherwise scans the twig item's
  `raw["pullRequests"]` list. Mahler-3 issue #30 documents the real-ADO
  hazard: that field is empty in twig's JSON today, so close_out
  escalates to `needs_human` and the operator must pass `--pr N`.

Tests:
`tests/test_pr_lifecycle_workflow.py` — 14 tests, all GH-only, all use
`FakePrToolkit`. The full crash-point matrix is in
`tests/test_resume_fidelity.py` and `tests/test_resume_fidelity_matrix.py`
(91 + 110 tests), neither specific to PR-lifecycle but defending the
INV-RESTART guarantee the workflow inherits from the engine.

### 1.2 What polyphony had (the reference)

Per `docs/references/polyphony-parity-inventory.md` §3 and
`docs/references/v0-parity-readiness.md` §2.2:

* **`feature-pr.yaml`** (v2.4.8, ~60 KB at
  `C:\Users\dangreen\projects\polyphony\.conductor\registry\workflows\feature-pr.yaml`)
  — aggregator. Integrates target-branch drift, opens the feature PR,
  routes through `pr_platform_router` to a platform-specific lifecycle,
  runs a *remediation cycle* (capped at 3) when the lifecycle returns
  `merged=false`, and finally escalates to a human gate.
* **`pr_platform_router`** — a `script` node whose output `{platform,
  ado_inputs_ready, missing}` drives three Jinja edges (lines 289-313 of
  `feature-pr.yaml`): `→ github-pr` / `→ ado-pr` / `→
  feature_pr_inputs_missing_gate`. The router is the only place in the
  workflow that knows the platform; downstream sub-workflows are
  platform-monomorphic.
* **`github-pr.yaml`** + **`ado-pr.yaml`** — the per-platform lifecycle
  sub-workflows. GitHub uses `gh`; ADO uses `polyphony pr post-comment-ado`
  + `pr poll-status-ado` verbs. Both export the same output shape
  (`merged`, `pr_url`, …) so the aggregator can route uniformly.
* **Remediation planner** (`feature-pr.yaml` lines 804+) — an agent node
  that, when a lifecycle returns `merged=false`, plans a follow-up
  merge-group. Then `implement-merge-group` re-runs against the
  remediation MG, then `feature_pr_updater` re-requests review, then the
  platform router fires again. Up to `pr_remediation_policy.
  max_remediation_cycles` (default 3) iterations.

### 1.3 What Mahler-3 said

`docs/references/v0-parity-readiness.md` §2.9 row #10 marks **platform-specific
PR lifecycles** as the v0 non-negotiable that Requiem cannot meet
today: GitHub ✅, ADO ❌. The audit's path-to-GO (§5) sequences
*"Add `ado_pr` workflow + `twig` PR helpers"* as step 4 of 7 — implicitly
adopting Option C (sibling workflow). This ADR exists because that
sequencing was a parity recommendation, not an architecture decision.
Daniel's prompt opens the architecture.

### 1.4 What the error deep-dive said

`docs/references/error-handling-deep-dive.md` row (d) calls out that
reviewer agents have *"zero detection for schema drift and hallucinated
success"*. The synthesise-comments step in our current
`pr_lifecycle.py:707-725` produces a `CommentSynthesis` pydantic model
and routes `bad_output` to `needs_human_end` (line 956) — we are not
inheriting that polyphony gap. But every architecture proposed below
must preserve that property: the reviewer/synth/address loop terminates
on `BadOutput`, on `max_iterations`, on `no_progress`, on
`PermanentFailure`, or on merge. There is no fifth exit.

---

## 2. Invariants this design must honour

| Invariant | Where it bites the PR-lifecycle | Source |
|---|---|---|
| INV-SINGLE-PROCESS | The platform abstraction lives in the same Python process as the engine. No second binary; no IPC. | `docs/north-star.md` §2 |
| INV-RESTART | Every PR-lifecycle verb must be idempotent: `merge_pr`, `git_push`, `request_review`, `comment` all re-runnable. Current `pr_lifecycle.py:38-48` documents this for GH; ADO must match. | `docs/north-star.md` §2 |
| INV-NO-CORRUPT-FORWARD | Unknown reviewer schema / unknown `gh` exit-1 / `mergeable: null` ⇒ `NeedsHuman`, never `RetryableFailure`. See `pr_lifecycle.py:846-854` (mergeability_unknown) and `_map_gh_error` (lines 480-538). | `docs/north-star.md` §2 |
| INV-DISCRIMINATED-OUTCOMES | The platform-abstraction methods raise typed errors; verbs translate to the six-variant union. The Protocol does not expose `Outcome` directly — that's the workflow's job. | `docs/north-star.md` §2 |
| INV-CANCEL-SHORT-CIRCUITS-RETRY | A long-polling `poll_review` (current default `poll_timeout_s=600.0`, `pr_lifecycle.py:1112`) must honour cancel between ticks. Already true for GH; ADO equivalent must follow the same pattern. | `docs/north-star.md` §2 |
| INV-NO-ENGINE-ABANDONMENT | The escalation-after-N-remediations route is `NeedsHuman` (terminate disposition), never `abandoned`. Polyphony's `escalation_gate` is a `human_gate` — our peer is `terminate("needs_human_end")`. | `docs/north-star.md` §2 |
| INV-SUBWORKFLOW-LOG-ISOLATION | If we *do* keep an inner remediation sub-workflow (Option A or a hybrid), it must run in its own child Engine with its own `{sub_run_id}.events.jsonl`. ADR 0005 gives us this for free. | ADR 0005 |

The architecture in §7 is chosen so every row above is satisfied **by
construction** — no row requires a runtime check.

---

## 3. The boundary that matters: what *is* "the PR lifecycle"?

Before debating shapes we have to fix vocabulary. Three nested concepts:

1. **The platform client.** A typed Python surface over `gh` / `az` /
   `twig` / REST. Today: `GhClient` and `TwigClient`. Returns typed
   values, raises typed errors, knows zero workflow vocabulary.
2. **The PR-lifecycle workflow.** Verbs that drive *a single open PR to
   merge or to a human*. Synthesise comments → address → push → re-poll
   → merge. Mostly platform-agnostic; the only platform-specific bits
   are the calls into (1) and a small amount of vocabulary translation
   (`APPROVED` / `CHANGES_REQUESTED` on GH ↔ `approved` /
   `waitingForAuthor` on ADO).
3. **The feature-PR aggregator** (polyphony's `feature-pr.yaml`).
   Drift-integrates, opens the PR, calls (2), runs a remediation cycle
   if (2) returns `merged=false`, escalates on cap.

Conflating (2) and (3) is exactly polyphony's mistake — the aggregator
existed because YAML composition needs a wrapper file to host the
remediation loop. Python composition does not. This ADR proposes that:

* (1) **stays one module per platform** (`gh.py`, `azuredevops.py` —
  TBD; see §5.6).
* (2) **is one workflow** (`pr_lifecycle.py`), parameterised by a
  `PrPlatform` Protocol that abstracts (1).
* (3) **becomes an in-workflow loop inside** (2), capped by a
  `max_remediation_cycles` input, with the remediation-planner agent and
  the per-cycle re-implementation either inline or as a `SubWorkflowNode`
  call to `implementation.py` — see §5.3.

---

## 4. Load-bearing vs conductor-inherited

Following the Stravinsky rubric, we have to separate which parts of
polyphony's PR architecture are **load-bearing** (we keep their *intent*)
and which are **conductor-inherited** (artefacts of the YAML world).

| Polyphony artefact | Load-bearing? | Why |
|---|---|---|
| `pr_platform_router` script node | **Conductor-inherited.** | The router exists because YAML edges can only switch on a script's typed output. In Python we just inject the right `PrPlatform`. |
| `feature-pr.yaml` aggregator wrapper | **Conductor-inherited.** | Polyphony's only mechanism to host a loop around a sub-workflow was another sub-workflow. Python has `while`. |
| Remediation planner agent | **Load-bearing.** | This is real work: when reviewers won't accept the current PR, an agent reads the feedback and proposes a follow-up MG. Polyphony has this; we must too. |
| Cap of N remediation cycles | **Load-bearing.** | INV-NO-ENGINE-ABANDONMENT requires a terminal; without a cap the workflow loops forever. We honour this with a `max_remediation_cycles` input mirroring our existing `max_iterations` (`pr_lifecycle.py:1106`). |
| Escalation gate (human-in-the-loop after cap) | **Load-bearing.** | Already implemented as `terminate("needs_human_end")` for the inner loop; same shape applies to the outer cap. |
| Per-platform sub-workflow (`github-pr.yaml`, `ado-pr.yaml`) | **Conductor-inherited.** | Polyphony split them because YAML doesn't compose Protocols. The split also caused the very drift `docs/references/ado-feature-pr-parity.md` had to retrofit. |
| Drift-integration node (`integrate_target_drift` in `feature-pr.yaml:37`) | **Load-bearing if** the workflow opens the PR; **moot if** PR opening stays in `implementation.py`. | See §5.1. |
| The `merged=false` exit code from the sub-workflow | **Load-bearing as a signal**, but expressible as a discriminated outcome (`PermanentFailure(error_kind="pr.unmergeable_after_loop")` or similar) without a sub-workflow boundary. |

The conductor-inherited rows are the *cost* of the YAML world. Requiem
has already paid the cost to leave that world (ADR 0001). We do not
re-import the cost.

---

## 5. The six design questions — Britten's positions

These are Daniel's prompt verbatim, with Britten's recommended position
and the reasoning. Each is up for revision in §9.

### 5.1 Does `feature_pr` need to be a separate workflow?

**Position: NO.** The work polyphony's `feature-pr.yaml` does that is
*not already done elsewhere in Requiem* is:

* **Target-branch drift integration.** Re-rebase `feature/<root>` onto
  `main` before opening the PR. Today this is implicit — we never have
  a feature branch that drifts because we open the PR right after
  `commit_changes` in `implementation.py`. When we grow merge-group
  topology (per Mahler-3 audit, non-negotiable #7), drift will become
  real and we will need a `rebase_onto_target` verb. That verb belongs
  on `implementation.py`'s exit (right before `create_pr`) or in a new
  `merge_group.py`, not in a separate `feature_pr.py`.
* **The remediation loop** (covered in §5.3).
* **The platform routing** (covered in §5.2).

None of these justify a separate top-level workflow. They live as:

* a verb in `implementation.py` (drift integration);
* an in-workflow loop in `pr_lifecycle.py` (remediation);
* a constructor-time choice in `pr_lifecycle.build_engine` (platform).

The `full_sdlc.py` topology stays as it is today
(`dispatch → plan → implement → pr_lifecycle → close_out`). When MG
topology lands, `implement` may expand into
`implement → merge_group_close → pr_lifecycle`; that's a different
ADR.

### 5.2 Is `pr_platform_router → pr_lifecycle_{github,ado}` the right abstraction?

**Position: NO. Use a `PrPlatform` Protocol injected at `build_engine`
time.** Our existing `PrToolkit` (`pr_lifecycle.py:209-229`) already
*is* this — it is a Protocol with seven async methods that the workflow
calls uniformly. The Real implementation wraps `GhClient` + git push.
The Fake is scriptable. To support ADO we widen the Protocol's
contract (a few new methods, e.g. `list_threads`, `add_comment`,
`complete_pr`), provide an `AdoPrToolkit` implementation that wraps
the ADO REST API (or `az`/`twig` — see §5.6), and rename the type
`PrPlatform` to reflect its new scope.

The platform router pattern in `feature-pr.yaml:289-313` exists because
YAML cannot have a verb whose static type is *"some implementation of
the platform interface"*. Python's `Protocol` is exactly that — at the
type level. At runtime, the kernel dispatches verbs that close over a
single concrete instance, so there is no per-step platform check; the
"router" decision happens once, at the parent's
`build_engine` (cf. `full_sdlc.py:281-297`'s shim).

What the router *gained* polyphony was a place to surface
*"the ADO inputs are missing"* before any ADO verb fired
(`feature-pr.yaml:313`). We get the same property by validating
`PrPlatform` construction at `build_engine` entry: if you pass
`platform="ado"` with missing `organization`/`project`/`repository`,
`build_engine` raises before the run starts. The check moves from
"runtime workflow node" to "engine construction" — strictly earlier and
unambiguous.

### 5.3 Where does the remediation planner live?

**Position: in-workflow loop, with an optional `SubWorkflowNode` call
back to `implementation.py` for the remediation MG.**

The remediation loop is:

```
poll_review                               (already exists)
  → if reviewer_says_rework_whole_thing:
       remediation_planner agent          (new)
         → seed remediation MG            (new — `SubWorkflowNode`?)
         → re-implement                   (call into implementation.py)
         → re-push, re-poll               (loop back to poll_review)
       cap at max_remediation_cycles      (mirrors loop_cap @ line 804)
```

Two options for the "re-implement" arm:

(a) **In-line agent.** Add a new agent spec next to `COMMENT_ADDRESSER`
that takes a *plan*, not a *list of comments*, and produces commits.
Cheap, but conflates two agent roles.

(b) **SubWorkflowNode back to `implementation.py`.** The remediation
planner emits an `ImplementationInputs` payload (the same dataclass
`full_sdlc.py:351-368` produces in `capture_implementation`); the
PR-lifecycle workflow `.subworkflow("remediate_impl",
workflow="requiem.workflows.implementation")` and re-uses the entire
implementation pipeline. The new feature is "re-enter implementation
with a fresh plan and an existing branch"; that's a feature
`implementation.py` may have to grow (an `existing_branch:` input
instead of `create_branch`), but the topology is right.

**Recommend (b)** because:

* It honours ADR 0005's "compose by sub-workflow" idiom.
* It re-uses `implementation.py`'s testing surface (24 tests) instead of
  growing a new code path.
* It lets remediation produce multiple commits (planning is naturally
  per-file/per-area; addressal so far has been "one commit per
  iteration") — a real-world remediation might want that.

The cost: `implementation.py` grows a new `existing_branch` mode and
must skip `create_branch` / `assert_clean_workspace` on entry. Two
verbs become idempotent no-ops; the rest unchanged.

If (b) lands but proves too heavyweight (the indirection makes the
verdict card harder to read, say), the fallback is (a). Both are
strictly inside `pr_lifecycle.py` — the outer-workflow split is *not*
on the table.

### 5.4 What's the contract with `close_out`?

**Position: `pr_lifecycle` writes a `PrLifecycleResult` whose
`pr_number` and `merge_sha` `close_out` reads directly.** We already
have `PrLifecycleResult` (`pr_lifecycle.py:94-109`) with `pr_number`,
`pr_url`, `merge_sha`. Make this the contract:
`close_out.build_engine`'s `pr_number` argument is the only documented
way to bind a PR to a close-out; the existing `resolve_pr` fallback (the
`item.raw["pullRequests"]` scan in `close_out.py:390-432`) becomes a
*recovery* code-path — only fires when `pr_lifecycle` ran out-of-band.

Today `full_sdlc.py:303-313` does exactly this via the `_CROSS_STAGE`
cell; we just promote it to documented contract. Mahler-3 issue #30
(twig has no `pullRequests` field) stops being a *blocker* for the
happy path — only matters when the operator runs `close_out` standalone
against an item whose PR was opened by something other than this
Requiem run.

Concretely:

* `close_out.CloseOutInputs.pr_number` (`close_out.py:200`) becomes
  *recommended* for any pipeline-driven close-out.
* `close_out`'s `resolve_pr` verb's fallback path keeps the
  `pullRequests` scan and the `gh pr list --search head:<branch>`
  candidate from Mahler-3 §4.1 as a documented v0.1 fast-follow.
* `pr_lifecycle`'s `update_item` verb (`pr_lifecycle.py:885-896` —
  currently a stub) becomes the place that posts the merge comment to
  the work item via `twig.comment_async`. That comment becomes the
  artefact `close_out` can text-search for if `pr_number` is missing.

### 5.5 How do we handle review-loop termination?

**Position: keep the three-way termination already in
`pr_lifecycle.py` and add a fourth for the remediation outer loop.**

Current inner loop terminators (`pr_lifecycle.py:781-824`):

* `no_progress` — same SHA two iterations in a row → `NeedsHuman`.
* `max_iterations` — hit the input cap → `NeedsHuman`.
* `BadOutput` from synthesizer/addresser → `NeedsHuman`.
* (implicit) clean merge → `end_merged`.

Add for the outer remediation loop (when §5.3 lands):

* `max_remediation_cycles` — hit the input cap → `NeedsHuman` with a
  prompt distinguishing it from the inner cap.

All four landing routes are `terminate("needs_human_end")`. The verdict
card distinguishes which cap fired via the
`details` dict on the `PermanentFailure` (existing pattern, see
`pr_lifecycle.py:802-815`).

INV-NO-ENGINE-ABANDONMENT (§2) is satisfied because every cap routes to
operator surrender, never to silent abandon. INV-CANCEL-SHORT-CIRCUITS-RETRY
is satisfied because the kernel honours `cancel_requested` between
loop ticks regardless of which cap is approaching.

Polyphony's `escalation_gate` was a `human_gate` with choices like
*"escalate" / "retry-once" / "abort"*. We can replicate that — a
`HumanGateNode` between `check_progress(needs_human.*)` and the
terminate — **but Britten recommends not.** A four-way human gate
inside a workflow that has *already* surfaced four distinct failure
modes overloads the gate's purpose; the operator can re-run with a
larger cap if they want, via `requiem run … --max-iterations N`. The
default behaviour stays surrender.

Counter-argument open in §9.3.

### 5.6 What does the ADO PR surface require from twig?

**Position: do NOT extend twig. Add a new
`src/requiem/clients/azuredevops.py` REST client.** Three options were
considered:

(a) **Extend `twig`.** Mahler-3's audit notes twig has `show`,
`set_state`, `list_children`, `comment` — none of the PR primitives.
Adding `pr_create_async`, `pr_view_async`, `pr_threads_async`,
`pr_complete_async`, `pr_iterations_async` would roughly double the
twig surface. Twig today is a *work-item* tool; PRs are a different
resource with their own API and lifecycle.

(b) **Shell out to `az repos pr …`.** `az` is heavyweight (a full
Python install) and our `gh`/`twig` pattern is "wrap a fast native CLI
in a typed Python facade." `az` is neither fast nor predictable across
versions.

(c) **Direct REST via `httpx`.** A small `AdoClient` wrapping
`POST /git/repositories/{repo}/pullRequests`, `GET
/pullRequests/{id}`, `GET /threads`, `POST /threads`, `PATCH
/pullRequests/{id}` (for `completionOptions`). Auth via PAT through
`AZURE_DEVOPS_EXT_PAT` env var (the same one `az`/`twig` consume).
Typed error taxonomy mirroring `GhClient`'s.

**Recommend (c).** Reasoning:

* The ADO REST API is stable, documented, and small for our scope (~6
  endpoints).
* We can mirror `GhClient`'s typed-error contract directly
  (`AdoRateLimitedError`, `AdoAuthError`, `AdoNotFoundError`,
  `AdoServerError`, `AdoUnknownError`) — Ravel L-1 applies identically.
* It does not entangle twig (which would force every twig user to
  understand PR semantics).
* It does not require `az` on PATH (Tchaikovsky's bug-bash already
  caught one cross-CLI lockstep break — see Mahler-3 §4.2).

The `AdoPrToolkit` implementing `PrPlatform` then wraps `AdoClient` +
local `git push` (the same `git_push` already in our toolbelt). The
twig changes Mahler-3 asks for (issue #30: PR-link surfacing) are
*orthogonal* — they help `close_out.resolve_pr`, not the lifecycle
itself.

The cost: a new ~300-LOC client + test suite. Net new surface area
roughly the size of `gh.py`. Acceptable for v0+1.

---

## 6. Alternative architectures evaluated

Four shapes considered, two seriously. All assume §3's distinction
between platform-client and lifecycle-workflow.

### Option A — Polyphony-compatible port

Mirror polyphony's structure 1:1:

* `feature_pr.py` — top-level aggregator, drift-integrate, open PR,
  call platform router, loop on remediation, escalate.
* `pr_platform_router.py` — a verb returning `{platform, …}`.
* `pr_lifecycle_github.py` — current `pr_lifecycle.py`, renamed,
  GH-only.
* `pr_lifecycle_ado.py` — sibling, ADO-only.
* `remediation_planner` agent + `feature_pr_updater` verb inside the
  aggregator.

**Pros:** familiar to anyone who has read polyphony's YAMLs;
documentation porting is mechanical; reviewer feedback shape and edge
names track 1:1 to polyphony.

**Cons:**

* Imports the *cost* of YAML composition into Python. We have
  `Protocol`; we don't need a `script` node to dispatch on platform.
* Quadruples module count (1 → 4) for a domain that has one coherent
  shape.
* The "platform router" is dead weight: every router call is "look at
  the input you were given and pass it through."
* Two PR-lifecycle Python files will drift. Polyphony's own history
  shows this — `docs/decisions/ado-feature-pr-parity.md` exists in
  `polyphony` to undo the drift between `github-pr.yaml` and
  `ado-pr.yaml`.

**Cost estimate:** ~1500 LOC of new code (drift-integrate + aggregator
+ ADO peer of pr_lifecycle), most of which duplicates existing GH
lifecycle code. ~30 new tests.

**Verdict:** REJECT. The cost is structural — it grows with every
behaviour change.

### Option B — Single workflow + `PrPlatform` Protocol — **RECOMMENDED**

One `pr_lifecycle.py` workflow. The existing `PrToolkit` Protocol
(`pr_lifecycle.py:209-229`) widens to `PrPlatform`:

```python
class PrPlatform(Protocol):
    async def pr_view(self, repo: str, number: int) -> PrSnapshot: ...
    async def list_reviews(self, repo: str, number: int) -> list[ReviewSummary]: ...
    async def list_review_comments(self, repo: str, number: int) -> list[ReviewComment]: ...
    async def request_review(self, repo: str, number: int, reviewers: list[str] | None) -> dict: ...
    async def mergeability(self, repo: str, number: int) -> MergeabilityReport: ...
    async def merge_pr(self, repo: str, number: int, strategy: str) -> MergeResult: ...
    async def git_push(self, repo_path: Path, branch: str) -> str: ...
    # New for remediation:
    async def add_pr_comment(self, repo: str, number: int, body: str) -> None: ...
```

Two concrete implementations:

* `GitHubPrPlatform` — current `RealPrToolkit`, lightly renamed.
* `AzureDevOpsPrPlatform` — new, wrapping new `AdoClient` (§5.6).

`build_engine` grows a `platform: Literal["github", "ado"] = "github"`
input that selects the implementation. Inputs validation at
`build_engine` raises if ADO inputs are missing (replaces polyphony's
`feature_pr_inputs_missing_gate`).

The remediation loop is **new code in the existing workflow**, sitting
between `poll_review` and the terminate, gated by a new
`max_remediation_cycles` input. The remediation planner is a new agent
spec; the re-implementation arm is a `SubWorkflowNode` to
`implementation.py` (per §5.3, recommendation (b)).

**Pros:**

* One workflow, one test surface, one verdict card. Vocabulary
  translation (`APPROVED` ↔ `approved`) lives in the platform
  implementations, not the workflow.
* Adding a third platform (Bitbucket, hypothetically) is a new
  `Protocol` implementation, zero workflow change.
* INV-RESTART, INV-CANCEL, INV-NO-CORRUPT-FORWARD are inherited
  unchanged; existing 14 `pr_lifecycle` tests stay green; new tests
  for the ADO branch parameterise over `platform`.
* The `_CROSS_STAGE` PR-number handoff (`full_sdlc.py:306`) keeps
  working as-is — the parent workflow doesn't know or care which
  platform was used.

**Cons:**

* Widens the existing `PrToolkit` Protocol's method count from 7 to ~10.
  Bigger interface; more for `FakePrToolkit` to script.
* The two platforms have different vocabulary that we have to *not*
  leak into the workflow — `MergeabilityReport.mergeable_state` today
  uses GH-specific strings (`"clean"`, `"blocked"`, `"dirty"`,
  `pr_lifecycle.py:197`). We must promote these to a platform-agnostic
  enum or document a contract.
* The remediation loop adds ~150 LOC to a 1167-LOC file. The file
  approaches 1500 LOC — consistent with `planning.py` (1527) and
  `close_out.py` (1387), so within Requiem's accepted size.

**Cost estimate:** ~600 LOC across pr_lifecycle.py changes + new
AdoClient + AdoPrPlatform + tests. ~15-20 new tests (ADO parameterised
peers of existing 14 plus 4-5 remediation-loop tests).

**Verdict:** RECOMMEND.

### Option C — Platform-specific workflows, root chooses

Keep `pr_lifecycle.py` GH-only. Add `ado_pr_lifecycle.py` as a sibling.
`full_sdlc.py` picks one based on an input. No shared Protocol.

**Pros:**

* No abstraction tax; each file is monomorphic and grep-friendly.
* Mahler-3's path-to-GO §5 step 4 implicitly assumes this.

**Cons:**

* Duplicates the lifecycle topology in two files. Every loop-cap,
  no-progress, BadOutput, INV-RESTART, cancel-short-circuit test must
  be written twice.
* When we touch one (e.g. issue #29's terminate-disposition fix from
  Mahler-3 §4.1), it's trivially easy to forget the other. This is
  *exactly* the failure mode `docs/references/ado-feature-pr-parity.md`
  in polyphony was written to fix.
* `full_sdlc.py`'s `_register_pr_lifecycle_shim` (line 278) doubles or
  becomes conditional, complicating an already-fragile cross-stage
  shim.

**Cost estimate:** ~1100 LOC (full peer of `pr_lifecycle.py` minus
shared bits) + 14 peer tests + ADO client. Worst of B and C combined
in module count.

**Verdict:** REJECT. The drift hazard is documented prior art.

### Option D — Open-and-wait vs drift-watcher split

Orthogonal to A/B/C. Split *time-scale*:

* `pr_lifecycle_open.py` — fast, deterministic, opens the PR and
  returns immediately (or waits a short window).
* `pr_lifecycle_watch.py` — long-lived. Re-attaches to an existing PR
  by number, polls until merge or terminal, runs the address loop.

Polyphony does not have this split exactly; its `feature-pr.yaml` is
one workflow that runs as long as needed. But Requiem's
INV-RESTART means a paused workflow is *already* re-attachable: kill
the process, run `requiem resume <run_id>`, the engine picks up at the
last `poll_review` and continues. The only thing Option D buys is
*architectural permission* to declare a `pr_lifecycle` "done" while
the PR is still open — which then needs a separate watcher to drive
it to completion.

**Pros (if adopted):**

* `full_sdlc.py` could complete its `pr_lifecycle` stage without
  waiting hours; close-out could happen against a not-yet-merged PR
  (it currently asserts `pr.merged`, see `close_out.py:478-491`, so
  this would need rework).
* Operator can launch a "PR watcher" separately, decoupled from any
  particular run.

**Cons:**

* Conflicts with `close_out`'s contract (only valid for merged PRs).
* Doubles the workflow count for a benefit that INV-RESTART already
  provides through resume.
* No operator complaint exists; this is anticipatory.

**Verdict:** DEFER. Cite as future work in §8.4. Revisit only if
operator-scale evidence shows a single `pr_lifecycle` run blocks
`full_sdlc` in a problematic way.

---

## 7. Decision

**Adopt Option B.**

Concrete deliverables (in order, each its own seat):

1. **Widen `PrToolkit` → `PrPlatform`** in `pr_lifecycle.py`. Add
   `add_pr_comment` method. Promote `MergeabilityReport.mergeable_state`
   to an enum (`CLEAN | BLOCKED | DIRTY | UNKNOWN`) translated by each
   platform impl. Rename `RealPrToolkit` → `GitHubPrPlatform`. Rename
   `FakePrToolkit` → `FakePrPlatform`. Update the 14 existing tests.
2. **Add `src/requiem/clients/azuredevops.py`** — REST client mirroring
   `GhClient`'s shape. Typed error taxonomy. ~25 tests in
   `tests/clients/test_azuredevops.py` peering with
   `tests/clients/test_gh.py`.
3. **Add `AzureDevOpsPrPlatform`** in `pr_lifecycle.py`. Reuses the
   workflow unchanged; only the platform impl is new.
4. **Add `platform: Literal["github","ado"]`** to
   `pr_lifecycle.build_engine`; build-time validation of platform
   inputs.
5. **Add remediation loop** (the §5.3 work). New
   `remediation_planner` agent spec; new outer loop with
   `max_remediation_cycles` cap; `SubWorkflowNode` arm calling
   `implementation.py` with an `existing_branch` input.
6. **Promote `pr_lifecycle → close_out` PR-handoff** to documented
   contract (the §5.4 work). Make `pr_number` the *primary* binding;
   `resolve_pr`'s scan becomes a recovery fallback. Implement the
   `gh pr list --search head:<branch>` fallback fast-follow from
   Mahler-3 §4.1.

Steps 1-4 unblock the v0 §9 non-negotiable #10. Step 5 closes the
parity gap with polyphony's `feature-pr.yaml` remediation cycle. Step 6
closes Mahler-3 issue #30's structural half.

Steps 1-4 are blocking for ADO parity; step 5 is blocking for
*polyphony parity* but not for the re-scoped v0 in Mahler-3 §5
("Alternative: re-scoped v0"). Step 6 is independent and can land at
any time.

---

## 8. Consequences

### 8.1 Positive

* **Single source of truth for PR-lifecycle topology.** Edge changes,
  error-routing changes, loop-cap changes happen once.
* **ADR-0005 sub-workflow primitive is exercised in production.** The
  remediation loop's re-implementation arm is the second real
  sub-workflow caller after `full_sdlc.py`. INV-SUBWORKFLOW-LOG-ISOLATION
  gets real-world coverage.
* **Test surface scales linearly.** Every new test is *either* a
  workflow test (parameterised over platform) *or* a platform-impl
  test. No quadratic explosion.
* **The platform-router YAML idiom retires.** When we write the
  migration-from-polyphony guide's PR chapter (deferred from Purcell's
  guide), the answer to "where is the platform router?" is "it's
  `Protocol` dispatch at engine construction."
* **Mahler-3 §9 non-negotiable #10 closes** with one new client and
  one new platform implementation, not four new workflow modules.

### 8.2 Negative

* **`PrPlatform` Protocol becomes a load-bearing API surface.**
  Changes to it ripple to both implementations *and* the
  `FakePrPlatform` (and every test that scripts it). This is the same
  surface-management cost we already pay for `FilesystemClient`,
  `GhClient`, `TwigClient`, and ADR-0005's input shapes; manageable but
  not free.
* **GH-specific vocabulary leaks must be paid down.** The current
  `MergeabilityReport.mergeable_state` uses GH strings literally
  (`pr_lifecycle.py:197`). Step 1 of the decision pays this debt;
  whoever writes step 1 must read every consumer (verdict card,
  `_report_dict` at line 901, the four NeedsHuman details payloads at
  lines 836-854) and ensure the platform-agnostic enum reads the same.
* **`implementation.py` grows an `existing_branch` mode.** Step 5
  forces this. It is a real semantic change — "re-enter on a branch
  the workflow did not create" — and the workspace-clean check
  (`pr_lifecycle.py`/`implementation.py:1-50` topology comment)
  becomes conditional. Risk: subtle INV-RESTART regression if
  `create_branch` and `assert_clean_workspace` are not properly
  no-op'd. Mitigation: a peer of the `INV-RESTART` test in
  `test_implementation_workflow.py` for the existing-branch mode.
* **The `AdoClient` becomes the second async REST client in the
  codebase** (the first being LLM providers). We have no shared
  HTTP toolbelt yet. Step 2 either inlines `httpx` or proposes one.
  Britten suggests inlining for v0 and refactoring later if a third
  REST client appears.

### 8.3 Risks and mitigations

| Risk | Mitigation |
|---|---|
| ADO REST shape changes break `AdoClient` silently. | Same defence as `GhClient`: typed-error taxonomy + explicit field requesting + integration test against a recorded fixture. Mirror `tests/clients/test_gh.py`'s pattern. |
| The remediation loop adds two new failure modes (`max_remediation_cycles`, `remediation_planner.bad_output`) without distinct verdict-card narration. | Add details-dict discrimination (already the pattern at `pr_lifecycle.py:802-815`); extend the verdict-card builder in step 5. |
| Two platforms produce subtly different `mergeable` semantics ADO's `mergeStatus` ≠ GH's `mergeable_state`. | Translation lives in the platform impl; the workflow only sees the agnostic enum. Document the translation table in a new section of the workflow module docstring. |
| `AzureDevOpsPrPlatform.git_push` needs to authenticate differently than GitHub. | Use the same `git push` subprocess pattern as GitHub; auth flows through git's credential helper (already what `polyphony pr` does today). Document the env-var requirement in `getting-started.md`. |

### 8.4 Deferred / future

* **Option D (open-and-wait + drift-watcher split)** stays on file.
  Revisit when an operator complaint exists about long-running
  `pr_lifecycle` blocking other work.
* **Polyphony-style human escalation gate** (§5.5 counter-argument).
  Revisit only if surrender-on-cap proves operationally insufficient.
* **Twig's `pullRequests` field gap** (Mahler-3 issue #30). Independent
  of this ADR; close_out's recovery fallback (gh-pr-list-search) is
  the immediate workaround.
* **A third platform** (Bitbucket, GitLab). Out of scope; the Protocol
  shape makes this additive when the day comes.

---

## 9. Open questions for Daniel

The six numbered questions are direct restatements of the prompt;
Britten's positions in §5 are *recommendations*, not decisions, until
Daniel rules.

### 9.1 The six prompt questions — disposition matrix

| # | Question | Britten's recommendation | Daniel: confirm / override |
|---|---|---|---|
| 1 | `feature_pr` separate workflow? | NO — drift in `implementation.py`, remediation in `pr_lifecycle.py` (§5.1). | ☐ confirm  ☐ override |
| 2 | `pr_platform_router → pr_lifecycle_{gh,ado}` right? | NO — single workflow + `PrPlatform` Protocol (§5.2). | ☐ confirm  ☐ override |
| 3 | Where does remediation planner live? | In-workflow loop; remediation MG re-uses `implementation.py` via `SubWorkflowNode` (§5.3, option b). | ☐ confirm  ☐ override (option a in §5.3 is the fallback) |
| 4 | Contract with `close_out`? | `pr_lifecycle` writes `pr_number`; `close_out` reads it as primary; `resolve_pr` scan + gh-pr-list-search become recovery fallbacks (§5.4). | ☐ confirm  ☐ override |
| 5 | Review-loop termination? | Surrender-on-cap (four-way) without an escalation human-gate (§5.5). | ☐ confirm  ☐ override (escalation gate is the counter-position) |
| 6 | ADO PR surface — where? | New `AdoClient` (REST via `httpx`), not twig, not `az` (§5.6). | ☐ confirm  ☐ override |

### 9.2 Genuine open questions Britten cannot answer alone

#### Q-A. PAT vs OIDC for `AdoClient` auth

> **CLOSED 2026-06-01 during Bruckner walkthrough.** Resolution:
> **OIDC is required.** PATs are not supported in Daniel's primary
> ADO org; `AZURE_DEVOPS_EXT_PAT` is not a viable v0 path for the
> #1 customer scenario. Wire `azure-identity` + `DefaultAzureCredential`
> as the credential-discovery step for `AdoClient`. Britten's draft
> recommendation and Mahler-3's "today-workflow uses PATs" evidence
> are both superseded by this decision.
>
> **Daniel's framing:** *"OIDC is the scenario I need to support,
> PAT is not supported in my org."*
>
> **Implementation hook:** wrap the credential source in a
> `CredentialProvider`-shaped seam inside `AdoClient` so PAT-based
> envs (e.g. local dev outside the locked-down org, or
> non-OIDC-enabled tenants) remain a possible additive option
> without re-plumbing the client. Default = OIDC; PAT = additive
> fallback only.
>
> **Open follow-up (NOT this ADR's call):** if the work-item-side
> surface (`twig.comment_async`, `twig.update_item_async`)
> authenticates to ADO via PAT, that path is *also* broken in the
> primary v0 org and the Q1/Q3 orthogonality story has a parity
> gap. Three possibilities, listed for resolution:
> (a) `twig` already has an OIDC path Bruckner / Mahler didn't
> capture; (b) Daniel doesn't run `twig` against this org;
> (c) the work-item-side credential story needs its own ADR /
> twig-side fix before v0 ships. Tracked as a parking-lot item
> until Daniel confirms which is true.

`GhClient` doesn't manage auth — it delegates to `gh auth`. The
equivalent for ADO is either `AZURE_DEVOPS_EXT_PAT` (a PAT in env) or
federated OIDC via `az login`. Daniel's single-operator workflow today
uses PATs (per Mahler-3 §2.4 evidence on twig). Confirm we ship v0
with `AZURE_DEVOPS_EXT_PAT` and defer OIDC, or call out OIDC as a
blocker now.

**Britten's lean:** PAT for v0, document the env var in
`getting-started.md`. OIDC is a post-v0 feature.

#### Q-B. Drift-integration timing

When MG topology lands (Mahler-3 §9 non-negotiable #7, post-v0), where
does the `rebase feature/<root> onto main` verb live? Three places:

(i) `implementation.py` exit — before `create_pr`.
(ii) A new `merge_group.py` workflow — the polyphony-equivalent of
`implement-merge-group.yaml`.
(iii) `pr_lifecycle.py` entry — right after `fetch_pr`, before
`request_review`.

(iii) is closest to polyphony's `integrate_target_drift` node. (i) is
the simplest. (ii) is the right place when MG lands. Britten's §5.1
position assumes (i) for v0 and (ii) post-v0 — but this is unresolved
because MG is not in scope yet. Flag for the MG-topology ADR (Wave 6
sibling, mg-rethink worktree).

#### Q-C. What does "the platform" mean for git operations?

> **CLOSED 2026-06-01 during Bruckner walkthrough.** Resolution:
> Option (i) — single `PrPlatform` Protocol selects the PR host (ADO
> primary, GitHub supported). PR host and work-item tracker are
> orthogonal concerns; v0 models the PR host with a Protocol and the
> work-item tracker implicitly (always via `twig`). The "GH PR + ADO
> work item" topology falls out for free as `platform="github"` with
> the operator's `twig` config pointed at ADO. If a non-`twig`
> work-item tracker ever appears (or we want multiple in one run),
> that's the trigger to introduce a second Protocol
> (`WorkItemPlatform`). Daniel's framing: "I think it's somewhat
> orthogonal, yeah" — orthogonality is real and documented, but does
> not earn a second Protocol today.
>
> **Vocabulary clarification for §3:** the audit row's phrase "ADO
> PR lifecycle" means *PRs hosted in Azure Repos*. The
> "GH-PR-with-ADO-work-item" topology — Daniel's secondary scenario —
> is `platform="github"` with twig-side reporting, not a distinct
> platform value. ADO-end-to-end is the primary v0 target.

`PrPlatform.git_push` today is a method on the Protocol because GitHub's
PR is on a branch the local repo can push to. For ADO, the same model
works for repos hosted in Azure Repos (push to
`https://dev.azure.com/{org}/{project}/_git/{repo}`). What about
**ADO work items linked to a GitHub PR** (common in Daniel's setup, per
the EMU split documented in `gh.py:43-47`)? The PR-lifecycle drives a
GitHub PR; the work item is in ADO. Is that:

(i) `platform="github"` with twig integration for the work-item
comment (status quo);
(ii) `platform="github+ado"` — a third mode;
(iii) two `PrPlatform`s composited?

**Britten's lean:** (i). The PR's *location* is the platform; the
work-item integration is separate (handled by `update_item` /
`twig.comment_async`). This was already the case in
`pr_lifecycle.py:885-896`; Daniel should confirm that's what he meant
by "ADO PR lifecycle" or call out a richer requirement.

#### Q-D. Should the remediation planner read the PR's review comments, or only the reviewer's *summary*?

Polyphony's `remediation_planner` (`feature-pr.yaml` lines 804+) reads
the full git diff plus the reviewer's structured feedback. Our
synthesize_comments agent (`pr_lifecycle.py:707-725`) already produces
a structured `CommentSynthesis` per cycle. Does the remediation
planner read:

(i) the latest `CommentSynthesis` only;
(ii) the synthesis + the merged set of all PR comments ever posted;
(iii) the synthesis + the full `git diff origin/main..HEAD`.

(iii) is most informative; (i) is cheapest; (ii) is in between.

**Britten's lean:** (iii) for the planner agent (it's planning,
context matters); (i) for the addresser agent (it's executing, context
should be tight). But this is a prompt-engineering call as much as an
architecture call; the workflow shape supports any of the three.

### 9.3 Counter-position on §5.5: explicit escalation gate

If Daniel prefers polyphony's explicit `human_gate` ("retry / abort /
override") over surrender-on-cap, the architectural delta is small:
replace `terminate("needs_human_end")` for the two iteration caps with
a `HumanGateNode("escalate", options=("retry", "extend_cap", "abort"))`,
and route `retry` back to `poll_review`, `extend_cap` to a verb that
bumps `max_iterations`, `abort` to terminate. The cost is one extra
node and an asymmetry between in-workflow caps and protocol-error caps
(which would still surrender directly).

Britten's lean stays "no gate" because the operator surrender path
(`requiem resume` after editing inputs, or `requiem run … --max-iterations N`)
delivers the same flexibility without a node that overloads
"escalation" with "configuration tuning." But this is judgement-shaped
and Daniel's call.

### 9.4 Sequencing question

Mahler-3 §5's path-to-GO sequences ADO PR (step 4) **after** MG
topology (step 3) and worktree (step 5). The decision in §7 of this
ADR is platform-only and does not depend on MG topology — meaning step 4
*could* land before step 3 if Daniel wants ADO parity for the
single-leaf demo before MG. Confirm the sequencing.

**Britten's lean:** ADO platform support is independently valuable
(unblocks the dogfood demo on ADO-hosted repos) and the §7 decision is
not blocked on MG. Take steps 1-4 of §7 immediately; steps 5-6 follow
when their dependencies land.

---

## 10. References

* `docs/north-star.md` — INV-SINGLE-PROCESS, INV-RESTART,
  INV-NO-CORRUPT-FORWARD, INV-EVENT-LOG-AUTHORITATIVE,
  INV-DISCRIMINATED-OUTCOMES, INV-CANCEL-SHORT-CIRCUITS-RETRY,
  INV-NO-ENGINE-ABANDONMENT, INV-SUBWORKFLOW-LOG-ISOLATION.
* `docs/decisions/0001-single-process-architecture.md` — the seam this
  ADR builds on.
* `docs/decisions/0004-cross-cutting-defaults.md` — discriminated-outcome
  shape and the closed `error_kind` enum the remediation loop must
  honour.
* `docs/decisions/0005-subworkflow-invocation-primitive.md` — the
  primitive the §5.3 (b) recommendation depends on.
* `docs/references/v0-parity-readiness.md` — Mahler-3, esp. §2.2
  workflow catalogue (rows `feature-pr`, `github-pr`, `ado-pr`), §2.4
  external integrations, §4.1 issues #29/#30/#31, §4.7 long-poll
  ceiling, §5 path-to-GO.
* `docs/references/polyphony-parity-inventory.md` — esp. §3
  workflow catalogue (`feature-pr.yaml`, `github-pr.yaml`, `ado-pr.yaml`
  entries) and the `pr/*` verb group at §1.
* `docs/references/error-handling-deep-dive.md` row (d) — reviewer-agent
  hallucination defence; row (e) — `ManifestPlanLedger` PR idempotency
  pattern, the gold standard the remediation loop should track.
* `docs/references/error-deep-dive-ravel-review.md` — L-1 "unknown gh
  exit-1 is NeedsHuman" caveat applied at every platform-call site in
  the proposed `PrPlatform`.
* `src/requiem/workflows/pr_lifecycle.py` (1167 LOC) — current GH-only
  workflow; the `PrToolkit` Protocol at lines 209-229 is the seed of
  the proposed `PrPlatform`; the loop-cap pattern at lines 781-824 is
  the model for the outer remediation cap.
* `src/requiem/workflows/implementation.py:818-902` — `create_pr` verb;
  must grow an `existing_branch` mode per §5.3 step 5.
* `src/requiem/workflows/close_out.py:390-432` — `resolve_pr` verb;
  becomes a recovery path under §5.4.
* `src/requiem/workflows/full_sdlc.py:278-313` — the `_CROSS_STAGE`
  PR-number cell; becomes documented contract under §5.4.
* `src/requiem/clients/gh.py` (425 LOC) — the model for the new
  `AdoClient`.
* `src/requiem/clients/twig.py` (265 LOC) — *not* extended by this ADR;
  Mahler-3 issue #30 (PR-link surfacing) handled separately.
* `tests/test_pr_lifecycle_workflow.py` (14 tests) — peers required for
  the ADO branch under step 3.
* `C:\Users\dangreen\projects\polyphony\.conductor\registry\workflows\feature-pr.yaml`
  (v2.4.8) — reference for what the remediation loop's polyphony peer
  does. **Do not port; read.**
* `C:\Users\dangreen\projects\polyphony\.conductor\registry\workflows\github-pr.yaml`
  (v2.5.0) — reference for the GH-specific lifecycle shape.
* `C:\Users\dangreen\projects\polyphony\.conductor\registry\workflows\ado-pr.yaml`
  — reference for the ADO-specific lifecycle shape; the API endpoints
  this YAML invokes are the shopping list for `AdoClient`.

---

*End of draft ADR-0007 — Britten, Wave 6.*
*Open for Daniel's six rulings in §9.1 and four follow-ups in §9.2.*
