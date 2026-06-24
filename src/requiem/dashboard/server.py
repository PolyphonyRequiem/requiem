"""requiem.dashboard.server — a stdlib-only web dashboard for run event logs.

No FastAPI, no uvicorn, no framework: a ``ThreadingHTTPServer`` +
``BaseHTTPRequestHandler`` serving a tiny JSON API and one self-contained HTML
page (ADR-0019). All read endpoints are pure projections of the event log
(``requiem.dashboard.projection``); the single write endpoint
(``POST /api/gates/<run_id>/resolve``, phase 2) appends one guarded,
append-only ``gate_resolved`` event via ``requiem.dashboard.resolution`` and
leaves continuation to a separate ``requiem resume`` (it never runs the engine).

Binds to ``127.0.0.1`` — an operator-local tool, not a public service.

Routes::

    GET  /                              → the HTML page
    GET  /api/runs                      → list_runs(...)        as JSON
    GET  /api/runs/<run_id>             → run_detail(...)       as JSON  (404 if absent)
    GET  /api/gates                     → pending_gates(...)    as JSON
    GET  /api/state/<item_id>           → compute_work_state(...) as JSON (ADR-0031 / R4)
    POST /api/gates/<run_id>/resolve    → resolve_gate(...)     {"choice": "..."}
    GET  /healthz                       → {"ok": true}
"""
from __future__ import annotations

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from requiem.dashboard import projection
from requiem.dashboard.page import PAGE_HTML
from requiem.dashboard.auto_resume import AutoResumeError as _AutoResumeError
from requiem.dashboard.auto_resume import spawn_resume as _spawn_resume
from requiem.dashboard.resolution import GateResolutionError, resolve_gate


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, default=str).encode("utf-8")


# ---- ADR-0031 / R4: /api/state/<item_id> wiring -------------------------
#
# The dashboard process generally has no twig/azure-identity bootstrap.
# Rather than crash on import or invocation, we lazy-construct
# TwigClient + repo_client per request and return 503 on any
# construction failure. Tests inject a synthetic provider (see
# ``make_handler(state_provider=...)``) so they don't need a live ADO.


def _default_state_provider(
    *, item_id: int, log_dir: Path, ado_repo: str | None,
    github_repo: str | None,
) -> dict[str, Any]:
    """Compute the work-state projection for ``item_id`` (default impl).

    Constructs a real :class:`TwigClient` + repo client and runs
    ``compute_work_state`` synchronously. Raises on construction or
    fetch failure — the handler maps the exception to 503.
    """
    from requiem.clients.twig import TwigClient
    from requiem.end_to_end import _resolve_repo_target
    from requiem.projections import compute_work_state

    _, repo_client = _resolve_repo_target(
        github_repo=github_repo, ado_repo=ado_repo, gh=None,
    )
    twig = TwigClient()
    projection_obj = asyncio.run(compute_work_state(
        root_item_id=item_id,
        twig=twig,
        repo_client=repo_client,
        log_dir=log_dir,
        github_repo=github_repo,
        ado_repo=ado_repo,
    ))
    return projection_obj.to_dict()


def make_handler(
    log_dir: Path,
    *,
    auto_resume: bool = False,
    state_provider: Any | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Build a request-handler class bound to ``log_dir``.

    A factory (rather than a module global) so multiple dashboards / tests can
    run against different log dirs in one process without clobbering state.

    ``auto_resume`` (opt-in, default off): when True, a successful gate resolution
    also spawns ``requiem resume`` for the run as a detached subprocess, so the
    operator doesn't have to run it by hand (ADR-0019). Off by default — the
    dashboard's safe contract is append-the-decision-and-stop.

    ``state_provider`` (ADR-0031 / R4): callable used to compute the
    work-state projection for ``/api/state/<item_id>``. Signature::

        provider(*, item_id: int, log_dir: Path, ado_repo: str | None,
                 github_repo: str | None) -> dict

    Defaults to :func:`_default_state_provider` (real TwigClient + repo
    client). Tests inject a synthetic provider so they don't need a
    live ADO. Any exception raised by the provider is mapped to a 503
    with the error message — a missing twig binary or expired token
    must not crash the dashboard process.
    """
    provider = state_provider or _default_state_provider

    class _Handler(BaseHTTPRequestHandler):
        server_version = "requiem-dashboard/1.0"

        # ---- helpers ----

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # localhost operator tool; allow same-origin fetch only.
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj: Any, status: int = 200) -> None:
            self._send(status, _json_bytes(obj), "application/json; charset=utf-8")

        def _not_found(self, detail: str = "not found") -> None:
            self._json({"error": detail}, status=404)

        # ---- routing ----

        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            path = unquote(urlparse(self.path).path)
            if path == "/" or path == "/index.html":
                self._send(200, PAGE_HTML.encode("utf-8"),
                           "text/html; charset=utf-8")
                return
            if path == "/healthz":
                self._json({"ok": True})
                return
            if path == "/api/runs":
                runs = projection.list_runs(log_dir)
                self._json({"runs": [r.to_dict() for r in runs]})
                return
            if path == "/api/gates":
                gates = projection.pending_gates(log_dir)
                self._json({"gates": [g.to_dict() for g in gates]})
                return
            if path.startswith("/api/runs/"):
                run_id = path[len("/api/runs/"):]
                if not run_id or "/" in run_id or "\\" in run_id:
                    self._not_found("invalid run id")
                    return
                detail = projection.run_detail(log_dir, run_id)
                if detail is None:
                    self._not_found(f"no such run {run_id!r}")
                    return
                self._json(detail.to_dict())
                return
            if path.startswith("/api/state/"):
                # ADR-0031 / R4: read-only work-state projection for one
                # ADO item. ``?ado_repo=org/proj/repo`` or
                # ``?github_repo=Owner/Repo`` enables repo-linkage
                # surfacing; otherwise the tree is pure ADO state.
                raw_id = path[len("/api/state/"):]
                if not raw_id or "/" in raw_id or "\\" in raw_id:
                    self._json(
                        {"error": "invalid item id"}, status=400,
                    )
                    return
                try:
                    item_id = int(raw_id)
                except ValueError:
                    self._json(
                        {"error": f"item id {raw_id!r} is not an integer"},
                        status=400,
                    )
                    return
                query = parse_qs(urlparse(self.path).query)
                ado_repo = (query.get("ado_repo") or [None])[0]
                github_repo = (query.get("github_repo") or [None])[0]
                if ado_repo and github_repo:
                    self._json(
                        {"error": "ado_repo and github_repo are mutually "
                                  "exclusive — pass one or the other"},
                        status=400,
                    )
                    return
                try:
                    payload = provider(
                        item_id=item_id,
                        log_dir=Path(log_dir),
                        ado_repo=ado_repo,
                        github_repo=github_repo,
                    )
                except KeyError as e:
                    # FakeTwig / real twig raises KeyError for "not found"
                    # (the projection's twig.show_async contract). Surface
                    # as 404 rather than 503 — the item genuinely doesn't
                    # exist (or isn't visible to this caller).
                    self._json(
                        {"error": f"work item {item_id} not found: {e}"},
                        status=404,
                    )
                    return
                except Exception as e:  # noqa: BLE001 — defensive
                    # Construction or fetch failure: 503 with the
                    # message, NEVER let it crash the dashboard.
                    self._json(
                        {"error": f"projection failed: "
                                  f"{type(e).__name__}: {e}"},
                        status=503,
                    )
                    return
                self._json(payload)
                return
            self._not_found()

        # ---- write: gate resolution (phase 2) ----

        def do_POST(self) -> None:  # noqa: N802
            path = unquote(urlparse(self.path).path)
            # POST /api/gates/<run_id>/resolve
            if path.startswith("/api/gates/") and path.endswith("/resolve"):
                run_id = path[len("/api/gates/"):-len("/resolve")]
                if not run_id or "/" in run_id or "\\" in run_id:
                    self._json({"error": "invalid run id"}, status=404)
                    return
                body = self._read_json_body()
                if body is None:
                    self._json({"error": "body must be JSON"}, status=400)
                    return
                choice = body.get("choice")
                if not isinstance(choice, str) or not choice:
                    self._json({"error": "missing 'choice'"}, status=400)
                    return
                try:
                    res = resolve_gate(log_dir, run_id, choice)
                except GateResolutionError as e:
                    # 404 for a missing run, 409 for a state/choice conflict.
                    status = 404 if e.reason == "run_not_found" else 409
                    self._json({"error": str(e), "reason": e.reason}, status=status)
                    return
                # Success: the gate_resolved event is committed. Optionally fire
                # `requiem resume` (opt-in) so the run continues without a manual
                # step. Best-effort — a spawn failure is reported but never undoes
                # the already-committed resolution.
                payload = res.to_dict()
                if auto_resume:
                    try:
                        _spawn_resume(log_dir, run_id)
                        payload["auto_resume"] = "spawned"
                    except _AutoResumeError as e:
                        payload["auto_resume"] = f"failed: {e}"
                else:
                    payload["auto_resume"] = "disabled"
                self._json(payload)
                return
            self._json({"error": "not found"}, status=404)

        def _read_json_body(self) -> dict[str, Any] | None:
            try:
                length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                return None
            if length <= 0 or length > 64 * 1024:  # sane cap for a control message
                return None
            raw = self.rfile.read(length)
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return None
            return obj if isinstance(obj, dict) else None

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        # Silence the default stderr request logging (operator tool).
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            return

    return _Handler


def build_server(
    log_dir: Path, host: str = "127.0.0.1", port: int = 8770,
    *, auto_resume: bool = False, state_provider: Any | None = None,
) -> ThreadingHTTPServer:
    """Construct (but do not start) the dashboard HTTP server.

    ``auto_resume`` (opt-in, default off) spawns ``requiem resume`` after a
    successful dashboard gate resolution — see :func:`make_handler`.

    ``state_provider`` (ADR-0031 / R4) lets tests inject a synthetic
    work-state computer; production callers use the default (real
    twig + repo client).
    """
    handler = make_handler(
        Path(log_dir),
        auto_resume=auto_resume,
        state_provider=state_provider,
    )
    return ThreadingHTTPServer((host, port), handler)


def serve(
    log_dir: Path, host: str = "127.0.0.1", port: int = 8770,
    *, auto_resume: bool = False,
) -> None:
    """Run the dashboard until interrupted (Ctrl-C)."""
    httpd = build_server(log_dir, host, port, auto_resume=auto_resume)
    sa = httpd.socket.getsockname()
    print(f"requiem dashboard → http://{sa[0]}:{sa[1]}  (log-dir: {Path(log_dir).resolve()})")
    print("read-only" + (" + auto-resume ON" if auto_resume else "") + "; Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping.")
    finally:
        httpd.server_close()
