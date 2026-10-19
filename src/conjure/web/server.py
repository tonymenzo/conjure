"""HTTP server for the conjure web UI (standard library only).

Routes — every ``/api/*`` call needs the session token in an
``X-Conjure-Token`` header:

  GET  /                      the page (static; holds no session data)
  GET  /static/<file>         its script and stylesheet
  GET  /api/meta              session name, live/replay, journal bounds
  GET  /api/snapshot          tree + cost + activity + permissions
  GET  /api/agent?addr=       one agent's spec, capabilities, inbox
  GET  /api/events?addr=&since=&before=&limit=
                              byte-offset tail of an agent's event log
  GET  /api/inbox?addr=&after=&until=   messages to an agent, by time
  GET  /api/replay            the quiet stretches a player can skip
  GET  /api/graph             spawn / message / capability edges
  GET  /api/timeline          per-agent lanes: turns, tools, messages
  GET  /api/files/workspaces  every workspace, nested, with its agents
  GET  /api/files/list?addr=&path=&hidden=
  GET  /api/files/read?addr=&path=      metadata, and text when it is text
  GET  /api/files/find?addr=&q=&hidden=
  GET  /api/files/recent?addr=&hidden=
  GET  /api/files/raw?addr=&path=       bytes (images, PDFs, downloads)
  POST /api/send              {addr, body}
  POST /api/terminate         {addr, cascade}
  POST /api/permission        {req_id, decision, scope}
  POST /api/auto_mode         {on}

Replay sessions accept ``at=<unix ts>`` on every GET to view the session
as it stood at that instant, and refuse every POST.

Security: the UI can approve tool calls (``Bash`` included), so a
loopback bind is not enough on its own. The token is required on every
API call; it travels in a custom header, which a cross-site page can't
set without a CORS preflight this server never answers; and the
``Host`` header must name this server, which defeats DNS rebinding.
"""

from __future__ import annotations

import hmac
import json
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from conjure.web.state import ReadOnly, SessionView

STATIC_DIR = Path(__file__).parent / "static"
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
}
_MAX_BODY = 1_000_000
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; frame-src blob:; connect-src 'self'; "
        "frame-ancestors 'none'"
    ),
}

ViewProvider = Callable[[float | None], SessionView]


class WebServer:
    """Serves one session. ``provider(at)`` returns the view to read:
    the live session (``at`` ignored) or a replay at that instant."""

    def __init__(
        self,
        provider: ViewProvider,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        token: str | None = None,
        live: bool = True,
    ) -> None:
        self.provider = provider
        self.token = token or secrets.token_urlsafe(24)
        self.live = live
        self._httpd = _bind(host, port, self)
        self.host, self.port = self._httpd.server_address[:2]
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "") else self.host
        return f"http://{host}:{self.port}/?token={self.token}"

    def start(self) -> "WebServer":
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="conjure-web", daemon=True
        )
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        self._serving = True
        self._httpd.serve_forever()

    def stop(self) -> None:
        # ``shutdown`` waits for a serve loop to acknowledge; with none
        # running it would block forever.
        if self._thread is not None or getattr(self, "_serving", False):
            self._httpd.shutdown()
        self._httpd.server_close()


def _bind(host: str, port: int, app: WebServer) -> ThreadingHTTPServer:
    """Bind ``port``, or the next free one (up to +20) — a second
    session shouldn't fail just because the first holds the default.
    ``port=0`` asks the OS."""
    handler = type("Handler", (_Handler,), {"app": app})
    last_err: OSError | None = None
    candidates = [0] if port == 0 else range(port, port + 21)
    for p in candidates:
        try:
            httpd = ThreadingHTTPServer((host, p), handler)
            httpd.daemon_threads = True
            return httpd
        except OSError as exc:
            last_err = exc
    raise OSError(f"no free port in {port}..{port + 20}: {last_err}")


class _Handler(BaseHTTPRequestHandler):
    app: WebServer  # bound per server in ``_bind``
    server_version = "conjure-web"

    def log_message(self, *_args: Any) -> None:  # quiet by default
        pass

    # ----- plumbing -----

    def _send(self, status: int, body: bytes, ctype: str, *, cache: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=60" if cache else "no-store")
        for k, v in _SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"ok": False, "error": message}, status)

    def _host_ok(self) -> bool:
        """DNS-rebinding defense: the Host must *name* this server. The
        port is deliberately not compared: an SSH tunnel from a laptop
        (``ssh -L 9000:127.0.0.1:8765 node``) arrives as
        ``localhost:9000``, and a rebinding attacker can't choose the
        hostname, which is what this check pins."""
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):  # [::1]:port
            name = host.split("]", 1)[0] + "]"
        else:
            name = host.rsplit(":", 1)[0] if ":" in host else host
        allowed = {"127.0.0.1", "localhost", "[::1]", str(self.app.host).lower()}
        return bool(name) and name in allowed

    def _authed(self) -> bool:
        given = self.headers.get("X-Conjure-Token") or ""
        return hmac.compare_digest(given.encode(), self.app.token.encode())

    # ----- routing -----

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._error(HTTPStatus.MISDIRECTED_REQUEST, "bad Host header")
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self._static("index.html")
        if url.path.startswith("/static/"):
            return self._static(url.path[len("/static/"):])
        if not url.path.startswith("/api/"):
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        if not self._authed():
            return self._error(HTTPStatus.UNAUTHORIZED, "missing or bad token")
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            at = float(q["at"]) if q.get("at") else None
            view = self.app.provider(at)
            if url.path == "/api/files/raw":
                data, ctype = view.files_raw(q.get("addr", ""), q.get("path"))
                return self._send(HTTPStatus.OK, data, ctype)
            if url.path == "/api/export":
                filename, data = view.export_archive()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Cache-Control", "no-store")
                for k, v in _SECURITY_HEADERS.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)
                return None
            return self._json(self._get(view, url.path, q))
        except ValueError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # surface, don't kill the UI
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")

    def _get(self, view: SessionView, path: str, q: dict[str, str]) -> Any:
        if path == "/api/meta":
            return {"ok": True, **view.meta()}
        if path == "/api/snapshot":
            return view.snapshot()
        if path == "/api/agent":
            return view.agent(q.get("addr", ""))
        if path == "/api/events":
            return view.events(
                q.get("addr", ""),
                since=int(q["since"]) if q.get("since") else None,
                before=int(q["before"]) if q.get("before") else None,
                limit=int(q["limit"]) if q.get("limit") else None,
            )
        if path == "/api/inbox":
            return view.inbox(
                q.get("addr", ""),
                after=float(q["after"]) if q.get("after") else None,
                until=float(q["until"]) if q.get("until") else None,
            )
        if path == "/api/replay":
            return view.replay_index()
        if path == "/api/graph":
            return view.graph()
        if path == "/api/timeline":
            return view.timeline()
        if path == "/api/run":
            return view.run_record()
        if path.startswith("/api/files/"):
            hidden = q.get("hidden") in ("1", "true")
            addr, rel = q.get("addr", ""), q.get("path")
            if path == "/api/files/workspaces":
                return view.files_workspaces()
            if path == "/api/files/list":
                return view.files_list(addr, rel, hidden=hidden)
            if path == "/api/files/read":
                return view.files_read(addr, rel)
            if path == "/api/files/find":
                return view.files_find(addr, q.get("q", ""), hidden=hidden)
            if path == "/api/files/recent":
                return view.files_recent(addr, hidden=hidden)
        raise ValueError(f"unknown route {path}")

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._error(HTTPStatus.MISDIRECTED_REQUEST, "bad Host header")
        if not self._authed():
            return self._error(HTTPStatus.UNAUTHORIZED, "missing or bad token")
        if not self.app.live:
            return self._error(HTTPStatus.FORBIDDEN, "replayed sessions are read-only")
        length = int(self.headers.get("Content-Length") or 0)
        if length > _MAX_BODY:
            return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("body must be a JSON object")
        except ValueError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, f"bad JSON: {exc}")
        view = self.app.provider(None)
        path = urlparse(self.path).path
        try:
            if path == "/api/send":
                body = data.get("body")
                if body is None or body == "":
                    raise ValueError("empty message")
                return self._json(view.send(str(data.get("addr", "")), body))
            if path == "/api/terminate":
                return self._json(view.terminate(str(data.get("addr", "")), bool(data.get("cascade", True))))
            if path == "/api/permission":
                return self._json(
                    view.resolve_permission(
                        str(data.get("req_id", "")),
                        str(data.get("decision", "")),
                        data.get("scope"),
                    )
                )
            if path == "/api/auto_mode":
                return self._json(view.set_auto_mode(bool(data.get("on"))))
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        except ReadOnly as exc:
            return self._error(HTTPStatus.FORBIDDEN, str(exc))
        except ValueError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")

    def _static(self, name: str) -> None:
        target = (STATIC_DIR / name).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        if not target.is_file():
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        ctype = _CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        self._send(HTTPStatus.OK, target.read_bytes(), ctype)
