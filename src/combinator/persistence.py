"""Append-only JSONL journal for the runtime.

One line per event; entries are dicts of shape
``{"kind": str, "ts": float, "payload": dict}``. Payloads are
JSON-serializable — pydantic models are dumped via ``model_dump(by_alias=True)``;
sets are dumped as sorted lists.

v0.1 flushes after every write but does not fsync per entry. A clean
shutdown (``Journal.close()``) issues a final flush.

When the runtime is constructed without a ``store_dir``, the journal is
a no-op — useful for tests that don't care about persistence.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel

JOURNAL_FILENAME = "journal.jsonl"


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
        if store_dir is not None:
            store_dir.mkdir(parents=True, exist_ok=True)
            self._path = store_dir / JOURNAL_FILENAME
            self._file = self._path.open("a", encoding="utf-8")

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
        self._file.write(line)
        self._file.write("\n")
        self._file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.flush()
            self._file.close()
            self._file = None

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
