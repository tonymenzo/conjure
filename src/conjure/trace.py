"""Provenance trace: Conjure's side of toolbase's call trace.

With ``toolbase.trace: true`` (or ``TOOLBASE_TRACE=1``), a session writes
one trace directory, by default ``<toolbase project>/.toolbase/traces/
<session-id>/``. toolbase's orchestrators write ``calls.jsonl`` there, one
record per toolkit call, stamped with the calling agent. Conjure registers
the agents (``agents.jsonl``: id, label, parent, model, engine, harness
``conjure``) and appends its own ``messages.jsonl`` (spawns with their
briefs, sends, terminations). See ``docs/TRACE.md`` in toolbase.

Off by default; with it off nothing here is constructed. Needs a toolbase
with ``toolbase.trace``: an older one leaves tracing unavailable, with a
warning, and the session runs untraced.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any, Mapping

HARNESS = "conjure"
MESSAGES_FILE = "messages.jsonl"

_ON = {"1", "true", "yes", "on"}
_OFF = {"0", "false", "no", "off", ""}


class TraceUnavailable(RuntimeError):
    """Tracing was asked for but this toolbase cannot trace."""


def env_switch(environ: Mapping[str, str]) -> bool | None:
    """``TOOLBASE_TRACE``, read as toolbase reads it: 1/true/yes/on or
    0/false/no/off (empty is off); unset or anything else is no opinion."""
    raw = environ.get("TOOLBASE_TRACE")
    if raw is None:
        return None
    v = raw.strip().lower()
    if v in _ON:
        return True
    if v in _OFF:
        return False
    return None


def trace_wanted(configured: bool, environ: Mapping[str, str] | None = None) -> bool:
    """Whether this session traces. toolbase's order for ``serve`` is
    ``--trace``, then ``TOOLBASE_TRACE``, then the project's setting;
    Conjure's ``toolbase.trace`` is the project-level setting, so
    ``TOOLBASE_TRACE`` (either way) overrides it."""
    env = os.environ if environ is None else environ
    switch = env_switch(env)
    return bool(configured) if switch is None else switch


def resolve_trace_dir(
    *,
    project_root: Path | None,
    session_id: str | None,
    configured_dir: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """The session's trace directory: ``TOOLBASE_TRACE_DIR``, else
    ``toolbase.trace_dir`` (relative to the toolbase project), else
    ``<project>/.toolbase/traces/<session-id>/``."""
    env = os.environ if environ is None else environ
    if env.get("TOOLBASE_TRACE_DIR"):
        return Path(env["TOOLBASE_TRACE_DIR"]).expanduser().absolute()
    root = _project_root(project_root)
    if configured_dir:
        p = Path(configured_dir).expanduser()
        if not p.is_absolute():
            p = (root or Path.cwd()) / p
        return p.absolute()
    from toolbase.trace.session import new_session_id, traces_dir

    if root is None:
        raise TraceUnavailable("no toolbase project to hold the trace; set toolbase.trace_dir")
    return (traces_dir(root) / (session_id or new_session_id())).absolute()


def _project_root(project_root: Path | None) -> Path | None:
    if project_root is not None:
        return Path(project_root).expanduser().absolute()
    try:
        from toolbase.envs import default_project_root

        return Path(default_project_root()).absolute()
    except Exception:
        return None


def trace_api() -> Any:
    """``toolbase.trace``, or ``TraceUnavailable`` saying why not."""
    try:
        import toolbase.trace as api

        api.Trace  # noqa: B018 - a module of that name without the API is too old
    except Exception as exc:
        raise TraceUnavailable(
            "toolbase.trace is requested but the installed toolbase has no provenance "
            f"trace ({type(exc).__name__}: {exc}); upgrade toolbase or turn "
            "toolbase.trace off. This session runs untraced."
        ) from exc
    return api


class SessionTrace:
    """One session's trace directory and each agent's ``Trace`` handle.

    Registration and ending are idempotent per agent id, so a re-served
    mount, a cascade and a shutdown never write an agent twice. Every
    write is best-effort: a trace failure is reported once on stderr and
    never reaches the run."""

    def __init__(self, dir: Path, *, api: Any | None = None) -> None:
        self._api = api or trace_api()
        self.dir = Path(dir).expanduser().absolute()
        self._root = self._api.Trace(self.dir, agent="conjure")
        self._handles: dict[str, Any] = {}
        self._started: set[str] = set()
        self._ended: set[str] = set()
        self._lock = threading.Lock()
        self._warned = False

    def handle(self, agent_id: str) -> Any:
        """The ``Trace`` handle for ``agent_id``, the same object each time
        (so every orchestrator built for that agent, across re-serves,
        stamps its calls with that agent)."""
        with self._lock:
            h = self._handles.get(agent_id)
            if h is None:
                h = self._handles[agent_id] = self._root.for_agent(agent_id)
            return h

    def agent_started(
        self,
        agent_id: str,
        *,
        label: str | None = None,
        parent: str | None = None,
        model: str | None = None,
        engine: str | None = None,
    ) -> None:
        with self._lock:
            if agent_id in self._started:
                return
            self._started.add(agent_id)
        self._safe(lambda: self.handle(agent_id).register_agent(
            label=label or None, parent=parent, model=model, engine=engine, harness=HARNESS,
        ))

    def agent_ended(self, agent_id: str) -> None:
        with self._lock:
            if agent_id not in self._started or agent_id in self._ended:
                return
            self._ended.add(agent_id)
        self._safe(lambda: self.handle(agent_id).end_agent())

    def end_all(self) -> None:
        with self._lock:
            live = sorted(self._started - self._ended)
        for agent_id in live:
            self.agent_ended(agent_id)

    @property
    def agents(self) -> set[str]:
        with self._lock:
            return set(self._started)

    def message(self, event: str, *, from_: str | None, to: str | None, **fields: Any) -> None:
        """Append ``{"event", "ts", "from", "to", ...}`` to ``messages.jsonl``."""
        def write() -> None:
            record = {"event": event, "ts": self._api.now_iso(), "from": from_, "to": to}
            record.update(fields)
            self._api.append_jsonl(self.dir / MESSAGES_FILE, record)

        self._safe(write)

    def _safe(self, fn: Any) -> None:
        try:
            fn()
        except Exception as exc:
            if not self._warned:
                self._warned = True
                print(f"conjure: trace write failed in {self.dir}: {exc}", file=sys.stderr)

