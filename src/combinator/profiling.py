"""Opt-in cProfile instrumentation.

Set ``COMBINATOR_PROFILE`` to enable. When the value is a directory the
profile is written to ``<dir>/<name>-<pid>.pstats``; when it's a file
path the profile is written directly there; when it's any other truthy
value the profile is written to ``./combinator-<name>-<pid>.pstats``.

Usage:

    from combinator.profiling import profile_session

    with profile_session("daemon"):
        run_the_runtime()

Inspect with ``python -m pstats <file>``::

    sort cumulative
    stats 30

A ``COMBINATOR_PROFILE_PRINT=1`` env var also dumps the top-30 cumulative
rows to stderr at exit, which is convenient for quick benchmark runs.
"""

from __future__ import annotations

import contextlib
import cProfile
import io
import os
import pstats
import sys
from pathlib import Path
from typing import Iterator


def _resolve_target(name: str) -> Path | None:
    """Return the output path for a profile run, or ``None`` if
    profiling is disabled."""
    raw = os.environ.get("COMBINATOR_PROFILE", "").strip()
    if not raw:
        return None
    path = Path(raw)
    pid = os.getpid()
    if path.is_dir() or raw.endswith(("/", os.sep)):
        path.mkdir(parents=True, exist_ok=True)
        return path / f"{name}-{pid}.pstats"
    # Bare truthy value (e.g. "1") — drop next to the cwd.
    if raw in ("1", "true", "yes", "on"):
        return Path.cwd() / f"combinator-{name}-{pid}.pstats"
    # Explicit file path.
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextlib.contextmanager
def profile_session(name: str = "session") -> Iterator[cProfile.Profile | None]:
    """Profile the wrapped block when ``COMBINATOR_PROFILE`` is set.

    The yielded value is the active ``cProfile.Profile`` instance (or
    ``None`` when profiling is disabled) so callers can pause/resume
    with ``prof.disable()`` / ``prof.enable()`` around uninteresting
    setup if they want.
    """
    target = _resolve_target(name)
    if target is None:
        yield None
        return
    prof = cProfile.Profile()
    prof.enable()
    try:
        yield prof
    finally:
        prof.disable()
        try:
            prof.dump_stats(str(target))
        except OSError:
            pass
        if os.environ.get("COMBINATOR_PROFILE_PRINT"):
            _print_top(prof, name)


def _print_top(prof: cProfile.Profile, name: str, *, rows: int = 30) -> None:
    buf = io.StringIO()
    stats = pstats.Stats(prof, stream=buf).sort_stats("cumulative")
    stats.print_stats(rows)
    sys.stderr.write(f"\n[combinator-profile {name}] top {rows} by cumulative time:\n")
    sys.stderr.write(buf.getvalue())
    sys.stderr.flush()
