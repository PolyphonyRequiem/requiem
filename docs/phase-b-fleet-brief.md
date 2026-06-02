# Phase B Fleet Brief — DRAFT

**Status:** Draft, awaiting Verdi-2 promotion + ADRs #12 to merge.
**Audience:** Daniel (greenlight), then Phase B agents at dispatch time.

## What Phase B is

Phase A delivered the **shape** of the engine. Phase B delivers the
**first real workflow** running on it, end to end, against real Twig + real
GitHub, producing workday-shaped human output (per the demo-failure signal
on 2026-05-31).

**Phase B is done when:** Daniel can sit at a terminal in a real ADO-backed
repo, type `requiem run close-out --item <id>`, and watch a coherent
human-readable trail end in a merged PR + an archived plan, with the run
fully resumable from the event log.

## Target workflow: `close-out`

Smallest end-to-end workflow that touches every major seam:

1. Resolve work item via Twig (Toolbelt `twig` client)
2. Read PR linked to item via Toolbelt `gh` client
3. Verify PR is merged (`Success`) / failing (`PermanentFailure`)
4. Archive plan (Toolbelt filesystem op)
5. Update work item state to Closed (Twig)
6. Emit summary event (Brahms-events; rendered by `requiem events`)

Approximate node count: 6-8. Approximate verbs: 4-6. Approximate runtime
in happy path: <30 s.

**Why close-out first:** smallest workflow that exercises agent boundary
(state-resolution verb may invoke an agent for ambiguous cases), tool
boundary (Twig + gh), persistence (event log), kernel (gates around
"PR not merged — human?"). No team-step needed for v0.

## Proposed Phase B fleet (~8 seats)

Same model: parallel, branch-per-seat, PR-per-seat. Lower seat count than
Phase A because there are fewer green-field decisions; most work is
adoption of the Phase A baseline against real systems.

| Seat | Branch | Scope |
|---|---|---|
| **mendelssohn-twig** | `phaseB/toolbelt-twig` | Real `TwigClient` in Toolbelt — wraps `twig show / state / area`. Exit code → outcome mapping (Ravel's L-1: unknown `twig exit 1` → `NeedsHuman`, never `RetryableFailure`). |
| **chopin-gh** | `phaseB/toolbelt-gh` | Real `GhClient` in Toolbelt — `gh pr view / pr list / api`. Rate-limit detection → `RetryableFailure(after: timedelta)`. |
| **schumann-fs** | `phaseB/toolbelt-fs` | `FilesystemClient` in Toolbelt — git-aware archive (`git mv`), atomic writes. Windows path bites caught at the client. |
| **sibelius-closeout** | `phaseB/workflow-close-out` | Write the `close-out` workflow itself using Wagner DSL + the three Toolbelt clients above. ~150 LOC. |
| **dvorak-cli** | `phaseB/cli-render` | `requiem events <run_id>` rendering per Debussy's demo contract (file 07). Workday-shaped output, not engineer-shaped. |
| **rachmaninov-resume** | `phaseB/resume-fidelity` | Crash-test matrix: kill -9 at every node of close-out; verify restart-from-log produces identical terminal state. |
| **tchaikovsky-bug-bash** | `phaseB/integration-bugs` | Run close-out against 3 real ADO items in `polyphony-squad-spike`. File bugs; fix or escalate. |
| **shostakovich-docs** | `phaseB/v0-getting-started` | `README.md`, `docs/getting-started.md`, `docs/concepts.md` from a user's seat (not author's). |

## Dispatch sequencing

```
Wave 1 (independent, parallel):
  mendelssohn-twig
  chopin-gh
  schumann-fs
  dvorak-cli   ← can develop against fixture events from PR #11
  shostakovich-docs

Wave 2 (after Wave 1 PRs are reviewable):
  sibelius-closeout   ← needs the three Toolbelt clients

Wave 3 (after Wave 2):
  rachmaninov-resume   ← needs a real workflow to crash-test
  tchaikovsky-bug-bash ← needs the workflow + CLI rendering
```

## Phase B success metrics

1. **Workday demo passes the customer test.** Daniel runs
   `requiem run close-out --item <real-id>` and replies with something other
   than "i don't get what I'm looking at." Concretely: he can describe what
   happened from the rendered output without reading code or events.jsonl.
2. **Resume fidelity is provable.** rachmaninov-resume's matrix shows
   identical post-crash terminal state for every kill-point in close-out.
3. **No engine changes required.** All Phase B work is verbs, Toolbelt
   clients, workflows, and CLI rendering. If a seat needs an engine change,
   they file a follow-up PR against `src/requiem/` and we know we have a
   seam shape problem in Phase A — open follow-up ADR.

## What Phase B is NOT

- Not the full polyphony parity. That's Phase C (vertical slices for plan
  generation, implementation, PR lifecycle).
- Not UI. UI is held per Daniel's 2026-05-31 direction; CLI rendering only.
- Not perf tuning. close-out is small; perf concerns come with Phase C
  larger workflows.

## Open coordination question (for Daniel before dispatch)

Phase B is ~8 agents × ~30-60 min each × cost. Confirm dispatch authority,
or pause for greenlight after Verdi-2 + ADRs merge.

Lean: dispatch on best-judgement per the standing "use your best
judgement / keep going" instruction, with a one-shot preview message to
Daniel listing the 8 seats just before dispatch.

## References

- ADR 0002 — Phase A Integrated Design (in PR #12)
- ADR 0003 — Agent Teams as First-Class Primitive (in PR #12)
- ADR 0004 — Cross-Cutting Defaults (in PR #12)
- `docs/phase-a-demos.md` §"What Phase B demos must NOT look like"
- Verdi-2 promotion PR (pending)
- Debussy file 07 — Demo contract (pending)
