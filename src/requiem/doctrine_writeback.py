"""Doctrine write-back — the legible self-improvement loop (ADR-0016 §4).

ADR-0016's fourth clause: *"Learning flows back as pull requests. When a run
discovers something durable, Requiem proposes an edit to process.yaml / doctrine
— a normal PR a human reviews. Self-improving, fully on the record."*

This module is the **legible core** of that loop, and only that. It does not
decide *what* is worth learning (the trigger is a separate, still-open concern —
no run has yet produced a learning-extraction signal) and it does not open the
PR itself (that is a thin composition over the existing ``GhClient.pr_create`` /
``FsClient`` push helpers, validated against a live repo). What it owns is the
part that makes write-back *legitimate* under ADR-0016: turning a candidate
learning into a **reviewable, provenance-bearing, idempotent** proposed edit to
the repo-resident doctrine, so the change traces to a committed artifact and the
event log rather than to opaque agent memory.

Scope deliberately narrowed to the **doctrine** target (free-form markdown — the
natural fit for "house-style a returning contributor just knows"). Structured
``process.yaml`` edits are a harder, separate proposal shape and are deferred.

Idempotency is load-bearing: a run that re-discovers the same learning must not
stack duplicate sections. Each requiem-managed section is fenced by a stable
marker so a re-proposal updates in place (or no-ops when identical), never
appends a near-duplicate.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from requiem.doctrine import Doctrine

# A stable, greppable fence so a requiem-managed section can be relocated and
# updated in place across runs (idempotency) and is obvious to a human reviewer.
_OPEN = "<!-- requiem:doctrine-section:{slug} -->"
_CLOSE = "<!-- /requiem:doctrine-section:{slug} -->"


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    return s or "section"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DoctrineProposal:
    """A reviewable, provenance-bearing proposed edit to the doctrine.

    ``proposed_text`` is the FULL new doctrine document (so applying it is a
    single overwrite, and a reviewer diffs it against the current file).
    ``base_sha256`` pins the doctrine the proposal was computed against, so a
    stale proposal (the doctrine moved underneath it) can be detected before it
    is opened as a PR.
    """

    section_title: str
    body: str
    rationale: str
    run_id: str
    proposed_text: str
    base_sha256: str
    root_item: str | None = None
    evidence: tuple[str, ...] = ()
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def slug(self) -> str:
        return _slug(self.section_title)

    @property
    def branch(self) -> str:
        """A stable, run-scoped branch name for the write-back PR."""
        return f"requiem/doctrine/{self.slug}-{self.run_id}"


def _render_section(title: str, body: str, *, run_id: str,
                    rationale: str, evidence: tuple[str, ...]) -> str:
    slug = _slug(title)
    lines = [
        _OPEN.format(slug=slug),
        f"## {title}",
        "",
        body.strip(),
        "",
        f"_Proposed by requiem run `{run_id}` — {rationale.strip()}_",
    ]
    if evidence:
        lines.append(f"_Evidence: {', '.join(evidence)}_")
    lines.append(_CLOSE.format(slug=slug))
    return "\n".join(lines)


def _section_span(text: str, slug: str) -> tuple[int, int] | None:
    """The [start, end) char span of an existing requiem-managed section."""
    open_marker = _OPEN.format(slug=slug)
    close_marker = _CLOSE.format(slug=slug)
    start = text.find(open_marker)
    if start == -1:
        return None
    end = text.find(close_marker, start)
    if end == -1:
        return None
    return start, end + len(close_marker)


def propose_doctrine_section(
    current: Doctrine,
    *,
    section_title: str,
    body: str,
    rationale: str,
    run_id: str,
    root_item: str | None = None,
    evidence: tuple[str, ...] = (),
) -> DoctrineProposal | None:
    """Compute a proposed doctrine edit, or ``None`` when it is a no-op.

    If the doctrine already carries a requiem-managed section with this title
    whose rendered content is identical, there is nothing to propose (idempotent
    re-discovery). Otherwise the section is inserted (appended) or updated in
    place, and the full new document is returned in the proposal.
    """
    slug = _slug(section_title)
    section = _render_section(section_title, body, run_id=run_id,
                              rationale=rationale, evidence=evidence)
    base = current.text
    span = _section_span(base, slug)

    if span is not None:
        start, end = span
        existing = base[start:end]
        # Compare ignoring the proposer/run provenance line so a different run
        # re-asserting the SAME house-style is a real no-op, not churn.
        if _semantic_eq(existing, section):
            return None
        proposed_text = base[:start] + section + base[end:]
    else:
        if base.strip() == "":
            proposed_text = section + "\n"
        else:
            proposed_text = base.rstrip("\n") + "\n\n" + section + "\n"

    if proposed_text == base:
        return None

    return DoctrineProposal(
        section_title=section_title,
        body=body.strip(),
        rationale=rationale.strip(),
        run_id=run_id,
        proposed_text=proposed_text,
        base_sha256=_sha256(base),
        root_item=root_item,
        evidence=tuple(evidence),
    )


def _strip_provenance(section: str) -> str:
    """Drop the run-specific provenance lines so two runs proposing identical
    house-style compare equal (idempotency must not hinge on which run spoke)."""
    kept = [
        ln for ln in section.splitlines()
        if not ln.startswith("_Proposed by requiem run")
        and not ln.startswith("_Evidence:")
    ]
    return "\n".join(kept).strip()


def _semantic_eq(a: str, b: str) -> bool:
    return _strip_provenance(a) == _strip_provenance(b)


def is_stale(proposal: DoctrineProposal, current: Doctrine) -> bool:
    """True when the doctrine moved since the proposal was computed.

    A stale proposal must not be opened as a PR blindly — recompute it against
    the current doctrine first (the same fail-closed instinct as the run's
    config/doctrine snapshot durability).
    """
    return _sha256(current.text) != proposal.base_sha256


def render_pr_title(proposal: DoctrineProposal) -> str:
    scope = f" ({proposal.root_item})" if proposal.root_item else ""
    return f"doctrine: {proposal.section_title}{scope}"


def render_pr_body(proposal: DoctrineProposal) -> str:
    """A legible PR description that traces the proposal to its provenance.

    Per ADR-0016, write-back is only legitimate when it is on the record: the
    body names the run, the rationale, and the supporting evidence so a human
    reviews a *traceable* change, not an opaque "the agent learned something".
    """
    lines = [
        f"Proposed by requiem run `{proposal.run_id}` as a house-style "
        "write-back (ADR-0016 §4).",
        "",
        "## Why",
        proposal.rationale,
        "",
        "## Proposed doctrine section",
        f"**{proposal.section_title}**",
        "",
        proposal.body,
    ]
    if proposal.root_item:
        lines += ["", f"Originating work item: `{proposal.root_item}`"]
    if proposal.evidence:
        lines += ["", "## Evidence",
                  *(f"- {e}" for e in proposal.evidence)]
    lines += [
        "",
        "---",
        "_This is a normal PR. Execution stays hermetic; this is the only "
        "channel by which a run changes repo-resident house-style — reviewed, "
        "on the record, never via opaque agent memory._",
    ]
    return "\n".join(lines)
