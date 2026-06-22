"""Per-leaf context pack synthesiser + commit verb (ADR-0030 §1).

The pack is the curated knowledge slice Requiem hands the per-leaf coder
agent — synthesised from the planner's output, capped doctrine excerpt,
and acceptance criteria, then committed to the leaf branch's ``.requiem/``
directory BEFORE the implementer runs. Without this, the worker would see
only the work item's title/body — the planner's rationale, the doctrine
section that matches the leaf, the expected file set, are all written into
the run's event log but never reach the agent's prompt (ADR-0030 §Context).

Two halves:

* :func:`build_context_pack` is a **pure**, **deterministic** synthesiser.
  Same ``(leaf, plan_payload, doctrine, process_config)`` inputs produce
  byte-identical output (the ``plan_hash`` is the proof). No I/O, no clock,
  no network — doctrine content is passed in, not read from disk.

* :func:`commit_context_pack` is the verb that lands the three files
  (``AGENTS.md``, ``rationale.md``, ``acceptance.md``) on the leaf branch
  in a single chore commit. **Idempotent** under the recorded plan_hash:
  a second call on the same plan_hash no-ops via the ``.requiem/.plan_hash``
  sentinel embedded in the prior commit's tree.

Out of scope (ADR-0030 §Out of scope):

* Per-run RAG / vector retrieval — only the doctrine-slice heuristic.
* $/token pricing or model routing — Part 1 of ADR-0030.
* Cross-run agent memory — ADR-0003 explicitly defers.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from requiem.clients.fs import FilesystemClient, FsClientError, FsGitError
from requiem.doctrine import Doctrine, default_doctrine
from requiem.outcomes import Outcome, PermanentFailure, Success
from requiem.process_config import ProcessConfig


# ---- public dataclasses ------------------------------------------------


# Default cap for the doctrine slice section (4 KB, per ADR-0030 §Failure
# modes "context_pack_truncated" bullet). Configurable per-call.
DEFAULT_DOCTRINE_CAP_BYTES = 4096


@dataclass(frozen=True, slots=True)
class ContextPackLeaf:
    """The per-leaf inputs the synthesiser needs.

    Adapter shape: orchestrators (fanout, kanban_executor) build this
    from whatever leaf type they hold (``FanoutLeaf``, ``LeafSpec``,
    ``ResolvedLeaf``). Keeping the synthesiser tolerant of one shape
    avoids forcing every leaf representation into a shared base class
    just to feed the pack.

    Fields the synthesiser uses:

    * ``leaf_id`` — stable identity, used in the AGENTS.md header and
      the commit message; usually the real ADO id stringified.
    * ``title`` — the work item title.
    * ``body`` — the work item description / inline plan text.
    * ``work_item_type`` — used by the doctrine matcher (Task, Bug,
      Feature, etc.). Defaults to ``"Task"``.
    * ``labels`` — extra tags used by the doctrine matcher (e.g. labels
      attached to the ADO item by the planner). Empty tuple is fine.
    * ``expected_files`` — optional file list the planner predicted will
      be touched; rendered into AGENTS.md as a hint. Empty tuple → the
      fallback "touch only what's needed" line.
    * ``acceptance_criteria`` — the criteria the verifier will check;
      rendered as bullets. Empty tuple → a generic fallback bullet.
    * ``rationale`` — the planner's full "why this leaf" prose; mirrored
      verbatim into ``rationale.md`` and summarised into AGENTS.md.
    * ``summary`` — short prose for the AGENTS.md "What this leaf is"
      block. Defaults to ``title`` when empty.
    """

    leaf_id: str
    title: str
    body: str = ""
    work_item_type: str = "Task"
    labels: tuple[str, ...] = ()
    expected_files: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    rationale: str = ""
    summary: str = ""

    @classmethod
    def from_mapping(cls, m: Mapping[str, Any]) -> "ContextPackLeaf":
        """Build from a dict — tolerant of partial inputs.

        Useful for the orchestrator paths that hand the synthesiser a
        thin projection rather than a typed leaf.
        """
        def _tup(key: str) -> tuple[str, ...]:
            v = m.get(key)
            if not v:
                return ()
            return tuple(str(x) for x in v)
        return cls(
            leaf_id=str(m.get("leaf_id") or m.get("real_id") or ""),
            title=str(m.get("title", "")),
            body=str(m.get("body", "")),
            work_item_type=str(m.get("work_item_type", "Task")),
            labels=_tup("labels"),
            expected_files=_tup("expected_files"),
            acceptance_criteria=_tup("acceptance_criteria"),
            rationale=str(m.get("rationale", "")),
            summary=str(m.get("summary", "")),
        )


@dataclass(frozen=True, slots=True)
class ContextPack:
    """The rendered, hashable per-leaf context bundle.

    ``agents_md`` is the prose the coder agent reads via the AGENTS.md
    convention. ``rationale_md`` / ``acceptance_md`` are the forensic
    full-fidelity dumps that AGENTS.md links to (so the agent can drill
    deeper without bloating the primary prompt).

    ``plan_hash`` is the deterministic identity of the inputs that
    produced this pack — used by :func:`commit_context_pack` for
    idempotency (a re-run with the same hash no-ops; a re-plan with a
    new hash forces a fresh pack).

    ``doctrine_truncated`` is True when the matching doctrine slice
    exceeded the configured cap and was sliced at a section boundary.
    Observability only — the truncation does not fail the pack build;
    the verb emits a ``context_pack_truncated`` event when set.
    """

    leaf_id: str
    agents_md: str
    rationale_md: str
    acceptance_md: str
    plan_hash: str
    doctrine_truncated: bool = False


# ---- doctrine slicing (deterministic, capped) --------------------------


def _split_doctrine_sections(text: str) -> list[tuple[str, str]]:
    """Split a doctrine markdown into (heading, body) sections.

    A "section" is everything from a ``## `` heading line up to (but not
    including) the next ``## `` heading. Content before the first ``##``
    is returned under the synthetic heading ``"(preamble)"``.

    Deterministic — same input, same output, same order.
    """
    sections: list[tuple[str, str]] = []
    current_heading = "(preamble)"
    current_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current_lines or current_heading != "(preamble)":
                sections.append((current_heading, "\n".join(current_lines).rstrip()))
            current_heading = line[3:].strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_lines or current_heading != "(preamble)":
        sections.append((current_heading, "\n".join(current_lines).rstrip()))
    return sections


def _section_tags(heading: str, body: str) -> set[str]:
    """The lowercased word set used to match a section against a leaf.

    We use the heading + first non-blank body line (the "summary line"
    convention) so a section titled "Testing" with a body discussing
    Tasks matches a Task-typed leaf without a section rename.
    """
    tags = set(heading.lower().split())
    for line in body.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            tags.update(w.strip(",.:;-").lower() for w in s.split())
            break
    return tags


def _select_doctrine_sections(
    doctrine: Doctrine,
    leaf: ContextPackLeaf,
    *,
    cap_bytes: int,
) -> tuple[str, bool]:
    """Pick doctrine sections relevant to the leaf, capped at ``cap_bytes``.

    Heuristic (per ADR-0030 §1 "Doctrine relevant to this leaf"):

    * Match a section if any of its tags overlaps the leaf's labels OR
      the lowercased work-item type.
    * If no section matches, fall back to including ALL sections (the
      whole doctrine is the "relevant" set for a leaf with no hints).
    * Truncate at the SECTION BOUNDARY when the cumulative byte size
      exceeds ``cap_bytes`` (never mid-section — the slice is always a
      coherent prose fragment).

    Returns ``(rendered_text, was_truncated)``. ``rendered_text`` is
    empty when the doctrine itself is empty. ``was_truncated`` is True
    when at least one matched section was dropped due to the cap.
    """
    if doctrine.is_empty:
        return "", False

    sections = _split_doctrine_sections(doctrine.text)
    if not sections:
        return "", False

    # Build the leaf's match set: labels + the work-item type word.
    leaf_tags: set[str] = set()
    for label in leaf.labels:
        leaf_tags.update(w.strip(",.:;-").lower() for w in label.split())
    if leaf.work_item_type:
        leaf_tags.update(w.strip(",.:;-").lower() for w in leaf.work_item_type.split())

    matched = [
        (h, b) for (h, b) in sections
        if leaf_tags & _section_tags(h, b)
    ]
    # Fallback: no targeted match → use all sections.
    if not matched:
        matched = sections

    out_parts: list[str] = []
    used = 0
    truncated = False
    for heading, body in matched:
        if heading == "(preamble)":
            chunk = body.rstrip() + "\n"
        else:
            chunk = f"## {heading}\n{body.rstrip()}\n"
        size = len(chunk.encode("utf-8"))
        if used + size > cap_bytes and out_parts:
            truncated = True
            break
        out_parts.append(chunk)
        used += size
        if used >= cap_bytes:
            # Hit the cap exactly at a section boundary; remaining
            # matched sections are dropped.
            if len(out_parts) < len(matched):
                truncated = True
            break

    rendered = "\n".join(out_parts).rstrip() + "\n"
    return rendered, truncated


# ---- markdown rendering ------------------------------------------------


def _render_agents_md(
    leaf: ContextPackLeaf,
    *,
    rationale_excerpt: str,
    acceptance_lines: list[str],
    expected_files_block: str,
    doctrine_slice: str,
) -> str:
    """Render the AGENTS.md prose per the ADR-0030 §1 template.

    Deterministic: identical inputs → byte-identical output. No clock,
    no env. The order of bullets / lines / sections is fixed.
    """
    summary = leaf.summary or leaf.title or f"leaf {leaf.leaf_id}"
    rationale_block = rationale_excerpt.strip() or (
        "No structured rationale was emitted by the planner for this leaf. "
        "Treat the work-item title + body above as the plan."
    )
    crit_block = (
        "\n".join(f"- {line}" for line in acceptance_lines)
        if acceptance_lines
        else "- (no acceptance criteria were configured for this leaf — the "
             "verifier will check that the diff matches the plan and tests pass)"
    )
    doctrine_block = doctrine_slice.strip() if doctrine_slice.strip() else (
        "(no doctrine sections matched this leaf; the repo may not yet have a "
        "`.requiem-config/doctrine.md` — see `docs/references/doctrine.example.md`)"
    )
    lines = [
        f"# Context for leaf: {leaf.leaf_id}",
        "",
        "You are working on one slice of a larger feature. Requiem (the SDLC",
        "orchestrator that dispatched you) has already done the planning, branch",
        "setup, and review wiring. Your job is the implementation of THIS leaf",
        "only.",
        "",
        "## What this leaf is",
        "",
        summary,
        "",
        "## Why this leaf exists",
        "",
        rationale_block,
        "",
        "## Acceptance criteria",
        "",
        "Requiem's verifier will check these after you mark the task complete:",
        "",
        crit_block,
        "",
        "## Files Requiem expects you to touch",
        "",
        expected_files_block,
        "",
        "DO NOT modify files outside this list without explicitly noting why in your",
        "PR description. The handoff verifier (ADR-0017 §4) compares your",
        "`changed_files` claim against the actual diff.",
        "",
        "## Doctrine relevant to this leaf",
        "",
        doctrine_block,
        "",
        "## Out of scope",
        "",
        "- The trunk PR (Requiem opens it after every leaf merges).",
        "- Coordinating with sibling leaves (Requiem owns acceptance-gated release).",
        "- Long-term refactors (this is one slice).",
        "",
        "## Forensic detail",
        "",
        "- Full planner rationale: `./rationale.md`",
        "- Full acceptance criteria: `./acceptance.md`",
        "",
    ]
    return "\n".join(lines)


def _render_rationale_md(leaf: ContextPackLeaf) -> str:
    """Forensic dump of the planner rationale for this leaf.

    Carries the full prose verbatim — no truncation, no editorialising.
    AGENTS.md links to this file so a curious agent can read the full
    "why this leaf" without bloating the primary prompt.
    """
    rationale = leaf.rationale.strip() or "(no rationale emitted by the planner)"
    body_block = leaf.body.strip() or "(no inline plan body)"
    return (
        f"# Rationale dump — leaf {leaf.leaf_id}\n"
        f"\n"
        f"## Work item\n"
        f"\n"
        f"- ID: {leaf.leaf_id}\n"
        f"- Title: {leaf.title}\n"
        f"- Type: {leaf.work_item_type}\n"
        f"\n"
        f"## Planner rationale\n"
        f"\n"
        f"{rationale}\n"
        f"\n"
        f"## Inline plan body\n"
        f"\n"
        f"{body_block}\n"
    )


def _render_acceptance_md(leaf: ContextPackLeaf) -> str:
    """Forensic dump of the acceptance criteria.

    Mirrors what the verifier will check, in the format
    ``process_config`` / ``speckit tasks.md`` produced. The verifier is
    the source of truth at run time; this file is the audit trail.
    """
    if not leaf.acceptance_criteria:
        body = (
            "(no acceptance criteria were configured for this leaf — the "
            "verifier will check that the diff matches the plan and tests pass)"
        )
    else:
        body = "\n".join(f"- {c}" for c in leaf.acceptance_criteria)
    return (
        f"# Acceptance criteria — leaf {leaf.leaf_id}\n"
        f"\n"
        f"{body}\n"
    )


# ---- deterministic plan_hash ------------------------------------------


def _stable_dump(obj: Any) -> str:
    """Stable JSON dump for hashing — sort_keys, no trailing whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _process_config_hash(cfg: ProcessConfig | None) -> str:
    """Stable hash of a ProcessConfig for plan_hash composition.

    Prefers the recorded ``sha256`` (set by ``load_process_config``) so
    two runs that loaded the same file yield identical hashes. Falls
    back to a hash of the to_snapshot() projection when ``sha256`` is
    absent (e.g. ProcessConfig built in a test).
    """
    if cfg is None:
        return hashlib.sha256(b"<no-process-config>").hexdigest()
    if cfg.sha256:
        return cfg.sha256
    try:
        snap = cfg.to_snapshot()
    except Exception:  # pragma: no cover — defensive
        snap = {}
    return hashlib.sha256(_stable_dump(snap).encode("utf-8")).hexdigest()


def _compute_plan_hash(
    leaf: ContextPackLeaf,
    plan_payload: Mapping[str, Any] | None,
    doctrine: Doctrine,
    process_config: ProcessConfig | None,
) -> str:
    """Deterministic hash over the four inputs that drive the pack.

    Same inputs → same hash. ANY change in leaf identity, plan payload,
    doctrine content, or process-config content forces a different hash
    — which is the signal :func:`commit_context_pack` uses to re-write
    the leaf's ``.requiem/`` directory and produce a fresh commit.

    NOTE on stability: the leaf is reduced to its frozen-tuple fields
    via :func:`_stable_dump`; the plan payload is sorted JSON; the
    doctrine collapses to its ``sha256`` (computed by ``doctrine.py``);
    the process config collapses to its ``sha256`` or projection hash.
    """
    parts = {
        "leaf": {
            "leaf_id": leaf.leaf_id,
            "title": leaf.title,
            "body": leaf.body,
            "work_item_type": leaf.work_item_type,
            "labels": list(leaf.labels),
            "expected_files": list(leaf.expected_files),
            "acceptance_criteria": list(leaf.acceptance_criteria),
            "rationale": leaf.rationale,
            "summary": leaf.summary,
        },
        "plan_payload": dict(plan_payload) if plan_payload else {},
        "doctrine_sha256": doctrine.sha256,
        "process_config_sha256": _process_config_hash(process_config),
    }
    return hashlib.sha256(_stable_dump(parts).encode("utf-8")).hexdigest()


# ---- the pure synthesiser ---------------------------------------------


def build_context_pack(
    leaf: ContextPackLeaf | Mapping[str, Any],
    plan_payload: Mapping[str, Any] | None = None,
    process_config: ProcessConfig | None = None,
    doctrine: Doctrine | None = None,
    *,
    doctrine_cap_bytes: int = DEFAULT_DOCTRINE_CAP_BYTES,
) -> ContextPack:
    """Build the per-leaf context pack from the four inputs.

    Pure. Deterministic. No I/O. Same inputs → byte-identical output
    (the ``plan_hash`` is the proof).

    Parameters
    ----------
    leaf
        Per-leaf identity + planner-derived fields. Accepts either a
        :class:`ContextPackLeaf` dataclass or a mapping with the same
        keys (``leaf_id``/``real_id``, ``title``, ``body``, …).
    plan_payload
        Optional dict from the planner output (e.g. the leaf's slice of
        the plan_tree.json). Folded into the plan_hash so a re-plan
        that touches the leaf forces a fresh pack.
    process_config
        The effective process config; its ``sha256`` folds into
        ``plan_hash`` so a config edit invalidates the prior pack.
    doctrine
        The effective doctrine; sliced per the matching heuristic. When
        ``None``, an empty doctrine is used.
    doctrine_cap_bytes
        Soft cap on the doctrine slice size; truncation happens at the
        SECTION boundary, never mid-section. Default 4 KB.
    """
    # Tolerate a mapping input — orchestrators may have leaf shapes that
    # don't share a base class.
    if not isinstance(leaf, ContextPackLeaf):
        leaf = ContextPackLeaf.from_mapping(leaf)
    if doctrine is None:
        doctrine = default_doctrine()

    doctrine_slice, truncated = _select_doctrine_sections(
        doctrine, leaf, cap_bytes=doctrine_cap_bytes
    )

    expected_files_block = (
        "\n".join(f"- `{p}`" for p in leaf.expected_files)
        if leaf.expected_files
        else "(no specific files predicted; touch only what's needed for this leaf)"
    )
    # Rationale on AGENTS.md is the planner's prose (verbatim); the full
    # forensic dump lives in rationale.md.
    rationale_excerpt = leaf.rationale.strip() or leaf.body.strip()
    acceptance_lines = list(leaf.acceptance_criteria)

    agents_md = _render_agents_md(
        leaf,
        rationale_excerpt=rationale_excerpt,
        acceptance_lines=acceptance_lines,
        expected_files_block=expected_files_block,
        doctrine_slice=doctrine_slice,
    )
    rationale_md = _render_rationale_md(leaf)
    acceptance_md = _render_acceptance_md(leaf)
    plan_hash = _compute_plan_hash(leaf, plan_payload, doctrine, process_config)

    return ContextPack(
        leaf_id=leaf.leaf_id,
        agents_md=agents_md,
        rationale_md=rationale_md,
        acceptance_md=acceptance_md,
        plan_hash=plan_hash,
        doctrine_truncated=truncated,
    )


# ---- the commit verb (idempotent) -------------------------------------


# On-disk layout written by commit_context_pack:
#   .requiem/AGENTS.md
#   .requiem/rationale.md
#   .requiem/acceptance.md
#   .requiem/.plan_hash    # the idempotency sentinel
CONTEXT_PACK_DIR = ".requiem"
AGENTS_MD_NAME = "AGENTS.md"
RATIONALE_MD_NAME = "rationale.md"
ACCEPTANCE_MD_NAME = "acceptance.md"
PLAN_HASH_NAME = ".plan_hash"


@dataclass(frozen=True, slots=True)
class CommitReceipt:
    """What :func:`commit_context_pack` returns on Success.

    Returned inside the ``value`` of a :class:`Success` outcome. Carries
    enough provenance to render a "what changed" report (which files
    landed, which commit, whether the call was a no-op).
    """

    plan_hash: str
    leaf_branch: str
    leaf_id: str
    committed: bool
    files_changed: tuple[str, ...] = ()
    reason: str | None = None
    commit_sha: str | None = None
    doctrine_truncated: bool = False


def _pack_files(pack: ContextPack) -> list[tuple[str, str]]:
    """The (relative_path, content) tuples this verb writes.

    The order is fixed — ``AGENTS.md`` first so a coding agent that
    only scans the head of a tree finds the prose first; the forensic
    dumps next; the plan_hash sentinel last (it's an implementation
    detail, not user-facing).
    """
    return [
        (f"{CONTEXT_PACK_DIR}/{AGENTS_MD_NAME}", pack.agents_md),
        (f"{CONTEXT_PACK_DIR}/{RATIONALE_MD_NAME}", pack.rationale_md),
        (f"{CONTEXT_PACK_DIR}/{ACCEPTANCE_MD_NAME}", pack.acceptance_md),
        (f"{CONTEXT_PACK_DIR}/{PLAN_HASH_NAME}", pack.plan_hash + "\n"),
    ]


def _read_existing_plan_hash(repo_path: Path) -> str | None:
    """Return the sentinel plan_hash on disk, or None if absent.

    A previous successful :func:`commit_context_pack` call wrote this
    sentinel; we read it to detect "same hash → no-op" without spawning
    a git process to inspect commit messages.
    """
    sentinel = repo_path / CONTEXT_PACK_DIR / PLAN_HASH_NAME
    if not sentinel.exists():
        return None
    try:
        return sentinel.read_text(encoding="utf-8").strip()
    except OSError:
        return None


async def commit_context_pack(
    fs: FilesystemClient,
    repo_path: Path,
    leaf_branch: str,
    pack: ContextPack,
    *,
    dry_run: bool = False,
) -> Outcome:
    """Land the three pack files on the leaf branch in ONE chore commit.

    Behaviour (ADR-0030 §1 + §Idempotency-and-resume-safety):

    * **Idempotent** — if the sentinel ``.requiem/.plan_hash`` already
      records ``pack.plan_hash``, returns Success with
      ``committed=False, reason="already_current"`` and writes nothing.
      This is the resume-safe path: a re-run on the same plan replays
      this verb as a no-op rather than producing a duplicate commit
      whose only difference is the timestamp.

    * **dry_run** — produces the pack content (via the caller, already
      done) and returns Success with ``committed=False,
      reason="dry_run"``. Never touches the worktree.

    * **Fresh hash** — writes all four files, stages them, creates one
      commit with the message ``chore(context): requiem context pack
      for leaf <id> [plan_hash <hash>]``. The plan_hash in the message
      is a secondary forensic record (the primary check is the sentinel
      file's content).

    Parameters
    ----------
    fs
        :class:`FilesystemClient` bound to the leaf branch's working
        tree (the fanout worktree, or the orchestrator's main repo for
        the sequential path).
    repo_path
        The worktree root, used for file writes and the sentinel probe.
    leaf_branch
        The branch the pack is being committed onto. Recorded in the
        receipt; used to make the commit message and assert-failures
        legible (the verb does NOT switch HEAD — the caller is expected
        to have the leaf branch checked out already).
    pack
        The :class:`ContextPack` from :func:`build_context_pack`.
    dry_run
        When True, computes nothing more and returns immediately.
    """
    if dry_run:
        return Success(value={
            "plan_hash": pack.plan_hash,
            "leaf_branch": leaf_branch,
            "leaf_id": pack.leaf_id,
            "committed": False,
            "files_changed": [],
            "reason": "dry_run",
            "doctrine_truncated": pack.doctrine_truncated,
        })

    # Idempotency probe — match the sentinel against the new hash.
    existing = _read_existing_plan_hash(repo_path)
    if existing is not None and existing == pack.plan_hash:
        return Success(value={
            "plan_hash": pack.plan_hash,
            "leaf_branch": leaf_branch,
            "leaf_id": pack.leaf_id,
            "committed": False,
            "files_changed": [],
            "reason": "already_current",
            "doctrine_truncated": pack.doctrine_truncated,
        })

    files = _pack_files(pack)
    written_rel: list[str] = []
    try:
        for rel, content in files:
            target = repo_path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            fs.write_text(target, content)
            written_rel.append(rel)
    except FsClientError as e:
        return PermanentFailure(
            error_kind="context_pack.write_failed",
            message=f"could not write context pack file: {e}",
            details={"leaf_id": pack.leaf_id, "leaf_branch": leaf_branch},
        )

    try:
        commit_sha = await fs.git_commit(
            f"chore(context): requiem context pack for leaf {pack.leaf_id} "
            f"[plan_hash {pack.plan_hash[:12]}]",
            paths=[Path(p) for p in written_rel],
        )
    except FsGitError as e:
        return PermanentFailure(
            error_kind="context_pack.commit_failed",
            message=(
                f"git commit of context pack for leaf {pack.leaf_id} on "
                f"{leaf_branch} failed: {e.stderr.strip() or e}"
            ),
            details={
                "leaf_id": pack.leaf_id,
                "leaf_branch": leaf_branch,
                "files": written_rel,
            },
        )

    return Success(
        value={
            "plan_hash": pack.plan_hash,
            "leaf_branch": leaf_branch,
            "leaf_id": pack.leaf_id,
            "committed": True,
            "files_changed": written_rel,
            "reason": None,
            "commit_sha": commit_sha,
            "doctrine_truncated": pack.doctrine_truncated,
        },
        inspected_artifacts=tuple(f"file:{p}" for p in written_rel),
    )


# ---- helper for reading the pack back from a worktree -----------------


def read_agents_md(repo_path: Path) -> str | None:
    """Return the bytes of ``.requiem/AGENTS.md`` if present, else None.

    Used by ``implementation.coder_prompt`` to splice the curated
    context into the coder agent's prompt when a pack has been
    committed onto the leaf branch (the typical case under ADR-0030
    §1). Returns None when no pack exists (the worker is running
    outside the Requiem dispatch path or the commit verb hasn't run
    yet) so the caller can fall back to its baseline prompt.
    """
    path = repo_path / CONTEXT_PACK_DIR / AGENTS_MD_NAME
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None
