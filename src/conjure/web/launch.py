"""``conjure web`` — run a session behind the browser UI, replay a
finished one, or reopen a running one's URL.

- ``conjure web <config>``              live session in the foreground;
                                        Ctrl+C stops it.
- ``conjure web <config> --detach``     live session as a daemon
                                        (``conjure quit`` stops it).
- ``conjure web --replay <session>``    read-only view of a journaled
                                        session, with a time scrubber.
- ``conjure web --attach [<session>]``  print (and open) a running
                                        session's URL.
- ``conjure run <config> --web``        tmux cockpit plus the web UI on
                                        one runtime.

The URL carries the session token; it's also written, owner-only, to
``~/.conjure/sessions/<session>.url`` for ``--attach``.
"""

from __future__ import annotations

import os
import signal
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Any

from conjure import _ui
from conjure.daemon import (
    daemonize,
    list_session_names,
    log_path_for,
    pid_path_for,
    session_dir,
)
from conjure.web.server import WebServer
from conjure.web.state import ReplayCache, SessionView

DEFAULT_PORT = 8765


def url_path_for(session: str) -> Path:
    return session_dir() / f"{session}.url"


def _write_url(session: str, url: str) -> None:
    path = url_path_for(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(url + "\n")


def _remove_url(session: str) -> None:
    try:
        url_path_for(session).unlink()
    except OSError:
        pass


def start_live_web(session: Any, *, port: int = DEFAULT_PORT, host: str = "127.0.0.1") -> WebServer:
    """Serve ``session`` (a ``conjure.session.Session``) in a background
    thread and record its URL."""
    view = SessionView.live_session(session.runtime, session.control, name=session.name)
    web = WebServer(lambda _at: view, host=host, port=port, live=True).start()
    _write_url(session.name, web.url)
    return web


def stop_live_web(web: WebServer, session_name: str) -> None:
    try:
        web.stop()
    except Exception:
        pass
    _remove_url(session_name)


# ----- conjure web <config> -----


def cmd_web(
    config_path: Path,
    *,
    port: int = DEFAULT_PORT,
    host: str = "127.0.0.1",
    open_browser: bool = True,
    detach: bool = False,
) -> int:
    from conjure.config import load_config
    from conjure.env import load_env_files

    console = _ui.make_console()
    load_env_files()
    try:
        cfg = load_config(config_path)
    except Exception as exc:
        _ui.print_error(console, f"config invalid: {exc}")
        return 2

    name = f"conjure-{uuid.uuid4().hex[:6]}"
    if detach:
        pid_path = pid_path_for(name)
        child = daemonize(log_path=log_path_for(name), pid_path=pid_path)
        if child > 0:
            url = _wait_for_url(name, timeout_s=30.0)
            if url is None:
                _ui.print_error(
                    console,
                    f"session did not come up within 30s (see {log_path_for(name)})",
                )
                return 1
            _announce(console, name, url, detached=True)
            if open_browser:
                webbrowser.open(url)
            return 0
        return _serve_live(cfg, name, port=port, host=host, pid_path=pid_path)
    return _serve_live(
        cfg, name, port=port, host=host, console=console, open_browser=open_browser
    )


def _serve_live(
    cfg: Any,
    name: str,
    *,
    port: int,
    host: str,
    pid_path: Path | None = None,
    console: Any = None,
    open_browser: bool = False,
) -> int:
    from conjure.session import start_session

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        session = start_session(cfg, name)
    except Exception as exc:
        msg = f"failed to build runtime: {exc}"
        if console is not None:
            _ui.print_error(console, msg)
        else:
            print(msg, flush=True)
        _cleanup_pid(pid_path)
        return 2
    try:
        web = start_live_web(session, port=port, host=host)
    except Exception as exc:
        print(f"web UI failed to start: {exc}", flush=True)
        session.close()
        _cleanup_pid(pid_path)
        return 2
    if console is not None:
        _announce(console, name, web.url, detached=False)
        if open_browser:
            webbrowser.open(web.url)
    if cfg.mode == "one-shot" and cfg.initial_task:
        session.runtime.send_external(to=session.root, body=cfg.initial_task)
    stop.wait()
    stop_live_web(web, name)
    session.close()
    _cleanup_pid(pid_path)
    return 0


def _wait_for_url(name: str, *, timeout_s: float) -> str | None:
    deadline = time.monotonic() + timeout_s
    path = url_path_for(name)
    pid_seen = False
    while time.monotonic() < deadline:
        if path.exists():
            url = path.read_text(encoding="utf-8").strip()
            if url:
                return url
        # The forked child writes its PID file a moment after the fork;
        # only a PID file that appeared and then vanished means the
        # daemon died during startup.
        if pid_path_for(name).exists():
            pid_seen = True
        elif pid_seen:
            return None
        time.sleep(0.1)
    return None


def _announce(console: Any, name: str, url: str, *, detached: bool) -> None:
    _ui.print_system(console, f"session [cyan]{name}[/] up")
    console.print(f"  [bold]{url}[/]")
    if detached:
        console.print(
            "  [dim]`conjure web --attach` reopens it · `conjure quit` stops it[/]"
        )
    else:
        console.print("  [dim]Ctrl+C stops the session[/]")


def _cleanup_pid(pid_path: Path | None) -> None:
    if pid_path is None:
        return
    try:
        pid_path.unlink()
    except OSError:
        pass


# ----- conjure web --replay -----


def resolve_session_dir(target: str, store_dir: Path) -> Path | None:
    """``target`` is a session directory, or a session name under
    ``<store_dir>/sessions/``."""
    direct = Path(target).expanduser()
    if (direct / "journal.jsonl").exists():
        return direct
    named = store_dir / "sessions" / target
    if (named / "journal.jsonl").exists():
        return named
    return None


def cmd_web_replay(
    target: str,
    *,
    store_dir: Path,
    port: int = DEFAULT_PORT,
    host: str = "127.0.0.1",
    open_browser: bool = True,
) -> int:
    console = _ui.make_console()
    sdir = resolve_session_dir(target, store_dir)
    if sdir is None:
        sessions = sorted(
            p.name for p in (store_dir / "sessions").glob("*") if (p / "journal.jsonl").exists()
        ) if (store_dir / "sessions").is_dir() else []
        hint = f" (sessions in {store_dir}: {', '.join(sessions[-5:])})" if sessions else ""
        _ui.print_error(console, f"no journaled session at {target!r}{hint}")
        return 1
    cache = ReplayCache(sdir)
    cache.at(None)  # fail fast on a broken journal
    web = WebServer(cache.at, host=host, port=port, live=False)
    _ui.print_system(console, f"replaying [cyan]{sdir.name}[/] (read-only)")
    console.print(f"  [bold]{web.url}[/]")
    console.print("  [dim]Ctrl+C to stop[/]")
    if open_browser:
        webbrowser.open(web.url)
    try:
        web.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        web.stop()
    return 0


# ----- conjure web --attach -----


def cmd_web_attach(session: str | None, *, open_browser: bool = True) -> int:
    console = _ui.make_console()
    if session is None:
        live = [n for n in list_session_names() if url_path_for(n).exists()]
        if not live:
            _ui.print_error(console, "no running session with a web UI")
            return 1
        live.sort(key=lambda n: pid_path_for(n).stat().st_mtime, reverse=True)
        session = live[0]
    path = url_path_for(session)
    if not path.exists():
        _ui.print_error(console, f"session {session} has no web UI running")
        return 1
    url = path.read_text(encoding="utf-8").strip()
    console.print(url)
    if open_browser:
        webbrowser.open(url)
    return 0
