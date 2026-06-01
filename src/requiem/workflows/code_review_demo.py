"""Code-review demo workflow — the walking-skeleton α composition, promoted.

Composes every Phase A recommended variant into one runnable workflow:

    start                  (script)   emit run_started
      → read_snippet       (script)   Toolbelt FileClient
      → flaky_lint         (script)   retry: fails once, succeeds on attempt 2
      → review_team        (team)     parallel_fork over 3 reviewer agents
      → synthesize         (agent)    reads team findings, produces Verdict
      → human_gate         (gate)     auto-resolved by the demo handler
      → archive            (script)   writes a markdown summary
      → end                (terminate)

Public entry points (the contract `requiem run <module>` consumes):

* ``build_workflow() -> Workflow``
* ``build_engine(log_dir, *, gate_handler=...) -> Engine``
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from requiem.agent import AgentSpec, FakeProvider
from requiem.dsl import AgentRegistry, VerbRegistry, Workflow, WorkflowBuilder
from requiem.kernel import Engine
from requiem.outcomes import PermanentFailure, RetryableFailure, Success
from requiem.toolbelt import FileMissing, FileRead, Toolbelt


# ---- the snippet under review (inlined for self-contained demo) ----


SAMPLE_SNIPPET = '''\
def lookup_or_compute(x, cache={}):
    """Tiny intentionally-flawed snippet for the code-review demo."""
    if x in cache.keys():
        return cache[x]
    value = int(x) * 2
    cache[x] = value
    return value
'''


# ---- typed agent outputs --------------------------------------------


class ReviewFinding(BaseModel):
    severity: Literal["info", "warn", "blocking"]
    category: str
    summary: str
    line_hint: int | None = None


class Verdict(BaseModel):
    recommend_merge: bool
    rationale: str
    top_finding: str
    severity_seen: list[Literal["info", "warn", "blocking"]] = Field(
        default_factory=list
    )


# ---- agent specs ----------------------------------------------------


STYLE = AgentSpec(
    name="style_reviewer",
    charter=(
        "You enforce house style. Look for naming, formatting, mutability, "
        "and clarity. You do NOT block on perf or correctness."
    ),
    response_model=ReviewFinding,
)

CORRECTNESS = AgentSpec(
    name="correctness_reviewer",
    charter=(
        "You hunt for bugs. Off-by-one, exception swallowing, mutable "
        "default args, missing error handling. Severity is `blocking` if "
        "the code is observably wrong."
    ),
    response_model=ReviewFinding,
)

PERFORMANCE = AgentSpec(
    name="performance_reviewer",
    charter=(
        "You look for O(n^2) loops, redundant I/O, missing async, and "
        "obvious allocations. Severity is `warn` unless catastrophic."
    ),
    response_model=ReviewFinding,
)

SYNTHESIZER = AgentSpec(
    name="synthesizer",
    charter=(
        "You read all reviewer findings and decide whether to recommend "
        "merge. Blocking findings veto. You synthesize one rationale."
    ),
    response_model=Verdict,
)

ALL_SPECS = [STYLE, CORRECTNESS, PERFORMANCE, SYNTHESIZER]


# ---- FakeProvider scripts (the canonical happy path) ---------------


def scripted_provider() -> FakeProvider:
    return FakeProvider(scripts={
        "style_reviewer": [
            {"severity": "warn", "category": "style",
             "summary": "mutable default argument `cache={}` will leak state across calls",
             "line_hint": 3},
        ],
        "correctness_reviewer": [
            {"severity": "blocking", "category": "correctness",
             "summary": "`int(x)` raises ValueError on bad input; no handling",
             "line_hint": 5},
        ],
        "performance_reviewer": [
            {"severity": "info", "category": "performance",
             "summary": "linear scan of `cache.keys()` could be O(1) dict lookup",
             "line_hint": 7},
        ],
        "synthesizer": [
            {"recommend_merge": False,
             "rationale": "1 blocking + 1 warn; correctness reviewer's "
                          "unhandled ValueError must be fixed before merge.",
             "top_finding": "unhandled ValueError on int(x)",
             "severity_seen": ["warn", "blocking", "info"]},
        ],
    })


# ---- verb library ---------------------------------------------------


def build_verb_registry(snippet_path: Path) -> VerbRegistry:
    verbs = VerbRegistry()

    @verbs.register("start_run")
    def _start(ctx):
        return Success(value={"intent": "code-review", "target": str(snippet_path)})

    @verbs.register("read_snippet")
    def _read(ctx):
        outcome = ctx.toolbelt.files.read_text(snippet_path)
        match outcome:
            case FileRead(content=text):
                return Success(
                    value={"snippet": text, "loc": len(text.splitlines())},
                    inspected_artifacts=(f"file:{snippet_path}",),
                )
            case FileMissing(path=p):
                return PermanentFailure(
                    error_kind="snippet.missing",
                    message=f"snippet not found at {p}",
                )

    @verbs.register("flaky_lint")
    def _flaky(ctx):
        # Cross-attempt counter rides in ctx.attempt; the verb is idempotent
        # because the retry decision is the engine's, not the verb's.
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
        out = snippet_path.parent / ".runs" / f"{ctx.run_id}.summary.md"
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
            value={
                "summary_path": str(out),
                "recommend_merge": verdict["recommend_merge"],
            },
            inspected_artifacts=(f"file:{out}",),
        )

    return verbs


def build_agent_registry() -> AgentRegistry:
    reg = AgentRegistry()
    for spec in ALL_SPECS:
        reg.register(spec)
    return reg


# ---- the workflow (Wagner A fluent builder) ------------------------


def build_workflow() -> Workflow:
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
                .edge("synthesize", on="bad_output", to="fail_end")
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


# ---- engine factory (the `requiem run` contract) -------------------


def _default_gate_handler(node_id: str, prompt: str, options: tuple[str, ...]) -> str:
    """Demo gate handler: prints the prompt and auto-picks `approve`."""
    print(f"  [gate {node_id}] {prompt}")
    print(f"  [gate {node_id}] options: {options} → auto-picking 'approve'")
    return "approve"


def build_engine(
    log_dir: Path,
    *,
    snippet_path: Path | None = None,
    gate_handler=None,
) -> Engine:
    """Construct a runnable Engine for this workflow.

    The CLI calls this with `log_dir` set to the chosen output directory.
    `snippet_path` defaults to a tempfile written from the inline SAMPLE_SNIPPET
    so the demo is self-contained (no external file required).
    """
    if snippet_path is None:
        snippet_path = log_dir / "sample_snippet.py"
        snippet_path.parent.mkdir(parents=True, exist_ok=True)
        if not snippet_path.exists():
            snippet_path.write_text(SAMPLE_SNIPPET, encoding="utf-8")

    return Engine(
        workflow=build_workflow(),
        verbs=build_verb_registry(snippet_path),
        agents=build_agent_registry(),
        provider=scripted_provider(),
        toolbelt=Toolbelt.real(),
        log_dir=log_dir,
        gate_handler=gate_handler or _default_gate_handler,
    )
