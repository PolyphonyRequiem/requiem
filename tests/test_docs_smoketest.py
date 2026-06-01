"""Smoketest for the user-facing docs.

Re-runs the commands shown in the docs and asserts the narration lines
shown in `README.md` and `docs/getting-started.md` still appear verbatim
in the actual output. This is a stale-doc detector, not an exhaustive
behaviour test — `test_integration_code_review.py` owns that.

What "verbatim" means here: each canonical narration line must appear as
a substring of the captured CLI output. We do NOT pin the run id (it is
timestamped), the elapsed-ms timing, or the absolute log path — those
legitimately vary per machine and per run. We DO pin the glyph + label
prefix and the message body so a renderer change or a workflow
re-tagging fails the smoketest loudly.
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

import pytest

from requiem.cli.main import main as cli_main


REPO_ROOT = Path(__file__).resolve().parent.parent

DOC_FILES = {
    "README.md":                REPO_ROOT / "README.md",
    "getting-started.md":       REPO_ROOT / "docs" / "getting-started.md",
    "concepts.md":              REPO_ROOT / "docs" / "concepts.md",
    "writing-workflows.md":     REPO_ROOT / "docs" / "writing-workflows.md",
    "cookbook.md":              REPO_ROOT / "docs" / "cookbook.md",
}


# ---- expected narration lines (the one-screen success path) -------
#
# These appear verbatim in README.md's "What you'll see" block and in
# getting-started.md's step-2 sample output. If the renderer or the
# demo workflow drifts, this list breaks and the docs need updating.

EXPECTED_NARRATION_LINES = [
    "▶ run_started — code-review on sample_snippet.py",
    "✓ Read sample_snippet.py — 7 lines",
    "🔁 Lint failed: linter spawned a child process that exited 137 (OOM) — retrying (attempt 2)",
    "✓ Lint passed on attempt 2",
    "▶ Started 3 reviewers in parallel",
    "  ✓ style_reviewer: warn — mutable default argument `cache={}` will leak state across calls",
    "  ✓ correctness_reviewer: blocking — `int(x)` raises ValueError on bad input; no handling",
    "  ✓ performance_reviewer: info — linear scan of `cache.keys()` could be O(1) dict lookup",
    "✓ Synthesized verdict — don't merge (1 warn, 1 blocking, 1 info)",
    "🚦 Gate: Reviewer team finished. Approve verdict? (auto-approved for demo)",
    "     ↳ verdict: don't merge — top finding: unhandled ValueError on int(x)",
    "■ Completed — code-review on sample_snippet.py",
]

# The verdict card block — same source-of-truth.
EXPECTED_VERDICT_LINES = [
    "🚫 Don't merge",
    "Top finding:  unhandled ValueError on int(x)",
    "Rationale:    1 blocking + 1 warn; correctness reviewer's unhandled ValueError must be fixed before merge.",
]


# ---- helpers ------------------------------------------------------


def _run_cli(argv: list[str]) -> tuple[int, str]:
    """Invoke `requiem` in-process, capturing stdout."""
    # `rich` autodetects terminal width; force a wide buffer so lines
    # don't wrap and confuse substring matches.
    import os
    old_cols = os.environ.get("COLUMNS")
    os.environ["COLUMNS"] = "200"
    buf = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = buf
    try:
        rc = cli_main(argv)
    finally:
        sys.stdout = real_stdout
        if old_cols is None:
            os.environ.pop("COLUMNS", None)
        else:
            os.environ["COLUMNS"] = old_cols
    return rc, buf.getvalue()


# ---- the smoketest -------------------------------------------------


def test_demo_run_matches_documented_narration(tmp_path: Path) -> None:
    """`requiem run requiem.workflows.code_review_demo` produces every
    narration line the docs claim it produces."""
    log_dir = tmp_path / "runs"
    rc, output = _run_cli([
        "run",
        "requiem.workflows.code_review_demo",
        "--run-id", "smoketest-001",
        "--log-dir", str(log_dir),
    ])
    assert rc == 0, f"requiem run exited {rc}; output:\n{output}"
    missing = [line for line in EXPECTED_NARRATION_LINES if line not in output]
    assert not missing, (
        "The docs claim these narration lines appear verbatim, but they "
        "didn't show up in the actual `requiem run` output. Either fix "
        "the renderer/workflow or update the docs.\n\nMissing:\n  "
        + "\n  ".join(missing)
        + "\n\nFull output:\n"
        + output
    )
    for vline in EXPECTED_VERDICT_LINES:
        assert vline in output, (
            f"Verdict card line missing from output: {vline!r}\n"
            f"Full output:\n{output}"
        )


def test_demo_run_writes_event_log(tmp_path: Path) -> None:
    """`requiem events <run_id> --raw` works on the freshly-written log,
    matching the cookbook claim."""
    log_dir = tmp_path / "runs"
    rc, _ = _run_cli([
        "run",
        "requiem.workflows.code_review_demo",
        "--run-id", "smoketest-002",
        "--log-dir", str(log_dir),
    ])
    assert rc == 0
    log_path = log_dir / "smoketest-002.events.jsonl"
    assert log_path.exists(), f"docs claim a log appears at {log_path}; it didn't"

    rc, raw = _run_cli([
        "events", "smoketest-002",
        "--log-dir", str(log_dir),
        "--raw",
    ])
    assert rc == 0
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    assert lines, "raw events output was empty"
    # Every line must parse as JSON with a 'kind' field — that's the
    # stable contract the cookbook hands consumers.
    import json
    for ln in lines:
        ev = json.loads(ln)
        assert "kind" in ev, f"event without 'kind': {ev!r}"


def test_demo_describe_lists_team_branches(tmp_path: Path) -> None:
    """`requiem describe` prints what the docs claim — used as the
    'inspect the workflow shape' step in getting-started.md."""
    rc, output = _run_cli([
        "describe",
        "requiem.workflows.code_review_demo",
    ])
    assert rc == 0
    for needle in (
        "workflow: code-review",
        "entry : start",
        "branches=[style_reviewer,correctness_reviewer,performance_reviewer]",
        "human_gate",
        "humanize",
    ):
        assert needle in output, (
            f"`requiem describe` did not contain {needle!r}. The "
            f"docs reference this. Output:\n{output}"
        )


# ---- doc-shape assertions (Demo Contract §3.11-§3.13) -------------


def _read_doc(name: str) -> str:
    p = DOC_FILES[name]
    assert p.exists(), f"expected doc {p} to exist"
    return p.read_text(encoding="utf-8")


def test_readme_opens_with_workday_vignette() -> None:
    """Demo Contract §3.11 — README's first paragraph is the workday
    vignette, not a thesis about Phase A."""
    body = _read_doc("README.md")
    first_para = body.split("\n\n", 2)[1].lower()  # skip the `# Requiem` title
    assert "monday" in first_para or "morning" in first_para or "coffee" in first_para, (
        "README's first paragraph should be the workday vignette per Demo "
        f"Contract §3.11. Got:\n{first_para}"
    )
    forbidden_in_first_para = ["phase a", "walking skeleton", "successor to polyphony"]
    for term in forbidden_in_first_para:
        assert term not in first_para, (
            f"README's first paragraph contains {term!r} — that's a thesis, "
            f"not a vignette. Move it lower. Para:\n{first_para}"
        )


def test_readme_toc_has_workday_section() -> None:
    """Demo Contract §3.12 — README has a 'What does this mean for my
    workday?' section."""
    body = _read_doc("README.md")
    assert "What does this mean for my workday" in body, (
        "README must include a section heading exactly 'What does this "
        "mean for my workday?' per Demo Contract §3.12."
    )


def test_readme_what_youll_see_fits_one_screen() -> None:
    """Demo Contract §3.13 — the 'what you'll see' block fits in ~40
    terminal lines (one screen)."""
    body = _read_doc("README.md")
    m = re.search(r"## What you'll see\s*\n(.*?)(?=\n## )", body, re.DOTALL)
    assert m, "README missing '## What you'll see' section"
    block = m.group(1)
    code_blocks = re.findall(r"```[^\n]*\n(.*?)\n```", block, re.DOTALL)
    assert code_blocks, "'What you'll see' section needs at least one fenced code block"
    longest = max(len(cb.splitlines()) for cb in code_blocks)
    assert longest <= 40, (
        f"'What you'll see' code block is {longest} lines; Demo Contract "
        f"§3.13 says one terminal screen (~40). Trim it."
    )


def test_readme_sample_output_matches_real_narration() -> None:
    """The narration lines pasted into the README must match the actual
    `requiem run` output. Catches doc drift after a renderer/workflow
    change."""
    readme = _read_doc("README.md")
    # Spot-check three load-bearing narration lines from the README's
    # `## What you'll see` block. (Full verbatim is checked by
    # test_demo_run_matches_documented_narration above against the live
    # CLI output; this asserts the README and the runtime are pinned to
    # the same strings.)
    for line in (
        "▶ run_started — code-review on sample_snippet.py",
        "🔁 Lint failed: linter spawned a child process that exited 137 (OOM) — retrying (attempt 2)",
        "✓ Synthesized verdict — don't merge (1 warn, 1 blocking, 1 info)",
        "■ Completed — code-review on sample_snippet.py",
    ):
        assert line in readme, (
            f"README's 'What you'll see' block is missing the verbatim "
            f"narration line: {line!r}"
        )


def test_getting_started_sample_output_matches_real_narration() -> None:
    """Same drift check for `getting-started.md` step 2."""
    body = _read_doc("getting-started.md")
    for line in (
        "▶ run_started — code-review on sample_snippet.py",
        "✓ Read sample_snippet.py — 7 lines",
        "■ Completed — code-review on sample_snippet.py",
    ):
        assert line in body, (
            f"getting-started.md is missing the verbatim narration line: "
            f"{line!r}"
        )


# ---- commands-mentioned cross-check --------------------------------


@pytest.mark.parametrize("doc,name", sorted(DOC_FILES.items()))
def test_doc_exists_and_is_nontrivial(doc: str, name: Path) -> None:
    """Belt-and-braces: every shipped doc exists and isn't empty."""
    assert name.exists(), f"missing doc: {name}"
    body = name.read_text(encoding="utf-8")
    assert len(body) > 500, f"doc {doc} is suspiciously short: {len(body)} bytes"
