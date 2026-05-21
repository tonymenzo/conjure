"""Unix-socket JSON-RPC control plane for the daemon.

The daemon starts a ``ControlServer`` on a per-session Unix domain
socket at ``~/.combinator/sessions/<session>.sock``. The meta-view
popup (``combinator meta``) connects through ``ControlClient``,
issues short-lived requests, and renders the responses.

Protocol: one JSON object per line, request + response. Methods:

- ``tree``            → nested tree of {addr, label, status, children}
- ``status``          → flat list of all agents with caps + inbox sizes
- ``cost``            → per-agent costs + total (USD)
- ``inbox``           → envelopes in a given agent's inbox
- ``send``            → inject a message into a given agent's inbox
- ``terminate``       → terminate an agent (cascading by default)

All responses are ``{"ok": True, ...}`` or ``{"ok": False, "error": ...}``.

The server is read-side-only for state; the only state-mutating
methods are ``send`` and ``terminate``. Mutations go through the same
runtime API that the REPL uses, so capability rules apply.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from typing import Any


class ControlServer:

    def __init__(self, *, runtime: Any, socket_path: Path) -> None:
        self.runtime = runtime
        self.socket_path = socket_path
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(self.socket_path))
        sock.listen(8)
        sock.settimeout(0.2)  # so the accept loop can check _stop
        self._sock = sock
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="control-server"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        try:
            self.socket_path.unlink()
        except OSError:
            pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()  # type: ignore[union-attr]
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                self._handle(conn)
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    def _handle(self, conn: socket.socket) -> None:
        request = _read_line(conn)
        if request is None:
            return
        try:
            payload = json.loads(request)
        except Exception:
            _write_line(conn, {"ok": False, "error": "bad request"})
            return
        try:
            reply = self._dispatch(payload)
        except Exception as exc:
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        _write_line(conn, reply)

    def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        method = request.get("method")
        if method == "tree":
            return self._tree()
        if method == "status":
            return self._status()
        if method == "cost":
            return self._cost()
        if method == "inbox":
            return self._inbox(request.get("addr"))
        if method == "snapshot":
            return self._snapshot(request.get("addr"))
        if method == "send":
            return self._send(request.get("addr"), request.get("body"))
        if method == "terminate":
            return self._terminate(
                request.get("addr"), request.get("cascade", True)
            )
        return {"ok": False, "error": f"unknown method: {method}"}

    def _snapshot(self, addr_id: str | None) -> dict[str, Any]:
        """Return ``tree`` + ``cost`` + (optionally) ``inbox`` for one
        address in a single round-trip. The main UI ticks 2 Hz; doing
        three separate socket calls per tick is wasteful, so the UI
        uses this consolidated query."""
        tree_reply = self._tree()
        cost_reply = self._cost()
        result: dict[str, Any] = {
            "ok": True,
            "tree": tree_reply.get("tree"),
            "cost": {
                "total": cost_reply.get("total", 0.0),
                "rows": cost_reply.get("rows", []),
            },
        }
        if addr_id:
            inbox_reply = self._inbox(addr_id)
            if inbox_reply.get("ok"):
                result["inbox"] = inbox_reply.get("envelopes", [])
            else:
                result["inbox_error"] = inbox_reply.get("error")
        return result

    # ---- methods ----

    def _tree(self) -> dict[str, Any]:
        root = self.runtime.root_addr
        if root is None:
            return {"ok": True, "tree": None}

        def walk(addr: Any) -> dict[str, Any]:
            rec = self.runtime.record_for(addr)
            event_log = getattr(rec, "event_log", None)
            log_path = (
                str(event_log.path)
                if event_log is not None and getattr(event_log, "path", None)
                else None
            )
            return {
                "addr": addr.id,
                "label": addr.label or addr.id,
                "status": rec.status,
                "log_path": log_path,
                "children": [
                    walk(c) for c in sorted(rec.children, key=lambda a: a.id)
                ],
            }

        return {"ok": True, "tree": walk(root)}

    def _status(self) -> dict[str, Any]:
        with self.runtime._lock:  # noqa: SLF001 - inspector
            records = list(self.runtime._records.values())  # noqa: SLF001
        return {
            "ok": True,
            "agents": [
                {
                    "addr": r.addr.id,
                    "label": r.addr.label or r.addr.id,
                    "status": r.status,
                    "caps": len(r.capabilities),
                    "inbox_size": len(r.inbox),
                }
                for r in records
            ],
        }

    def _cost(self) -> dict[str, Any]:
        rows = self.runtime.costs_by_agent()
        return {
            "ok": True,
            "total": sum(c for _addr, c in rows),
            "rows": [
                {
                    "addr": addr.id,
                    "label": addr.label or addr.id,
                    "cost": cost,
                }
                for addr, cost in rows
            ],
        }

    def _inbox(self, addr_id: str | None) -> dict[str, Any]:
        if not addr_id:
            return {"ok": False, "error": "missing addr"}
        addr = self.runtime.address_by_id(addr_id)
        if addr is None:
            return {"ok": False, "error": f"unknown addr: {addr_id}"}
        envs = self.runtime.read_inbox(addr)
        return {
            "ok": True,
            "envelopes": [
                {
                    "seq": e.seq,
                    "msg_id": e.msg_id,
                    "from": e.from_.id,
                    "from_label": e.from_.label,
                    "thread_id": e.thread_id,
                    "body": e.body,
                }
                for e in envs
            ],
        }

    def _send(self, addr_id: str | None, body: Any) -> dict[str, Any]:
        if not addr_id:
            return {"ok": False, "error": "missing addr"}
        addr = self.runtime.address_by_id(addr_id)
        if addr is None:
            return {"ok": False, "error": f"unknown addr: {addr_id}"}
        # Drop a clean ``user_input`` event into the target agent's
        # event log before dispatching to the runtime. The agent's
        # context will receive a driver-wrapped multiline prompt
        # (which serializes as a noisy ``user`` event); the UI uses
        # this cleaner event for the chat-history replay so swapping
        # panes shows what the human actually typed.
        record = self.runtime.record_for(addr)
        event_log = getattr(record, "event_log", None)
        if event_log is not None:
            text = body if isinstance(body, str) else repr(body)
            try:
                event_log.emit({"kind": "user_input", "text": text})
            except Exception:
                pass
        msg_id = self.runtime.send_external(to=addr, body=body)
        return {"ok": True, "msg_id": msg_id}

    def _terminate(self, addr_id: str | None, cascade: bool) -> dict[str, Any]:
        if not addr_id:
            return {"ok": False, "error": "missing addr"}
        addr = self.runtime.address_by_id(addr_id)
        if addr is None:
            return {"ok": False, "error": f"unknown addr: {addr_id}"}
        terminated = self.runtime.terminate(
            addr, requested_by="control", cascade=bool(cascade)
        )
        return {"ok": True, "terminated": [a.id for a in terminated]}


class ControlClient:

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = str(socket_path)

    def call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        request = {"method": method, **kwargs}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(self.socket_path)
            _write_line(s, request)
            line = _read_line(s, timeout=10.0)
        if line is None:
            return {"ok": False, "error": "empty response"}
        try:
            return json.loads(line)
        except Exception as exc:
            return {"ok": False, "error": f"bad response: {exc}"}


# ---- low-level read/write helpers ----

def _read_line(conn: socket.socket, *, timeout: float | None = None) -> str | None:
    """Read one ``\\n``-terminated UTF-8 line from a stream socket."""
    if timeout is not None:
        conn.settimeout(timeout)
    buf = bytearray()
    while True:
        try:
            chunk = conn.recv(65536)
        except socket.timeout:
            return None
        if not chunk:
            break
        buf.extend(chunk)
        if b"\n" in buf:
            break
    if not buf:
        return None
    line, _, _ = bytes(buf).partition(b"\n")
    try:
        return line.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _write_line(conn: socket.socket, payload: dict[str, Any]) -> None:
    line = json.dumps(payload, default=str) + "\n"
    conn.sendall(line.encode("utf-8"))
