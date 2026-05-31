# Seam #7 — DSL / Workflow Definition Shape

**Author:** Wagner (DSL-shape seat) · **Phase:** A · **Branch:** `seam/dsl-shape`
**Status:** prototypes ready for Daniel's hands-on review.

---

## What this seam is

Every workflow Requiem will eventually execute (`root`, `plan-level`,
`implement-merge-group`, `feature-pr`, `github-pr`, `ado-pr`, `close-out`,
the support workflows — 11+ today and growing) has to be *written by someone*.
This seam decides what that someone writes.

The polyphony+conductor era was YAML. Daniel said explicitly *"entirely ours,
harness and all"* — the YAML is not coming with us. The question is what
replaces it.

Three load-bearing constraints govern the choice:

1. **The engine must introspect topology without running.** The UI needs to
   render the graph, the harness needs to enumerate paths for scenario
   coverage. Whatever the author writes, it has to lower to a data structure
   the engine can walk.
2. **Authors are the only audience.** Workflows are authored in code, not in
   a GUI (north-star §5). So author ergonomics is most of what we are optimising.
3. **Typos must die early.** A route to a non-existent node is a corruption of
   the topology — INV-NO-CORRUPT-FORWARD says we surrender to a gate rather
   than execute that. Better: refuse to construct.

## Which invariants this seam touches

All seven from the north-star, but most directly:

- **INV-DISCRIMINATED-OUTCOMES** — the DSL must express routes that key off
  the outcome variant tag, never the payload. All three variants do.
- **INV-EVENT-LOG-AUTHORITATIVE** — topology is *static* and inspectable; the
  *trace* is what the engine emits. The DSL never lets the topology change
  mid-run. All three variants do.
- **INV-NO-CORRUPT-FORWARD** — typo-catching is a topology validity check.
  All three variants fail loudly at construction time.

---

## The variants

All three lower to the **same** `Workflow` data model defined in `core.py`
(pydantic). The engine, the topology pretty-printer, and the harness
`next_node()` helper consume that single shape. **The choice is purely an
authoring-surface choice; the engine sees one structure regardless.**

The same `close-out` workflow is authored three ways. Run:

```powershell
cd prototypes/dsl-shape
python variant-a-fluent-builder/demo.py
python variant-b-decorators/demo.py
python variant-c-declarative-pydantic/demo.py

python -m pytest variant-a-fluent-builder/test_topology.py -v
python -m pytest variant-b-decorators/test_topology.py -v
python -m pytest variant-c-declarative-pydantic/test_topology.py -v
```

All three demos print the same topology, the same trace, and catch the same
typo. The tests assert path coverage *without running the workflow* — that's
the harness's primitive.

### Variant A — Fluent Python builder

```python
wf = (
    WorkflowBuilder("close-out")
        .entry("verify")
        .verb("verify", verify_done)
        .route("verify", on="success", to="human_approve")
        .route("verify", on="failure", to="$end")
        .human_gate("human_approve", prompt="Approve close-out?")
        .route("human_approve", on="success", to="archive")
        .route("human_approve", on="failure", to="$end")
        .subworkflow("archive", calls="notify")
        .route("archive", on="*", to="$end")
        .build()
)
```

### Variant B — Decorators on a workflow class

```python
@workflow("close-out", entry="verify")
class CloseOut:
    @verb
    @staticmethod
    def verify(ctx): return verify_done(ctx)

    @human_gate(prompt="Approve close-out?")
    @staticmethod
    def human_approve(ctx): pass

    @subworkflow(calls="notify")
    @staticmethod
    def archive(ctx): pass

    routes = [
        route("verify", on="success", to="human_approve"),
        route("verify", on="failure", to="$end"),
        route("human_approve", on="success", to="archive"),
        route("human_approve", on="failure", to="$end"),
        route("archive", on="*", to="$end"),
    ]
```

### Variant C — Declarative pydantic data

```python
CLOSE_OUT = Workflow(
    name="close-out", entry="verify",
    nodes=[
        Node(name="verify", kind=NodeKind.VERB, verb=verify_done),
        Node(name="human_approve", kind=NodeKind.HUMAN_GATE,
             prompt="Approve close-out?"),
        Node(name="archive", kind=NodeKind.SUBWORKFLOW, subworkflow="notify"),
    ],
    routes=[
        Route(from_node="verify", when="success", to_node="human_approve"),
        Route(from_node="verify", when="failure", to_node="$end"),
        Route(from_node="human_approve", when="success", to_node="archive"),
        Route(from_node="human_approve", when="failure", to_node="$end"),
        Route(from_node="archive", when="*", to_node="$end"),
    ],
)
```

---

## Variant comparison

| Axis | A — Fluent builder | B — Decorators | C — Declarative pydantic |
|---|---|---|---|
| **Author ergonomics** | High. Reads left-to-right, one expression. | Medium-high. Functions feel natural; routes-as-list is a two-spotting tax. | Low-medium. Verbose; every node/route is a constructor call with named fields. |
| **IDE — autocomplete on DSL keywords** | Excellent. Each method shows the next legal call. | Good for decorators; routes are plain function calls. | Excellent (pydantic field hints). |
| **IDE — jump-to-def on verbs** | Indirect (verb is an argument). | Best — the verb *is* a method; F12 just works. | Indirect (verb is a field value). |
| **Introspectability (UI/harness)** | Same. All three lower to the same `Workflow`. | Same. | Same. |
| **Typo catching point** | At `.build()` (pydantic ValidationError). | At `@workflow` class decoration. | At `Workflow(...)` construction. |
| **Static analysis (mypy/pyright)** | Method chain is opaque to type narrowing. | Functions are typed; routes list is `list[Route]`. | **Best** — pydantic models are the most analysable. |
| **Composability (sub-workflows)** | `.subworkflow(name, calls="other")` — name-based. | Same — name-based via `@subworkflow(calls=...)`. | Same — `subworkflow="other"` field. |
| **Parameterised workflows** | Easy — function returning `WorkflowBuilder`. | Awkward — decorators bind at class-definition time. Needs a builder fn that returns a class. | Easy — function returning `Workflow`. |
| **Serialisability (round-trip to JSON/YAML)** | Lossy — verb callables won't dump. Needs a verb registry. | Same caveat. | **Best** — `model_dump()` round-trips structure natively. Migration tool from old YAML lands here. |
| **Harness / test ergonomics** | Same — `next_node(wf, "verify", "success")` works on all three. | Same. | Same. |
| **Learning curve for a YAML refugee** | Moderate — the chain mirrors the YAML hierarchy. | Steepest — Python-class metaprogramming is unfamiliar to YAML thinkers. | **Gentlest** — `Node`/`Route` map 1:1 to YAML keys. |
| **Visual density (close-out workflow)** | 10 lines. | ~18 lines (class skeleton + routes list). | 16 lines. |
| **Magic level** | Some — builder mutates internal state. | Highest — decorator side-effects, class-as-data. | Lowest — what you see is what runs. |
| **Refactor: rename a node** | Two edits (define + every route mention). | Two edits + method rename — IDE rename can catch the method def, not the route strings. | Two edits + every route mention. All three are equal here; strings everywhere. |

---

## Recommendation

**Adopt Variant A (fluent builder) as the surface; Variant C (declarative
pydantic) as the floor.**

Concretely: keep the pydantic `Workflow`/`Node`/`Route` model as the canonical
artifact (Variant C *is* the engine's input), and ship the fluent builder
(Variant A) as the *default* author surface. The builder is a thin
constructor for the pydantic model. Authors who need the full data shape
(round-trip from YAML, programmatic construction, schema export for the UI)
drop down to Variant C; authors writing day-to-day workflows use Variant A.

Why not Variant B (decorators):

- The two-spotting tax — verb body in one place, routes list in another — is
  the same readability problem `actionable.yaml` has today.
- Class-as-data metaprogramming makes parameterised workflows awkward (you
  need a builder function that returns a class, which is two layers of
  indirection).
- The "jump-to-def on verbs" win is real but does not justify the rest. We
  get the same win in A and C by passing real function references to
  `WorkflowBuilder.verb(...)` / `Node(verb=...)`.

Why not Variant C alone:

- Verbosity. A 30-node workflow (the size of `implement-merge-group`) is a
  600-line wall of pydantic constructors. The fluent surface buys ~3x
  density without hiding anything (it lowers to the same data).

The A-on-top-of-C combination gives us: best author ergonomics for the
common case (A), best analysability and serialisability for tools (C), and
no choice between them — they're the same data.

---

## What about the YAML refugees?

This is the question Daniel will feel most. He's been writing conductor YAML
for a year; muscle memory says "open the .yaml, find the node by name,
trace the routes." Switching to Python is a real comprehension shift.

The honest answers:

1. **Variant A reads the closest to YAML.** The chain mirrors the
   `agents:` → `routes:` structure. A node and its outgoing routes sit next
   to each other in the source, the way they do in a well-organised YAML
   file. A YAML refugee will find Variant A familiar within a session.
2. **Variant C is the migration target if we ever want a one-way YAML→Python
   converter.** Because the data IS the workflow, an old conductor YAML can
   be parsed into a `Workflow` instance verbatim. This is the cheap escape
   hatch if Daniel decides 6 months in that some workflows want to stay
   data-authored (e.g. operator-edited remediation flows).
3. **The transition is explicit.** We are not pretending Python YAML-with-
   different-syntax. The Python form gets us: real types, real
   jump-to-definition on verbs, real tests, real refactoring tools, real
   debugger breakpoints inside verbs. None of those work in YAML. The trade
   is "less greppable" for "more analysable," and Requiem is betting on the
   second.
4. **What we do *not* do:** ship a YAML-frontend that compiles to Python.
   That would re-import the three-vocabulary problem (Python ↔ YAML ↔ UI),
   which is half of what we're paying off by going single-process. If we
   ship YAML support, we ship two DSLs to maintain.

If Daniel wants a graphical/grep-friendly view of a workflow's topology,
the answer is **the UI's graph view**, not an alternate YAML source. The UI
is rendering the same `Workflow` data either way; let it be the inspection
surface.

---

## Open questions for Daniel

1. **A vs A+C dual-surface vs C-only.** I'm recommending A+C (A as the
   author default, C as the underlying data). Is that the right split, or
   do you want a single surface with no fallback? Single-surface is simpler
   to teach; dual gives a clean escape hatch for migration tooling and
   programmatic construction.
2. **Verb registration: by reference or by name?** All three variants
   currently pass verb *callables* directly. This means a `Workflow` can't be
   round-tripped to JSON without losing the verbs. Alternative: register
   verbs in a `VerbRegistry` and store names in nodes. Cleaner for
   serialisation, less direct (can't ctrl-click the verb name to jump). I'd
   default to **callables-by-reference + a registry only at the YAML-
   migration boundary**, but it's worth deciding now.
3. **Outcome vocabulary.** I shipped 4 outcome variants: `Success`,
   `Failure`, `NeedsHuman`, `Cancelled`. The error-handling deep dive
   suggested a 5-class shape: success / retryable / permanent / needs-
   human / cancelled. Should the DSL/engine see 4 or 5? (I went with 4 on
   the grounds that retryable-vs-permanent is the *router's* concern, not
   the author's — the author writes `on="failure"` and the route table
   decides whether to retry. But Daniel's call.)
4. **Sub-workflow invocation: by name or by reference?** I went by name
   (`subworkflow(calls="notify")`). Same trade as #2.
5. **Async verbs.** The prototype verbs are synchronous. Real verbs will
   be `async def`. The DSL needs no change; the engine grows an
   `asyncio.run` boundary. Is there any reason to surface async in the
   DSL itself (e.g. for parallel `for_each` nodes)? I'd say no — the engine
   handles concurrency, the author writes nodes.
6. **`route(when=...)` matching: does `"*"` (wildcard) earn its keep, or
   should every route be explicit?** Wildcard is convenient for "any
   non-error path continues" but it can hide a missed case. Explicit-only
   would force authors to write every outcome they handle, which is what
   the deep-dive's "fail honestly" line argues for. Worth a decision.

---

## What I'd ship next

If Daniel picks A+C:

- Promote `core.py`'s `Workflow`/`Node`/`Route`/`Outcome` to the engine's
  canonical model (ADR-worthy).
- Move `WorkflowBuilder` into the engine package; delete variants B and the
  variant-A folder; promote variant C's `demo.py` shape into an
  `examples/` folder.
- Write ADR 0002: "Workflow DSL: fluent builder on top of pydantic data
  model" and link to this seam README.
- Pull `next_node()` and `describe()` into a `requiem.introspect` module —
  the harness and the UI both consume them.

If Daniel picks something other than A or A+C: most of `core.py` survives
(the data model is the same regardless of the author surface), and we throw
away the chosen variant's author file. The cost of being wrong is bounded
because the engine never sees the difference.
