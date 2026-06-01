# Wave 7 — Bruckner briefing: 11 open questions across ADR-0006 and ADR-0007

> **Audience:** Daniel.
> **Purpose:** the decision-grade synthesis of the 11 open questions raised in
> [ADR-0006 (merge-group topology)](../decisions/0006-merge-group-topology.md)
> "Open questions for Daniel" and
> [ADR-0007 (PR-lifecycle architecture)](../decisions/0007-pr-lifecycle-architecture.md)
> §9.2. Read this end-to-end (≈30 min), then circle back to one question at
> a time. Each question is self-contained; the ADRs are linked when you
> want the long form.
> **Author:** Bruckner (synthesis seat, Wave 7).
> **Not authoritative.** Decisions go back into the ADRs and the open
> questions close there, not here.

> ---
>
> **META (added 2026-06-01 during walkthrough):**
>
> 1. **ADO-first orientation.** Requiem's #1 customer scenario is pure
>    ADO. GitHub is supported, not the default. Where the briefing's
>    existing prose says "GitHub PR" or assumes `gh` as the default,
>    read "ADO PR by default; GitHub when configured." Q3/Q4 onwards
>    are framed ADO-first explicitly.
> 2. **Curator-policy pattern (from Q1).** Several questions below
>    have "always vs never" binaries that are actually staged: (a) do
>    we *build the surface*? (b) what's the *agent-evaluated policy*
>    that decides when to use it? The policy half lands in
>    **ADR-0008 (curated artifacts across the run lifecycle)** —
>    seeded by Q1, scope expanded by Q2 to cover plan PR cut + gate +
>    persist, in addition to impl-slice review-surface curation.

---

## Table of contents

| # | Slug | Source | Decision needed (one line) |
|---|------|--------|---------------------------|
| Q1 | [non-negotiable-7 reading](#q1-non-negotiable-7-reading-strict-mg-prefix-vs-load-bearing) | ADR-0006 OQ1 | **CLOSED 2026-06-01: Option D (load-bearing reading). See "Q1 — closed" below.** |
| Q2 | [plan-PR realisation](#q2-plan-pr-real-branchpr-vs-markdown-in-the-feature-pr) | ADR-0006 OQ2 | **CLOSED 2026-06-01: build the plan-PR capability; cut/gate/persist policy → ADR-0008. See "Q2 — closed" below.** |
| Q3 | [platform meaning for git](#q3-what-does-the-platform-mean-for-git-operations) | ADR-0007 Q-C | Is "platform" the PR-host only (GitHub) with twig-side work-item integration, or a richer cross-platform composite? |
| Q4 | [ADO auth: PAT vs OIDC](#q4-pat-vs-oidc-for-the-adoclient) | ADR-0007 Q-A | Ship the ADO client with PAT (env var) for v0, or block on OIDC? |
| Q5 | [atomic co-merge recovery](#q5-how-strict-is-the-atomic-co-merge-guarantee-when-one-leaf-fails-mid-trunk) | ADR-0006 OQ3 | Abandon-trunk-and-restart, or roll-forward with a human gate, when one leaf fails after siblings landed? |
| Q6 | [replan mid-flight in scope](#q6-is-replan-mid-flight-in-scope-for-v0) | ADR-0006 OQ4 | Is mid-flight replan in scope for v0 (implies stable planner-declared item ids)? |
| Q7 | [review-group labels](#q7-should-the-planner-emit-review_group-labels) | ADR-0006 OQ5 | Does the planner emit `review_group: "data-layer"`-style UI labels (no branch impact)? |
| Q8 | [same-root run-lock UX](#q8-same-root-run-lock-ux-refuse-or-attach) | ADR-0006 OQ6 | Refuse-or-attach (polyphony's behaviour) or plain refusal when a fresh run hits an existing trunk? |
| Q9 | [drift-rebase location](#q9-where-does-the-rebase-featureroot-onto-main-verb-live) | ADR-0007 Q-B | Where does `rebase feature/<root> onto main` live — `implementation.py` exit, a new `merge_group.py`, or `pr_lifecycle.py` entry? |
| Q10 | [leaf-only degenerate case](#q10-should-featureitem_id-survive-for-the-leaf-only-root) | ADR-0006 OQ7 | When the root *is* a leaf, do we collapse to today's `feature/<item>` shape or pay the consistency tax? |
| Q11 | [remediation planner context](#q11-what-context-does-the-remediation-planner-read) | ADR-0007 Q-D | Does the remediation planner read the latest `CommentSynthesis` only, +all PR comments, or +the full diff? |

### Dependency graph (which question blocks which)

```
Q1 (non-negotiable #7 reading)
 ├─ if A: closes Q2, Q5, Q6, Q7, Q8, Q9, Q10 by reference to ADR-0006 §A
 └─ if D: unblocks Q2, Q5, Q6, Q7, Q8, Q9, Q10
                    │
                    ├─ Q2  (plan-PR shape) ─────────► informs Q11's context budget
                    ├─ Q5  (atomic co-merge recovery) ► informs Q8 (lock UX)
                    ├─ Q6  (replan in scope?) ──────► gates Q7 (review-group labels)
                    ├─ Q9  (drift-rebase location) ── independent once Q1=D
                    └─ Q10 (leaf-only special case) ─ independent once Q1=D

Q3 (platform meaning for git) ── independent of Q1
Q4 (PAT vs OIDC) ─────────────── independent of Q1
Q11 (planner context) ────────── lightly informed by Q2; otherwise independent
```

**Practical reading order:** answer Q1 first (it can collapse seven of the
remaining ten); Q3 and Q4 can be answered in parallel to anything else
because they live in the ADR-0007 lane; Q11 is the smallest blast radius
and can sit at the end.

---

## Q1: Non-negotiable #7 reading — strict `mg/` vs load-bearing

> **STATUS: CLOSED 2026-06-01 during Bruckner walkthrough.**
>
> **Resolution: Option D (load-bearing reading).** The audit row's wording
> is amended from `"Merge-group implementation (mg/, impl/) with
> idempotent re-entry"` to `"Per-item review + integration surface with
> idempotent re-entry"`.
>
> **Reframe captured (Daniel's framing):** MGs were really about
> *reviewable sets*. Some PRs are small enough to auto-merge without
> human review; some should not even open a PR at all. The north star is
> "surface reviews to the user that they're likely to want to care
> about, at a size that is reasonable" — and that decision should be
> **agent-tunable**, not a static config knob.
>
> **Cascade:** This separates two concerns. ADR-0006 (this file) owns
> **branch topology** (the chassis). A new **ADR-0008 (review-surface
> curation)** will own the **reviewable-set policy** (which diffs we
> surface for review, how, at what size). Q5, Q7, Q10, and Q2 below all
> have their framing shifted by the reframe; the inline notes in those
> sections capture how. ADR-0008 will be dispatched as a Wave 7 design
> seat after the remaining Q's are resolved (Q7 is the natural touch
> point).
>
> See [ADR-0006 → Decision log → Q1](../decisions/0006-merge-group-topology.md)
> for the canonical record.

---

### § Decision needed (historical)
Does Mahler-3's non-negotiable #7 ("Merge-group implementation (`mg/`,
`impl/`) with idempotent re-entry") require the literal `mg/` branch
prefix — committing us to ADR-0006 Option A — or a load-bearing reading
("per-leaf review surface + integration surface") that Option D satisfies?

### § Context
- The audit's verbatim language is "Merge-group implementation (`mg/`,
  `impl/`) with idempotent re-entry"
  ([`docs/references/v0-parity-readiness.md:166`](../references/v0-parity-readiness.md)).
- Stravinsky's Option D ships `feature/<root>` + `impl/<root>-<item>` +
  optional `plan/<root>`. **No `mg/` prefix.** Option A ports polyphony's
  full Rev 4 model verbatim, including nested `mg/{root}_{path}_…`
  branches.
- Stravinsky's whole "wheat-vs-chaff" classification
  ([ADR-0006 lines 116-132](../decisions/0006-merge-group-topology.md))
  rests on the argument that polyphony needed nested `mg/` branches as a
  *state substrate* because conductor's checkpoints are ephemeral
  ([ADR-0006 lines 134-154](../decisions/0006-merge-group-topology.md)).
  Requiem has **INV-EVENT-LOG-AUTHORITATIVE** (north-star §2) and
  **INV-SUBWORKFLOW-LOG-ISOLATION** (north-star §2, ADR-0005). Branches
  don't have to carry workflow state any more.
- Cost delta: D is ~10 working-days for one seat; A is ~3-4× that plus
  operator-doc work
  ([ADR-0006 lines 481-494](../decisions/0006-merge-group-topology.md)).
- **What's NOT in scope:** the recovery model (Q5), the lock UX (Q8),
  whether the plan PR is real (Q2). Those all assume the topology is
  decided.

### § Options + trade-offs

| Option | One-line | Trade-offs |
|---|---|---|
| **Strict (Option A)** | Audit text is law; ship the literal `mg/` prefix and the rest of polyphony Rev 4 with it. | + Full §9 parity. + Path to multi-tenant later. − Imports the cost polyphony pays for branches-as-state. − Erodes the INV-SINGLE-PROCESS / INV-EVENT-LOG-AUTHORITATIVE payoff. − 3-4× cost. |
| **Load-bearing (Option D)** | Audit text means "per-leaf review + integration surface"; Option D satisfies it. Renegotiate the audit row. | + ~10-day budget; all load-bearing problems closed. + Aligns with the architectural bet of Requiem. + Migration to A later is *additive*. − Requires you to formally amend Mahler-3 non-negotiable #7 (one-line revision; this ADR is the right place). |

**Stravinsky's recommendation:** load-bearing (Option D), and he says so
explicitly at [ADR-0006 lines 437-473](../decisions/0006-merge-group-topology.md).
He flags this as the gating call.

### § Executive brief
What this question is really about: do you want Requiem to be "a simpler
thing that replaces polyphony", or to be "polyphony reimplemented in
Python"? The audit was written before ADR-0005 made INV-SUBWORKFLOW-LOG-ISOLATION
law. Once that landed, the *reason* polyphony needs nested `mg/` branches
— branches-as-state-substrate, because conductor's checkpoints are
ephemeral — stopped applying to us. The question on the table is whether
the audit's literal text outranks the architectural rationale beneath
it. Stravinsky believes it does not, and recommends amending the audit
row's wording to "per-item review + integration surface". You wrote the
audit; you can amend it.

What's at stake: this is the most reversible-feeling question that is
actually the least reversible, because every other ADR-0006 question
collapses or expands depending on the answer. Picking A commits us to
nested-MG recursion, topology-hash machinery, cross-MG rebase, the
three-delimiter discipline, and the operator surface to teach all of it —
a ~30-40 day chunk of work before any of it produces operator value
beyond what D produces in ~10. Picking D commits us to renegotiating one
audit-row line and accepting that "no `mg/` prefix" is the on-disk shape
operators will see. **Lower-regret default: D.** A is the wrong way to be
wrong here because the migration D → A is additive while A → D is not
(branches are one-way doors, polyphony skill line 628 cited in
[ADR-0006 lines 475-478](../decisions/0006-merge-group-topology.md)).

---

## Q2: Plan PR — real branch+PR vs markdown in the feature PR

> **STATUS: CLOSED 2026-06-01 during Bruckner walkthrough.**
>
> **Resolution: Build the plan-PR capability for v0** (`plan/<root>`
> branch + PR-cut verb against the feature trunk). Whether to use it
> on any given run, and what happens to its content downstream, is an
> **agent-evaluated × user-policy** decision deferred to ADR-0008.
>
> The original Q2 binary ("real PR vs virtual markdown") was a false
> dichotomy. Daniel's framing collapsed it into three staged decisions:
>
> | Stage | Question | Owner |
> |---|---|---|
> | **Cut** | Should we open a plan PR for *this* run? | Agent evaluates initial-planning complexity × user policy. If below threshold → no plan PR, keep going. |
> | **Gate** | If cut, impl waits on its trunk merge. | Deterministic from the cut decision. INV-PLAN-PR-PRECEDES-IMPL is reworded "*if* a plan PR is cut, impl waits on its trunk merge." |
> | **Persist** | When the feature trunk merges to main, do plan docs ride along, get transformed (squash to an as-built summary), or get stripped? | Agent evaluates "does this still add post-impl value?" × user policy. |
>
> **Why "build the surface" is the v0 call:** without the capability,
> the curator has no lever to pull and the gate/replan shape is
> foreclosed forever. With the capability, the curator can decide
> per-run whether to use it (including "skip for trivial roots,
> auto-confirm without operator review"). Same ~1-day cost as
> Stravinsky's original recommendation; what changes is *when* it
> gets used.
>
> **ADR-0008 scope expansion (this question grew it):** the ADR now
> covers **curated artifacts across the run lifecycle**, not just
> impl-slice review-surface curation. The pattern:
>
> | Family | Cut? | Gate? | Persist? |
> |---|---|---|---|
> | Plan | curator | conditional on cut | curator |
> | Impl slice | `auto_merge` / `local_review` / `pr_review` | depends on review intent | always (it's the code) |
> | *(future)* design docs, ADRs, scratch notes | same pattern | same pattern | same pattern |
>
> **Replan carve-out:** replan policy may differ from initial-plan
> policy. Punted to Q6 (replan in scope for v0). If Q6 = "no replan
> in v0," replan-curator policy doesn't need to be settled today.
>
> See [ADR-0006 → Decision log → Q2](../decisions/0006-merge-group-topology.md)
> for the canonical record.

---

### § Decision needed (historical)
Is the plan PR a real `plan/<root>` branch with its own GitHub PR
(reviewable in the browser before any impl branch is cut), or markdown
rendered into the feature-PR's description body?

### § Context
- Today, `planning.py` writes a `<run_id>.plan.tree.json` sidecar
  ([`src/requiem/workflows/planning.py:160-176`](../../src/requiem/workflows/planning.py)
  for `PlanResult`) but opens no PR.
- Mahler-3 audit flags "Plan PR open/merge ❌" as half of non-negotiable
  #6
  ([`docs/references/v0-parity-readiness.md:165`](../references/v0-parity-readiness.md)).
- Polyphony ships a real plan PR; per the parity inventory and ADR-0006's
  framing, it is "the most operator-loved feature in dogfood"
  ([ADR-0006 line 120](../decisions/0006-merge-group-topology.md)).
- Option D names it as a load-bearing element: `plan/<root>` branches off
  the feature trunk, opens a PR against the trunk, merges before impl
  branches are cut (INV-PLAN-PR-PRECEDES-IMPL,
  [ADR-0006 lines 356-358](../decisions/0006-merge-group-topology.md)).
- **What's NOT in scope:** the plan PR's *content* (the planner's prompt
  engineering is its own problem); whether multiple plan PRs can stack
  (replan, see Q6).

### § Options + trade-offs

| Option | One-line | Trade-offs |
|---|---|---|
| **Real plan PR** | A `plan/<root>` branch with the JSON-rendered-to-markdown plan as the diff; opens a PR against `feature/<root>`. | + Operator reviews the plan in the GitHub UI exactly like code. + Polyphony's dogfood-validated UX. + Closes audit non-negotiable #6's plan-PR half. − One more branch, one more PR per run; new `plan_pr` verb (~1 day per ADR-0006 cost table line 485). |
| **Virtual plan (markdown in feature-PR body)** | The plan is rendered into the feature PR's `--body` at trunk-open time. | + Zero new branches; no `plan_pr` verb. + Single PR-per-run for small features. − Plan is reviewable only *after* the feature PR opens, which is *after* impl work has begun under D. − Defeats INV-PLAN-PR-PRECEDES-IMPL — you can't gate impl on plan-approval if the plan and impl share one PR. − Audit row #6 stays partial. |

**Stravinsky's recommendation:** real plan PR
([ADR-0006 OQ2, lines 568-571](../decisions/0006-merge-group-topology.md)).
He flags this as the choice he is most confident on among ADR-0006's
seven OQs.

### § Executive brief
What this question is really about: where in the operator's day does the
"is the plan good?" decision get made? With a real plan PR, the operator
sees the plan in the GitHub UI before *any* coder agent has burned tokens
on it; comments on the plan PR are how the operator and the planner
negotiate; the impl phase only opens once the operator hit "Merge plan
PR." With a virtual plan, the plan becomes a section in a body the
operator skims after the work is already done — review-shaped, not
gate-shaped. The polyphony dogfood evidence is that the gate shape is
the entire reason operators trust the pipeline.

What's at stake: real plan PR costs roughly one day of engineering and
adds one new branch type to teach. Virtual plan saves that day and
forecloses INV-PLAN-PR-PRECEDES-IMPL; if you later want the gate, you
have to retrofit it through a workflow re-entry. Real plan PR also
unlocks the natural shape for replan (Q6) — a second plan PR against the
trunk, easy to reason about. **Lower-regret default: real plan PR.** It
is the part of polyphony's UX that survived its own complexity and the
audit explicitly flagged it as a parity gap. Going virtual would be
deciding to half-ship non-negotiable #6.

---

## Q3: What does "the platform" mean for git operations?

### § Decision needed
When `PrPlatform` is selected as `"github"` or `"ado"`, does that selection
refer only to where the PR lives (and the work item is handled separately
via twig), or does Requiem need a richer composite mode for "GitHub PR
linked to ADO work item" — which is your common setup per the EMU split
in [`src/requiem/clients/gh.py:43-47`](../../src/requiem/clients/gh.py)?

### § Context
- Today, `pr_lifecycle.py` is GH-only and the work-item side is handled
  by `update_item` ([`src/requiem/workflows/pr_lifecycle.py:885-896`](../../src/requiem/workflows/pr_lifecycle.py)),
  which posts via `twig.comment_async`. The PR's *location* (GitHub) and
  the work-item's location (ADO) are already orthogonal in the current
  code path.
- Britten's §5.6 recommendation is a new `AdoClient` for the ADO PR
  surface ([ADR-0007 lines 395-440](../decisions/0007-pr-lifecycle-architecture.md))
  — when the PR *itself* lives in Azure Repos.
- Your real-world topology often has the PR in GitHub (under EMU) and
  the work item in ADO. That isn't "the ADO PR lifecycle" — it's the
  status quo with twig-side reporting.
- **What's NOT in scope:** what `AdoClient` looks like (Q4 partly), or
  whether the ADO REST or `az` CLI is the substrate (Britten closed that
  in §5.6 — REST via `httpx`).

### § Options + trade-offs

| Option | One-line | Trade-offs |
|---|---|---|
| **(i) Platform = PR host only** (status quo) | `platform="github"` covers GH-PR + ADO-workitem via the existing `update_item` → `twig` split. `platform="ado"` is for Azure-Repos-hosted PRs only. | + No new architectural concept. + Matches your common topology today. − "ADO PR lifecycle" means *exclusively* Azure-Repos-hosted PRs, which is a smaller use case than the audit phrasing suggested. |
| **(ii) Platform = `"github+ado"` composite** | A third platform mode that knows both surfaces. | − Introduces a mode whose only job is "delegate to the other two." − Forces every platform-checking site to handle three values when two would do. + Could expose "where do reviewer comments land?" cleanly. |
| **(iii) Two `PrPlatform`s composited** | The workflow holds a `PrPlatform` *and* a separate `WorkItemPlatform`. | + Cleanest separation of concerns. − Refactor on the work-item side (today implicit in `twig.comment_async` calls). + Aligns with Britten's §5.4 close-out contract — `pr_number` is the PR-side artefact; the work-item side stays orthogonal. |

**Britten's recommendation:** (i)
([ADR-0007 lines 796-801](../decisions/0007-pr-lifecycle-architecture.md)).

### § Executive brief
What this question is really about: is "ADO PR lifecycle" a story about
Azure Repos as a code-hosting platform, or a story about ADO as a
work-item-tracker that happens to link back to a PR somewhere? Britten
read the audit as the former. Your daily setup is the latter. The
difference matters because if "ADO PR lifecycle" really means
"Azure-Repos-hosted PRs," then writing `AdoClient` is genuinely
unblocking new operator scenarios. If it means "the work-item-tracker
side of every PR you ship," then `AdoClient` is mostly aspirational and
the actual work is `twig`'s `pullRequests` field gap (Mahler-3 issue
#30).

What's at stake: confirming (i) lets Britten's §7 plan proceed unchanged
(`AdoClient` is a real net-new component; ADO-hosted PRs become a
supported topology). Picking (iii) would defer the `AdoClient` work and
re-prioritise the twig PR-link issue as the actual blocker. Picking (ii)
is the worst of both worlds. **Lower-regret default: (i), with one
sentence in ADR-0007 §3 clarifying that "ADO" means Azure-Repos-hosted
PRs and that GH-PR-with-ADO-work-item is the status quo's `platform="github"`
path.** This is a vocabulary clarification, not a redesign — but the
vocabulary matters because it sets the operator's expectation about
which scenario the new `AdoClient` unblocks.

---

## Q4: PAT vs OIDC for the AdoClient

### § Decision needed
Ship the new `AdoClient` reading `AZURE_DEVOPS_EXT_PAT` for v0 and defer
OIDC, or block on OIDC?

### § Context
- `GhClient` doesn't manage auth — it delegates to `gh auth`
  (425 LOC in [`src/requiem/clients/gh.py`](../../src/requiem/clients/gh.py)).
- The equivalent for ADO is either `AZURE_DEVOPS_EXT_PAT` (a PAT in env
  var, same one `az` and `twig` consume per Mahler-3 §2.4) or federated
  OIDC via `az login`.
- Your today-workflow uses PATs (per Mahler-3 §2.4 evidence on twig).
- North-star §5 lists single-operator audience as in-scope and
  "multi-operator simultaneous use" as out-of-scope for v0.
- **What's NOT in scope:** the rest of `AdoClient` (Britten settled REST
  via `httpx` in §5.6). PAT vs OIDC is *just* the credential-discovery
  step.

### § Options + trade-offs

| Option | One-line | Trade-offs |
|---|---|---|
| **PAT for v0, defer OIDC** | Read `AZURE_DEVOPS_EXT_PAT` from env. Document in `getting-started.md`. | + Matches your existing setup. + No new dependency. + ~30 LOC. − If you ever ship to a teammate, they need to mint a PAT (single-operator audience makes this fine). |
| **Block on OIDC** | Wire `azure-identity` + `DefaultAzureCredential` before any ADO work lands. | + Future-proof for org-wide rollout. − Adds an `azure-identity` dependency and the credential-chain complexity. − Delays v0 ADO support. − No operator-evidence today says PAT is insufficient. |

**Britten's recommendation:** PAT for v0
([ADR-0007 line 763](../decisions/0007-pr-lifecycle-architecture.md)).

### § Executive brief
What this question is really about: does v0 have to anticipate the
auth-modernisation conversation, or can it ship the credential model you
actually use today? The north-star §5 single-operator scope says it can
ship today's model. PAT is what `twig` and `az` already use; the
operator surface to teach is "set this env var" and the AzureDevOps
docs do that better than we ever could.

What's at stake: PAT is a five-minute decision; OIDC is a several-day
detour. The reversibility cost is low — both paths produce the same
typed-error taxonomy and the auth lives at exactly one entry point in
`AdoClient`, so swapping later is a localised refactor. The only way
PAT is wrong is if v0 ships to multi-operator environments before OIDC
lands, which north-star §5 forbids anyway. **Lower-regret default: PAT
for v0, with a one-line note in `AdoClient`'s docstring naming the OIDC
swap as the post-v0 follow-up.**

---

## Q5: How strict is the atomic co-merge guarantee when one leaf fails mid-trunk?

### § Decision needed
Under Option D, if leaf B fails to land on the trunk after leaf A
succeeded, do we (a) **abandon the trunk** — delete the branch and
re-run the root — or (b) **roll forward** — `needs_human` gate lets the
operator retry leaf B or skip-with-justification?

### § Context
- Option D's promise is "the feature trunk merges to `main` atomically
  or nothing does." But *individual leaf-impl PRs* still land on the
  trunk one at a time
  ([ADR-0006 §D, lines 366-394](../decisions/0006-merge-group-topology.md)).
- The failure mode in scope: leaf A's PR merges into the trunk; leaf B's
  PR fails (CI red, conflict on a sibling's commit, agent dies after N
  remediation cycles, etc.). The trunk now has A's code committed and no
  path forward without operator intervention.
- INV-NO-CORRUPT-FORWARD (north-star §2) — a workflow that suspects
  state corruption surrenders to a human gate; the engine never
  best-efforts past an unverified precondition.
- INV-NO-ENGINE-ABANDONMENT (north-star §2) — the engine does not
  initiate abandonment; retry exhaustion routes to *surrender*.
- Polyphony's analogue: `feature-pr.yaml`'s remediation cycle (the
  `feature-pr.yaml:804+` block — see [ADR-0007 lines 115-117](../decisions/0007-pr-lifecycle-architecture.md)).
- **What's NOT in scope:** which leaf to retry first (Q11's
  remediation-planner-context question); how the lock interacts with
  abandon-trunk semantics (Q8 is the lock UX).

### § Options + trade-offs

| Option | One-line | Trade-offs |
|---|---|---|
| **(a) Abandon trunk** | Operator deletes the trunk branch; re-runs root from scratch with a fresh trunk. | + Cleanest INV-RESTART story (no partial state to reconcile). + No new gate state. − Throws away successful leaf work; agent re-cost. − Operator UX is "your hour of agent time is now scrap." |
| **(b) Roll forward via human gate** | `NeedsHuman` gate with two options: "retry leaf B" or "skip-with-justification" (records the skip in the event log; trunk continues). | + Preserves leaf A's landed work. + Maps naturally to polyphony's remediation cycle. − Skip-with-justification is a real semantic — it means the feature ships *without* the skipped leaf's contribution; the close-out needs to know. − One new `HumanGateNode` plus the skip-event taxonomy. |
| **(c) Hybrid: roll-forward by default, abandon as the gate's third option** | The gate offers "retry / skip / abandon-and-restart." | + Operator picks. − Three-way gate is the kind of thing Britten warned about in §5.5 ([ADR-0007 lines 384-391](../decisions/0007-pr-lifecycle-architecture.md)) — overloads escalation with configuration. |

**Stravinsky's recommendation:** does not pick a side; both shipped
as legitimate options
([ADR-0006 OQ3, lines 573-580](../decisions/0006-merge-group-topology.md)).

### § Executive brief
What this question is really about: when the pipeline produces a
half-built trunk, do we treat that trunk as garbage to clean up, or as
state to negotiate with? Abandon-trunk treats it as garbage; the
operator's mental model is "the run is a transaction and the trunk is a
journal — failed transactions roll back." Roll-forward treats it as
state; the operator's mental model is "the trunk is a real branch with
real value on it and we negotiate the remaining leaves through a gate."
Polyphony's `feature-pr.yaml` is firmly in the roll-forward camp, with a
capped remediation cycle and a `human_gate` at the cap.

What's at stake: roll-forward (b) is more expensive to build (the
`needs_human` surface + skip-with-justification taxonomy + event-log
schema for the skip), but it preserves agent work and aligns with
polyphony's remediation pattern, which ADR-0007 §5.3 is already going to
need anyway. Abandon-trunk (a) is cheaper but turns a 6-leaf feature
where leaf 5 fails into a 6-leaf re-run, which dominates agent cost.
**Lower-regret default: (b), specifically the polyphony-cycle shape —
the remediation loop from ADR-0007 §5.3 is the natural place for the
"retry leaf B" arm, and the skip-with-justification arm is the second
human-gate choice.** That keeps the entire roll-forward story inside
`pr_lifecycle.py`'s remediation loop and avoids a second escalation
surface in the MG topology layer. If you can't decide today, the
roll-forward implementation strictly subsumes the abandon path — the
operator can choose abandon at the gate, so deferring (a) costs nothing.

---

## Q6: Is replan mid-flight in scope for v0?

### § Decision needed
Does v0 need to support mid-flight replan — the operator (or planner
agent) modifies the plan tree while impl branches are open against the
trunk — which would force stable planner-declared item ids and a
"refuse to rename" rule in the executor?

### § Context
- The audit
  ([`docs/references/v0-parity-readiness.md:155-169`](../references/v0-parity-readiness.md))
  does not list replan-mid-flight as a non-negotiable.
- Polyphony's machinery for this is heavy: stable MG ids
  (`^[a-z][a-z0-9-]{0,30}$`), topology hash, parent-plan-generation
  serialisation
  ([ADR-0006 lines 78-87](../decisions/0006-merge-group-topology.md)).
- Stravinsky's wheat-vs-chaff places stable IDs as
  "LOAD-BEARING IF P7 is in scope; OPTIMISATION otherwise"
  ([ADR-0006 line 123](../decisions/0006-merge-group-topology.md)).
- ADR-0006's Option D currently assumes "no replan-mid-flight"
  ([lines 393-395](../decisions/0006-merge-group-topology.md)).
- **What's NOT in scope:** how replan would actually be triggered (UI?
  CLI verb? planner re-running automatically?); that's a separate ADR
  if replan lands.

### § Options + trade-offs

| Option | One-line | Trade-offs |
|---|---|---|
| **No replan in v0** | Plan is closed once the plan PR merges. Re-running the root re-plans from scratch. | + Smallest topology surface. + No stable-id discipline needed. + Aligns with single-operator audience. − Operator must abandon-and-restart if the plan is wrong after impl begins. |
| **Replan in scope for v0** | Planner can re-emit a plan against an existing trunk; executor refuses to rename impl branches and uses planner-declared item ids as the key. | + Real polyphony-shape resilience to mid-stream discovery. − Forces stable-id discipline now (planner must declare `^[a-z][a-z0-9-]{0,30}$`-style ids); topology-hash gate becomes load-bearing for resume; cross-sibling rebase becomes a real concern; ~5-10 days of additional work. |

**Stravinsky's recommendation:** no replan in v0
([ADR-0006 OQ4, lines 582-585](../decisions/0006-merge-group-topology.md));
defer until a real use case appears.

### § Executive brief
What this question is really about: how often does the plan turn out to
be wrong *after* impl has started? In single-operator dogfood, the
answer is "rarely, and when it happens, abandon-and-restart is
acceptable." In multi-operator polyphony dogfood the answer was
different — replan was real, hence the machinery. Picking "no replan in
v0" is a bet that the single-operator audience won't hit this often
enough to justify carrying polyphony's stable-id discipline forward into
Requiem.

What's at stake: deciding "yes" buys polyphony's full topology-stability
story but costs ~5-10 days now and a layer of planner discipline (every
plan output must include `mg_id`-style stable ids; the executor must
refuse to rename in a way that's testable and idempotent under
INV-RESTART). Deciding "no" defers that work and lets us treat
implementable items by their natural ADO id; if replan turns out to be
necessary post-v0, we add stable ids as a planner-output extension
(additive). **Lower-regret default: no.** The decision is reversible at
the cost of a planner-output schema migration, which is small. Stable
ids without a replan use case are pure overhead.

---

## Q7: Should the planner emit `review_group` labels?

### § Decision needed
Even without nested `mg/` branches, should the planner emit a
`review_group: "data-layer"`-style field on each implementable child
([`src/requiem/workflows/planning.py:181-198`](../../src/requiem/workflows/planning.py)
for `ChildPlan` / `PlannerOutput`), so the dashboard can cluster impl
PRs by group? Nothing changes in git.

### § Context
- Option D's P5 (reviewer cognitive load) is "partially solved" by the
  feature trunk alone; for large fan-outs, a UI grouping helps
  ([ADR-0006 lines 376-378](../decisions/0006-merge-group-topology.md)).
- This is a planner-output schema addition + a dashboard-side render.
  Zero impact on branch naming, no new gates.
- ADR-0006 lines 660-661 cite this as a candidate addition to
  `ChildPlan`.
- **What's NOT in scope:** the dashboard itself
  (non-negotiable #8, separate ADR); whether labels are validated against
  a closed enum; whether the planner is *required* to assign one.

### § Options + trade-offs

| Option | One-line | Trade-offs |
|---|---|---|
| **Yes, optional label** | Add `review_group: str \| None` to `ChildPlan`. Planner may or may not set it. Dashboard groups when set. | + Cheap (~half a day). + Useful for 10+-leaf features. + Doesn't commit topology. − Adds a planner-prompt knob to teach. |
| **No, defer** | Ship D without review-group labels; revisit when fan-out gets unmanageable. | + No new schema surface. − When a 20-leaf feature happens, the operator sees 20 ungrouped impl PRs in the dashboard. |
| **Yes, required label** | Planner must emit a `review_group`; absent → validation error. | + Forces useful structure. − Makes 2-leaf fan-outs awkward ("data-layer" + "ui-layer" for two PRs is silly). |

**Stravinsky's recommendation:** does not pick; flags as "cheap,
useful for big fan-outs, doesn't commit us to topology"
([ADR-0006 OQ5, lines 587-591](../decisions/0006-merge-group-topology.md)).

### § Executive brief
What this question is really about: do we plant the seed of UI-side
hierarchy now, so the dashboard work in Wave 7+ has something to render,
or do we wait until fan-out hurts? Labels-as-optional is one schema
field and a no-op for small fan-outs; the cost is the planner-prompt
copy. Labels-as-required forces a structure that doesn't make sense for
small features. Defer-until-pain leaves the dashboard work with no
clustering signal when it's authored.

What's at stake: this is a half-day decision with a forever-shelf-life
because the planner-output schema is forward-compatible — adding the
field later is harmless, but consumers (the dashboard) that hardcode an
absence pay migration cost. **Lower-regret default: yes, optional
label.** It's the smallest commitment that gives the dashboard wave
something to render and the planner a place to express groupings when it
sees them. If you're feeling lazy, defer is fine; the cost of adding it
later is the cost of a `git pull` and a planner-prompt edit.

---

## Q8: Same-root run-lock UX — refuse-or-attach?

### § Decision needed
When an operator starts a fresh run on a root that already has an open
`feature/<root>` trunk and unmerged impl PRs, what does Requiem do?
Polyphony's behaviour is "refuse-or-attach" — the run either refuses
with a hint or attaches to the existing trunk
([ADR-0006 OQ6 cites skill line ~494](../decisions/0006-merge-group-topology.md)).
Is that the right behaviour for the single-operator audience, or overkill?

### § Context
- Today, `root_dispatch.write_manifest`
  ([`src/requiem/workflows/root_dispatch.py:1-44`](../../src/requiem/workflows/root_dispatch.py))
  is idempotent read-or-create — a second dispatch reuses the existing
  manifest. No branch-level lock.
- Option D's INV-FEATURE-TRUNK-PER-RUN
  ([ADR-0006 lines 351-353](../decisions/0006-merge-group-topology.md))
  says one trunk per run; concurrent runs would stomp.
- INV-SINGLE-PROCESS (north-star §2) + single-operator audience
  (north-star §5) mean genuinely-concurrent dispatch is a rare event in
  v0 — but resume-after-crash makes it look concurrent if the operator
  isn't careful.
- This question interacts with Q5: if you picked roll-forward there, the
  "open trunk with unmerged impl PRs" state is a normal mid-run state;
  the lock needs to recognise resume vs fresh-start.
- **What's NOT in scope:** the lock implementation substrate (file lock?
  manifest entry? git-ref check?). That's an implementation detail; the
  question is the *UX*.

### § Options + trade-offs

| Option | One-line | Trade-offs |
|---|---|---|
| **Plain refusal** | Refuse with an error: "trunk exists; use `requiem resume <run_id>` or delete the trunk first." | + Simplest. + Operator decides every time. − Operator has to know the resume command. − Forces a manual decision on every re-dispatch. |
| **Refuse-or-attach (polyphony)** | Detect the existing trunk; if the manifest's run_id matches, attach (= resume); if not, refuse. | + Matches polyphony's behaviour; familiar. + One-command UX for the common "I crashed; restart" case. − One more code path. |
| **Attach by default, force-fresh flag** | Always attach if trunk exists; require `--force-fresh` to delete. | + Friendliest UX. − Risks accidental attachment to a stale trunk if the manifest isn't where you think it is. |

**Stravinsky's recommendation:** does not pick; calls it out as a UX
question for the single-operator audience
([ADR-0006 OQ6, lines 593-598](../decisions/0006-merge-group-topology.md)).

### § Executive brief
What this question is really about: when you (or your future-self next
week) re-runs the dispatch verb on a root that already has work in
flight, what should happen by default? The polyphony refuse-or-attach
shape was designed for multi-operator scenarios where "attach" is
genuinely safe because the lock is the only thing standing between two
operators stomping each other. In a single-operator world the lock's
job is mostly to prevent your own self from accidentally starting a
parallel run after forgetting the first one.

What's at stake: refusal is cheaper to ship (one error, no detect-then-attach
branch); refuse-or-attach is more polished and matches polyphony. The
reversibility is excellent — going from refusal to refuse-or-attach is a
strict UX upgrade and the lock substrate is the same. **Lower-regret
default: refuse-or-attach, polyphony-shape.** It is the one case where
"do what polyphony does" is genuinely the right answer because the lock
exists *specifically* for the operator-confusion case, and polyphony's
operators are the closest thing we have to your dogfood audience. The
cost delta is ~half a day and the operator UX is markedly better in the
single common case (resume-after-crash).

---

## Q9: Where does the `rebase feature/<root> onto main` verb live?

### § Decision needed
When MG topology lands (Q1 = D), where does the `rebase feature/<root>
onto main` drift-integration verb live? Three candidate hosts: (i)
`implementation.py` exit, (ii) a new `merge_group.py` workflow, or
(iii) `pr_lifecycle.py` entry.

### § Context
- Today there is no drift because we open the PR immediately after
  `commit_changes` in `implementation.py`
  ([ADR-0007 lines 226-230](../decisions/0007-pr-lifecycle-architecture.md)).
- Polyphony's `feature-pr.yaml:37` calls this `integrate_target_drift`
  and runs it at the top of the PR workflow.
- Britten's §5.1 position assumes (i) for v0 and (ii) post-v0 — but
  flags this as unresolved because MG isn't ratified yet (it depends on
  Q1).
- INV-RESTART: whichever host runs the rebase, the verb must be
  idempotent (re-running after a partial rebase must converge).
- **What's NOT in scope:** the rebase strategy (interactive? auto-resolve?
  surrender-on-conflict?). The host question is structural; strategy is
  implementation.

### § Options + trade-offs

| Option | One-line | Trade-offs |
|---|---|---|
| **(i) `implementation.py` exit** | New verb right before `create_pr`. | + Simplest. + Stays inside the per-leaf sub-workflow. − Doesn't fit the polyphony mental model. − When MG lands, each leaf rebases against the trunk anyway; the trunk-vs-main rebase is a *trunk-level* concern, not a leaf-level one. |
| **(ii) New `merge_group.py`** | The MG workflow (the missing Berlioz-Phase-D piece, [ADR-0006 line 488](../decisions/0006-merge-group-topology.md)) does drift integration at trunk-merge time. | + Matches polyphony's `implement-merge-group.yaml` shape. + Trunk-level drift belongs to the trunk-level workflow. − Requires `merge_group.py` to exist (it must under Option D anyway). |
| **(iii) `pr_lifecycle.py` entry** | Right after `fetch_pr`, before `request_review`. | + Closest to polyphony's `feature-pr.yaml:37`. − Conflates PR-driving with topology-managing. − Forces every platform impl to support the same rebase verb. |

**Britten's recommendation:** (i) for v0, (ii) post-v0
([ADR-0007 lines 776-780](../decisions/0007-pr-lifecycle-architecture.md)) —
defers the call because Q1 wasn't ratified.

### § Executive brief
What this question is really about: who owns the integration concern in
Requiem's vocabulary — the leaf workflow, the trunk workflow, or the PR
workflow? In polyphony the answer was "the PR aggregator" because that's
where the YAML composition allowed it. In Requiem, the trunk workflow
(`merge_group.py` per ADR-0006 Option D's cost table) is the natural
host because trunk-vs-main is a trunk concern, but `merge_group.py`
doesn't exist yet — it's part of the same ~10-day chunk Q1=D commits to.

What's at stake: if Q1 = A, this question is closed by ADR-0006 §A's
spec (drift lives in the `implement-merge-group` peer). If Q1 = D and
`merge_group.py` is being built anyway, drift should live there — option
(ii). Britten's (i) was a hedge against Q1 being unresolved at his
draft time. **Lower-regret default: (ii), conditional on Q1 = D.**
Putting it in `pr_lifecycle.py` (iii) ties topology to PR-platform,
which is exactly the conflation ADR-0007 §3 separated. Putting it in
`implementation.py` (i) puts trunk-level work in a leaf-level workflow
and forces re-entry semantics that don't naturally belong there. The
only argument for (i) is "we don't have `merge_group.py` yet" — but
under Q1 = D, we will.

---

## Q10: Should `feature/<item_id>` survive for the leaf-only root?

### § Decision needed
When the root *is* a leaf (no children, single implementable item),
Option D's topology degenerates to `feature/<root>` + `impl/<root>-<root>`
+ one trunk PR — two branches and two PRs for what was one PR today. Do
we special-case this to today's `feature/<item_id>` shape, or pay the
consistency tax?

### § Context
- Today's leaf-only case is one branch (`feature/<item_id>`) and one PR
  ([`src/requiem/workflows/implementation.py:385`](../../src/requiem/workflows/implementation.py)).
- Under D, the leaf-only case is two branches and two PRs — the trunk
  exists even when there's only one impl, the impl PR merges to the
  trunk, the trunk PR merges to `main`.
- The operator-visible cost is one extra PR-merge click per leaf-only
  run.
- The codebase-visible cost is exactly zero — the executor doesn't care
  if `len(children) == 1`.
- **What's NOT in scope:** the runtime cost (negligible — git is fast,
  the PR-merge click is the only "cost").

### § Options + trade-offs

| Option | One-line | Trade-offs |
|---|---|---|
| **Special-case leaf-only** | Detect `len(implementable_leaves) == 1` and skip the trunk. Use today's `feature/<item_id>` shape. | + No regression in the small case. − Branching the executor on tree shape. − Two code paths to test (leaf-only vs fan-out). − Operator mental model becomes "sometimes there's a trunk PR, sometimes there isn't." |
| **Pay the consistency tax** | Always use the trunk shape. Leaf-only runs see two PRs. | + One code path. + Operator mental model is uniform. − One extra PR-merge per leaf-only run; one extra branch in the repo. − Risk operators learn "the trunk PR is just a click" and stop reading it. |

**Stravinsky's recommendation:** open
([ADR-0006 OQ7, lines 600-605](../decisions/0006-merge-group-topology.md)),
flagged "minor."

### § Executive brief
What this question is really about: do we optimise the topology for the
common-today case (leaf-only) or for the common-tomorrow case (fan-out)?
The migration story to MG topology is mostly *for* fan-out, which means
"leaf-only" is the case the new code paths are *least* exercised on.
Special-casing it preserves today's UX for the demo and small features;
paying the consistency tax means every run uses the same code path and
the same tests cover both.

What's at stake: special-casing introduces a branch in the executor on
tree shape, which is the kind of branch that grows asymmetric bugs over
time ("we forgot the leaf-only path supports X"). Consistency tax adds
one PR-merge click and one branch per leaf-only run. The reversibility
is fine in either direction — both are local changes. **Lower-regret
default: pay the consistency tax.** It's the boring answer and the
boring answer is correct here because the asymmetry of the
special-cased path is the kind of thing that bites you in Wave 9 when
you're three months past having written the special case. If the
operator complains about the extra PR-merge click, *that's* the moment
to special-case — at which point you have one operator complaint
documenting the actual cost.

---

## Q11: What context does the remediation planner read?

### § Decision needed
When the remediation planner agent runs (ADR-0007 §5.3 step 5), does it
read (i) the latest `CommentSynthesis` only, (ii) the synthesis +
all PR comments ever posted, or (iii) the synthesis + the full
`git diff origin/main..HEAD`?

### § Context
- The `synthesize_comments` agent already produces a structured
  `CommentSynthesis` per cycle
  ([`src/requiem/workflows/pr_lifecycle.py:707-725`](../../src/requiem/workflows/pr_lifecycle.py)).
- Polyphony's `remediation_planner` (`feature-pr.yaml:804+`) reads the
  full git diff plus the reviewer's structured feedback.
- Britten's lean: (iii) for the planner (it's planning; context matters)
  and (i) for the addresser (it's executing; context should be tight)
  ([ADR-0007 lines 816-820](../decisions/0007-pr-lifecycle-architecture.md)).
- This is a prompt-engineering call as much as an architecture call — the
  workflow shape supports any of the three; only the agent spec changes.
- **What's NOT in scope:** the addresser's context (Britten settled that
  in the same lean — (i)); the remediation cycle cap (covered in §5.5).

### § Options + trade-offs

| Option | One-line | Trade-offs |
|---|---|---|
| **(i) Synthesis only** | Cheapest tokens, tightest signal. | + Lowest cost per cycle. − Planner can't see what's in the diff that the reviewer might have under-articulated. |
| **(ii) Synthesis + all PR comments** | Includes raw reviewer voice across iterations. | + Captures nuance the synthesiser flattens. − Quadratic-ish growth over cycles. |
| **(iii) Synthesis + full diff** | Planner sees the full diff to plan against. | + Maximum information for planning. + Matches polyphony. − Largest token cost; for big features the diff is the whole context window. |

**Britten's recommendation:** (iii)
([ADR-0007 lines 816-820](../decisions/0007-pr-lifecycle-architecture.md)).

### § Executive brief
What this question is really about: when an LLM is being asked to
*re-plan* (not re-execute), is the synthesised reviewer feedback enough
context, or does it need the underlying code? Britten's instinct is "it
needs the code" because re-planning is a load-bearing decision and the
synthesiser has thrown away signal by design. The cost is real but
bounded — `origin/main..HEAD` is exactly the PR's diff, which is the
upper bound on relevant code anyway.

What's at stake: this is the smallest blast-radius question in the
pack — wrong answer means worse remediation quality on some cycles, not
INV-violation or topology rework. Reversibility is excellent (it's an
agent prompt; change it in one file). **Lower-regret default: (iii), as
Britten recommends, with one knob:** cap the diff context at some
sensible token budget (say 50k) and fall back to (ii) when over. That
preserves the "planner sees code" property for the common case while
keeping huge features tractable. If you're feeling more conservative,
(ii) is fine for v0 and (iii) is an easy upgrade later — the workflow
doesn't care which context the agent gets.

---

## Cascade lock-in — which questions unblock which execution work

These are the Wave 7 execution lanes blocked on each answered question.
Use this to decide what to dispatch first after walking the briefing.

| Question | Unblocks |
|---|---|
| **Q1** | Everything in ADR-0006's "Decision" path. The `merge_group.py` workflow (~10 days under D, ~30+ under A); the `feature/<root>` ↔ `impl/<root>-<item>` branch-naming refactor; the per-leaf sub-workflow shim for fan-out; INV-FEATURE-TRUNK-PER-RUN tests. Also unblocks the audit row #7 revision (under D) or commits us to the audit's literal text (under A). |
| **Q2** | The `plan_pr` verb in `planning.py` (~1 day under "real PR"; zero under "virtual"); INV-PLAN-PR-PRECEDES-IMPL; closes audit non-negotiable #6's plan-PR half. |
| **Q3** | The `AdoClient` scoping — confirms whether v0's "ADO PR" work targets Azure-Repos-hosted PRs (Britten's plan proceeds unchanged) or escalates the twig PR-link issue (#30) as the actual blocker. |
| **Q4** | The first commit of `src/requiem/clients/azuredevops.py` — auth path is the entry point and the file can't be drafted without this answer. |
| **Q5** | The recovery branch of `merge_group.py` (under D); the skip-with-justification taxonomy in the event log; the `pr_lifecycle.py` remediation loop's "retry vs skip" gate (intersects with ADR-0007 §5.3 step 5). |
| **Q6** | The planner-output schema: do we add stable `mg_id`-style fields to `ChildPlan` now (yes) or leave them out (no)? Gates Q7's "required vs optional" choice. |
| **Q7** | A single-field schema change to `ChildPlan` + a dashboard-side render hint; trivially small but the dashboard wave (audit row #8) needs to know. |
| **Q8** | The same-root lock implementation in `root_dispatch.py`; the operator-facing CLI message; resume-vs-fresh-start UX. |
| **Q9** | The drift verb's home (`implementation.py` exit / `merge_group.py` / `pr_lifecycle.py` entry) — and therefore which workflow's test surface owns drift-conflict regression tests. |
| **Q10** | One conditional in the fan-out executor — small but needs to be ratified before the fan-out tests are written, because each path has its own test matrix. |
| **Q11** | The `remediation_planner` agent spec — prompt content, token budget, retrieval shape. Sits inside ADR-0007 §5.3 step 5; doesn't block anything except itself. |

**Dispatch order suggestion** (assumes Q1 = D and that you walk in the
TOC order): once Q1, Q2, Q3, Q4, Q9, Q10 are answered, the entire
ADR-0006 / ADR-0007 implementation lane unblocks. Q5–Q8 and Q11 are
implementation-detail-grade — they shape *how* the code reads, not
*whether* it can be written.

---

*End of briefing — Bruckner, Wave 7.*
*Decisions ratify in the source ADRs; this document is the synthesis,
not the record.*
