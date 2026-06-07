"""Daemonize the runtime process.

``conjure run`` calls ``daemonize`` to detach the runtime from the
controlling terminal. The parent ``conjure run`` invocation returns
immediately; the detached child becomes the runtime process, owns the
tmux session, and keeps running across detach/reattach cycles.

Lifecycle:

- The child writes its PID to ``<store_dir>/<session_name>.pid``.
- ``conjure quit [session]`` reads that PID and sends SIGTERM.
- On SIGTERM the daemon shuts the runtime down cleanly and removes
  the PID file.
- If the daemon dies for any other reason, the PID file is stale; the
  ``conjure run --attach`` and ``conjure quit`` commands handle
  stale files defensively.

This module is deliberately POSIX-only — conjure is a Unix tool.
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path


def session_dir() -> Path:
    """Where conjure stores per-session daemon state (PID + log).

    Decoupled from any project's ``store_dir`` so ``conjure quit``
    can find a daemon without needing the original config.
    """
    return Path.home() / ".conjure" / "sessions"


def pid_path_for(session: str) -> Path:
    return session_dir() / f"{session}.pid"


def log_path_for(session: str) -> Path:
    return session_dir() / f"{session}.log"


def socket_path_for(session: str) -> Path:
    return session_dir() / f"{session}.sock"


def list_session_names() -> list[str]:
    """Return ``conjure-*`` session names with live PID files."""
    d = session_dir()
    if not d.exists():
        return []
    out: list[str] = []
    for p in d.glob("*.pid"):
        name = p.stem
        if not name.startswith("conjure-"):
            continue
        if is_daemon_running(p):
            out.append(name)
    return sorted(out)


def daemonize(*, log_path: Path, pid_path: Path) -> int:
    """Fork once, detach the child from the controlling terminal,
    redirect its stdio to ``log_path``, and write its PID to
    ``pid_path``.

    In the *parent* process, returns the daemon child's PID (parent
    should print user-facing info and exit cleanly afterward).

    In the *daemon child*, returns ``0`` after detaching and
    redirecting stdio. The caller continues with runtime setup.
    """
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    pid = os.fork()
    if pid > 0:
        # Parent — return the child's PID.
        return pid

    # Child — detach.
    os.setsid()
    os.umask(0o022)

    sys.stdout.flush()
    sys.stderr.flush()

    with open(os.devnull, "rb") as null:
        os.dup2(null.fileno(), 0)
    log = open(log_path, "ab", buffering=0)
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)

    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    return 0


def is_daemon_running(pid_path: Path) -> bool:
    """Check whether the daemon recorded in ``pid_path`` is still alive."""
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    try:
        # Signal 0 doesn't deliver anything but raises if the process
        # doesn't exist.
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop_daemon(pid_path: Path, *, timeout_s: float = 10.0) -> bool:
    """Send SIGTERM to the daemon recorded in ``pid_path`` and wait for
    it to exit. Returns True if the daemon stopped (or was already
    gone), False if it ignored the signal."""
    import time

    if not pid_path.exists():
        return True
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return True

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        # Already dead.
        _remove(pid_path)
        return True

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            _remove(pid_path)
            return True
        time.sleep(0.1)
    return False


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
