# ADR 0012 — Plan-PR Lifecycle (`plan_pr` workflow)

**Status:** ACCEPTED (Phase C parity slice — non-negotiable #6, "plan PR")
**Date:** 2026-06
**Author:** Recorded by the design seat; design hardened by a rubber-duck critique pass.
**Supersedes:** none
**Superseded by:** —
**Cross-cuts:** ADR-0006 (merge-group topology — the plan PR is the Option-D
aggregator's first artefact), ADR-0007 (pr-lifecycle — review→merge takes over
after this workflow opens the PR), ADR-0011 (plan-commit seeding — the sibling
half of non-negotiable #6).

---

## TL;DR

The recursive `planning` workflow writes an approved plan **sidecar**
(`<run>.plan.tree.json` for decomposable roots, `<run>.plan.md` for leaves) but
never surfaces it for human review on the platform. This ADR adopts a separate
**`plan_pr` workflow** that takes the already-written, already-approved plan and
opens it as a reviewable **`plan/<root>` PR**: it renders the plan to a committed
markdown document, cuts the `plan/<root>` branch off the base trunk, commits
**only** that document, pushes, and opens (or idempotently reuses) the PR — then
hands off to `pr_lifecycle` (ADR-0007) for review and merge.

Planning stays a pure decision producer. `plan_pr` owns *surfacing*, not
*deciding*: it fails closed unless the artefact is explicitly approved, and it
never merges (merge is `pr_lifecycle`'s job).

## Decision

### Open-and-handoff, not open-and-merge

`plan_pr` mirrors `implementation.py`: it OPENS the PR and stops. Merge authority
lives in `pr_lifecycle` so there is exactly one merge seat in the system. The
topology is linear:

```
start → load_plan → create_branch → write_plan_doc → commit → push → open_pr → link_pr → end_success
```

with `end_failed` (permanent failures) and `end_human` (recoverable gates).

### Fail closed on approval

`load_plan` validates the artefact before any mutation:

* `.tree.json` — requires `schema_version >= 2`, top-level `verdict == "approved"`,
  and `item_id == root_item_id` (no cross-root surfacing).
* `.plan.md` (leaf) — planning writes the *same filename* for both approved and
  needs-human leaves, so the only signal is the verdict line. `plan_pr` refuses
  unless a `Verdict: approved` line is present (`_md_is_approved`). A
  needs-human leaf → `end_failed`, never a PR.

### Commit only the plan document

`commit` stages **only** the rendered plan doc (`fs.git_commit(msg, paths=[doc])`),
never `git add -A`. A dirty working tree from an unrelated edit must not be swept
into the plan PR. (Adopted from the rubber-duck critique.)

### Repo-path containment

The plan doc path (default `.requiem/plans/<root>.plan.md`, overridable) is
validated to resolve **inside** the repo — absolute paths and `..` escapes →
`end_failed` (`EK_BAD_DOC_PATH`), so a malformed override cannot write outside
the worktree.

### Idempotent + gated re-entry

* `create_branch` — already on `plan/<root>` → no-op; branch exists but HEAD is
  elsewhere → `NeedsHuman(branch_exists_foreign)`.
* `open_pr` — an open PR with the same head is reused (no duplicate). An open PR
  on the **wrong base** → `NeedsHuman(pr_exists_wrong_base)` rather than a silent
  reuse.

Non-interactive runs auto-abort gates (recoverable by a human re-run after
cleanup); interactive callers override `gate_handler`.

### Base-branch forward-compat (ADR-0006 Option D)

`base_branch` defaults to `main` but is a first-class input. The Option-D
aggregator will pass `feature/<root>` so the plan PR targets the integration
trunk instead of `main` — no code change required, only a different base.

### Best-effort ADO backlink

`link_pr` posts the PR url back to the work item via `twig.comment_async`, but a
failed backlink does **not** block the handoff (both `success` and
`permanent_failure` route to `end_success`) — the PR being open is what matters.

## Consequences

* Non-negotiable #6 ("recursive planning with seeding") is now fully closed:
  child seeding (ADR-0011, `commit_plan`) **and** the plan PR (this ADR).
* The plan PR is the natural insertion point for the ADR-0006 Option-D
  aggregator; this workflow is the leaf the aggregator will drive.
* Freeze/supersede semantics (a committed plan PR as a plan-generation freeze
  point) remain deferred to the merge-group/plan-generation work (ADR-0006),
  exactly as ADR-0011 deferred them.
