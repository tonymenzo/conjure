"""Per-agent append-only JSONL event log.

Each spawned agent gets its own ``EventLog``. The runtime's display
hook writes one event per orchestral context update. The renderer
process for that agent's tmux window tails the file and renders.

Two concerns the writer must handle:

- **Concurrency** — a single agent's events are emitted from one
  driver thread, but multiple ``EventLog`` instances may write to
  different files from different threads simultaneously. The
  per-instance lock keeps any one log self-consistent.
- **Atomicity** — partial line writes (writer killed mid-line) would
  poison the reader. We assemble the full JSON line + ``\n`` first
  and use a single ``write`` so the OS gives us atomicity for any
  payload up to PIPE_BUF on POSIX. ``flush`` ensures the reader sees
  it promptly.

Reader (``tail``) is a generator: it yields parsed dicts as they
appear, blocking on a poll interval when caught up.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Iterator


class EventLog:

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a", encoding="utf-8")
        self._lock = threading.Lock()
        self._closed = False

    def emit(self, event: dict[str, Any]) -> None:
        """Append one event as a single JSONL line. No-op after close.

        Injects ``ts`` (wall-clock seconds) when the caller didn't
        supply one. This is what lets the activity feed merge tool
        events (sourced from these logs) with envelope events
        (sourced from inbox seqs) on a single time-sorted timeline."""
        if "ts" not in event:
            event = {**event, "ts": time.time()}
        line = json.dumps(event, default=str, ensure_ascii=False) + "\n"
        with self._lock:
            if self._closed:
                return
            self._file.write(line)
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._file.flush()
                self._file.close()
            except Exception:
                pass
            self._closed = True

    @property
    def is_closed(self) -> bool:
        return self._closed


def tail(
    path: Path,
    *,
    poll_interval: float = 0.05,
    stop_event: threading.Event | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield each newly-appended event from ``path`` as a parsed dict.

    Waits for the file to exist if it doesn't yet. Blocks on a short
    poll interval when caught up. Tolerant of partial-line writes
    (yields nothing until the trailing newline lands).

    Stops when ``stop_event`` is set (if provided).
    """

    def _stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    # Wait for file existence.
    while not path.exists():
        if _stopped():
            return
        time.sleep(poll_interval)

    with path.open("r", encoding="utf-8") as fh:
        buffer = ""
        while True:
            chunk = fh.readline()
            if not chunk:
                if _stopped():
                    return
                time.sleep(poll_interval)
                continue
            buffer += chunk
            if not buffer.endswith("\n"):
                # Partial line — wait for the rest.
                continue
            line = buffer.strip()
            buffer = ""
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                # Malformed line — skip and continue.
                continue
