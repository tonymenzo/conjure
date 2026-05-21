"""Append-only JSONL journal for the runtime.

One line per event; entries are dicts of shape
``{"kind": str, "ts": float, "payload": dict}``. Payloads are
JSON-serializable — pydantic models are dumped via ``model_dump(by_alias=True)``;
sets are dumped as sorted lists.

Buffered writes: a background flusher thread pushes the Python file
buffer to the OS every ``FLUSH_INTERVAL_S`` (100ms) and on shutdown.
Writes themselves are append-only into the user-space buffer, which
makes the spawn / send hot path cheap — under heavy fan-out the old
per-write ``flush()`` was a measurable serial bottleneck. The bounded
loss window is ~100ms of journal events on a hard process crash; a
clean ``close()`` flushes synchronously.

When the runtime is constructed without a ``store_dir``, the journal is
a no-op — useful for tests that don't care about persistence.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel

JOURNAL_FILENAME = "journal.jsonl"

# How long the background flusher waits between buffer pushes. Bounds
# the on-crash data-loss window; 100ms is short enough that
# user-visible state is never significantly out of sync, and long
# enough that the flush cost is amortized across many writes.
FLUSH_INTERVAL_S: float = 0.1


def _json_default(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(by_alias=True)
    if isinstance(obj, set):
        return sorted(obj, key=str)
    raise TypeError(f"object not JSON-serializable: {type(obj).__name__}")


class Journal:

    def __init__(self, store_dir: Path | None) -> None:
        self.store_dir = store_dir
        self._path: Path | None = None
        self._file = None
        # Serializes append + close vs the background flusher. The
        # runtime's own RLock already serializes top-level writes, but
        # ``close()`` may fire from a different thread than ``write``,
        # and the flusher is its own thread.
        self._lock = threading.Lock()
        self._flusher_stop = threading.Event()
        self._flusher: threading.Thread | None = None
        if store_dir is not None:
            store_dir.mkdir(parents=True, exist_ok=True)
            self._path = store_dir / JOURNAL_FILENAME
            self._file = self._path.open("a", encoding="utf-8")
            self._start_flusher()

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def is_active(self) -> bool:
        return self._file is not None

    def write(self, kind: str, payload: dict[str, Any]) -> None:
        if self._file is None:
            return
        entry = {"kind": kind, "ts": time.time(), "payload": payload}
        line = json.dumps(entry, default=_json_default)
        with self._lock:
            if self._file is None:  # closed concurrently
                return
            self._file.write(line)
            self._file.write("\n")

    def flush(self) -> None:
        """Push the Python buffer to the OS. Called by the background
        flusher and ``close()``; callers may also invoke it directly
        before an inspection point that needs the journal on disk."""
        with self._lock:
            if self._file is not None:
                try:
                    self._file.flush()
                except (OSError, ValueError):
                    pass

    def close(self) -> None:
        self._flusher_stop.set()
        flusher = self._flusher
        if flusher is not None:
            flusher.join(timeout=1.0)
        with self._lock:
            if self._file is not None:
                try:
                    self._file.flush()
                except (OSError, ValueError):
                    pass
                self._file.close()
                self._file = None

    def _start_flusher(self) -> None:
        def _run() -> None:
            while not self._flusher_stop.wait(FLUSH_INTERVAL_S):
                self.flush()

        thread = threading.Thread(
            target=_run, daemon=True, name="journal-flusher"
        )
        self._flusher = thread
        thread.start()

    @staticmethod
    def read_all(store_dir: Path) -> Iterator[dict[str, Any]]:
        """Yield every entry in the journal at ``store_dir``, in order.

        Returns an empty iterator if no journal exists at that location.
        """
        path = store_dir / JOURNAL_FILENAME
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line)
