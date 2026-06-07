"""Thin wrapper over libtmux.

The CLI's tmux orchestrator (``conjure run``) uses this to manage
its tmux session lifecycle. The class hides libtmux's deprecation
churn behind a small, intentful API focused on what conjure
actually needs:

- create a named session (or look one up by name)
- open windows that run specific shell commands
- list and kill windows
- attach the current process to the session (``os.execvp``)
- kill the session at shutdown

tmux operations go through subprocess invocations; cross-process
concurrency is handled by the tmux server. Operations from this
process are not thread-safe at the libtmux layer — call sites in the
conjure CLI serialize their usage.
"""

from __future__ import annotations

import os
import shutil

import libtmux


class TmuxSessionError(Exception):
    """tmux operation failed in a way the framework can't recover from."""


def tmux_available() -> bool:
    """Return True if the ``tmux`` binary is on PATH."""
    return shutil.which("tmux") is not None


class TmuxSession:
    """A conjure tmux session — one per running conjure process."""

    def __init__(self, name: str) -> None:
        if not tmux_available():
            raise TmuxSessionError("tmux binary not found on PATH")
        self.name = name
        self._server = libtmux.Server()
        self._session = None  # libtmux.Session — set by attach_or_create

    @classmethod
    def attach_or_create(
        cls,
        name: str,
        *,
        initial_window_name: str | None = None,
        initial_command: str | None = None,
    ) -> "TmuxSession":
        """Return a wrapper bound to an existing session of that name,
        or freshly created if absent. The returned wrapper does not
        attach the calling process — use ``attach()`` for that.

        When the session is created (not attached to), ``initial_window_name``
        and ``initial_command`` configure window 0 — useful for putting
        the input prompt (or any anchor) in the first window without
        a follow-on rename.
        """
        s = cls(name)
        if s._server.has_session(name):
            s._session = s._server.sessions.get(session_name=name)
            return s
        kwargs: dict[str, str] = {}
        if initial_window_name:
            kwargs["window_name"] = initial_window_name
        if initial_command:
            kwargs["window_command"] = initial_command
        s._session = s._server.new_session(
            session_name=name,
            attach=False,
            kill_session=False,
            **kwargs,
        )
        return s

    def has_session(self) -> bool:
        return self._server.has_session(self.name)

    def new_window(self, *, name: str, command: str) -> str:
        """Open a new window named ``name`` whose initial shell runs
        ``command``. Returns the window id (e.g. ``"@7"``)."""
        if self._session is None:
            raise TmuxSessionError("session not initialized")
        window = self._session.new_window(
            window_name=name,
            attach=False,
            window_shell=command,
        )
        return window.window_id or ""

    def kill_window(self, name: str) -> None:
        """Kill the first window with the given name. Idempotent."""
        if self._session is None:
            return
        for w in self._session.windows:
            if w.window_name == name:
                w.kill()
                return

    def list_windows(self) -> list[dict[str, str]]:
        """Return a list of ``{"id": ..., "name": ...}`` for inspection."""
        if self._session is None:
            return []
        return [
            {"id": w.window_id or "", "name": w.window_name or ""}
            for w in self._session.windows
        ]

    def rename_window(self, *, current_name: str, new_name: str) -> None:
        """Rename a window. Idempotent if not found."""
        if self._session is None:
            return
        for w in self._session.windows:
            if w.window_name == current_name:
                w.rename_window(new_name)
                return

    def attach(self) -> int:
        """Replace the current process with ``tmux attach-session``.

        Does not return on success. Returns the exit code if exec
        somehow fails (it normally doesn't on Unix).
        """
        return os.execvp("tmux", ["tmux", "attach-session", "-t", self.name])

    def kill(self) -> None:
        """Kill the entire session. Idempotent."""
        if self._session is None:
            return
        try:
            self._session.kill()
        except Exception:
            # Best effort — tmux may have already torn the session down,
            # or libtmux may be confused. Either way, we proceed.
            pass
        self._session = None
