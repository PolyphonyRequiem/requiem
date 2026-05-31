# Walking Skeleton α — Phase A integration demo

> **What this is.** A single end-to-end runnable demo that exercises
> **every** Phase A seam's *recommended variant* in one workflow. The
> goal is proof, not packaging: if this composes cleanly, Phase A
> converges and Phase B can start. If anything feels off, the README's
> last section names exactly which seam to revisit.

```powershell
cd demos/walking-skeleton-alpha
pip install -r requirements.txt
python demo.py
```

Wall-clock: **~90 ms**. Zero API keys. Drops a real `.events.jsonl`
in `.runs/` you can `cat` or `jq` afterwards.

---

## What this demo proves

The integrated Phase A design — nine recommended variants — composes
into one runnable workflow without friction. The demo:

1. Lowers a fluent-builder DSL to pydantic data
2. Interprets that data through a data-driven kernel
3. Routes on a discriminated outcome union
4. Persists every step to an append-only event log
5. Invokes a `parallel_fork` team through a Protocol-based agent boundary
6. Calls a synthesizer agent that reads team output
7. Suspends at a real human gate (and the demo auto-resolves it)
8. Touches the filesystem through a frozen `Toolbelt`
9. Demonstrates retry-then-succeed (proves retry policy)
10. Resumes mid-workflow from the event log alone (proves INV-RESTART)

The whole composition lives in ~1.4 KLOC. The engine package itself is
~700 LOC.

---

## Which variants compose here

| Seam | Variant used | Source PR |
|---|---|---|
| Verb outcomes | **Stravinsky B** — PEP 604 sealed unions + `match` | [#4](https://github.com/PolyphonyRequiem/requiem/pull/4) |
| Event stream | **Brahms-events B** — envelope-loose + typed `emit_*` helpers | [#5](https://github.com/PolyphonyRequiem/requiem/pull/5) |
| Kernel | **Beethoven C** — data-driven interpreter | [#7](https://github.com/PolyphonyRequiem/requiem/pull/7) |
| Persistence | **Bach A** — pure log | [#2](https://github.com/PolyphonyRequiem/requiem/pull/2) |
| Agent boundary | **Mahler A** — Protocol `AgentProvider` + FakeProvider | [#8](https://github.com/PolyphonyRequiem/requiem/pull/8) |
| DSL | **Wagner A** — fluent builder lowered to pydantic data | [#1](https://github.com/PolyphonyRequiem/requiem/pull/1) |
| External-process | **Liszt B+C hybrid** — per-tool clients in a frozen `Toolbelt` | [#3](https://github.com/PolyphonyRequiem/requiem/pull/3) |
| Harness | **Brahms-harness B** — pytest fixtures | [#6](https://github.com/PolyphonyRequiem/requiem/pull/6) |
| Agent teams | **Pattern #9** — `.team(...)` sugar over `parallel_fork` | [#9](https://github.com/PolyphonyRequiem/requiem/pull/9) |

---

## The workflow

```
start                  (script)     emit run_started
  → read_snippet       (script)     Liszt FileClient via Toolbelt
  → flaky_lint         (script)     retry: fails once, succeeds on attempt 2
  → review_team        (team)       parallel_fork over 3 reviewer agents
  → synthesize         (agent)      reads team findings, produces Verdict
  → human_gate         (gate)       auto-resolved by the demo handler
  → archive            (script)     writes a markdown summary
  → end                (terminate)
```

Three reviewer agents (`style_reviewer`, `correctness_reviewer`,
`performance_reviewer`) each have a distinct charter and return a typed
`ReviewFinding`. The synthesizer reads all three findings and returns a
typed `Verdict`. Every agent goes through `FakeProvider` — scripted by
agent name, the same shape Mahler-2 wants in the polyphony harness today.

---

## What you'll see when you run it

```text
========================================================================
Walking Skeleton α — run_id=demo-1780269477
========================================================================
workflow      : code-review  (9 nodes, 11 edges)
recommended   : Stravinsky B + Brahms B + Beethoven C + Bach A
              + Mahler A + Wagner A + Liszt B+C + Pattern #9
------------------------------------------------------------------------
  [gate human_gate] Reviewer team finished. Approve verdict?
  [gate human_gate] options: ('approve', 'reject') → auto-picking 'approve'
------------------------------------------------------------------------
wall-clock    : 90.7 ms
result.kind   : Completed
disposition   : completed  (final_node=end)
projection    : {
  "nodes_entered": ["start","read_snippet","flaky_lint","flaky_lint",
                    "review_team","synthesize","human_gate","archive","end"],
  "verbs_completed": 9,
  "retries": 1,
  "team_branches_completed": 3,
  "terminal": "completed",
  "total_events": 34
}
event log     : .runs/demo-1780269477.events.jsonl  (10494 bytes)
agent calls   : 4 (style + correctness + performance + synthesizer, one each)
```

Note `flaky_lint` appearing twice in `nodes_entered`: that is the retry
path — first attempt returned `RetryableFailure`, second attempt returned
`Success`. The kernel routed via `retry_attempted` between them.

---

## Where to look afterward

| File | What it shows |
|---|---|
| `.runs/<run_id>.events.jsonl` | The truth substrate — Bach A's pure log. Every kind has an envelope; every payload is a dict. |
| `.runs/<run_id>.summary.md` | The markdown verdict the `archive` verb writes. |
| `engine/kernel.py` | The Beethoven C interpreter. Read `_execute` and `run` for the dispatch loop. |
| `workflow.py` | The Wagner A fluent build of the demo workflow. Reads top-down. |
| `reviewers.py` | The Mahler A agent specs + FakeProvider scripts. One dict per agent. |

`jq` recipes:

```powershell
# every verb completion + its outcome variant tag
jq -c 'select(.kind=="verb_completed") | {node: .node_id, outcome: .payload.outcome.kind}' .runs/*.events.jsonl

# every team branch outcome
jq -c 'select(.kind=="team_branch_completed") | {agent: .agent_id, kind: .payload.outcome.kind}' .runs/*.events.jsonl

# the route the engine took at each fork
jq -c 'select(.kind=="route_taken") | {from: .node_id, on: .payload.key, to: .payload.to_node}' .runs/*.events.jsonl
```

---

## The INV-RESTART proof

```powershell
python demo_resume.py
```

Drives the demo through two runs against the **same `run_id`**, with a
synthetic crash in between:

1. **Pass 1.** Full run. 34 events written.
2. **Truncate.** The script chops the event log right after the team's
   last `team_branch_completed` (simulating a crash *after* the team
   finished but *before* the synthesizer started).
3. **Pass 2.** A fresh `Engine` opens the same `run_id`. The kernel
   replays the log, sees that `review_team` is in `completed`, and
   resumes from the next undecided edge.

The pass-2 `FakeProvider` is scripted with **one entry per reviewer**.
If the engine re-ran the team, the reviewer scripts would exhaust and
the run would fail. It does not — proving the kernel did not
re-execute already-completed nodes. The script's final assertion:

```
entry counts  : {'read_snippet': 1, 'review_team': 1, 'synthesize': 1, 'archive': 1, 'flaky_lint': 2}
agent calls 2 : 1 (synthesizer only, not reviewers)
INV-RESTART: ✓  resume picked up at the next undecided edge
```

(`flaky_lint` shows `2` because both of its original entries — the
retry — survive in the truncated log; they are *first-run* entries,
not re-executions.)

---

## Harness tests

```powershell
pytest tests/ -v
```

Four pytest scenarios using Brahms-harness B fixtures
(`make_engine`, `auto_approve`, `log_dir`):

- `test_full_run_completes` — end-to-end happy path; every seam fires.
- `test_retry_then_succeeds` — exactly one `retry_attempted` event with `next_attempt=2`.
- `test_human_gate_suspends_without_handler` — proves the gate is real (the engine returns `Suspended` when no handler is wired).
- `test_invariant_event_log_authoritative` — the log alone reconstructs run state; no sidecar manifest exists.

Wall-clock: ~0.8 s for all four.

---

## If any of this feels off, here's the seam to revisit

| Smell | Seam to look at |
|---|---|
| Outcome dispatch feels boilerplate, or a sixth kind seems painful to add | [#4 Stravinsky](https://github.com/PolyphonyRequiem/requiem/pull/4) — try variant A or C |
| `.events.jsonl` is hard to read with `jq`, or envelope fields feel wrong | [#5 Brahms-events](https://github.com/PolyphonyRequiem/requiem/pull/5) — try variant A (typed) or C (CloudEvents) |
| The kernel hides too much, or topology isn't introspectable enough | [#7 Beethoven](https://github.com/PolyphonyRequiem/requiem/pull/7) — try variant A (class+table) |
| Resume logic feels too clever, or you want snapshots / a SQLite view | [#2 Bach](https://github.com/PolyphonyRequiem/requiem/pull/2) — try variant B or C |
| The FakeProvider feels like the wrong shape, or you want pydantic-ai sugar | [#8 Mahler](https://github.com/PolyphonyRequiem/requiem/pull/8) — try variant B |
| Workflow authoring feels chatty, or you want decorators instead | [#1 Wagner](https://github.com/PolyphonyRequiem/requiem/pull/1) — try variant B (decorators) or C (pure data) |
| `Toolbelt.git.show(...)` reads wrong, or per-tool clients feel heavy | [#3 Liszt](https://github.com/PolyphonyRequiem/requiem/pull/3) — try variant A (single ProcessRunner) |
| Scenarios feel unergonomic, or you want YAML for reviewability | [#6 Brahms-harness](https://github.com/PolyphonyRequiem/requiem/pull/6) — try variant A (YAML) |
| The `.team(...)` API feels awkward, or `parallel_fork` should be sequential nodes | [#9 Pattern](https://github.com/PolyphonyRequiem/requiem/pull/9) — re-read §5 ("what does NOT compose easily") |

---

## Deviations from the recommendations (what to know)

- **Beethoven Q-K7 (`parallel_fork` primitive) is adopted.** Pattern #9
  flagged this as the one open Phase A question that makes agent teams
  ergonomic. The kernel here treats `TeamNode` as a first-class node
  kind. Without it, the `.team(...)` sugar would compile to N sub-workflow
  invocations and the demo would be uglier.
- **The kernel does not implement sub-workflows.** The data-driven
  prototype in PR #7 does; the walking skeleton elides them because the
  `code-review` workflow doesn't need them. Adding them is mechanical.
- **The `BadOutput` variant is collapsed into `PermanentFailure(error_kind="bad_output")`.**
  Mahler A's prototype keeps it as its own outcome tag; the skeleton
  treats it as a sub-tag of permanent failure to keep the outcome union
  at five members. Re-promoting to a sixth tag is a one-line change in
  `engine/outcomes.py` and one new arm in the kernel's match statement —
  not a structural deviation.
- **The `charter` field on `AgentSpec` is a `str`, not a `str | Path`.**
  Pattern #9 suggests either. The skeleton uses inline strings because
  the demo's charters are short. Switching to `Path` is one line.

Nothing else deviates from the recommended variants.

---

## File layout

```
demos/walking-skeleton-alpha/
├── README.md                # this file
├── requirements.txt         # pydantic, pytest, pytest-asyncio
├── pytest.ini               # asyncio_mode=auto
├── demo.py                  # `python demo.py` — one-shot run
├── demo_resume.py           # `python demo_resume.py` — INV-RESTART proof
├── workflow.py              # the code-review workflow (Wagner A fluent build)
├── reviewers.py             # 3 reviewer charters + synthesizer + FakeProvider scripts
├── sample_snippet.py        # the (intentionally flawed) code under review
├── engine/                  # the compact integrated engine
│   ├── __init__.py
│   ├── outcomes.py          # Stravinsky B — sealed dataclass union + match
│   ├── events.py            # Brahms B — envelope-loose + typed emit helpers
│   ├── persistence.py       # Bach A — pure log
│   ├── kernel.py            # Beethoven C — data-driven interpreter
│   ├── dsl.py               # Wagner A — fluent builder + pydantic node model
│   ├── agent.py             # Mahler A — Protocol AgentProvider + FakeProvider
│   ├── toolbelt.py          # Liszt B+C — frozen Toolbelt with per-tool clients
│   └── teams.py             # Pattern #9 — TeamBranch author-facing alias
└── tests/
    └── test_walking_skeleton.py    # Brahms-harness B — pytest fixtures
```

---

## What this demo is **not**

- Not a real engine. Not a production retry policy. Not a real human-gate
  resolver. Not real LLM calls. Not the right shape for sub-workflows.
- Not a substitute for reading the individual seam PRs. It is a single
  data point that says *the recommendations integrate*.
- Not the walking skeleton for Phase B. That one will pick a real workflow
  from the polyphony parity inventory and ship it under the real engine
  package. This demo's engine is a throwaway compaction of the seam
  variants for fast iteration on the integrated design.

If this demo runs and feels right, Phase A is done.
