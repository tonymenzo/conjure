"""What the web UI reads and writes — one surface over a live runtime
or a replayed journal.

``SessionView.live`` wraps the daemon's ``Runtime`` and its
``ControlServer``; reads and writes go through the same handlers the
tmux UI reaches over the control socket, called in-process.
``SessionView.replay`` rebuilds a finished (or running) session from
its journal, optionally as it stood at a given instant, and is
read-only. Both resolve each agent's event log the same way, so every
view below works identically on either.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from conjure.address import SYSTEM, USER, Address
from conjure.control import ControlServer
from conjure.persistence import JOURNAL_FILENAME
from conjure.runtime import Runtime, _engine_cost
from conjure.web import files

# Initial chat backlog per agent; older history pages in on demand.
EVENTS_WINDOW_BYTES = 512 * 1024
_SENTINELS = {USER.id, SYSTEM.id}


class ReadOnly(Exception):
    """A write was attempted on a replayed session."""


class _LogRef:
    """Stands in for ``record.event_log`` on replayed agents so the
    control handlers (which only read ``.path``) work unchanged."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def emit(self, _event: dict[str, Any]) -> None:  # replay is read-only
        pass


@dataclass
class _ParsedLog:
    identity: tuple[int, int]
    mtime_ns: int
    size: int
    offset: int
    rows: list[dict[str, Any]]


class SessionView:
    def __init__(
        self,
        *,
        runtime: Runtime,
        control: ControlServer,
        session_dir: Path | None,
        name: str,
        live: bool,
        until: float | None = None,
    ) -> None:
        self.runtime = runtime
        self.control = control
        self.session_dir = session_dir
        self.name = name
        self.live = live
        self.until = until
        self._parse_cache: dict[str, _ParsedLog] = {}
        self._config: dict[str, Any] = {}  # a replay's recorded config (manifest)
        self._cache_lock = threading.Lock()

    # ----- construction -----

    @classmethod
    def live_session(
        cls, runtime: Runtime, control: ControlServer, *, name: str
    ) -> "SessionView":
        return cls(
            runtime=runtime,
            control=control,
            session_dir=runtime.session_dir,
            name=name,
            live=True,
        )

    @classmethod
    def replay_session(
        cls, session_dir: Path, *, until: float | None = None
    ) -> "SessionView":
        session_dir = Path(session_dir)
        if not (session_dir / JOURNAL_FILENAME).exists():
            raise FileNotFoundError(f"no journal in {session_dir}")
        rt = Runtime.replay(session_dir, until=until)
        agents_dir = session_dir / "agents"
        config = _manifest_config(session_dir)
        for addr, rec in rt._records.items():  # noqa: SLF001
            if addr.id in _SENTINELS:
                continue
            rec.event_log = _LogRef(agents_dir / f"{addr.id}.jsonl")
            # Where the sandbox was when the run recorded it: its tool
            # calls name paths under it, wherever the copy is now.
            rec.recorded_sandbox_dir = rec.spec.sandbox_dir
            # Files view: an export carries sandboxes beside the journal;
            # a session in a store finds them in <store>/sandboxes.
            for cand in (session_dir / "sandboxes" / addr.id, session_dir.parent.parent / "sandboxes" / addr.id):
                if cand.is_dir():
                    rec.spec = rec.spec.model_copy(update={"sandbox_dir": str(cand)})
                    break
            else:
                moved = _moved_sandbox(rec.spec.sandbox_dir, session_dir, config)
                if moved is not None:
                    rec.spec = rec.spec.model_copy(update={"sandbox_dir": str(moved)})
            # Replay reconstructs structure, not liveness: an agent the
            # journal never terminated was alive (idle, as far as the
            # record shows) at this instant.
            if rec.status != "terminated":
                rec.status = "idle"
        view = cls(
            runtime=rt,
            control=ControlServer(runtime=rt, socket_path=session_dir / ".unused.sock"),
            session_dir=session_dir,
            name=session_dir.name,
            live=False,
            until=until,
        )
        view._config = config
        return view

    # ----- helpers -----

    def _records(self) -> dict[Address, Any]:
        with self.runtime._lock:  # noqa: SLF001
            return {
                a: r
                for a, r in self.runtime._records.items()  # noqa: SLF001
                if a.id not in _SENTINELS
            }

    def _addr(self, addr_id: str | None) -> Address | None:
        if not addr_id:
            return None
        addr = self.runtime.address_by_id(addr_id)
        return None if addr is None or addr.id in _SENTINELS else addr

    def log_path(self, rec: Any) -> Path | None:
        log = getattr(rec, "event_log", None)
        path = getattr(log, "path", None) if log is not None else None
        if path is not None:
            return Path(path)
        if self.session_dir is not None:
            return self.session_dir / "agents" / f"{rec.addr.id}.jsonl"
        return None

    def _require_live(self) -> None:
        if not self.live:
            raise ReadOnly("this is a replayed session; it is read-only")

    # ----- reads -----

    def meta(self) -> dict[str, Any]:
        first, last = self._journal_bounds()
        return {
            "session": self.name,
            "live": self.live,
            "until": self.until,
            "started": first,
            "last_event": last,
            "now": time.time(),
            "auto_mode": bool(getattr(self.runtime, "auto_mode", False)),
            "toolbase": getattr(self.runtime, "toolbase", None) is not None,
            "session_dir": str(self.session_dir) if self.session_dir else None,
        }

    def run_record(self) -> dict[str, Any]:
        """The run's provenance and totals: the session manifest (frozen
        config, versions; an export's ``export`` block) and the summary
        computed from the journal and event logs."""
        if self.session_dir is None:
            return {"ok": False, "error": "this session has no on-disk record"}
        from conjure.provenance import read_manifest, summarize

        return {
            "ok": True,
            "manifest": read_manifest(self.session_dir),
            "summary": summarize(self.session_dir),
        }

    def export_archive(self) -> tuple[str, bytes]:
        """``(filename, .tar.gz bytes)`` of this session's export (paths
        scrubbed, sandboxes included, large files listed not copied)."""
        import shutil
        import tempfile

        from conjure.export import export_session

        if self.session_dir is None:
            raise ValueError("this session has no on-disk record")
        store = getattr(self.runtime, "store_dir", None)
        tmp = Path(tempfile.mkdtemp(prefix="conjure-export-"))
        try:
            name = f"{self.session_dir.name}-export"
            rep = export_session(
                self.session_dir, tmp / name,
                store_dir=Path(store) if store else None, archive=True,
            )
            data = Path(rep["archive"]).read_bytes()
            return f"{name}.tar.gz", data
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def snapshot(self) -> dict[str, Any]:
        """Everything the always-visible panes need, in one call: the
        agent tree, cost, the cross-agent activity feed, and pending
        permission requests."""
        perms = self.control._permissions(None).get("pending", [])  # noqa: SLF001
        activity = self.control._activity(120).get("activity", [])  # noqa: SLF001
        tool_events = self.control._log_events(20).get("events", [])  # noqa: SLF001
        if self.until is not None:
            activity = [a for a in activity if (a.get("ts") or 0) <= self.until]
            tool_events = [e for e in tool_events if e["ts"] <= self.until]
        cost = self.control._cost()  # noqa: SLF001
        usages = {a: self._usage(r) for a, r in self._records().items()}
        return {
            "ok": True,
            "ts": time.time(),
            "tree": self._tree({p["addr"] for p in perms}, usages),
            "cost": {
                "total": round(sum(u["cost"] for u in usages.values()), 6),
                "subscription": cost.get("has_subscription_agent", False),
                "tokens": sum(u["tokens"] for u in usages.values()),
                "unpriced_tokens": sum(u["unpriced_tokens"] for u in usages.values()),
            },
            "activity": activity,
            "tool_events": tool_events,
            "permissions": perms,
            "auto_mode": bool(getattr(self.runtime, "auto_mode", False)),
        }

    def _usage(self, rec: Any) -> dict[str, Any]:
        """``{"cost", "tokens", "unpriced_tokens"}`` for one agent: from
        its engine when live; in a replay, from what the journal recorded
        (``agent_meta``) and, for journals from before conjure 0.2.7,
        the token counts in its event log."""
        engine = self.runtime._engine_for(rec)  # noqa: SLF001
        if engine is not None:
            u = _engine_usage(engine) or {}
            return {"cost": _engine_cost(engine), "tokens": u.get("tokens", 0),
                    "unpriced_tokens": u.get("unpriced_tokens", 0)}
        meta = getattr(rec, "meta", None) or {}
        tokens = meta.get("tokens")
        if not isinstance(tokens, int):
            evs = self._parsed_log(self.log_path(rec))
            if self.until is not None:
                evs = [e for e in evs if (e.get("ts") or 0) <= self.until]
            tokens = _log_tokens(evs)
        unpriced = meta.get("unpriced_tokens")
        if not isinstance(unpriced, int):
            unpriced = tokens if _unpriced_model(rec, meta, self._config) else 0
        return {"cost": float(meta.get("cost_usd") or 0.0), "tokens": tokens, "unpriced_tokens": unpriced}

    def _tree(self, awaiting: set[str], usages: dict[Address, dict[str, Any]] | None = None) -> dict[str, Any] | None:
        root = self.runtime.root_addr
        records = self._records()
        if root is None or root not in records:
            return None
        if usages is None:
            usages = {a: self._usage(r) for a, r in records.items()}
        costs = {a: u["cost"] for a, u in usages.items()}

        def walk(addr: Address) -> tuple[dict[str, Any], float]:
            rec = records[addr]
            kids = []
            subtree = costs.get(addr, 0.0)
            for child in sorted(rec.children, key=lambda c: (records[c].spawned_at if c in records else 0, c.id)):
                if child not in records:
                    continue
                node, sub = walk(child)
                kids.append(node)
                subtree += sub
            spec = rec.spec
            engine = self.runtime._engine_for(rec)  # noqa: SLF001
            model, model_source = model_info(engine, spec)
            meta = getattr(rec, "meta", None) or {}
            if engine is None and meta.get("model"):
                # Replay / export: what the journal recorded it ran on.
                model, model_source = meta["model"], meta.get("model_source", "observed")
            node = {
                "addr": addr.id,
                "label": addr.label or addr.id,
                "status": rec.status,
                "awaiting_permission": addr.id in awaiting,
                "depth": rec.depth,
                "parent": rec.parent.id if rec.parent else None,
                "engine": (meta.get("engine") if engine is None and meta.get("engine") else _engine_kind(engine, spec)),
                "model": model,
                "model_source": model_source,
                "internal": bool(spec.internal),
                "oneshot": bool(spec.oneshot),
                "budget": spec.budget,
                "cost": costs.get(addr, 0.0),
                "subtree_cost": subtree,
                # Tokens, and how many of them have no known price (a
                # gateway model without a list or configured price).
                "tokens": usages[addr]["tokens"],
                "unpriced_tokens": usages[addr]["unpriced_tokens"],
                "toolkits": toolkit_items(getattr(spec, "toolkits", None)),
                "inbox_size": len(rec.inbox),
                "spawned_at": rec.spawned_at,
                "children": kids,
            }
            return node, subtree

        return walk(root)[0]

    def agent(self, addr_id: str) -> dict[str, Any]:
        addr = self._addr(addr_id)
        if addr is None:
            return {"ok": False, "error": f"unknown agent: {addr_id}"}
        rec = self.runtime.record_for(addr)
        spec = rec.spec
        engine = self.runtime._engine_for(rec)  # noqa: SLF001
        model, model_source = model_info(engine, spec)
        meta = getattr(rec, "meta", None) or {}
        if engine is None and meta.get("model"):
            model, model_source = meta["model"], meta.get("model_source", "observed")
        caps = [
            {"addr": a.id, "label": a.label or a.id}
            for a in rec.capabilities.snapshot()
            if a != addr
        ]
        out: dict[str, Any] = {
            "ok": True,
            "addr": addr.id,
            "label": addr.label or addr.id,
            "status": rec.status,
            "depth": rec.depth,
            "parent": _ref(rec.parent),
            "children": [_ref(c) for c in sorted(rec.children, key=lambda c: c.id)],
            "capabilities": caps,
            "role_prompt": spec.role_prompt,
            "engine": meta.get("engine") if engine is None and meta.get("engine") else _engine_kind(engine, spec),
            "model": model,
            "model_source": model_source,
            "system_prompt": meta.get("system_prompt") or getattr(engine, "system_prompt_text", None),
            "tools": list(spec.tools or []),
            "permissions": dict(spec.permissions or {}),
            "budget": spec.budget,
            "oneshot": spec.oneshot,
            "lazy": spec.lazy,
            "sandbox_dir": spec.sandbox_dir,
            # Every absolute path this agent's sandbox has gone by (here,
            # and where the run recorded it), so the chat can show paths
            # in tool calls relative to it.
            "sandbox_paths": _sandbox_paths(rec, self.runtime),
            "toolkits": {
                k: v.model_dump(exclude_none=True)
                for k, v in (getattr(spec, "toolkits", None) or {}).items()
            },
            "toolbase_loadout": getattr(spec, "toolbase_loadout", None),
            "spawned_at": rec.spawned_at,
            "inbox": [
                {
                    "seq": e.seq,
                    "ts": e.ts,
                    "from": e.from_.id,
                    "from_label": e.from_.label or e.from_.id,
                    "kind": (e.headers or {}).get("kind", "msg"),
                    "body": e.body,
                }
                for e in rec.inbox.read_recent(max_n=200)
                if self.until is None or e.ts <= self.until
            ],
        }
        hub = getattr(self.runtime, "toolbase", None)
        if hub is not None and rec.parent is None:
            # The root's grantable set — the boundary on every toolkit
            # in the tree.
            try:
                out["may_grant"] = toolkit_items(hub.root_authority())
            except Exception:
                out["may_grant"] = []
            out["authority_mode"] = getattr(hub, "authority_mode", None)
        if self.live:
            ctx = self.control._context_usage_for(addr.id)  # noqa: SLF001
            if ctx is not None:
                out["context"] = ctx
        return out

    def events(
        self,
        addr_id: str,
        *,
        since: int | None = None,
        before: int | None = None,
        window: int = EVENTS_WINDOW_BYTES,
    ) -> dict[str, Any]:
        """Byte-offset tail of one agent's event log.

        ``since`` returns everything appended after that offset (the
        live poll). ``before`` returns up to ``window`` bytes ending
        there (paging back through history). Neither returns the last
        ``window`` bytes. Offsets always land on line boundaries.
        """
        addr = self._addr(addr_id)
        if addr is None:
            return {"ok": False, "error": f"unknown agent: {addr_id}"}
        path = self.log_path(self.runtime.record_for(addr))
        if path is None or not path.exists():
            return {"ok": True, "events": [], "start": 0, "offset": 0, "size": 0}
        size = path.stat().st_size
        if since is not None:
            start, end = min(max(0, since), size), size
        else:
            end = size if before is None else min(max(0, before), size)
            start = max(0, end - window)
        with path.open("rb") as fh:
            if start > 0 and since is None:
                # Align to the next full line.
                fh.seek(start - 1)
                if fh.read(1) != b"\n":
                    fh.readline()
                start = fh.tell()
            fh.seek(start)
            data = fh.read(max(0, end - start))
        cut = data.rfind(b"\n") + 1  # only complete lines
        events = _parse_lines(data[:cut])
        if self.until is not None:
            events = [e for e in events if (e.get("ts") or 0) <= self.until]
        return {
            "ok": True,
            "events": events,
            "start": start,
            "offset": start + cut,
            "size": size,
        }

    def graph(self) -> dict[str, Any]:
        """Nodes plus three edge kinds: ``spawn`` (parent → child),
        ``message`` (sender → recipient, counted, with the last time),
        and ``capability`` (an address held beyond the defaults — the
        introductions that let siblings talk)."""
        records = self._records()
        nodes = []
        spawn, caps = [], []
        for addr, rec in records.items():
            nodes.append(
                {
                    "addr": addr.id,
                    "label": addr.label or addr.id,
                    "status": rec.status,
                    "depth": rec.depth,
                    "parent": rec.parent.id if rec.parent else None,
                    "internal": bool(rec.spec.internal),
                }
            )
            if rec.parent is not None and rec.parent in records:
                spawn.append({"from": rec.parent.id, "to": addr.id})
            default = {addr, USER, SYSTEM, *rec.children}
            if rec.parent is not None:
                default.add(rec.parent)
            for held in rec.capabilities.snapshot():
                if held not in default and held in records:
                    caps.append({"from": addr.id, "to": held.id})
        counts: dict[tuple[str, str], dict[str, Any]] = {}
        for addr, rec in records.items():
            for env in rec.inbox.read_recent(max_n=10_000):
                if self.until is not None and env.ts > self.until:
                    continue
                key = (env.from_.id, addr.id)
                row = counts.setdefault(key, {"from": key[0], "to": key[1], "count": 0, "last_ts": 0.0})
                row["count"] += 1
                row["last_ts"] = max(row["last_ts"], env.ts)
        return {
            "ok": True,
            "nodes": nodes,
            "spawn": spawn,
            "capability": caps,
            "message": sorted(counts.values(), key=lambda r: r["last_ts"]),
        }

    def timeline(self) -> dict[str, Any]:
        """Per-agent lanes: lifetime (spawn → terminate), turns
        (``turn_start`` → ``turn_end`` from the driver, falling back to
        the claude engine's ``thinking_*``), tool calls and failures,
        and messages received."""
        terminated = self._terminate_times()
        records = self._records()
        lanes = []
        t_min, t_max = None, None
        for addr, rec in sorted(records.items(), key=lambda kv: kv[1].spawned_at):
            evs = self._parsed_log(self.log_path(rec))
            if self.until is not None:
                evs = [e for e in evs if (e.get("ts") or 0) <= self.until]
            turns, tools, errors = _turns_and_tools(evs)
            msgs = [
                {"ts": e.ts, "from": e.from_.id, "from_label": e.from_.label or e.from_.id}
                for e in rec.inbox.read_recent(max_n=10_000)
                if self.until is None or e.ts <= self.until
            ]
            end = terminated.get(addr.id)
            lanes.append(
                {
                    "addr": addr.id,
                    "label": addr.label or addr.id,
                    "status": rec.status,
                    "depth": rec.depth,
                    "parent": rec.parent.id if rec.parent else None,
                    "internal": bool(rec.spec.internal),
                    "start": rec.spawned_at,
                    "end": end,
                    "turns": turns,
                    "tools": tools,
                    "errors": errors,
                    "messages": msgs,
                }
            )
            for t in [rec.spawned_at, end] + [x["ts"] for x in tools + msgs]:
                if isinstance(t, (int, float)):
                    t_min = t if t_min is None else min(t_min, t)
                    t_max = t if t_max is None else max(t_max, t)
            for turn in turns:
                t_max = max(t_max or 0, turn["end"] or turn["start"])
        now = self.until if self.until is not None else (time.time() if self.live else t_max)
        return {"ok": True, "lanes": lanes, "t_min": t_min, "t_max": t_max, "now": now}

    # ----- files view (read-only; see web/files.py) -----

    def files_workspaces(self) -> dict[str, Any]:
        return {"ok": True, "workspaces": files.workspaces(self._records(), self.runtime)}

    def _workspace(self, addr_id: str) -> Path:
        addr = self._addr(addr_id)
        if addr is None:
            raise files.FileAccessError(f"unknown agent {addr_id!r}")
        root = files.sandbox_dir(self.runtime.record_for(addr), self.runtime)
        if root is None:
            raise files.FileAccessError("this agent has no workspace")
        if not root.is_dir():
            raise files.FileAccessError(
                "this workspace has not been created yet" if self.live
                else "this workspace is not in this copy of the run"
            )
        return root

    def files_list(self, addr_id: str, path: str | None, *, hidden: bool = False) -> dict[str, Any]:
        return files.listing(self._workspace(addr_id), path, hidden=hidden)

    def files_read(self, addr_id: str, path: str | None) -> dict[str, Any]:
        return files.read(self._workspace(addr_id), path)

    def files_find(self, addr_id: str, query: str, *, hidden: bool = False) -> dict[str, Any]:
        return files.find(self._workspace(addr_id), query, hidden=hidden)

    def files_recent(self, addr_id: str, *, hidden: bool = False) -> dict[str, Any]:
        return files.recent(self._workspace(addr_id), hidden=hidden)

    def files_raw(self, addr_id: str, path: str | None) -> tuple[bytes, str]:
        return files.raw(self._workspace(addr_id), path)

    # ----- writes (live only) -----

    def send(self, addr_id: str, body: Any) -> dict[str, Any]:
        self._require_live()
        return self.control._send(addr_id, body)  # noqa: SLF001

    def terminate(self, addr_id: str, cascade: bool = True) -> dict[str, Any]:
        self._require_live()
        return self.control._terminate(addr_id, cascade)  # noqa: SLF001

    def resolve_permission(
        self, req_id: str, decision: str, scope: str | None
    ) -> dict[str, Any]:
        self._require_live()
        return self.control._resolve_permission(req_id, decision, scope=scope)  # noqa: SLF001

    def set_auto_mode(self, on: bool) -> dict[str, Any]:
        self._require_live()
        self.runtime.auto_mode = bool(on)
        return {"ok": True, "on": self.runtime.auto_mode}

    # ----- internals -----

    def _journal_path(self) -> Path | None:
        if self.session_dir is None:
            return None
        return self.session_dir / JOURNAL_FILENAME

    def _journal_entries(self) -> list[dict[str, Any]]:
        path = self._journal_path()
        if path is None:
            return []
        if self.live:
            journal = getattr(self.runtime, "_journal", None)
            if journal is not None:
                journal.flush()
        return self._parsed_log(path)

    def _journal_bounds(self) -> tuple[float | None, float | None]:
        entries = self._journal_entries()
        stamps = [e["ts"] for e in entries if isinstance(e.get("ts"), (int, float))]
        if not stamps:
            return None, None
        return min(stamps), max(stamps)

    def _terminate_times(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for e in self._journal_entries():
            if e.get("kind") != "terminate":
                continue
            ts = e.get("ts")
            if self.until is not None and isinstance(ts, (int, float)) and ts > self.until:
                continue
            addr = (e.get("payload") or {}).get("addr") or {}
            if isinstance(addr, dict) and addr.get("id") and isinstance(ts, (int, float)):
                out.setdefault(addr["id"], float(ts))
        return out

    def _parsed_log(self, path: Path | None) -> list[dict[str, Any]]:
        """Parse appended complete lines only; reset on replacement/truncation.

        Cache updates are serialized so concurrent HTTP readers cannot regress
        the offset. An incomplete final line is retried on the next append.
        Returned lists remain stable when a later request updates the cache.
        """
        if path is None:
            return []
        key = str(path)
        with self._cache_lock:
            try:
                st = os.stat(path)
                identity = (st.st_dev, st.st_ino)
                hit = self._parse_cache.get(key)
                if hit and (hit.identity, hit.mtime_ns, hit.size) == (
                    identity, st.st_mtime_ns, st.st_size
                ):
                    return hit.rows
                with path.open("rb") as fh:
                    st = os.fstat(fh.fileno())
                    identity = (st.st_dev, st.st_ino)
                    append = hit is not None and hit.identity == identity and st.st_size > hit.size
                    offset = hit.offset if append else 0
                    fh.seek(offset)
                    data = fh.read(max(0, st.st_size - offset))
            except OSError:
                self._parse_cache.pop(key, None)
                return []
            cut = data.rfind(b"\n") + 1
            added = _parse_lines(data[:cut])
            rows = hit.rows + added if append else added
            self._parse_cache[key] = _ParsedLog(
                identity, st.st_mtime_ns, st.st_size, offset + cut, rows,
            )
            return rows


class ReplayCache:
    """Replayed views keyed by instant, so scrubbing back and forth
    doesn't re-read the journal for positions already visited."""

    def __init__(self, session_dir: Path, *, size: int = 8) -> None:
        self.session_dir = Path(session_dir)
        self._size = size
        self._views: OrderedDict[float | None, SessionView] = OrderedDict()
        self._lock = threading.Lock()

    def at(self, until: float | None) -> SessionView:
        # Exact instant: rounding could land just before an event the
        # caller is pointing at (the root's own spawn, at worst).
        key = None if until is None else float(until)
        with self._lock:
            view = self._views.get(key)
            if view is not None:
                self._views.move_to_end(key)
                return view
        view = SessionView.replay_session(self.session_dir, until=key)
        with self._lock:
            self._views[key] = view
            while len(self._views) > self._size:
                self._views.popitem(last=False)
        return view


# ---------- pure helpers ----------


def toolkit_items(toolkits: Any) -> list[str]:
    """Render a toolset back into ``tb activate`` grammar for display."""
    out: list[str] = []
    for kit, grant in sorted((toolkits or {}).items()):
        bundles = getattr(grant, "bundles", None)
        tools = getattr(grant, "tools", None)
        if bundles is None and tools is None:
            out.append(kit)
            continue
        out.extend(f"{kit}/{b}" for b in bundles or [])
        out.extend(f"{kit}__{t}" for t in tools or [])
    return out


def _ref(addr: Address | None) -> dict[str, str] | None:
    if addr is None:
        return None
    return {"addr": addr.id, "label": addr.label or addr.id}


def model_info(engine: Any, spec: Any) -> tuple[str | None, str]:
    """Which model an agent runs on, and how sure we are.

    Engines that know better say so via ``model_info()`` (claude_agent
    also reports ``resolved``: an alias looked up in the CLI's model
    table, exact before the first reply). Otherwise: ``observed`` — the
    engine has seen the model answer (orchestral calls the model it was
    built with). ``configured`` — an alias or id from the spec, not yet
    confirmed by a reply. ``default`` — nothing pinned; a claude_agent
    root runs on the ``claude`` CLI's default, which is only known
    after its first reply.
    """
    info = getattr(engine, "model_info", None)
    if callable(info):
        try:
            return info()
        except Exception:
            pass
    observed = getattr(engine, "_observed_model", None)
    if isinstance(observed, str) and observed:
        return observed, "observed"
    name = _model_name(engine)
    if name:
        is_claude = hasattr(engine, "_observed_model")
        return name, "configured" if is_claude else "observed"
    if getattr(spec, "model", None):
        return spec.model, "configured"
    return None, "default"


def _engine_kind(engine: Any, spec: Any) -> str:
    """The engine actually running (``auto`` resolved)."""
    if engine is None:
        return spec.engine
    name = type(engine).__name__
    return {
        "ClaudeAgentEngine": "claude_agent",
        "CodexEngine": "codex",
        "OrchestralEngine": "orchestral",
        "ScriptedEngine": "scripted",
    }.get(name, spec.engine)


def _model_name(engine: Any) -> str | None:
    fn = getattr(engine, "model_name", None)
    if not callable(fn):
        return None
    try:
        return fn()
    except Exception:
        return None


def _parse_lines(data: bytes) -> list[dict[str, Any]]:
    out = []
    for raw in data.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


def _turns_and_tools(
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    has_driver_turns = any(e.get("kind") == "turn_start" for e in events)
    start_kind, end_kind = (
        ("turn_start", "turn_end") if has_driver_turns else ("thinking_start", "thinking_end")
    )
    turns: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    open_turn: dict[str, Any] | None = None
    pending: list[dict[str, Any]] = []  # tool calls awaiting results, FIFO
    for e in events:
        kind = e.get("kind")
        ts = e.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        if kind == start_kind:
            if open_turn is not None:
                open_turn["end"] = ts
            open_turn = {"start": ts, "end": None, "errored": False}
            turns.append(open_turn)
        elif kind == end_kind:
            if open_turn is not None:
                open_turn["end"] = ts
                open_turn["errored"] = bool(e.get("errored"))
                open_turn = None
        elif kind in ("stream_end", "response"):
            for tc in e.get("tool_calls") or []:
                call = {"ts": ts, "name": tc.get("name") or "?", "failed": False, "done": None}
                tools.append(call)
                pending.append(call)
        elif kind == "tool":
            if pending:
                call = pending.pop(0)
                call["done"] = ts
                call["failed"] = bool(e.get("failed"))
        elif kind == "error":
            errors.append({"ts": ts, "text": (e.get("text") or "")[:300]})
    return turns, tools, errors


def _log_tokens(events: list[dict[str, Any]]) -> int:
    """Tokens from an event log's ``usage`` events. Each is cumulative
    within its turn, so a turn counts its largest."""
    total = turn_in = turn_out = 0
    for e in events:
        kind = e.get("kind")
        if kind in ("turn_start", "thinking_start"):
            total += turn_in + turn_out
            turn_in = turn_out = 0
        elif kind == "usage":
            turn_in = max(turn_in, int(e.get("tokens_in") or 0))
            turn_out = max(turn_out, int(e.get("tokens_out") or 0))
    return total + turn_in + turn_out


def _manifest_config(session_dir: Path) -> dict[str, Any]:
    try:
        from conjure.provenance import read_manifest

        config = (read_manifest(session_dir) or {}).get("config")
    except Exception:
        return {}
    return config if isinstance(config, dict) else {}


def _unpriced_model(rec: Any, meta: dict[str, Any], config: dict[str, Any]) -> bool:
    """Whether a replayed orchestral agent's model had no price under the
    recorded config: a gateway (``openai_compatible``) model with neither
    a configured nor a list price. Other agents were priced by their
    provider or engine."""
    if (meta.get("engine") or rec.spec.engine) not in (None, "orchestral"):
        return False
    entry = (config.get("llms") or {}).get(rec.spec.llm)
    if not isinstance(entry, dict) or str(entry.get("provider", "")).lower() != "openai_compatible":
        return False
    from conjure.pricing import PriceBook

    model = meta.get("model") or rec.spec.model or entry.get("model")
    try:
        return PriceBook(entry.get("pricing")).price(model) is None
    except Exception:
        return False


def _sandbox_paths(rec: Any, runtime: Any) -> list[str]:
    paths: list[str] = []
    candidates = [getattr(rec, "recorded_sandbox_dir", None), rec.spec.sandbox_dir]
    try:
        candidates.append(str(runtime.sandbox_of(rec.addr) or ""))
    except Exception:
        pass
    for p in candidates:
        if not p:
            continue
        p = str(p).rstrip("/")
        for form in (p, os.path.realpath(p) if os.path.isabs(p) else p):
            if form and form != "/" and form not in paths:
                paths.append(form)
    return paths


def _moved_sandbox(recorded: str | None, session_dir: Path, config: dict[str, Any]) -> Path | None:
    """Where a sandbox recorded on another machine is in this copy of the
    run. A session in a store (``<store>/sessions/<id>``) whose agent
    worked in ``<recorded store>/../workspace`` finds it at
    ``<this store>/../workspace``. Only paths inside the run directory
    (the store's parent) are mapped, and only when they exist here."""
    if not recorded or session_dir.parent.name != "sessions" or _is_dir(Path(recorded)):
        return None
    recorded_store = (config.get("runtime") or {}).get("store_dir")
    if not recorded_store:
        return None
    rel = os.path.relpath(recorded, Path(recorded_store).parent)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        return None
    local = session_dir.parent.parent.parent / rel
    return local if _is_dir(local) else None


def _is_dir(path: Path) -> bool:
    # macOS answers /home/... (an automount point) with EOPNOTSUPP,
    # which Path.is_dir raises rather than reporting False.
    try:
        return path.is_dir()
    except OSError:
        return False


def _engine_usage(engine: Any) -> dict[str, int] | None:
    """``{"tokens", "unpriced_tokens"}`` from engines that count them."""
    fn = getattr(engine, "usage", None)
    if not callable(fn):
        return None
    try:
        u = fn()
    except Exception:
        return None
    return {
        "tokens": int(u.get("tokens_in", 0)) + int(u.get("tokens_out", 0)),
        "unpriced_tokens": int(u.get("unpriced_tokens", 0)),
    }
