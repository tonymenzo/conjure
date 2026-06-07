"""Tests for spawn.daemon — pure file/PID logic.

``daemonize`` itself forks; we don't exercise the fork path in tests
(it would orphan a process). Instead we test the helpers that operate
on the PID file: ``is_daemon_running`` and ``stop_daemon``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from conjure.daemon import is_daemon_running, stop_daemon


def test_is_daemon_running_missing_file(tmp_path: Path):
    assert is_daemon_running(tmp_path / "missing.pid") is False


def test_is_daemon_running_garbage_file(tmp_path: Path):
    p = tmp_path / "bad.pid"
    p.write_text("not a pid", encoding="utf-8")
    assert is_daemon_running(p) is False


def test_is_daemon_running_dead_pid(tmp_path: Path):
    p = tmp_path / "dead.pid"
    # Find a PID that's almost certainly not in use.
    p.write_text("99999999", encoding="utf-8")
    assert is_daemon_running(p) is False


def test_is_daemon_running_live_pid(tmp_path: Path):
    # Spawn a short-lived process so we have a known-live PID.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        p = tmp_path / "live.pid"
        p.write_text(str(proc.pid), encoding="utf-8")
        assert is_daemon_running(p) is True
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_stop_daemon_missing_file(tmp_path: Path):
    # Nothing to stop — succeeds trivially.
    assert stop_daemon(tmp_path / "missing.pid") is True


def test_stop_daemon_kills_live_process(tmp_path: Path):
    import threading

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    # In production the daemon's parent is init, which reaps the daemon
    # immediately on death — so ``os.kill(pid, 0)`` returns ESRCH the
    # moment the daemon exits. In a test under pytest, the subprocess
    # would become a zombie because pytest doesn't auto-reap; we run a
    # reaper thread to mimic init's behavior.
    threading.Thread(target=proc.wait, daemon=True).start()

    p = tmp_path / "live.pid"
    p.write_text(str(proc.pid), encoding="utf-8")
    try:
        assert stop_daemon(p, timeout_s=5.0) is True
        assert not p.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
