"""requiem.dashboard.server — a stdlib-only, read-only web dashboard.

No FastAPI, no uvicorn, no framework: a ``ThreadingHTTPServer`` +
``BaseHTTPRequestHandler`` serving a tiny JSON API and one self-contained HTML
page (ADR-0019). Everything it shows is a pure projection of the event log
(``requiem.dashboard.projection``), so the server itself holds no state.

Read-only by design: no route mutates a run. Gate *resolution* from the browser
is phase 2 (ADR-0019 §4). Binds to ``127.0.0.1`` — an operator-local tool, not a
public service.

Routes::

    GET /                     → the HTML page
    GET /api/runs             → list_runs(...)        as JSON
    GET /api/runs/<run_id>    → run_detail(...)       as JSON  (404 if absent)
    GET /api/gates            → pending_gates(...)    as JSON
    GET /healthz              → {"ok": true}
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from requiem.dashboard import projection
from requiem.dashboard.page import PAGE_HTML


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, default=str).encode("utf-8")


def make_handler(log_dir: Path) -> type[BaseHTTPRequestHandler]:
    """Build a request-handler class bound to ``log_dir``.

    A factory (rather than a module global) so multiple dashboards / tests can
    run against different log dirs in one process without clobbering state.
    """

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
            self._not_found()

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        # Silence the default stderr request logging (operator tool).
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            return

    return _Handler


def build_server(log_dir: Path, host: str = "127.0.0.1", port: int = 8770) -> ThreadingHTTPServer:
    """Construct (but do not start) the dashboard HTTP server."""
    handler = make_handler(Path(log_dir))
    return ThreadingHTTPServer((host, port), handler)


def serve(log_dir: Path, host: str = "127.0.0.1", port: int = 8770) -> None:
    """Run the dashboard until interrupted (Ctrl-C)."""
    httpd = build_server(log_dir, host, port)
    sa = httpd.socket.getsockname()
    print(f"requiem dashboard → http://{sa[0]}:{sa[1]}  (log-dir: {Path(log_dir).resolve()})")
    print("read-only; Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping.")
    finally:
        httpd.server_close()
