# Variant C — data-driven graph

> **Shape:** the workflow is a pydantic data model (`WorkflowModel`
> with typed `Node` and `Edge` lists). Verbs live in a `VerbRegistry`
> keyed by string name. The engine is an *interpreter* over the
> model: it switches on node `kind`, looks up verbs by name, never
> sees user code as types.

## Run

```pwsh
python -m pip install -r ..\requirements.txt
python demo.py
```

Same 8-scenario suite as variants A and B, **plus** a 6th scenario
showing what only this variant gets: round-trip JSON serialisation
and static topology validation.

## Files

| File | What it owns |
|---|---|
| `outcomes.py` | Discriminated outcomes (copy). |
| `events.py` | Event log (copy). |
| `model.py` | `WorkflowModel`, `Edge`, six `Node` variants (`AgentNode`/`ScriptNode`/`HumanGateNode`/`RouteNode`/`SubworkflowNode`/`TerminateNode`) — all pydantic. `VerbRegistry` for verb lookup. |
| `engine.py` | The interpreter: dispatches on `isinstance(node, X)`, runs verbs from the registry, manages retry/cancel/gate/subworkflow. |
| `demo.py` | All scenarios. |

## Authoring shape

```python
wf = WorkflowModel(
    workflow_id="demo_basic", start="ingest",
    nodes=[
        AgentNode(node_id="ingest", verb="ingest"),
        AgentNode(node_id="llm_step", verb="llm_step", retry_max=2),
        HumanGateNode(node_id="approve", prompt="ok?", options=["yes", "no"]),
        RouteNode(node_id="branch", chooser="choose_branch"),
        TerminateNode(node_id="done", disposition="completed"),
    ],
    edges=[
        Edge(from_node="ingest", outcome_key="success", to_node="llm_step"),
        ...
    ],
)

verbs = VerbRegistry()
verbs.register("ingest", lambda ctx: Success(value={"text": "..."}))
verbs.register("llm_step", llm_step_fn)
verbs.register("choose_branch", lambda ctx: "fast")

engine = Engine({"demo_basic": wf}, verbs, log_dir)
engine.run("demo_basic", "run-001")
```

## Invariants honoured

Same as A and B. The data model adds **a static check**
(`WorkflowModel.validate_topology`) that catches edges to unknown
nodes, unreachable nodes, and missing terminals *before* a run
starts. The interpreter still relies on the event log for
INV-RESTART; topology validation is additional belt-and-braces, not a
replacement.

## Strengths

- **Workflows are pure data.** `wf.model_dump_json()` produces the
  full topology; `WorkflowModel.model_validate_json` round-trips it.
  The UI can render the graph from the JSON without instantiating
  any verb code.
- **Static analysis works.** `validate_topology()` already catches
  unknown-target edges and unreachable nodes; extending it for
  "every error key is routed" or "retry_max > 0 ⇒ retry_exhausted
  edge present" is straightforward and AST-free.
- **Verb registry forces an explicit boundary.** Workflow defines
  *what* runs; the registry defines *how*. This is the cleanest
  shape for Wagner's DSL seam — a YAML DSL maps 1:1 to
  `WorkflowModel`, and verb names give Wagner an enforceable
  contract surface.
- **Cross-machine portability without serialisation contortions.**
  A workflow can be authored in one place and executed in another;
  only the verb registry needs to agree on names.

## Weaknesses

- **Most boilerplate per workflow.** Compare a 5-node workflow in
  variant A (≈ 20 LOC) to variant C (≈ 50 LOC with explicit `Edge`
  objects and verb registration). A fluent builder would help but
  is an additional layer.
- **Verbs are dynamically dispatched by string name** — typos in
  `verb="ingset"` only surface at run time. A linter that checks
  every `verb=` against the registry closes the gap, but it's extra
  scaffolding.
- **The engine knows the most.** Each node `kind` requires a branch
  in `_execute`. Adding a new node type touches both `model.py` and
  `engine.py` — small files, but two of them.
- **No native async story.** Verbs are sync callables; introducing
  async would require a registry of `(sync_fn | async_fn)` and
  branching in the interpreter.

## Constraints on adjacent seams

- **Stravinsky:** outcome contract unchanged. The interpreter
  trusts the variant tag absolutely.
- **Brahms:** event format unchanged. Variant C has the easiest
  path to durable workflow definitions stored alongside event logs
  (`<run_id>.workflow.json` alongside `<run_id>.events.jsonl`).
- **Wagner:** this variant **is** the data model Wagner's DSL would
  target. Any DSL — Python fluent builder, YAML schema, decorator
  factory — should compile to a `WorkflowModel`. The verb registry
  is the integration point for Wagner's "how do I reference a
  verb?" question; it should be the same string Wagner's DSL uses.
