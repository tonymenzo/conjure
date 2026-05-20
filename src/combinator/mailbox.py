"""Mailbox — threadsafe per-agent inbox.

Append-only. FIFO. Per-inbox monotonic sequence numbers; no global order.

Readers track their own cursor (``since_seq``); the mailbox does not own
the notion of "read" vs "unread". This keeps semantics simple and lets
multiple readers (e.g. the agent itself plus a human-facing viewer)
observe independently.

`put` is atomic with seq assignment: callers pass an envelope with a
placeholder ``seq`` and receive back a copy with the real value, so the
log is guaranteed monotonic regardless of writer concurrency.

`read` is the single read primitive. ``timeout_s == 0`` is non-blocking;
``timeout_s > 0`` blocks until at least one match arrives (or the
deadline elapses). Filtering is on ``thread_id`` and ``from_id`` (a
sender's address id string); both default to empty (no filter).
"""

from __future__ import annotations

import threading
import time

from combinator.address import Address
from combinator.envelope import Envelope


class Mailbox:

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._items: list[Envelope] = []
        self._next_seq = 1

    def put(self, env: Envelope) -> Envelope:
        """Append ``env`` to the mailbox, assigning a fresh seq.

        Returns the stored envelope, which differs from the input by its
        ``seq`` field. The original is unmodified (Envelope is frozen).
        """
        with self._cond:
            seq = self._next_seq
            self._next_seq += 1
            stored = env.model_copy(update={"seq": seq})
            self._items.append(stored)
            self._cond.notify_all()
            return stored

    def replay_put(self, env: Envelope) -> None:
        """Insert ``env`` preserving its original ``seq``. Intended for
        journal replay only; advances the next-seq counter past
        ``env.seq`` so future puts remain monotonic."""
        with self._cond:
            self._items.append(env)
            if env.seq + 1 > self._next_seq:
                self._next_seq = env.seq + 1
            self._cond.notify_all()

    def read(
        self,
        *,
        since_seq: int = 0,
        max_n: int = 1,
        thread_id: str = "",
        from_id: str = "",
        timeout_s: float = 0.0,
    ) -> list[Envelope]:
        """Return up to ``max_n`` envelopes with ``seq > since_seq`` that
        match the (optional) filters, in FIFO order.

        ``timeout_s == 0`` returns immediately (possibly empty).
        ``timeout_s > 0`` blocks until at least one match arrives or the
        deadline elapses, whichever comes first.
        """
        if max_n <= 0:
            return []
        deadline = (time.monotonic() + timeout_s) if timeout_s > 0 else None
        with self._cond:
            while True:
                matches = self._scan_locked(
                    since_seq=since_seq,
                    max_n=max_n,
                    thread_id=thread_id,
                    from_id=from_id,
                )
                if matches or deadline is None:
                    return matches
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return matches
                self._cond.wait(timeout=remaining)

    def latest_seq(self) -> int:
        """Highest seq currently stored (0 if empty). Useful for
        initializing a reader's cursor."""
        with self._cond:
            return self._next_seq - 1

    def __len__(self) -> int:
        with self._cond:
            return len(self._items)

    def _scan_locked(
        self,
        *,
        since_seq: int,
        max_n: int,
        thread_id: str,
        from_id: str,
    ) -> list[Envelope]:
        out: list[Envelope] = []
        for env in self._items:
            if env.seq <= since_seq:
                continue
            if thread_id and env.thread_id != thread_id:
                continue
            if from_id and _sender_id(env.from_) != from_id:
                continue
            out.append(env)
            if len(out) >= max_n:
                break
        return out


def _sender_id(sender: Address) -> str:
    return sender.id
