"""Bring up a daemon-style session: runtime, per-agent event logs,
control server. Front-end agnostic — the tmux cockpit and the web UI
both start here and differ only in what they attach on top.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from conjure.address import Address
from conjure.control import ControlServer
from conjure.daemon import socket_path_for
from conjure.event_log import EventLog
from conjure.events import make_system_prompt_event
from conjure.record import AgentRecord
from conjure.runner import build_runtime
from conjure.runtime import Runtime


@dataclass
class Session:
    name: str
    runtime: Runtime
    root: Address
    control: ControlServer
    control_path: Path
    session_dir: Path

    def close(self) -> None:
        try:
            self.control.stop()
        except Exception:
            pass
        for record in list(self.runtime._records.values()):  # noqa: SLF001
            log = getattr(record, "event_log", None)
            if log is not None:
                try:
                    log.close()
                except Exception:
                    pass
        try:
            self.runtime.shutdown()
        except Exception:
            pass


def start_session(cfg: Any, name: str) -> Session:
    """Build the runtime with an event log per agent under
    ``<store_dir>/sessions/<name>/agents/`` and start the control
    server on the session's socket.

    The session name doubles as the on-disk session id, so what a UI
    shows and what's on disk always agree. Child agents' logs open with
    their ``system_prompt`` (the role they were spawned with) so a chat
    view starts with that context; the root's is the user's own config
    and isn't echoed back.
    """
    store_dir = Path(cfg.runtime.store_dir or "./.conjure/store")
    session_dir = store_dir / "sessions" / name
    agents_dir = session_dir / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    def spawn_listener(record: AgentRecord) -> None:
        record.event_log = EventLog(agents_dir / f"{record.addr.id}.jsonl")
        if record.parent is not None:
            record.event_log.emit(
                make_system_prompt_event(
                    text=record.spec.role_prompt or "",
                    label=record.spec.label or record.addr.label,
                )
            )

    def event_log_router(record: AgentRecord) -> EventLog | None:
        return record.event_log

    control_path = socket_path_for(name)
    runtime, root = build_runtime(
        cfg,
        session_id=name,
        event_log_router=event_log_router,
        spawn_listener=spawn_listener,
        stream=True,  # agent text streams into chat views
        control_socket=control_path,
    )
    try:
        from conjure.provenance import write_manifest

        write_manifest(Path(runtime.session_dir or session_dir), name=name, config=cfg, runtime=runtime)
    except Exception as exc:
        print(f"couldn't write the session manifest: {exc}", file=sys.stderr)
    control = ControlServer(runtime=runtime, socket_path=control_path)
    try:
        control.start()
    except Exception as exc:
        # The web UI works without the socket; tmux windows and the
        # claude_agent stdio MCP fallback don't. Say so and go on.
        print(f"control server failed to start: {exc}", file=sys.stderr)
    return Session(
        name=name,
        runtime=runtime,
        root=root,
        control=control,
        control_path=control_path,
        session_dir=Path(runtime.session_dir or session_dir),
    )
