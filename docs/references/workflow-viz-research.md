# Workflow / DAG Orchestration Traversal Visualization — State of the Art
**Research for Requiem UI · 2025-05-31**

> **Audience constraint:** A single power-user developer running SDLC workflows with multi-agent AI for 8+ hours/day on a 4K monitor. Optimize for information density and fast operator comprehension, not demo aesthetics.

---

## System Survey & Depth Ranking

Before the deep-dives, a quick tiering based on relevance to Requiem's target (long-running SDLC/AI orchestration, single-operator power use):

| Tier | System | Rationale |
|------|--------|-----------|
| **1 — Deep dive** | Temporal Web UI | Best in class for durable execution observability; event-history model is the most analogous to what Requiem needs |
| **1 — Deep dive** | AWS Step Functions | Best visual execution highlighting + "wait for callback" UX; the gold standard for state-machine canvas |
| **1 — Deep dive** | Airflow 3 | Most mature multi-view system (graph + grid + gantt + code tab); Grid View is the highest-density status surface in the field |
| **1 — Deep dive** | Dagster | Best asset-graph + run-timeline combination; Insights layer is unique |
| **2 — Medium** | Argo Workflows | Kubernetes-native DAG with strong status-colour conventions |
| **2 — Medium** | Prefect 3 | Dynamic (non-DAG) flows + best human-in-the-loop gate UI in the field |
| **2 — Medium** | n8n | Best node-data-inspection UX; execution overlay on the editor canvas |
| **3 — Skim** | GitHub Actions | Familiar to the audience; job-grid + log-stream conventions |
| **3 — Skim** | Netflix Conductor | Good reference for HUMAN task type and fork/join; UI is dated |
| **3 — Skim** | VS Code notebooks/tasks | Inspiration for cell-level output density and terminal-adjacent UX |

---

## Deep-Dive Analysis

### 1. Temporal Web UI

**Source:** `temporalio/ui` (Svelte + Vite), docs at `docs.temporal.io/web-ui`

**1. Primitive visual unit:** Not a node/box at all. The canonical view is a **flat event-history table** — each row is a `HistoryEvent` (e.g., `ActivityTaskScheduled`, `ActivityTaskCompleted`, `TimerFired`). The "Compact" view groups related events into logical groups (one row per Activity attempt, one for each Timer). There is no DAG canvas in the default view. The **Relationships** panel provides a tree of parent/child workflow executions — these are clickable pills in a hierarchy tree. The Compact view's groupings look like collapsible list items with a type badge, timestamp, and duration.

**2. State rendering:**
- **Running:** No close-time field; an animated spinner badge on the workflow-list row; the event list keeps appending live.
- **Completed:** Green dot / "Completed" pill on list row.
- **Failed:** Red dot; a `WorkflowExecutionFailed` event appears in history with cause.
- **Pending/Activity pending:** Shown in the "Pending Activities" side panel with attempt count, last attempt timestamp, and next retry schedule.
- **Waiting for human:** No first-class UI concept. Teams use Signals to wake a waiting workflow; the workflow appears "Running" with a `WorkflowTaskScheduled` event waiting at the polling boundary.

**3. Concurrent branches:** The event table is strictly sequential (events are ordered by `eventId`). Parallel activities that fire simultaneously produce interleaved event rows by event ID. There is no spatial separation of concurrent branches in the history view. The Relationships tree is the closest to a spatial branch view, showing child workflows as parallel siblings.

**4. Time dimension:** Timeline view provides chronological event rows with relative and absolute timestamps. A "duration" column shows elapsed time per event span. There is **no scrubbable timeline** or Gantt; scrubbing is done by scrolling the event list or using the JSON download for offline analysis.

**5. Retry/loop:** The Compact view shows a grouped row with a **retry counter badge** (e.g., "Attempt 3 of 5"). Expanding reveals each attempt as a sub-row. The Pending Activities panel shows `attempt`, `maximumAttempts`, `nextRetryDelay`, and the error from the last attempt.

**6. Subworkflow/nested call:** First-class via the **Relationships panel** — a collapsible tree showing Parent → current → Children, each as a clickable link to its own full history view. No expand-in-place.

**7. Log/output for a single node:** Clicking an event row in Compact or All view expands an **inline JSON detail panel** showing all fields of that event (input, output, error message, task queue, worker ID). No streaming log — output is the serialized activity result from the event history.

**8. Human-gate UX:** Not a first-class concept. The Signals tab lets you send a named signal from the UI, effectively "unblocking" a workflow waiting on `workflow.GetSignalChannel`. There is no inbox, no notification badge, and no form UI. This is the most significant gap in Temporal's UI relative to Requiem's needs.

**Standout strengths:** Compact history with retry grouping; Pending Activities panel showing retry schedule; JSON download; Saved Views with custom query filters; Call Stack query for live debugging.

---

### 2. AWS Step Functions Visual Workflow

**Source:** `docs.aws.amazon.com/step-functions`, Workflow Studio docs.

**1. Primitive visual unit:** A **rounded rectangle** (state box) with a type icon in the top-left (Task, Choice, Parallel, Map, Wait, Pass, Succeed, Fail). Labels use the state name. The canvas uses a top-down vertical flow by default; Parallel branches are rendered side-by-side with a fork/join bar above and below the parallel block.

**2. State rendering:**
- **Running/Currently executing:** The active state box gets a **blue pulsing border/fill** during a live execution. In the execution graph overlay, each state transitions through colours in real-time.
- **Completed:** Green fill.
- **Failed:** Red fill with error tooltip on hover.
- **Pending (not yet reached):** Grey/unfilled outline.
- **Waiting for callback (`.waitForTaskToken`):** A distinct **amber/yellow** state indicating the state machine is paused, waiting for an external system to call back with the task token. This is the closest thing in the field to a "human gate" colour treatment — it is visually distinct from both running and pending.
- **Wait state (timer):** Shows a clock icon with the delay duration; rendered in a muted blue while active.

**3. Concurrent branches:** The **Parallel state** renders as a horizontal split: each branch is a vertical column of states side-by-side, with a thick horizontal bar above (fork) and below (join). The Map state (iterator over array) shows a representative single iteration in the canvas, with a count badge showing "N iterations". Clicking the Map state opens an iteration selector to inspect individual iteration executions.

**4. Time dimension:** The **Execution event history table** (below the graph canvas) is a chronological list of state transitions with timestamps, event type, and duration. There is no Gantt or scrubbable timeline in the standard console. The graph is the real-time view; the table is the historical record.

**5. Retry:** Retry configuration is visible in the state's Inspector panel (retry policies, catch). During execution, each retry attempt produces separate `TaskFailed` + `TaskScheduled` events in the history table. The state box itself doesn't show a counter badge — you must read the history table. A failed-then-retried-then-succeeded state shows **green** (terminal state wins). This is a notable gap.

**6. Subworkflow:** States of type `Task` that call a nested Step Functions execution (via `states:startExecution`) are rendered as a normal task box but with a "nested execution" icon. Clicking opens a link to the child execution in a **new browser tab** — no expand-in-place.

**7. Log/output:** Clicking a state box during execution shows the **Inspector panel** (right sidebar) with Input/Output JSON, error details, and resource ARN. During history review, clicking an event row in the history table shows the same. Output can be large JSON blobs — rendered with syntax highlighting in a scrollable panel. No streaming log (Lambda logs go to CloudWatch separately).

**8. Human-gate UX:** The `.waitForTaskToken` pattern is the mechanism. A Task state configured with `waitForTaskToken` displays in **amber** while suspended. There is no native inbox or notification. The operator must use a separate mechanism (SES email, SNS notification, custom portal) to surface the callback URL/token to the human. The Step Functions UI only shows "this state is waiting for a callback."

**Standout strengths:** Real-time state-colour overlay on the DAG canvas is the best in the field for instant position comprehension; Map/Parallel state spatial rendering; `.waitForTaskToken` colour treatment.

---

### 3. Apache Airflow 3 UI

**Source:** `airflow.apache.org/docs/apache-airflow/stable/ui.html`

**1. Primitive visual unit:** In the Graph View: **rounded rectangle** with task operator name and task ID. Nodes are colour-coded borders by state. In the Grid View: **square cells** in a 2D matrix (task × run). This grid is arguably the highest-information-density view in the entire field.

**2. State rendering** (consistent colour palette across all views):
- **Running:** Light green with animated spinner icon (Graph); green-bordered cell (Grid).
- **Completed/Success:** Dark green.
- **Failed:** Red.
- **Up for retry:** Burnt orange/amber cell.
- **Queued:** Light grey/lavender.
- **Skipped:** Light grey with diagonal stripe.
- **Upstream failed:** Dark grey.
- **Deferred (sensor):** Purple — indicates the task is waiting for an external condition (sensor tick).

**3. Concurrent branches:** In the Graph View, independent task branches (no dependencies between them at a given level) are rendered **side-by-side** horizontally using an auto-layout DAG engine (Elk.js in Airflow 3). The grid view shows them as separate rows — concurrency is not spatially represented in the grid.

**4. Time dimension:** The **Grid View** is the primary time-dimension tool: columns are DAG runs (most recent rightmost), rows are tasks. This gives a compact historical heatmap across dozens of runs at once. The **Gantt view** (accessible per run) shows horizontal bars per task with wall-clock start/end, overlapping bars indicating parallel execution. There is also a run-duration sparkline in the DAG list view.

**5. Retry:** The Grid View shows a retry as a **different shade** of orange in the cell. The task detail panel (opened by clicking a cell) shows `Try Number`, `Max Tries`, and the list of all attempts with individual timestamps and log links. No stacked visual within the graph node — it's a detail-panel pattern.

**6. Subworkflow:** The legacy `SubDagOperator` is deprecated. Modern Airflow uses `TaskGroup` for visual grouping (collapsible rectangle containing child tasks) and `TriggerDagRunOperator` to kick off another DAG (which appears as a normal task node). There is no expand-in-place for triggered child DAGs.

**7. Log/output:** Clicking a task opens the **Task Instance Details side panel** with a Logs tab showing streaming log output with line numbers, worker host, and ANSI colour interpretation. Log content is paginated. This is one of the cleanest log-access patterns in the field.

**8. Human-gate UX:** Airflow has no first-class human approval concept. `ExternalTaskSensor` can wait for another DAG's completion. Some operators use a custom `wait_for_approval` pattern that polls an external ticket system. The Deferred state (purple) is the closest visual indicator of "waiting for something external," but it's not specifically human-targeted.

**Standout strengths:** Grid View heatmap for run-history density; multi-view switching (Graph/Grid/Gantt/Code/Details tabs); task state colour palette is the most complete and consistent in the field; log side panel.

---

### 4. Dagster UI

**Source:** `docs.dagster.io`, Dagster+ Insights docs.

**1. Primitive visual unit:** In the Asset Graph: **rounded rectangle** with asset key as the label, a small status dot in the corner, and lineage arrows. In the run "Gantt" view: horizontal bars. In the op execution graph (legacy jobs): rectangular boxes with op names.

**2. State rendering:**
- **Materializing (running):** Animated green spinner on the asset card; blue in the timeline bar.
- **Materialized (completed):** Green status dot; timestamp of last materialization shown inline on the card.
- **Failed:** Red dot with error indicator; the latest run card turns red.
- **Never materialized (stale/missing):** Grey dot or dashed border.
- **Stale (upstream changed, needs re-run):** Yellow/amber dot — this "staleness" concept is unique to Dagster and extremely useful for asset-centric pipelines.
- **Checking (asset checks running):** Spinner on check icon.

**3. Concurrent branches:** In the Asset Graph, independent assets with no dependency relationship are rendered at the same vertical level using a topological sort layout. The graph auto-layouts using a force-directed or layered algorithm. The Gantt timeline shows concurrent ops as overlapping/adjacent horizontal bars.

**4. Time dimension:** The **Run Timeline** (accessible from Runs list or via Insights) shows a time-axis Gantt for each op/asset within a run. The **Insights** panel provides time-series charts (success rate, materialization count, duration trends) over selectable windows (1 day, 7 days, 30 days). These are line/bar charts, not scrubbable. The asset catalog shows "Last materialized X ago" timestamps inline.

**5. Retry:** Step retries are tracked separately from run retries in Insights metrics. In the run detail view, a failed step shows a retry badge. The `step_retries` metric counts intra-run step retries. A retried step creates a new step execution entry in the Gantt with the attempt number. The Insights "Step retries" metric surfaces cross-run retry patterns as a time-series.

**6. Subworkflow:** Dagster uses asset-based composition — an asset can depend on another asset defined in a separate code location. These cross-location dependencies appear as edges in the global Asset Graph. There is no "nested workflow" concept per se, but `GraphDefinition` (nested graphs of ops) renders as an expandable group node in the op graph. Clicking expands the subgraph in-place.

**7. Log/output:** The Run Detail page has a **structured log panel** at the bottom, filterable by log level, step name, and timestamp. Logs are structured (each entry has `timestamp`, `level`, `message`, `step_key`). The panel is not a raw terminal stream — it's a queryable log table. This is the most operator-friendly log UX in the survey.

**8. Human-gate UX:** No first-class concept. Dagster sensors can watch for external triggers (including polling a ticket system), but there is no "pause for human approval" primitive in the OSS version. Dagster+ has "Asset Policies" for alerting but not human approval gates.

**Standout strengths:** Staleness indicator on asset graph (best freshness UX in field); structured filterable logs; Insights time-series for cross-run trend analysis; step vs. run retry distinction.

---

## Medium-Depth Analysis

### 5. Argo Workflows

**1. Primitive:** **Circle or rounded rectangle** nodes (depending on node type: regular step = rectangle, DAG task = circle with label). Status is shown via **border colour + background tint**: green = succeeded, blue (animated) = running, red = failed, grey = pending.

**2. State rendering:** Node border is the primary signal. Running nodes have a pulsing blue animation. The workflow graph redraws live. Suspended workflows (via `argo suspend` or the `suspend` template) show an amber-bordered "Suspended" state.

**3. Concurrent branches:** DAG templates with parallel tasks render spatially as side-by-side nodes at the same vertical level. `steps` templates with parallel items render similarly. There is no explicit fork/join bar — parallelism is inferred from layout proximity.

**4. Time dimension:** Each node shows elapsed time inline. There is no Gantt view in the OSS UI. The workflow list shows total duration per workflow. Event timeline is available only via Kubernetes events / Prometheus metrics.

**5. Retry:** The retry strategy shows a counter badge on the node (`attempt 2/3`). Each retry is a child node in the workflow graph — the node expands to show a "retry group" with individual attempt nodes inside.

**6. Subworkflow:** A `steps` or `dag` template can call another template by name. In the graph, this renders as a node that, on click, expands inline to show the child template's node graph. True cross-workflow references (separate Workflow resources) open in a drill-down navigation.

**7. Log/output:** Clicking a node opens a side panel with a **streaming log tab** (kubectl logs-equivalent) and an **inputs/outputs tab** showing parameter values. The log panel uses a monospace font with ANSI colour support.

**8. Human gates:** The `suspend` step type pauses execution. In the UI, a suspended workflow shows a "Resume" button at the workflow level. There is no per-step resume form or input collection — the operator must resume the whole workflow (or provide inputs via `argo resume --parameter`).

---

### 6. Prefect 3 / Prefect Cloud

**1. Primitive:** In the Flow Run graph view: **rounded pill/rectangle** per task run, auto-layout. In the state timeline: horizontal bars on a time axis (similar to Gantt).

**2. State rendering:** Prefect has one of the richest state models: `Scheduled`, `Pending`, `Running`, `Completed`, `Failed`, `Crashed`, `Cancelled`, `Paused`, `Suspended`. Each has a distinct colour: running = blue spinner, completed = green, failed = red, paused = amber/yellow, crashed = dark red.

**3. Concurrent branches:** Dynamic tasks (spawned at runtime via `.map()` or `async`) appear as parallel task run nodes in the flow run graph. The layout engine clusters them spatially.

**4. Time dimension:** The **state timeline** shows state transitions on a horizontal axis — thin coloured bars representing each state epoch (e.g., "Pending 2s → Running 45s → Completed"). This is more useful than a Gantt for understanding where time was spent.

**5. Retry:** Each retry creates a new `TaskRun` record, shown as a separate row in the task runs list with the same task name and an attempt number suffix. Retry history is visible in the task run detail.

**6. Subworkflow:** Prefect supports nested flows (a flow calling another flow). The child flow run appears as a linked record in the parent flow run's task run list, clickable to navigate to the child's own flow run page.

**7. Log/output:** Logs are embedded in the flow run page, with filter by task run, log level, and timestamp. Streaming via WebSocket in the Cloud UI.

**8. Human-gate UX:** **Best in the field.** `pause_flow_run(wait_for_input=MyModel)` causes the flow run to enter `Paused` state. The UI shows a **"Resume" button** that opens a **typed form** generated from the Pydantic model schema, with field names, types, and optional Markdown description rendered above the form. The form validates client-side using the JSON schema. This is the only system surveyed with a purpose-built, type-safe human-input UI.

---

### 7. n8n

**1. Primitive:** **Rounded rectangle with icon** (integration logo or category icon in the node centre). Nodes are larger and more visual than most orchestration UIs — they prioritise discoverability over density.

**2. State rendering:** Execution overlay on the editor canvas. Executed nodes get a **green checkmark badge** (success) or **red × badge** (failure) in the corner of the node. The active (currently running) node gets a **pulsing animated border**. Waiting nodes remain unstyled.

**3. Concurrent branches:** n8n doesn't have true parallel execution of the same graph — it's a sequential-with-branching model. Branches rendered side-by-side visually represent conditional routing (IF node → two output pins → two subsequent nodes).

**4. Time dimension:** The execution list shows start time and duration. There is no Gantt. Clicking an execution loads the editor in "execution mode" showing the flow frozen at the end of that run, with each node showing its data.

**5. Retry:** "Retry with currently saved workflow" or "Retry with original workflow" — manual retry from the execution list. No retry counter on the node canvas.

**6. Subworkflow:** The `Execute Sub-Workflow` node calls another n8n workflow. It renders as a regular node; clicking it doesn't drill down inline — you navigate to the child workflow separately.

**7. Log/output:** **Best data-inspection UX in the field.** Clicking any node in execution mode opens a **side panel with tabular input/output data**: each item in the node's output array is a row, each field a column. The operator can inspect every record that flowed through the node without searching logs. This is the most important single UX innovation in n8n for power users.

**8. Human gates:** Not supported natively. Webhooks are used as the mechanism — a workflow pauses at a Webhook node waiting for an HTTP call. No form UI, no typed schema.

---

### 8. GitHub Actions Workflow Run View

**1. Primitive:** **Rounded rectangle** per job. Jobs with identical names but matrix dimensions get separate boxes. Steps are **list items** inside the expanded job panel (not graph nodes).

**2. State rendering:** Job box: spinning icon = in progress, green checkmark = success, red × = failure, grey circle = skipped, amber clock = queued. Step items use the same icon set inline.

**3. Concurrent branches:** Jobs with no dependency (`needs:`) render in parallel horizontally in the visualization graph. Dependency arrows (lines) show the ordering.

**4. Time dimension:** Each job box shows elapsed time. Step items show duration. No Gantt or scrubbable timeline — the graph is pure topology + status, not temporal.

**5. Retry:** A "Re-run failed jobs" button at the workflow level. No counter badge per job showing retry number. Individual step retries (`continue-on-error` + subsequent steps) are not visually distinct.

**6. Subworkflow:** Reusable workflows (`uses: ./.github/workflows/foo.yml`) appear as a single job box in the parent graph; clicking navigates to the called workflow's run in a separate page.

**7. Log/output:** Clicking a step expands an **inline ANSI-colour log stream** inside the job panel. This is a terminal-adjacent experience — the most familiar UX for the developer audience. Log search within a run is supported.

**8. Human gates:** The `environment` protection rule with required reviewers creates a **"Waiting for review" state** on a job. The job box shows an amber lock icon. Reviewers get a GitHub notification and can approve/reject in a modal dialog. This is the second-best human-gate UX in the survey — clean, notification-driven, but limited to approve/reject (no typed input form).

---

### 9. Netflix Conductor OSS

**1. Primitive:** **Rounded rectangle** nodes in a top-down layout. Node label = task reference name. Type badge (SIMPLE, HTTP, FORK, JOIN, DECISION, HUMAN) in the node header.

**2. State rendering:** Task states: `SCHEDULED`, `IN_PROGRESS`, `COMPLETED`, `FAILED`, `TIMED_OUT`, `SKIPPED`. Colour-coded: in-progress = blue, completed = green, failed = red, skipped = grey. The HUMAN task type renders with a distinct person-icon badge.

**3. Concurrent branches:** `FORK_JOIN` operator renders as a fan-out node splitting into N parallel branches, each a column, rejoining at a JOIN node. This is the most explicit fork/join rendering in the survey.

**4. Time dimension:** Execution timeline shows task start/end timestamps in a table below the graph. No Gantt in the OSS UI.

**5. Retry:** Retry count shown as a badge on the task node. The `retryCount` and `retryDelaySeconds` are part of the task definition; failed attempts increment the counter visible on the node.

**6. Subworkflow:** `SUB_WORKFLOW` task type renders as a box with a drill-down arrow icon. Clicking navigates to the child workflow's own execution view.

**7. Log/output:** Task output is shown in the task detail panel as JSON. No streaming log — Conductor delegates logging to the worker implementation (external to the Conductor UI).

**8. Human gates:** The `HUMAN` task type is the most explicit in the field for the concept. The task sits `IN_PROGRESS` with `asyncComplete=true` until an external system (or the UI) marks it complete. In Orkes' Conductor Cloud, there is a dedicated "Human Tasks Inbox" UI with a form builder. In OSS Conductor, the HUMAN task is just a named placeholder — no built-in form UI.

---

### 10. VS Code Notebook + Task Views (Inspiration)

**1. Key patterns borrowed:** Jupyter-style cell execution with inline output rendered directly below the code cell. Status dot (running spinner, green check, red ×) per cell. The output is not in a side panel — it is spatially adjacent to the code that produced it, maintaining locality of reference.

**2. Terminal-adjacent UX:** The integrated terminal panel in VS Code shows process output in a scrollable, searchable, ANSI-capable terminal. Task runners (npm scripts, Make) show named terminal tabs. This pattern — named tabs per "worker" or "activity," each with a streaming terminal — is something no orchestration UI has fully adopted.

**3. Density inspiration:** The VS Code Problems panel (issues list with file:line links) and the Call Stack panel (tree of active stack frames) both achieve high information density through compact row height, icon-based type indicators, and click-to-navigate behaviour. These should directly inform how Requiem renders its event history.

---

## Cross-Cutting Pattern Analysis

### ✅ 3-5 Dominant Patterns (Everyone Does This)

**1. Topology-first, status-overlay second.**
Every mature system separates the workflow *topology* (the static DAG shape) from the *execution state* (colours/badges/animations applied at run time). The graph is the substrate; status is painted on top. Airflow, Step Functions, Argo, Dagster, n8n, and GitHub Actions all follow this model. The key insight: the graph shape should not change when a workflow is running.

**2. Semantic colour palette: green / blue / red / amber.**
All systems converge on: green = terminal success, red = terminal failure, blue (or animated) = in-progress, amber/yellow = waiting/paused/needs-attention. Grey = not-started or skipped. This is a near-universal convention the audience has internalised. Violating it (using, e.g., purple for failure) creates a comprehension tax.

**3. Side-panel / detail drawer for per-node deep-dive.**
Every system studied uses a **click-node → side panel** or **click-node → bottom drawer** pattern for accessing logs, I/O, and retry metadata. No system dumps all this inline on the canvas — it would be unreadable. The consistent affordance is: canvas = overview, panel = detail.

**4. Multi-view switching for different time horizons.**
Airflow's tabbed multi-view (Graph / Grid / Gantt / Code) is the canonical example, but GitHub Actions (graph → expand job → inline logs), Temporal (Timeline / Compact / All / JSON), and Dagster (asset graph / run detail / Gantt / Insights) all provide multiple complementary views rather than trying to show everything in one. Each view optimises for a different question: "What's the topology?", "What failed across 30 runs?", "How long did each step take?", "What's the raw data?"

**5. Event/history table as the authoritative audit trail.**
Temporal, Step Functions, Conductor, and Dagster all maintain an immutable ordered event log and surface it as a scrollable table. The graph/canvas view is a *derived* visualisation of this log. For the power user who needs to debug a subtle sequencing issue, the raw event list with timestamps is always more useful than the coloured graph.

---

### ⚖️ 2-3 Controversial Patterns (Legitimate Disagreement)

**1. Graph-first vs. log-first as the primary entry point.**
Temporal lands firmly on "log-first" — the event history table is the home view, and there is no DAG canvas. Step Functions and Airflow are "graph-first." The argument for log-first: for long-running workflows with thousands of events, a static-topology graph is less informative than a searchable time-ordered event stream. The argument for graph-first: humans orient spatially, and understanding *what the workflow is* requires seeing the topology before drilling into timeline. Dagster's approach — asset graph as browse mode, run timeline as execute mode — is the most pragmatic resolution.

**2. Inline retry nodes vs. counter badge on a single node.**
Argo renders each retry as a distinct child node in the DAG, which makes the retry *structure* visible spatially but clutters the graph during heavy retry scenarios. Temporal shows retries as grouped rows in the compact history. Step Functions uses counter badges in the history table. Airflow uses colour-coded cells in the grid. There is no consensus. For Requiem's single-operator use case, the **counter badge + expandable attempts list** (Temporal's compact model) is more density-efficient than spawning new canvas nodes per retry.

**3. Human-gate as "inbox" vs. "flow-run pause state."**
Prefect models human gates as a typed-input form surfaced in the flow run's own page (no navigation away). GitHub Actions models it as a repository-level review queue (navigate away from the run to approve). Conductor Cloud has a separate "Human Tasks Inbox" application. The disagreement is between "context-preserving inline approval" and "decoupled approval queue." For a single power user who *is* the human approver and wants to maintain context, the Prefect model is superior. For a team with separate approvers, the inbox model is necessary.

---

### 🚫 2-3 Anti-Patterns (Superseded)

**1. The "everything in one scrolling page" anti-pattern (legacy Airflow Tree View, old Conductor).**
Older Airflow had a "Tree View" that tried to show task status, run history, and topology in a single tall scrolling tree — a hybrid of the grid and graph. It was notoriously hard to read and was removed in Airflow 3. Lesson: forcing a 2D structure (topology × time) into a 1D list is an anti-pattern. Use separate views.

**2. Log modals (pop-up dialogs for log content).**
Some older CI systems (Travis CI, early Jenkins) showed logs in a modal dialog that blocked the rest of the UI. Every modern system uses an inline panel, drawer, or embedded terminal. Modals prevent the operator from keeping context (the graph/state) visible while reading logs.

**3. "Run only" views with no cross-run history.**
Systems that show only the current run's graph with no ability to see "this same step across the last 20 runs" (early Prefect, basic Argo) force operators to manually compare runs by opening multiple tabs. Airflow's Grid View and Dagster's Insights solve this. For a developer running workflows 8 hours/day, cross-run pattern recognition is as important as single-run debugging.

---

### 🌱 1-2 Emerging Patterns (Worth Adopting)

**1. Typed human-input forms generated from schema (Prefect 3).**
`pause_flow_run(wait_for_input=MyPydanticModel)` auto-generates a form from the type annotation, renders it in the UI with Markdown description, and validates client-side. This pattern — where the *workflow code* defines the approval schema and the *UI* renders it without any separate configuration — is appearing only in Prefect as of 2024-2025. It is directly applicable to Requiem's AI-agent-approval use case.

**2. Structured, queryable logs as a first-class view (Dagster + emerging in Temporal).**
Rather than raw ANSI terminal streams, Dagster's structured log panel (timestamp + level + step_key + message, all filterable) treats logs as data. Temporal's metadata view shows human-readable event annotations per step. As AI-generated workflows produce machine-structured output rather than human-readable terminal text, the operator needs to query log data, not scroll it. This is the direction observability is moving.

---

## Recommendation for Requiem

**Core layout:** Adopt a **split-primary-layout** with three persistent zones: (1) a topology canvas (left 50%), (2) an event/history feed (right-top 30%), and (3) a detail/output panel (right-bottom 20%). This mirrors how Dagster separates the asset graph from the run timeline, and how VS Code separates the editor from the Problems/Terminal panels — but collapses it into a single coherent view rather than forcing tab switches. The topology canvas should remain *static* — the workflow's defined shape — while execution state is painted on as a colour/badge overlay. Borrowing from **Step Functions**, currently-executing nodes should have a distinct animated border (not just colour — colour-blind users need the animation), completed = solid fill, failed = solid red with a badge showing error class. Use **Argo's** explicit fork/join spatial model for concurrent branches (side-by-side columns with a horizontal divider bar). **Never** use the Temporal model of interleaved sequential events to represent parallelism.

**Retry and history density:** Model Requiem's retry display on **Temporal's Compact view**: one row per logical activity, expandable to show individual attempts with timestamps, error, and next-retry schedule. Add a badge counter (`↻ 3/5`) on the canvas node itself so the operator can see retry status without opening the detail panel. For cross-run history, adopt **Airflow's Grid View** as a secondary view: task × run matrix, cells coloured by terminal state, with duration sparklines. This is the single most information-dense view in the field for the "8 hours/day" operator who needs to identify patterns (e.g., "the LLM-call step always fails on the 3rd run after midnight").

**Human gates and AI interaction:** For the SDLC/multi-agent use case, implement the **Prefect `wait_for_input` model** as a first-class primitive: when an activity suspends with a typed approval request, the topology canvas shows that node in a distinct amber pulsing state (adopting **Step Functions'** `.waitForTaskToken` colour treatment), a persistent banner appears at the top of the UI showing "1 workflow waiting for you," and clicking the node opens the detail panel with a **schema-driven form** (Pydantic/JSON Schema → rendered input fields). **GitHub Actions' notification + review button** is the right mental model for surfacing the gate without modal-blocking the rest of the UI. Log access should follow Dagster's model — structured, queryable, filterable by step and level — rather than raw terminal streams, because AI-generated output is structured data, not human-written text. This single decision (structured logs as a queryable table) will make Requiem substantially more useful than any existing system for its stated target audience.

---

*Sources: `docs.temporal.io/web-ui`, `temporalio/ui` (GitHub), `airflow.apache.org/docs/apache-airflow/stable/ui.html`, `docs.prefect.io/v3/advanced/interactive`, `docs.dagster.io` (Insights page), `argo-workflows.readthedocs.io/en/latest/walk-through/dag/`, `docs.aws.amazon.com/step-functions/latest/dg/getting-started.html`, `conductor-oss.github.io/conductor/documentation/configuration/workflowdef/`, `docs.n8n.io/workflows/executions/`, `docs.github.com/en/actions/monitoring-and-troubleshooting-workflows/monitoring-workflows/using-the-visualization-graph`*

---
