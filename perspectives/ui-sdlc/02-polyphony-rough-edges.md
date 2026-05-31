# 02 — Polyphony+Conductor rough edges, by SDLC phase

> Every symptom Daniel and the eight Opus seats cite in `error-handling-deep-dive.md` is, *at the operator surface*, a UX failure. This file mines them and reorganizes by SDLC phase so the UI seat (eventually) and the engine seats can see which edges are theirs to dissolve.

Each row tags a **symptom** → an **operator experience** → a **UI/UX fix shape** that's plausible (not yet committed). Sources cited with `DD:§n`, `PI:§n`, `NS:`, etc. per the README key.

---

## Phase 0 — Kickoff (start a root, preflight, worklist build)

| # | Symptom (today) | Operator experience | UX fix shape |
|---|---|---|---|
| K1 | Launcher is a `.ps1`; output streams to TTY; dashboard lags ~6s behind because it tails `.events.jsonl` (`PI:§8`; `ADR-0001` context #4) | Two surfaces (TTY + browser) showing the same run, neither authoritative; race when the browser hasn't caught up | One surface. Engine + UI backend share memory (`ADR-0001`), so SSE pushes the event as it's written. "+ start root" button replaces the launcher. |
| K2 | Preflight is opaque — a 30s wall of script output then a single boolean | Operator either trusts blindly or scrolls a TTY wall | Show preflight as a **named checklist** rendered live (manifest_loaded ✓, worklist_built ✓, edges_checked ✓...). The events already exist (`PI:§7`); the UI just has to render them. |
| K3 | `twig set` and `twig sync` are separate steps before launch | Three commands to start a thing | Inlined into the launch form. The UI calls `twig` via the verb library (one process, `ADR-0001`). |

## Phase 1 — Planning (architect agent, plan PR, plan reviewer)

| # | Symptom (today) | Operator experience | UX fix shape |
|---|---|---|---|
| P1 | Architect output is a JSON blob in a log file; child seeding happens inside `plan seed-children` with no visible structure | Operator can't tell what the architect proposed vs what got seeded | Tree-diff view in the detail panel: "architect proposed N children → seeded N (M new, K existed, 0 conflicts)" with each child clickable. Receipts (`DD:§2.1`) attach to the seed-children verb. |
| P2 | Plan reviewer routes to one of {approve, request_changes, abort} but the route reasoning lives in agent output JSON | Operator has to click into the agent output to see *why* | Route badges on the trace row showing `route=request_changes (reason: scope_too_broad)`. Use the `route_taken.matched_on_error` enrichment from `DD:§2.5 / Brahms-2 Appendix B` (event enrichment, not new events). |
| P3 | Recursion depth guard fires as a "depth-exceeded gate" with a single option that's actually an acknowledgement (`DD:§1 layer b`) | Operator clicks "OK" to a not-really-a-decision | The gate UI must distinguish **decision gates** (multiple meaningful options) from **acknowledgement gates** (one option). Acknowledgements should be a toast, not a modal. |
| P4 | Plan PR open/merge is two verbs; a network blip leaves the PR open but unmerged with no visible artifact (`DD:§1.5 b — PrCommentMarker class`) | Operator might post duplicate comments on retry | Show the **idempotency key** in the row metadata. If the verb is `pr post-comment-ado`, show "comment marker = ABCD"; on retry, the row says "deduplicated against existing comment ABCD." |

## Phase 2 — Implementation (MG branching, coder/reviewer, impl PR)

| # | Symptom (today) | Operator experience | UX fix shape |
|---|---|---|---|
| I1 | MG/impl branching produces `feature/{root}`, `mg/{root}_{path}`, `impl/{root}-{item}` (`PI:§3`); state lives in git and in `seed-manifest.json` | Operator has to mental-model branch hierarchy from `git branch -a` output | Branch tree view as a sidebar widget: nested branches with status pills (created/merged/abandoned). Click → focus run leg. |
| I2 | Coder agent succeeds, reviewer fails, reviewer-fix loop iterates 3 times with no clear "we're making progress" signal | Operator feels the loop spinning but can't see *what* changed each iteration | Iteration counter on the trace scope (Argo's per-attempt render, `WV:Argo §5`). Each iteration is a collapsible sub-scope showing the diff produced. |
| I3 | Reviewer hallucinates approval (`DD:§2.1`); without receipts, downstream merges a not-actually-reviewed PR | Silent corruption that surfaces days later as "wait, who reviewed this?" | Receipts badge on every reviewer row. If receipts missing → red badge → inbox row → `[hallucinated_success_gate]` (`DD:§S1.4`). The UI is the *receipts surface*, not just the gate handler. |
| I4 | Worktrees are per-item directories sibling to the repo (`..\polyphony-{N}\`); they accumulate across runs and can be left after crashes (`DD:§1 layer e: ManifestPlanLedger`) | Operator has to manually `Remove-Item` worktrees | Worktree manager surface: list of active worktrees, "last touched by run" annotation, "+ teardown" button per row. Cleanup is reversible (move to `.polyphony/trash/`, prune nightly). |
| I5 | Compaction blackout — long agent contexts auto-compact at ~200K tokens and the agent silently loses earlier context (`DD:§1 layer d, Stravinsky-2 §6`) | A reviewer that "approved" might have approved without seeing the early diff | Compaction event in the trace as a yellow horizontal divider: "context compacted: 187 tokens dropped, 4 messages summarized". Operator can hover for the summary and decide whether to re-review. |

## Phase 3 — PR lifecycle (feature PR, GitHub or ADO)

| # | Symptom (today) | Operator experience | UX fix shape |
|---|---|---|---|
| R1 | Feature PR remediation has a 60-line PowerShell aggregator workaround because subworkflow errors arrive as opaque exception strings (`DD:§1 layer b`) | When PR remediation fails, the failure message is "Exception thrown by subworkflow feature-pr" with no inner kind | Subworkflow rows in the trace are *first-class scopes*, not opaque agent rows. Drilling in shows the child workflow's full trace inline. (`PS:§4 path-based concurrency makes this natural.`) |
| R2 | github-pr and ado-pr workflows are platform forks; the UI today is identical-looking but the verbs differ | Operator doesn't always know which platform a given PR lives on | Platform badge on the run header (`gh` / `ado`). Click → opens the actual PR URL. |
| R3 | PR feedback fix loop can run forever in pathological cases (`DD:§1 layer a`) | Run that "should be done" keeps ticking | Iteration counter visible on the run header. Soft warning at iteration ≥ 3 with a "convert to surrender gate?" affordance. |

## Phase 4 — Gates (human-in-the-loop, surrender, supersession)

| # | Symptom (today) | Operator experience | UX fix shape |
|---|---|---|---|
| G1 | Gates exist in TTY + dashboard but discovery requires the operator to be looking (`PI:§6`) | Gate sits unanswered while operator is in VS Code | OS-level notification with prompt text + action buttons (covered in `01-feel-of-the-loop.md` § 12:00). |
| G2 | Single-option human gates pretending to be decisions (`DD:§1 layer b` — 8 of them route to `abort_run`) | Operator clicks "OK" without realizing they had no choice | Acknowledgement gates render as a toast, not a modal. Decision gates render as a modal-or-inline panel. Distinct visual treatment. |
| G3 | `actionable.yaml` silently loses `workflow_abandoned` / `workflow_error_gate` to the parent (`DD:§3 R1`) | Gate fired in child workflow but parent has no visible signal | Surface every domain signal in the inbox the moment it's emitted (`NS:§3.2`). The signal stream is a sibling of the event stream; the UI binds to both. |
| G4 | "Surrender" vs "abandonment" terminology is ambiguous; engine-initiated abandonment doesn't formally exist but the failure modes look identical to the operator (`DD:§2.3, §6 D-SUPERSEDED-TERMINAL`) | Operator can't tell why a run ended | Distinct terminal pills: `completed` (green), `surrendered` (amber, "you stopped this"), `superseded` (blue, "a newer plan replaced it"), `cancelled` (grey, "you killed it"). Each routes to a different inbox archival lane. |

## Phase 5 — Closeout & post-run

| # | Symptom (today) | Operator experience | UX fix shape |
|---|---|---|---|
| C1 | Closeout produces observations but they're not aggregated across the day | Operator doesn't know what shipped without reading git | "Today" view derived from event log (covered in `01-feel-of-the-loop.md` § 17:00). |
| C2 | Three exit-code dialects in production scripts (`DD:§1 layer c`) | When something fails, the operator sometimes sees "exit 1" and sometimes "exit 3" and sometimes "exit 0 with an error envelope" | The 5-class contract (`DD:§4 R3`) collapses this *at the script boundary*. The UI normalizes to the 5 outcome variants (`INV-DISCRIMINATED-OUTCOMES`) regardless of the script-side dialect. |
| C3 | No "what happened" forensic surface after a run; reconstruction means reading `.events.jsonl` and the manifest by hand | "Why did AB#3287 surrender?" takes 20 minutes to answer | Run-page becomes a **post-run artifact** automatically. Bookmarkable URL with the full trace, receipts, prompts, and terminal verdict pre-rendered. |

---

## Cross-cutting rough edges (not phase-specific)

### X1 — Watermark polling is the wrong shape for "live"

`DD:§1 layer a — Poll-PrStateDelta.ps1` is the gold-standard pattern but it exists *because* there's no in-process event bus the workflow author can subscribe to (ADR-0001 context #6). For PR status polling against a remote, polling is still correct (the remote doesn't push). But for everything internal — workflow events, domain signals — the UI must be **push-driven** (`PS:§2 SSE`). Polling for internal state would replicate polyphony's lag in Requiem unnecessarily.

### X2 — Cancellation is a sentinel file

Today: `CONDUCTOR_CANCEL_TOKEN` sentinel file on Windows because there's no `SIGTERM` semantics across the process boundary (`ADR-0001` context #5; `DD:§S3.3`). In single-process Python, this is `asyncio.CancelledError` and the only UX requirement is that cancel feels instant (covered in `01-feel-of-the-loop.md` § "Cancellation must feel instant"). The sentinel-file machinery does not need a UI equivalent; it just dissolves.

### X3 — Manifest as a JSON-blob debugging dependency

Operators inspect `seed-manifest.json` by hand to understand state (`PI:§3`). This is a UI failure: any state worth inspecting should be a first-class surface. The manifest is `INV-EVENT-LOG-AUTHORITATIVE`'s projection (`NS:INV-EVENT-LOG-AUTHORITATIVE`), so its contents are derivable. The UI should render *manifest as table* with diff-against-previous-state on every mutation. Operators should never `cat` the JSON.

### X4 — Run lock is OS-atomic but invisible to the operator

`DD:§1 layer e: RunLockStore.cs:90` — same-root concurrent runs are blocked by an OS-atomic file lock. The operator only finds out by trying to launch and getting a refusal. UI fix: the inbox shows running roots prominently; the "+ start root" form refuses to submit for a root that's already locked, with a "view existing run" link instead.

### X5 — The three-vocabulary problem is half-dissolved by ADR-0001

In polyphony today, event names, state names, and category names must agree across Python engine ↔ .NET verbs ↔ YAML workflows (`ADR-0001` context #1). Requiem collapses this to Python + UI (`ADR-0001` consequences). But the UI is still a vocabulary boundary — the UI seat needs to consume the engine's types directly (via Pydantic schemas exported to TS, or a generated client). If the UI invents its own names for things, the problem partially reappears. **Recommendation**: the SSE protocol must use the engine's type names verbatim, and the frontend types must be generated from the backend's Pydantic models, not hand-maintained.

### X6 — No cross-run pattern recognition

`WV:cross-cutting anti-pattern #3` — systems with no cross-run history force operators to compare runs by opening multiple tabs. Polyphony today has zero cross-run UI (`PI:§8`). For a developer running 8 hours/day, the Airflow Grid View (`WV:Airflow §3`) pattern is the canonical fix. **Defer to post-v0** (see `05-forward-looking-deferred.md`), but make sure the event log schema supports it — i.e., the log writes contain enough metadata that a future Grid View can be built without re-architecting persistence.

---

## Severity ranking (operator-pain proxy)

If I had to rank these by daily friction in old polyphony, my unscientific top 5:

1. **G1** — gate discovery latency (the limiting factor on most runs is Daniel-noticing-the-gate, not the engine)
2. **I3** — silent hallucinated approvals (high blast radius, hard to detect)
3. **K1+X1** — TTY+browser split and watermark-poll lag (constant low-grade tax on every glance)
4. **X3** — manifest-as-JSON debugging (every "why is this in this state" investigation pays this cost)
5. **C3** — post-run forensic effort (every "what happened to AB#3287" question takes >10 minutes today)

These five are the rough edges I'd most want to see *visibly resolved* at the Phase B walking-skeleton demo. Everything else is a refinement; these five are the headline difference in feel.
