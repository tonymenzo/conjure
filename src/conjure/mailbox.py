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

import bisect
import threading
import time
from collections.abc import Callable

from conjure.address import Address
from conjure.envelope import Envelope
from conjure.errors import IdempotencyConflict


class Mailbox:

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._items: list[Envelope] = []
        # Parallel list of envelope seqs, used by ``_scan_locked`` for
        # an O(log N) bisect to the first unread envelope. Stays in
        # lockstep with ``_items``; never touched outside the lock.
        self._seqs: list[int] = []
        self._next_seq = 1
        self._idempotency: dict[tuple[str, str], Envelope] = {}

    def put(
        self, env: Envelope, *, before_publish: Callable[[Envelope], None] | None = None
    ) -> Envelope:
        """Append ``env`` to the mailbox, assigning a fresh seq.

        Returns the stored envelope, which differs from the input by its
        ``seq`` field. The original is unmodified (Envelope is frozen).
        An explicit ``headers["idempotency_key"]`` retries the original send
        from this sender; conflicting payloads raise ``IdempotencyConflict``.
        ``before_publish`` runs under the mailbox lock before readers can see
        the message. It must not acquire the runtime registry lock.
        """
        with self._cond:
            key = env.headers.get("idempotency_key")
            identity = (env.from_.id, key) if key else None
            previous = self._idempotency.get(identity) if identity else None
            if previous is not None:
                def payload(e: Envelope) -> tuple:
                    thread = None if e.thread_id == e.msg_id else e.thread_id
                    return (e.body, thread, e.in_reply_to, e.headers)
                if payload(previous) != payload(env):
                    raise IdempotencyConflict("idempotency key reused with a different message")
                return previous
            seq = self._next_seq
            stored = env.model_copy(update={"seq": seq})
            # Serialize persistence with sequence assignment. The callback must
            # not acquire the runtime registry lock (registry -> inbox -> journal).
            # Publish only after it succeeds; readers cannot observe half a send.
            if before_publish is not None:
                before_publish(stored)
            self._next_seq += 1
            if identity:
                self._idempotency[identity] = stored
            self._items.append(stored)
            self._seqs.append(seq)
            self._cond.notify_all()
            return stored

    def replay_put(self, env: Envelope) -> None:
        """Insert ``env`` preserving its original ``seq``. Intended for
        journal replay only; advances the next-seq counter past
        ``env.seq`` so future puts remain monotonic."""
        with self._cond:
            # Older journals may contain concurrent sends out of seq order.
            index = bisect.bisect_left(self._seqs, env.seq)
            if index < len(self._seqs) and self._seqs[index] == env.seq:
                raise ValueError(f"duplicate mailbox sequence: {env.seq}")
            self._items.insert(index, env)
            self._seqs.insert(index, env.seq)
            key = env.headers.get("idempotency_key")
            if key:
                self._idempotency[(env.from_.id, key)] = env
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

    def read_recent(
        self,
        *,
        max_n: int,
        thread_id: str = "",
        from_id: str = "",
    ) -> list[Envelope]:
        """Return the most recent matching envelopes, oldest first.

        ``read`` is cursor-oriented and intentionally starts at the
        beginning when ``since_seq=0``. UI/status paths and duplicate
        checks usually want the tail instead; scanning backward avoids
        walking a long inbox only to discard its older entries.
        """
        if max_n <= 0:
            return []
        with self._cond:
            out: list[Envelope] = []
            for env in reversed(self._items):
                if thread_id and env.thread_id != thread_id:
                    continue
                if from_id and _sender_id(env.from_) != from_id:
                    continue
                out.append(env)
                if len(out) >= max_n:
                    break
            out.reverse()
            return out

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
        # ``_items`` is sorted by seq (``put`` assigns under the lock;
        # ``replay_put`` inserts historical envelopes in sequence order),
        # so the first envelope with ``seq > since_seq`` can be found
        # in O(log N). Without the bisect, every read of a long-lived
        # inbox would re-scan the entire history — a quiet O(N) tax
        # that becomes the dominant cost under high-fan-out HOFs.
        items = self._items
        start = bisect.bisect_right(self._seqs, since_seq) if since_seq > 0 else 0
        out: list[Envelope] = []
        for index in range(start, len(items)):
            env = items[index]
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
