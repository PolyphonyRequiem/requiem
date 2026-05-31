# Platespinner survey

## 1) What is it?

`conductor-platespinner` is a Python 3.11+ FastAPI/uvicorn dashboard for Conductor workflow runs, with a React 19 + TypeScript + Vite frontend. It serves a web UI (also installable as a PWA) that monitors orchestration execution in real time, including nested subworkflows, gates, dialogs, activity logs, metrics, and enrichment from Azure DevOps/git metadata. It targets developers/operators who need to inspect and interact with live orchestration runs. `platespinnerd` adds a tray-based supervisor for desktop use.
Refs: `README.md:11-39`, `pyproject.toml:1-26`, `platespinner/__main__.py:1-46`, `platespinner/supervisor.py:1-27`

## 2) Architecture sketch

**Backend**
- Single FastAPI app in `platespinner/server.py`.
- Parses `.events.jsonl` files into `WorkflowRun` / `AgentRun` / `Checkpoint` models.
- Publishes a dashboard plus SSE stream.
- Handles local utilities and process discovery (`psutil`) for active runs.
- Runs enrichers from `platespinner/enrichers/*` to add ADO/git context.
- Supports live workflow interaction via WebSocket for gates/dialogs (seen in frontend hooks and server comments).

**Frontend**
- React shell in `frontend/src/App.tsx`.
- Global state via Zustand stores (`dashboard-store`, `ui-store`, `notification-store`).
- SSE client in `frontend/src/hooks/use-sse.ts` connects to `/api/events` and hydrates state.
- Main UI is the trace-oriented "LabsTracePlayground" plus Metrics tab.
- The trace view is path-based, not stack-based, so parallel branches and foreach iterations render correctly.

**Data flow**
1. Workflow emits `.events.jsonl` (and `.notifications.jsonl` for domain signals).
2. Backend tails/parses files into structured run state.
3. Backend serves snapshot/update events over SSE (`/api/events`).
4. Frontend `useSSE()` ingests snapshot/update/ping + `domain_signals`.
5. React stores drive `LabsTracePlayground`, `TraceView`, metrics, notifications.
6. User actions on gates/dialogs go back through a WebSocket to conductor.

**IPC**
- Backend ↔ frontend: SSE for dashboard data; WebSocket for gate/dialog replies.
Refs: `README.md:127-158`, `platespinner/server.py:548-855`, `frontend/src/hooks/use-sse.ts:36-127`, `frontend/src/labs/LabsTracePlayground.tsx:39-41`

## 3) UI concepts worth borrowing

- **List-detail shell**: run list on the left, live run detail on the right. Good for "many workflows, one inspected workflow."
- **Breadcrumb through nested execution paths**: shows exact branch/subworkflow context, including `for_each` and parallel groups.
- **Focus-as-navigation**: clicking a scope focuses/expands it; the focus trail behaves like browser history.
- **Gate chips with jump-to-next**: a scope header shows pending gate counts and cycles through multiple gates on repeat clicks.
- **Inline activity log**: think/tool/result/msg/route lines are visually differentiated and copyable.
- **Sticky ancestor headers**: when drilling deep, ancestor scopes remain legible as a navigation spine.
- **Live pulse**: a subtle "streaming live" cue at the tail of the trace.
- **Optimistic gate resolution**: UI immediately collapses a gate once the user submits, even before server confirmation.

Refs: `README.md:24-39, 43-49`, `frontend/src/labs/LabsTracePlayground.tsx:129-260`, `frontend/src/labs/components/ScopeHeader.tsx:133-320`, `frontend/src/labs/components/TraceLineRow.tsx:171-260`, `frontend/src/labs/components/TraceView.tsx:169-258`

## 4) The "workflow traversal" rendering

This repo does **not** use a node-edge graph as the primary traversal view. It renders a **linearized trace with collapsible scopes**.

- **Primitive node**: a row (`scope-start`, `agent-start`, `log`, `gate`, `dialog`, `route`, etc.).
- **Primitive edge**: implicit parent/child nesting via `scope_path` and indentation guides.
- **Scope node**: `ScopeHeader` row.
- **Leaf event**: `TraceLineRow` variants.

How it behaves:
- Traversal is **path-based**, not stack-based, so concurrent branches interleave correctly.
- Collapsed scopes hide descendants, but their headers remain visible.
- A focused scope gets highlighted and can become the viewport root.
- Pending gates are summarized on the scope header (`🚦 gate` / `🚦 gate (N)`), and clicking cycles through matching gates.
- Failures are surfaced by failed scope/agent rows and summary chips (`failedCount`, red styling).
- Human-gate pending is represented by the gate card itself; `human_gate` agent start/end rows are suppressed so the gate is the single visible surface.
- Concurrent branches are shown as sibling scopes with indentation and summary counts, not as separate lanes.
- Dialogs and gates are interactive rows; dialog messages can be sent back through WebSocket.
- Live state is implied by `DurationTicker`, `LiveStreamPulse`, and tail-follow behavior.

Refs: `frontend/src/labs/components/TraceView.tsx:1-18, 169-258`, `frontend/src/labs/components/ScopeHeader.tsx:35-45, 94-98, 221-320`, `frontend/src/labs/components/TraceLineRow.tsx:197-260`, `platespinner/server.py:610-855`

## 5) Re-usable code units

**(a) Directly liftable**
- `platespinner/enrichers/__init__.py` — plugin loader + error isolation.
- `platespinner/domain_signals.py` — read-only notification tailer/deduper.
- `platespinner/supervisor.py` — tray supervisor pattern (if you want desktop supervision).

**(b) Liftable-with-extraction**
- `platespinner/server.py` — core event-log parsing and run-state derivation; depends on repo-specific paths, enrichers, and API routes.
- `frontend/src/labs/components/TraceView.tsx`
- `frontend/src/labs/components/ScopeHeader.tsx`
- `frontend/src/labs/components/TraceLineRow.tsx`
- `frontend/src/labs/LabsTracePlayground.tsx`
These are good patterns, but they're entangled with local stores, trace types, and API contracts.

**(c) Concept-only**
- Metrics UI (`frontend/src/components/metrics/*`) — useful idea, but probably re-implement.
- PWA/tray/update stack — likely not a first-day Requiem dependency.
- `widgets/` rail — conceptually useful, but probably domain-specific.

Refs: `README.md:127-158`, `platespinner/enrichers/__init__.py:1-152`, `platespinner/domain_signals.py:1-260`, `platespinner/supervisor.py:1-240`

## 6) Dependencies & build

**Backend deps**
- `fastapi`, `uvicorn[standard]`, `psutil`, `httpx`, `packaging`, `platformdirs`, `pystray`, `pillow`
- Python `>=3.11`

**Frontend deps**
- React 19, TypeScript, Vite
- `zustand`, `lucide-react`, `recharts`, `marked`, `highlight.js`
- Graph/layout libs: `@xyflow/react`, `@dagrejs/dagre` (likely for other views)
- `vite-plugin-pwa`, Tailwind v4

**Build**
- Python build via hatchling.
- Frontend build via `npm run build`.
- Desktop packaging/in-app update/tray bits add extra complexity.

Refs: `pyproject.toml:1-38`, `frontend/package.json:1-38`, `README.md:61-74, 111-125`

## 7) License

MIT. Copying is permissible for Requiem.
Refs: `pyproject.toml:17-25`, `README.md:190-192`

## 8) Gotchas / things to avoid

- **Don't assume graph UI is the main model**: the shipped prod UI is trace/list-detail, not a DAG canvas.
- **`LabsTracePlayground` is explicitly a sandbox/prototype surface**; some comments indicate future replacement (`flattenTrace`) and phased experimentation.
- **Human gates are intentionally de-noised** by suppressing agent start/end rows. Don't reintroduce duplicate surfaces.
- **Path-based concurrency handling is critical**; stack-based traversal will break parallel/foreach interleaving.
- **Tray/PWA/update machinery is heavy** and probably not worth carrying into Requiem's day-one UI.
- **Enrichment is opt-in and read-only**; don't couple core rendering to ADO/git specifics.
- `supervisor.py` is Windows-aware and process-management-heavy; useful only if you need a desktop shell.

Refs: `frontend/src/labs/components/TraceView.tsx:11-18, 181-186`, `TraceLineRow.tsx:203-209`, `platespinner/enrichers/__init__.py:15-17`, `platespinner/supervisor.py:7-25`

## 9) 5 most consequential files

1. `platespinner/server.py` — core event parsing, run state, live branch/focus derivation, API/SSE behavior.
2. `frontend/src/labs/LabsTracePlayground.tsx` — main UX composition for trace traversal and interaction.
3. `frontend/src/labs/components/TraceView.tsx` — path-based rendering algorithm for nested/concurrent workflow traversal.
4. `frontend/src/labs/components/ScopeHeader.tsx` — the key visual metaphor for scope, focus, summary, and gate navigation.
5. `frontend/src/hooks/use-sse.ts` — how backend snapshots, updates, and domain signals hydrate the UI.

Refs: `platespinner/server.py:520-855`, `frontend/src/labs/LabsTracePlayground.tsx:206-260`, `frontend/src/labs/components/TraceView.tsx:1-258`, `frontend/src/labs/components/ScopeHeader.tsx:133-320`, `frontend/src/hooks/use-sse.ts:36-127`
