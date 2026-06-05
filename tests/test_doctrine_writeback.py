"""Doctrine write-back — the legible self-improvement core (ADR-0016 §4).

These prove the part that makes write-back *legitimate*: a candidate learning
becomes a reviewable, provenance-bearing, idempotent proposed doctrine edit.
The trigger (what to learn) and the live PR-open are deliberately out of scope.
"""

from __future__ import annotations

from requiem.doctrine import Doctrine, default_doctrine
from requiem.doctrine_writeback import (
    DoctrineProposal,
    is_stale,
    propose_doctrine_section,
    render_pr_body,
    render_pr_title,
)


def _doc(text: str) -> Doctrine:
    return Doctrine(text=text)


def _propose(current: Doctrine, *, run_id: str = "run-1", **kw) -> DoctrineProposal | None:
    base = dict(section_title="Test command", body="Run `pytest -q`, never the full suite.",
                rationale="three runs hit the full-suite hang", run_id=run_id)
    base.update(kw)
    return propose_doctrine_section(current, **base)


def test_proposes_section_into_empty_doctrine():
    p = _propose(default_doctrine())
    assert p is not None
    assert "## Test command" in p.proposed_text
    assert "Run `pytest -q`" in p.proposed_text
    assert "requiem:doctrine-section:test-command" in p.proposed_text
    assert p.branch == "requiem/doctrine/test-command-run-1"


def test_appends_without_clobbering_existing_doctrine():
    current = _doc("# House style\n\nUse single quotes.\n")
    p = _propose(current)
    assert p is not None
    assert "Use single quotes." in p.proposed_text  # preserved
    assert "## Test command" in p.proposed_text       # added


def test_reproposing_identical_section_is_a_noop():
    current = default_doctrine()
    first = _propose(current)
    assert first is not None
    # A *different* run re-discovering the SAME house-style must not churn.
    second = _propose(_doc(first.proposed_text), run_id="run-2")
    assert second is None


def test_updating_section_body_replaces_in_place():
    current = default_doctrine()
    first = _propose(current)
    assert first is not None
    updated = propose_doctrine_section(
        _doc(first.proposed_text),
        section_title="Test command",
        body="Run `pytest -q -k <suite>`; the full suite hangs.",
        rationale="sharpened after another hang",
        run_id="run-3",
    )
    assert updated is not None
    # Exactly one managed section — updated in place, not duplicated.
    assert updated.proposed_text.count("<!-- requiem:doctrine-section:test-command -->") == 1
    assert "hangs" in updated.proposed_text


def test_provenance_is_carried_and_rendered():
    p = _propose(default_doctrine(), root_item="880",
                 evidence=("event:42", "leaf:22002"))
    assert p is not None
    assert p.root_item == "880"
    body = render_pr_body(p)
    assert "run `run-1`" in body
    assert "880" in body
    assert "event:42" in body and "leaf:22002" in body
    assert "ADR-0016" in body
    title = render_pr_title(p)
    assert title == "doctrine: Test command (880)"


def test_is_stale_detects_doctrine_drift():
    current = default_doctrine()
    p = _propose(current)
    assert p is not None
    assert not is_stale(p, current)
    # Doctrine moved underneath the proposal — must be flagged stale.
    assert is_stale(p, _doc("# something else entirely\n"))


def test_proposed_text_is_applied_as_single_overwrite():
    """proposed_text is the FULL new document, so applying it is one write."""
    current = _doc("# Doctrine\n\nExisting rule.\n")
    p = _propose(current)
    assert p is not None
    # Round-trip: feeding proposed_text back yields a stable no-op.
    again = _propose(_doc(p.proposed_text), run_id="run-9")
    assert again is None
