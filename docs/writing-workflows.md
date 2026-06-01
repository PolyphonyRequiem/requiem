# Writing workflows

A line-by-line tour of
[`code_review_demo.py`](../src/requiem/workflows/code_review_demo.py) —
the shortest path from "I ran the demo" to "I have my own workflow."

If you haven't yet, run it: `requiem run requiem.workflows.code_review_demo`.

Every Requiem workflow module exposes:

```python
def build_workflow() -> Workflow: ...
def build_engine(log_dir, *, ...) -> Engine: ...
```

The first is the topology. The second is the topology *plus* its
dependencies (verbs, agents, the LLM provider, the toolbelt, the gate
handler). The CLI calls `build_engine(log_dir)`.

---

## 1. The `Workflow(...)` builder

The fluent builder lives in [`requiem.dsl`](../src/requiem/dsl.py). You
chain a `WorkflowBuilder` and call `.build()` at the end:

```python
return (
    WorkflowBuilder("code-review")
        .entry("start")
        .script("start", verb="start_run")
        # ... more nodes and edges ...
        .terminate("end", disposition="completed")
        .build()
)
```

`.build()` runs topology validation: missing entry, duplicate node id,
edge to/from an unknown node, no terminator — every one of those raises
at construction. That's invariant `INV-NO-CORRUPT-FORWARD` applied to
authoring: a bad workflow can't run.

## 2. Adding nodes

Five node kinds. Pick by what the node *does*:

```python
.script("read_snippet", verb="read_snippet")     # call a Python verb
.agent("synthesize",                              # call one LLM agent
       agent="synthesizer",
       prompt_verb="synth_prompt")
.team("review_team",                              # N agents in parallel
      team_id="reviewers",
      branches=[("style_reviewer", "review_prompt"),
                ("correctness_reviewer", "review_prompt"),
                ("performance_reviewer", "review_prompt")])
.human_gate("human_gate",                         # pause and ask a human
            prompt="Approve verdict?",
            options=["approve", "reject"])
.terminate("end", disposition="completed")        # end the run
```

Every node has a unique `node_id`. That id appears in narration, in the
event log, in `requiem describe` output, and in your edges. Pick names
your future self will read.

## 3. Adding edges

Edges connect nodes by node id and key off the outcome variant of the
source node:

```python
.script("read_snippet", verb="read_snippet")
    .edge("read_snippet", on="success",            to="flaky_lint")
    .edge("read_snippet", on="permanent_failure",  to="fail_end")
.script("flaky_lint", verb="flaky_lint", retry_max=2)
    .edge("flaky_lint", on="success",          to="review_team")
    .edge("flaky_lint", on="retry_exhausted",  to="fail_end")
```

The valid `on=` strings mirror the outcome variants from
[`concepts.md`](concepts.md): `success`, `retryable_failure`,
`permanent_failure`, `bad_output`, `cancelled`. Two specials:

- `on="retry_exhausted"` — wired when `retry_max > 0`; the engine takes
  this edge after the retry budget runs out instead of `retryable_failure`.
- `on="needs_human:<choice>"` — for gates, one edge per option.

If a node returns an outcome you didn't wire an edge for, the run fails
explicitly rather than falling through silently.

## 4. Adding gates

Gates pause the run, surface a prompt + options, and route on the
human's choice. The CLI exits with code `2` ("suspended") when it hits a
gate; `requiem resume` reads the chosen option and continues.

```python
.human_gate(
    "human_gate",
    prompt="Reviewer team finished. Approve verdict?",
    options=["approve", "reject"],
)
    .edge("human_gate", on="needs_human:approve", to="archive")
    .edge("human_gate", on="needs_human:reject",  to="fail_end")
```

For unattended runs (CI, demos) you can pass a `gate_handler` to
`build_engine`; the demo's handler auto-picks `"approve"` and sets a
`__requiem_auto__ = True` attribute so the renderer notes
"(auto-approved for demo)":

```python
def _default_gate_handler(node_id, prompt, options):
    return "approve"
_default_gate_handler.__requiem_auto__ = True
```

## 5. The `humanize` map

The CLI renderer ships glyphs and outcome-aware lines; you supply
human-readable noun phrases per node:

```python
.humanize({
    "start":        "Starting code-review",
    "read_snippet": "Read sample_snippet.py",
    "flaky_lint":   "Lint",
    "review_team":  "reviewers",
    "synthesize":   "Synthesized verdict",
    "human_gate":   "approve verdict?",
    "archive":      "Wrote summary",
    "end":          "code-review",
    "fail_end":     "code-review",
})
```

This is the Demo Contract (`perspectives/ui-sdlc/07-demo-contract.md`)
applied at the workflow level. The renderer falls back to the raw
`node_id` when a node is missing, but every customer-facing node should
appear here. Without `humanize`, your output reads like a Python repr.

## 6. Teams: `parallel_fork` for agent squads

Three reviewers, one prompt verb each, all in parallel:

```python
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
```

The team succeeds when every branch succeeds. The combined success
payload is `{"findings": [{"agent": ..., "result": ...}, ...]}`, ready
for a follow-on synthesiser. If any branch returns
`PermanentFailure` / `BadOutput` / `Cancelled`, the team aggregates
to that outcome — your edges handle it.

See [ADR 0003](decisions/0003-agent-teams-first-class.md) for why teams
are first-class instead of "spawn N sub-workflows by hand."

## 7. Render hints: per-node detail + verdict card

The CLI is one renderer over the event log. Workflows opt into richer
output with two optional module-level functions: `render_hints()` and
`verdict_card(completed)`.

`render_hints()` returns a `dict` consumed by the renderer's
`RenderContext` (see [`requiem.cli.render`](../src/requiem/cli/render.py)):

```python
def render_hints():
    return {
        "artifact_name": "sample_snippet.py",      # appears in run_started / run_completed
        "details": {                                # per-node value -> noun phrase
            "read_snippet": lambda v: f"{v['loc']} lines",
            "synthesize":   _detail_synthesize,
            "archive":      lambda v: f"to {v['summary_path']}",
        },
        "gate_contexts": {                          # extra line under a gate
            "human_gate": _gate_context_human_gate,
        },
        "silent_nodes": frozenset({"start", "review_team", "end", "fail_end"}),
    }
```

`silent_nodes` is the escape hatch for orchestration scaffolding whose
story is already told by another event (the `run_started` line covers
`start`; team branch lines cover the aggregator's success; terminators
are covered by `run_completed`).

`verdict_card(completed)` prints the post-run summary (the
`─── Verdict ───` block):

```python
def verdict_card(completed):
    synth = completed.get("synthesize", {}).get("value", {}).get("parsed") or {}
    if not synth:
        return None
    head = "✓ Merge" if synth["recommend_merge"] else "🚫 Don't merge"
    return f"  {head}\n      Top finding: {synth['top_finding']}"
```

Both hooks are optional. Skip them and the CLI falls back to node ids
and no post-run card.

## 8. The engine factory

The CLI calls `build_engine(log_dir)`. Here is the demo's:

```python
def build_engine(log_dir, *, snippet_path=None, gate_handler=None) -> Engine:
    if snippet_path is None:
        snippet_path = log_dir / "sample_snippet.py"
        snippet_path.parent.mkdir(parents=True, exist_ok=True)
        if not snippet_path.exists():
            snippet_path.write_text(SAMPLE_SNIPPET, encoding="utf-8")

    return Engine(
        workflow=build_workflow(),
        verbs=build_verb_registry(snippet_path),
        agents=build_agent_registry(),
        provider=scripted_provider(),      # FakeProvider for the demo
        toolbelt=Toolbelt.real(),
        log_dir=log_dir,
        gate_handler=gate_handler or _default_gate_handler,
    )
```

In a real workflow:

- `provider=` is your LLM client (a `FakeProvider` for tests, a real
  one for production — Phase B ships them).
- `toolbelt=Toolbelt.real()` exposes typed per-tool clients (files, git,
  etc.) — see [`requiem.toolbelt`](../src/requiem/toolbelt.py).
- `gate_handler=` is `None` for interactive use (the kernel suspends),
  a callable for unattended runs.

## 9. Putting it together

A useful checklist before `git commit`:

- [ ] `WorkflowBuilder` chain ends in `.build()` and includes at least
      one `.terminate(...)`.
- [ ] Every node id appears in `.humanize(...)`.
- [ ] Every outcome a node can return has an outgoing edge.
- [ ] `build_engine(log_dir)` returns an `Engine` with all
      dependencies wired.
- [ ] `requiem describe <your.module>` prints what you expect.
- [ ] `requiem run <your.module>` succeeds; the narration reads like a
      product, not a stack trace.

When in doubt, copy `requiem.workflows.code_review_demo` and modify.
That file is canonical by design — when a Phase B PR adds a primitive,
the demo grows the example for it.

## See also

- [`concepts.md`](concepts.md) — the vocabulary.
- [`cookbook.md`](cookbook.md) — short recipes.
- [`requiem.dsl`](../src/requiem/dsl.py) — the builder source (small,
  read it).
- [ADR 0002](decisions/0002-phase-a-integrated-design.md) — why the
  engine is shaped this way.
- [ADR 0004](decisions/0004-cross-cutting-defaults.md) — defaults for
  every cross-cutting question (verb-by-name vs by-reference,
  `error_kind` taxonomy, etc.).
