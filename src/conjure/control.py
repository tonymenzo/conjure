"""Unix-socket JSON-RPC control plane for the daemon.

The daemon starts a ``ControlServer`` on a per-session Unix domain
socket at ``~/.conjure/sessions/<session>.sock``. The main TUI
(``conjure-main``) and the files popup (``conjure-files``)
connect through ``ControlClient``, issue short-lived requests, and
render the responses.

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
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any

from conjure.runtime import _engine_cost


class ControlServer:

    def __init__(self, *, runtime: Any, socket_path: Path) -> None:
        self.runtime = runtime
        self.socket_path = socket_path
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        # ``_log_events`` tail cache: path → (mtime, size, window,
        # parsed rows). The UI snapshot ticks at 2 Hz and most agent
        # logs are idle on any given tick — a stat() per file replaces
        # a read + JSON-parse of its whole tail window.
        self._log_tail_cache: dict[
            str, tuple[float, int, int, list[dict[str, Any]]]
        ] = {}

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
        # Each accepted connection runs in its own daemon thread so
        # one slow request (e.g. MCP ``Spawn`` waiting on a child
        # engine's ``await connect()``) can't block status / send /
        # snapshot from the UI. Threads exit when the conn closes.
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()  # type: ignore[union-attr]
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(
                target=self._serve_connection,
                args=(conn,),
                daemon=True,
                name="control-conn",
            ).start()

    def _serve_connection(self, conn: socket.socket) -> None:
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
        if method == "activity":
            return self._activity(int(request.get("limit", 80) or 80))
        if method == "log_events":
            return self._log_events(
                int(request.get("limit_per_agent", 20) or 20)
            )
        if method == "inboxes":
            return self._inboxes(int(request.get("limit", 20) or 20))
        if method == "sandbox":
            return self._sandbox(request.get("addr"), request.get("path"))
        if method == "sandbox_recent":
            return self._sandbox_recent(
                request.get("addr"),
                int(request.get("limit", 20) or 20),
            )
        if method == "permissions":
            return self._permissions(request.get("addr"))
        if method == "resolve_permission":
            return self._resolve_permission(
                request.get("req_id"),
                request.get("decision"),
                scope=request.get("scope"),
            )
        if method == "set_auto_mode":
            self.runtime.auto_mode = bool(request.get("on"))
            return {"ok": True, "on": self.runtime.auto_mode}
        if method == "get_auto_mode":
            return {"ok": True, "on": bool(self.runtime.auto_mode)}
        if method == "tool_call":
            return self._tool_call(request)
        if method == "send":
            return self._send(request.get("addr"), request.get("body"))
        if method == "terminate":
            return self._terminate(
                request.get("addr"), request.get("cascade", True)
            )
        return {"ok": False, "error": f"unknown method: {method}"}

    def _permissions(self, addr_id: str | None) -> dict[str, Any]:
        """List pending permission requests (optionally for one
        agent). Powers the chat-pane approval banner."""
        addr = None
        if addr_id:
            addr = self.runtime.address_by_id(addr_id)
        reqs = self.runtime.list_pending_permissions(addr=addr)
        return {
            "ok": True,
            "pending": [
                {
                    "req_id": r.req_id,
                    "addr": r.addr.id,
                    "addr_label": r.addr.label,
                    "tool_name": r.tool_name,
                    "args": r.args,
                    "ts": r.ts,
                }
                for r in reqs
            ],
        }

    def _tool_call(self, request: dict[str, Any]) -> dict[str, Any]:
        """Execute a conjure tool against the runtime, on behalf
        of the token-holding agent. Used by ``conjure-mcp`` to
        forward claude_agent SDK tool calls into the daemon's tool
        surface (capability checks, journaling, etc. all happen the
        same way they do for orchestral agents)."""
        token = request.get("token")
        name = request.get("name")
        args = request.get("args") or {}
        if not token or not name:
            return {"ok": False, "error": "missing token or name"}
        cls = _TOOL_CLASS_REGISTRY.get(name)
        if cls is None:
            return {"ok": False, "error": f"unknown tool: {name}"}
        try:
            tool = cls(runtime_token=token, **args)
            result = tool._run()  # noqa: SLF001
        except TypeError as exc:
            return {"ok": False, "code": "bad_args", "error": str(exc)}
        except Exception as exc:
            return {
                "ok": False,
                "code": "exec_error",
                "error": f"{type(exc).__name__}: {exc}",
            }
        return result if isinstance(result, dict) else {"ok": True, "result": result}

    def _resolve_permission(
        self,
        req_id: str | None,
        decision: str | None,
        *,
        scope: str | None = None,
    ) -> dict[str, Any]:
        if not req_id:
            return {"ok": False, "error": "missing req_id"}
        if decision not in ("allow", "deny"):
            return {"ok": False, "error": "decision must be 'allow' or 'deny'"}
        if scope not in (None, "once", "session"):
            return {
                "ok": False,
                "error": "scope must be 'once' or 'session' when set",
            }
        ok = self.runtime.resolve_permission(req_id, decision, scope=scope)
        return {"ok": True, "resolved": ok}

    def _snapshot(self, addr_id: str | None) -> dict[str, Any]:
        """Return ``tree`` + ``cost`` + cross-agent ``activity`` +
        pending permissions for the selected agent in a single
        round-trip. The main UI ticks 2 Hz; one consolidated query
        per tick is cheaper than separate ones."""
        tree_reply = self._tree()
        cost_reply = self._cost()
        activity_reply = self._activity(80)
        log_events_reply = self._log_events(20)
        # Return every agent's pending permissions, not just the
        # selected one — a child agent blocked on Edit/Write/Bash
        # while the user is reading the parent's chat would otherwise
        # sit silently waiting. The UI picks the request to prompt
        # for (preferring the selected agent so the modal labels the
        # one the user is reading).
        permissions_reply = self._permissions(None)
        result: dict[str, Any] = {
            "ok": True,
            "tree": tree_reply.get("tree"),
            "cost": {
                "total": cost_reply.get("total", 0.0),
                "rows": cost_reply.get("rows", []),
            },
            "activity": activity_reply.get("activity", []),
            "log_events": log_events_reply.get("events", []),
            "pending_permissions": permissions_reply.get("pending", []),
            "auto_mode": bool(self.runtime.auto_mode),
        }
        if addr_id:
            inbox_reply = self._inbox(addr_id)
            if inbox_reply.get("ok"):
                result["inbox"] = inbox_reply.get("envelopes", [])
            else:
                result["inbox_error"] = inbox_reply.get("error")
            # Context-window meter for the selected agent only —
            # keeps the per-tick cost bounded regardless of how many
            # agents are alive.
            ctx = self._context_usage_for(addr_id)
            if ctx is not None:
                result["context"] = ctx
        return result

    def _context_usage_for(self, addr_id: str) -> dict[str, int] | None:
        addr = self.runtime.address_by_id(addr_id)
        if addr is None:
            return None
        rec = self.runtime.record_for(addr)
        engine = self.runtime._engine_for(rec)  # noqa: SLF001
        return _engine_context_usage(engine)

    def _sandbox(self, addr_id: str | None, path: str | None) -> dict[str, Any]:
        """List sandbox contents (when ``path`` is None or a directory)
        or return file contents (when ``path`` is a file).

        Powers the file-browser popup. Always resolves through the
        per-agent sandbox; refuses any path that escapes it."""
        from pathlib import Path

        from conjure.tools.filesystem import _sandbox_for, _within

        if not addr_id:
            return {"ok": False, "error": "missing addr"}
        addr = self.runtime.address_by_id(addr_id)
        if addr is None:
            return {"ok": False, "error": f"unknown addr: {addr_id}"}
        record = self.runtime.record_for(addr)
        sandbox = _sandbox_for(record, self.runtime)
        if sandbox is None:
            return {
                "ok": False,
                "error": "agent has no sandbox dir configured",
            }
        rel = path or ""
        target = (sandbox / rel).resolve() if rel else sandbox
        if not _within(target, sandbox):
            return {"ok": False, "error": "path escapes sandbox"}
        if not target.exists():
            return {"ok": False, "error": "not found"}
        if target.is_dir():
            entries = []
            for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
                if child.name.startswith("."):
                    continue
                entries.append(
                    {
                        "name": child.name,
                        "path": str(child.relative_to(sandbox)),
                        "is_dir": child.is_dir(),
                        "size": child.stat().st_size if child.is_file() else 0,
                    }
                )
            return {
                "ok": True,
                "kind": "dir",
                "path": str(target.relative_to(sandbox)) if target != sandbox else "",
                "entries": entries,
            }
        # File path — read up to 200 KB as text.
        try:
            size = target.stat().st_size
            if size > 200_000:
                content = target.read_text(encoding="utf-8", errors="replace")[:200_000]
                truncated = True
            else:
                content = target.read_text(encoding="utf-8", errors="replace")
                truncated = False
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "kind": "file",
            "path": str(target.relative_to(sandbox)),
            "size": size,
            "content": content,
            "truncated": truncated,
        }

    def _log_events(self, limit_per_agent: int) -> dict[str, Any]:
        """Read recent tool / stream events from each agent's event
        log and return them as a flat, time-sorted list.

        Sourced from the per-agent JSONL files (the same ones the
        chat pane tails) so the daemon doesn't need to maintain a
        separate event buffer. For each agent we read the last
        ``limit_per_agent * 4`` lines (cheap upper bound — most
        events aren't tool-related), filter to the kinds the
        activity feed cares about, and tag each row with the
        agent's addr/label.

        Returned shape per row (mirrors the envelope-activity
        format where it can, so the renderer treats them uniformly):

            {"ts": float, "from": addr_id, "from_label": str,
             "kind": "tool_call" | "tool_result",
             "tool": tool_name, "failed": bool (results only)}
        """
        import json as _json

        out: list[dict[str, Any]] = []
        with self.runtime._lock:  # noqa: SLF001
            records = list(self.runtime._records.values())  # noqa: SLF001
        for rec in records:
            event_log = getattr(rec, "event_log", None)
            log_path = getattr(event_log, "path", None) if event_log else None
            if log_path is None:
                continue
            # Read from the file tail instead of scanning historical
            # stream chunks on every UI snapshot.
            window = max(40, limit_per_agent * 8)
            # Stat-guarded cache: an unchanged file (same mtime + size)
            # reuses the rows parsed on a previous tick.
            cache_key = str(log_path)
            try:
                st = os.stat(log_path)
            except OSError:
                continue
            cached = self._log_tail_cache.get(cache_key)
            if (
                cached is not None
                and cached[0] == st.st_mtime
                and cached[1] == st.st_size
                and cached[2] == window
            ):
                out.extend(cached[3][-limit_per_agent:])
                continue
            try:
                tail_lines = _tail_text_lines(log_path, window)
            except OSError:
                continue
            collected: list[dict[str, Any]] = []
            for raw in tail_lines:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = _json.loads(raw)
                except _json.JSONDecodeError:
                    continue
                kind = ev.get("kind")
                ts = ev.get("ts")
                if not isinstance(ts, (int, float)):
                    continue
                if kind == "stream_end":
                    for tc in ev.get("tool_calls") or []:
                        collected.append(
                            {
                                "ts": float(ts),
                                "from": rec.addr.id,
                                "from_label": rec.addr.label or rec.addr.id,
                                "kind": "tool_call",
                                "tool": (
                                    tc.get("name")
                                    or tc.get("tool_name")
                                    or "?"
                                ),
                            }
                        )
                elif kind == "tool":
                    collected.append(
                        {
                            "ts": float(ts),
                            "from": rec.addr.id,
                            "from_label": rec.addr.label or rec.addr.id,
                            "kind": "tool_result",
                            "failed": bool(ev.get("failed")),
                            "summary": (ev.get("text") or "")[:80],
                        }
                    )
            # Keep only the most recent ``limit_per_agent`` per agent
            # so a chatty agent can't crowd out a quieter one.
            collected.sort(key=lambda e: e["ts"])
            self._log_tail_cache[cache_key] = (
                st.st_mtime, st.st_size, window, collected
            )
            out.extend(collected[-limit_per_agent:])
        out.sort(key=lambda e: e["ts"])
        return {"ok": True, "events": out}

    def _sandbox_recent(
        self, addr_id: str | None, limit: int
    ) -> dict[str, Any]:
        """Walk the agent's sandbox and return the ``limit`` most
        recently-modified files (newest first). Hidden files / dirs
        (leading ``.``) are skipped. Capped at 10k files scanned so
        an enormous sandbox can't hang the popup."""
        from conjure.tools.filesystem import _sandbox_for

        if not addr_id:
            return {"ok": False, "error": "missing addr"}
        addr = self.runtime.address_by_id(addr_id)
        if addr is None:
            return {"ok": False, "error": f"unknown addr: {addr_id}"}
        record = self.runtime.record_for(addr)
        sandbox = _sandbox_for(record, self.runtime)
        if sandbox is None:
            return {
                "ok": False,
                "error": "agent has no sandbox dir configured",
            }
        entries: list[tuple[float, str, int]] = []
        scanned = 0
        try:
            for child in sandbox.rglob("*"):
                scanned += 1
                if scanned > 10_000:
                    break
                # Skip dotfiles and anything under a dot-directory —
                # checked via any ancestor name starting with ``.``.
                if any(part.startswith(".") for part in child.relative_to(sandbox).parts):
                    continue
                if not child.is_file():
                    continue
                try:
                    st = child.stat()
                except OSError:
                    continue
                entries.append(
                    (st.st_mtime, str(child.relative_to(sandbox)), st.st_size)
                )
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        entries.sort(key=lambda t: -t[0])
        out = [
            {"path": p, "mtime": mt, "size": sz}
            for mt, p, sz in entries[:limit]
        ]
        return {"ok": True, "entries": out, "scanned": scanned}

    def _inboxes(self, limit: int) -> dict[str, Any]:
        """Every agent's recent inbox envelopes + conversation peers.
        Powers the inbox popup in a single round-trip."""
        with self.runtime._lock:  # noqa: SLF001
            records = list(self.runtime._records.values())  # noqa: SLF001
        # Build a label lookup so we can resolve sentinels (@user,
        # @system) and unknown addrs to nice display names.
        label_of: dict[str, str] = {
            r.addr.id: (r.addr.label or r.addr.id) for r in records
        }
        # Walk a recent envelope window across every inbox to derive
        # per-agent peer summaries (who they've recently exchanged
        # with, in which direction). Keys: addr_id -> {peer_id ->
        # {"in": ts, "out": ts}}.
        peers: dict[str, dict[str, dict[str, float]]] = {}
        out: list[dict[str, Any]] = []
        for rec in records:
            if rec.addr.id in ("@user", "@system"):
                continue
            envs = rec.inbox.read_recent(max_n=max(limit, 50))
            for e in envs:
                me, peer = rec.addr.id, e.from_.id
                # Incoming from `peer` to `me`.
                peers.setdefault(me, {}).setdefault(peer, {})
                if e.ts > peers[me][peer].get("in", 0):
                    peers[me][peer]["in"] = e.ts
                # Same edge as outgoing from `peer` to `me` — useful
                # for the peer agent's own view (skip sentinels).
                if peer not in ("@user", "@system"):
                    peers.setdefault(peer, {}).setdefault(me, {})
                    if e.ts > peers[peer][me].get("out", 0):
                        peers[peer][me]["out"] = e.ts
            out.append(
                {
                    "addr": rec.addr.id,
                    "label": rec.addr.label or rec.addr.id,
                    "status": rec.status,
                    "depth": rec.depth,
                    "envelopes": [
                        {
                            "seq": e.seq,
                            "from": e.from_.id,
                            "from_label": e.from_.label,
                            "body": e.body,
                            "ts": e.ts,
                        }
                        for e in envs[-limit:]
                    ],
                }
            )
        # Attach peer summary to each row, sorted by recency.
        for row in out:
            peer_map = peers.get(row["addr"], {})
            peer_rows: list[dict[str, Any]] = []
            for peer_id, ts in peer_map.items():
                last_in = ts.get("in", 0)
                last_out = ts.get("out", 0)
                last_ts = max(last_in, last_out)
                if last_ts <= 0:
                    continue
                peer_rows.append(
                    {
                        "peer": peer_id,
                        "peer_label": label_of.get(peer_id, peer_id),
                        "last_ts": last_ts,
                        "last_in_ts": last_in,
                        "last_out_ts": last_out,
                        # Awaiting reply = we (row) sent something
                        # to peer more recently than we received from
                        # them. The peer "owes" us a response.
                        "awaiting_reply": last_out > last_in,
                    }
                )
            peer_rows.sort(key=lambda r: -r["last_ts"])
            row["peers"] = peer_rows
        out.sort(key=lambda r: (r["depth"], r["label"]))
        return {"ok": True, "agents": out}

    def _activity(self, limit: int) -> dict[str, Any]:
        """Most-recent ``limit`` envelopes across all agent inboxes,
        sorted oldest-first. Used by the main UI's activity feed."""
        rows: list[dict[str, Any]] = []
        with self.runtime._lock:  # noqa: SLF001
            records = list(self.runtime._records.values())  # noqa: SLF001
        for rec in records:
            if rec.addr.id in ("@user", "@system"):
                continue
            envs = rec.inbox.read_recent(max_n=limit)
            for e in envs:
                rows.append(
                    {
                        "ts": e.ts,
                        "from": e.from_.id,
                        "from_label": e.from_.label,
                        "to": rec.addr.id,
                        "to_label": rec.addr.label,
                        "body": e.body,
                        "headers": dict(e.headers) if e.headers else {},
                        "in_reply_to": e.in_reply_to,
                    }
                )
        rows.sort(key=lambda r: r.get("ts") or 0)
        return {"ok": True, "activity": rows[-limit:]}

    # ---- methods ----

    def _tree(self) -> dict[str, Any]:
        """Tree walk per tick. Stays *cheap* — only fields that are
        O(1) per agent. ``context_usage`` is intentionally excluded
        (some engines do an async-bridge call with a 2s timeout, and
        running that for every agent every 500ms would lag the UI).
        Context usage is fetched separately for the selected agent in
        ``_snapshot``."""
        root = self.runtime.root_addr
        if root is None:
            return {"ok": True, "tree": None}
        # One lock acquisition for the whole walk. ``record_for`` per
        # node was N RLock round-trips per 500ms tick — pure contention
        # against spawns/sends on a busy tree. Children are listed
        # under the same lock so a concurrent spawn can't mutate a set
        # mid-iteration.
        with self.runtime._lock:  # noqa: SLF001
            snapshot = {
                a: (r, sorted(r.children, key=lambda c: c.id))
                for a, r in self.runtime._records.items()  # noqa: SLF001
            }

        def walk(addr: Any) -> dict[str, Any]:
            rec, children = snapshot[addr]
            event_log = getattr(rec, "event_log", None)
            log_path = (
                str(event_log.path)
                if event_log is not None and getattr(event_log, "path", None)
                else None
            )
            engine = self.runtime._engine_for(rec)  # noqa: SLF001
            return {
                "addr": addr.id,
                "label": addr.label or addr.id,
                "status": rec.status,
                "engine": rec.spec.engine,
                "model": _engine_model_name(engine),
                "log_path": log_path,
                "internal": rec.spec.internal,
                "children": [walk(c) for c in children],
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
        # Drop the @user / @system sentinels — they have no engine and
        # never accrue cost; surfacing them as $0 lines is just noise.
        # Single lock pass: ``costs_by_agent`` + a per-row
        # ``record_for`` was 1 + N lock acquisitions per UI tick.
        with self.runtime._lock:  # noqa: SLF001
            pairs = [
                (addr, rec)
                for addr, rec in self.runtime._records.items()  # noqa: SLF001
                if addr.id not in ("@user", "@system")
            ]
        has_subscription_agent = False
        out_rows: list[dict[str, Any]] = []
        total = 0.0
        for addr, rec in pairs:
            engine = self.runtime._engine_for(rec)  # noqa: SLF001
            cost = _engine_cost(engine)
            total += cost
            uses_sub = bool(
                getattr(engine, "uses_subscription", None)
                and engine.uses_subscription()
            )
            if uses_sub:
                has_subscription_agent = True
            out_rows.append(
                {
                    "addr": addr.id,
                    "label": addr.label or addr.id,
                    "cost": cost,
                    "uses_subscription": uses_sub,
                }
            )
        return {
            "ok": True,
            "total": total,
            "rows": out_rows,
            "has_subscription_agent": has_subscription_agent,
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

    # Default read timeout for UI-style queries (snapshot, status,
    # send). Tool-call dispatches (the MCP bridge → daemon path) need
    # a much longer ceiling — set per-call via ``timeout=``.
    DEFAULT_TIMEOUT_S: float = 10.0

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = str(socket_path)

    def call(
        self,
        method: str,
        *,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        request = {"method": method, **kwargs}
        wait = timeout if timeout is not None else self.DEFAULT_TIMEOUT_S
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(self.socket_path)
            _write_line(s, request)
            line = _read_line(s, timeout=wait)
        if line is None:
            return {
                "ok": False,
                "code": "rpc_timeout",
                "error": (
                    f"daemon did not respond within {wait:.0f}s — "
                    "the call may still be running in the background"
                ),
            }
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


def _tail_text_lines(
    path: Path,
    max_lines: int,
    *,
    chunk_size: int = 8192,
) -> list[str]:
    """Read up to ``max_lines`` lines from the end of a UTF-8 text file."""
    if max_lines <= 0:
        return []
    with path.open("rb") as fh:
        fh.seek(0, 2)
        pos = fh.tell()
        chunks: list[bytes] = []
        newline_count = 0
        while pos > 0 and newline_count <= max_lines:
            read_size = min(chunk_size, pos)
            pos -= read_size
            fh.seek(pos)
            chunk = fh.read(read_size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")
    data = b"".join(reversed(chunks))
    return data.decode("utf-8", errors="replace").splitlines()[-max_lines:]


# ---- tool class registry (for the MCP bridge's tool_call RPC) ----

def _build_tool_registry() -> dict[str, type]:
    """Map tool short names (used over the MCP wire) to their
    conjure tool classes. Lazily imported so importing
    ``conjure.control`` doesn't drag the whole tool surface in."""
    from conjure.tools.combinators import (
        AgentFilterTool,
        AgentFixedPointTool,
        AgentFoldTool,
        AgentMapTool,
    )
    from conjure.tools.primitives import (
        CallTool,
        IntroduceTool,
        ListInboxTool,
        PeekTool,
        RecvTool,
        SendTool,
        SpawnTool,
        TerminateTool,
        WaitForTool,
    )

    return {
        "spawn": SpawnTool,
        "send": SendTool,
        "recv": RecvTool,
        "wait_for": WaitForTool,
        "terminate": TerminateTool,
        "introduce": IntroduceTool,
        "list_inbox": ListInboxTool,
        "peek": PeekTool,
        "call": CallTool,
        "agent_map": AgentMapTool,
        "agent_fold": AgentFoldTool,
        "agent_filter": AgentFilterTool,
        "agent_fixed_point": AgentFixedPointTool,
    }


_TOOL_CLASS_REGISTRY: dict[str, type] = _build_tool_registry()


# ---- engine introspection helpers (optional capabilities) ----

def _engine_model_name(engine: Any) -> str | None:
    if engine is None:
        return None
    fn = getattr(engine, "model_name", None)
    if not callable(fn):
        return None
    try:
        return fn()
    except Exception:
        return None


def _engine_context_usage(engine: Any) -> dict[str, int] | None:
    if engine is None:
        return None
    fn = getattr(engine, "context_usage", None)
    if not callable(fn):
        return None
    try:
        result = fn()
    except Exception:
        return None
    if result is None:
        return None
    used, total = result
    return {"used": int(used), "max": int(total)}
