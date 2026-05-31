"""The walking-skeleton α `code-review` workflow.

Composes every Phase A recommended variant into one runnable shape:

    start                  (script)        emit RunStarted
      → read_snippet       (script)        Liszt FileClient via Toolbelt
      → flaky_lint         (script)        retry: fails once, succeeds on attempt 2
      → review_team        (team)          parallel_fork over 3 reviewers
      → synthesize         (agent)         reads team findings, produces Verdict
      → human_gate         (gate)          auto-resolved in the demo
      → archive            (script)        writes a markdown summary
      → end                (terminate)
"""
from __future__ import annotations

from pathlib import Path

from engine.dsl import AgentRegistry, VerbRegistry, WorkflowBuilder
from engine.outcomes import RetryableFailure, Success
from engine.toolbelt import FileMissing, FileRead

from reviewers import ALL_SPECS

SNIPPET_PATH = Path(__file__).parent / "sample_snippet.py"


# ---- verb library ---------------------------------------------------


def build_verb_registry() -> VerbRegistry:
    verbs = VerbRegistry()

    @verbs.register("start_run")
    def _start(ctx):
        return Success(value={"intent": "code-review", "target": str(SNIPPET_PATH)})

    @verbs.register("read_snippet")
    def _read(ctx):
        outcome = ctx.toolbelt.files.read_text(SNIPPET_PATH)
        match outcome:
            case FileRead(content=text):
                return Success(
                    value={"snippet": text, "loc": len(text.splitlines())},
                    inspected_artifacts=(f"file:{SNIPPET_PATH}",),
                )
            case FileMissing(path=p):
                from engine.outcomes import PermanentFailure
                return PermanentFailure(
                    error_kind="snippet.missing",
                    message=f"sample_snippet.py not found at {p}",
                )

    # Cross-attempt state: closure over `attempts`. Verbs MUST be idempotent
    # w.r.t. side effects on retry — this one is, because the attempt count
    # is ctx.attempt, not the closure counter.
    @verbs.register("flaky_lint")
    def _flaky(ctx):
        if ctx.attempt < 2:
            return RetryableFailure(
                retry_key=f"{ctx.run_id}:flaky_lint",
                error_kind="lint.transient",
                message="linter spawned a child process that exited 137 (OOM)",
                attempt=ctx.attempt,
            )
        return Success(value={"lint_passed": True, "attempts_used": ctx.attempt})

    @verbs.register("review_prompt")
    def _review_prompt(ctx):
        snippet = ctx.completed["read_snippet"]["value"]["snippet"]
        return f"Review this code:\n```python\n{snippet}```"

    @verbs.register("synth_prompt")
    def _synth_prompt(ctx):
        team_result = ctx.completed["review_team"]["value"]
        findings = team_result["findings"]
        body = "\n".join(
            f"- {f['agent']}: severity={f['result']['parsed']['severity']} — "
            f"{f['result']['parsed']['summary']}"
            for f in findings
        )
        return f"Reviewer findings:\n{body}\n\nProduce a Verdict."

    @verbs.register("archive_summary")
    def _archive(ctx):
        verdict = ctx.completed["synthesize"]["value"]["parsed"]
        team = ctx.completed["review_team"]["value"]
        out = SNIPPET_PATH.parent / ".runs" / f"{ctx.run_id}.summary.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        body = [
            f"# code-review summary — {ctx.run_id}",
            "",
            f"**Recommend merge:** {verdict['recommend_merge']}",
            f"**Top finding:** {verdict['top_finding']}",
            f"**Rationale:** {verdict['rationale']}",
            "",
            "## Reviewer findings",
        ]
        for f in team["findings"]:
            p = f["result"]["parsed"]
            body.append(f"- **{f['agent']}** ({p['severity']}): {p['summary']}")
        out.write_text("\n".join(body) + "\n", encoding="utf-8")
        return Success(
            value={"summary_path": str(out), "recommend_merge": verdict["recommend_merge"]},
            inspected_artifacts=(f"file:{out}",),
        )

    return verbs


def build_agent_registry() -> AgentRegistry:
    reg = AgentRegistry()
    for spec in ALL_SPECS:
        reg.register(spec)
    return reg


# ---- the workflow itself (Wagner A fluent builder) -----------------


def build_workflow():
    return (
        WorkflowBuilder("code-review")
            .entry("start")
            .script("start", verb="start_run")
                .edge("start", on="success", to="read_snippet")
            .script("read_snippet", verb="read_snippet")
                .edge("read_snippet", on="success", to="flaky_lint")
                .edge("read_snippet", on="permanent_failure", to="fail_end")
            .script("flaky_lint", verb="flaky_lint", retry_max=2)
                .edge("flaky_lint", on="success", to="review_team")
                .edge("flaky_lint", on="retry_exhausted", to="fail_end")
            .team(
                "review_team",
                team_id="reviewers",
                branches=[
                    ("style_reviewer",        "review_prompt"),
                    ("correctness_reviewer",  "review_prompt"),
                    ("performance_reviewer",  "review_prompt"),
                ],
            )
                .edge("review_team", on="success", to="synthesize")
                .edge("review_team", on="permanent_failure", to="fail_end")
            .agent("synthesize", agent="synthesizer", prompt_verb="synth_prompt")
                .edge("synthesize", on="success", to="human_gate")
            .human_gate(
                "human_gate",
                prompt="Reviewer team finished. Approve verdict?",
                options=["approve", "reject"],
            )
                .edge("human_gate", on="needs_human:approve", to="archive")
                .edge("human_gate", on="needs_human:reject",  to="fail_end")
            .script("archive", verb="archive_summary")
                .edge("archive", on="success", to="end")
            .terminate("end", disposition="completed")
            .terminate("fail_end", disposition="failed")
            .build()
    )
