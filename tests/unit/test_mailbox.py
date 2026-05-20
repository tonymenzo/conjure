"""Tests for combinator.mailbox.Mailbox."""

from __future__ import annotations

import threading
import time

from combinator.address import SYSTEM, USER, Address
from combinator.envelope import Envelope
from combinator.mailbox import Mailbox


def _env(*, msg_id: str, from_: Address, to: Address, body=None,
         thread_id: str | None = None, in_reply_to: str | None = None) -> Envelope:
    return Envelope(
        seq=0,                                   # overwritten by Mailbox.put
        msg_id=msg_id,
        from_=from_,
        to=to,
        thread_id=thread_id or msg_id,
        in_reply_to=in_reply_to,
        body=body,
        ts=0.0,
    )


def test_put_assigns_monotonic_seq():
    mb = Mailbox()
    a, b = Address(id="ag-a"), Address(id="ag-b")
    stored1 = mb.put(_env(msg_id="m1", from_=a, to=b))
    stored2 = mb.put(_env(msg_id="m2", from_=a, to=b))
    assert stored1.seq == 1
    assert stored2.seq == 2
    assert mb.latest_seq() == 2
    assert len(mb) == 2


def test_read_returns_fifo():
    mb = Mailbox()
    a, b = Address(id="ag-a"), Address(id="ag-b")
    mb.put(_env(msg_id="m1", from_=a, to=b, body=1))
    mb.put(_env(msg_id="m2", from_=a, to=b, body=2))
    mb.put(_env(msg_id="m3", from_=a, to=b, body=3))

    result = mb.read(max_n=10)
    assert [e.body for e in result] == [1, 2, 3]


def test_read_respects_since_seq():
    mb = Mailbox()
    a, b = Address(id="ag-a"), Address(id="ag-b")
    for i in range(5):
        mb.put(_env(msg_id=f"m{i}", from_=a, to=b, body=i))

    result = mb.read(since_seq=2, max_n=10)
    assert [e.seq for e in result] == [3, 4, 5]


def test_read_caps_at_max_n():
    mb = Mailbox()
    a, b = Address(id="ag-a"), Address(id="ag-b")
    for i in range(10):
        mb.put(_env(msg_id=f"m{i}", from_=a, to=b))

    result = mb.read(max_n=3)
    assert len(result) == 3
    assert [e.seq for e in result] == [1, 2, 3]


def test_read_filter_by_thread_id():
    mb = Mailbox()
    a, b = Address(id="ag-a"), Address(id="ag-b")
    mb.put(_env(msg_id="m1", from_=a, to=b, thread_id="t1"))
    mb.put(_env(msg_id="m2", from_=a, to=b, thread_id="t2"))
    mb.put(_env(msg_id="m3", from_=a, to=b, thread_id="t1"))

    result = mb.read(thread_id="t1", max_n=10)
    assert [e.msg_id for e in result] == ["m1", "m3"]


def test_read_filter_by_from_id():
    mb = Mailbox()
    a, b, c = Address(id="ag-a"), Address(id="ag-b"), Address(id="ag-c")
    mb.put(_env(msg_id="m1", from_=a, to=c))
    mb.put(_env(msg_id="m2", from_=b, to=c))
    mb.put(_env(msg_id="m3", from_=a, to=c))

    result = mb.read(from_id="ag-a", max_n=10)
    assert [e.msg_id for e in result] == ["m1", "m3"]


def test_read_filter_by_sentinel_sender():
    mb = Mailbox()
    a = Address(id="ag-a")
    mb.put(_env(msg_id="m1", from_=USER, to=a))
    mb.put(_env(msg_id="m2", from_=SYSTEM, to=a))
    mb.put(_env(msg_id="m3", from_=USER, to=a))

    result = mb.read(from_id="@user", max_n=10)
    assert [e.msg_id for e in result] == ["m1", "m3"]


def test_read_empty_when_no_match_and_no_timeout():
    mb = Mailbox()
    assert mb.read(max_n=10) == []


def test_read_max_n_zero_returns_empty():
    mb = Mailbox()
    a, b = Address(id="ag-a"), Address(id="ag-b")
    mb.put(_env(msg_id="m1", from_=a, to=b))
    assert mb.read(max_n=0) == []


def test_blocking_read_unblocks_on_put():
    mb = Mailbox()
    a, b = Address(id="ag-a"), Address(id="ag-b")
    result: list[Envelope] = []

    def reader():
        result.extend(mb.read(timeout_s=2.0))

    t = threading.Thread(target=reader)
    t.start()
    time.sleep(0.05)
    mb.put(_env(msg_id="m1", from_=a, to=b))
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert len(result) == 1
    assert result[0].msg_id == "m1"


def test_blocking_read_times_out_with_empty():
    mb = Mailbox()
    start = time.monotonic()
    result = mb.read(timeout_s=0.1)
    elapsed = time.monotonic() - start
    assert result == []
    assert 0.05 <= elapsed < 0.5


def test_concurrent_writers_preserve_monotonic_seq():
    mb = Mailbox()
    a, b = Address(id="ag-a"), Address(id="ag-b")
    n_writers = 8
    n_per = 50

    def writer(tag: str):
        for i in range(n_per):
            mb.put(_env(msg_id=f"{tag}-{i}", from_=a, to=b))

    threads = [threading.Thread(target=writer, args=(f"w{k}",)) for k in range(n_writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    stored = mb.read(max_n=10_000)
    assert len(stored) == n_writers * n_per
    seqs = [e.seq for e in stored]
    assert seqs == sorted(seqs), "seqs must be monotonically increasing"
    assert seqs == list(range(1, n_writers * n_per + 1))


def test_combined_filters():
    mb = Mailbox()
    a, b, c = Address(id="ag-a"), Address(id="ag-b"), Address(id="ag-c")
    mb.put(_env(msg_id="m1", from_=a, to=c, thread_id="t1"))
    mb.put(_env(msg_id="m2", from_=b, to=c, thread_id="t1"))
    mb.put(_env(msg_id="m3", from_=a, to=c, thread_id="t2"))
    mb.put(_env(msg_id="m4", from_=a, to=c, thread_id="t1"))

    result = mb.read(thread_id="t1", from_id="ag-a", max_n=10)
    assert [e.msg_id for e in result] == ["m1", "m4"]


def test_latest_seq_initially_zero():
    mb = Mailbox()
    assert mb.latest_seq() == 0


def test_read_then_cursor_advances_via_since_seq():
    """Caller-managed cursor: read returns msgs, caller updates its since_seq."""
    mb = Mailbox()
    a, b = Address(id="ag-a"), Address(id="ag-b")
    cursor = 0
    for i in range(3):
        mb.put(_env(msg_id=f"m{i}", from_=a, to=b))
    first = mb.read(since_seq=cursor, max_n=10)
    cursor = first[-1].seq
    assert [e.msg_id for e in first] == ["m0", "m1", "m2"]

    mb.put(_env(msg_id="m3", from_=a, to=b))
    second = mb.read(since_seq=cursor, max_n=10)
    assert [e.msg_id for e in second] == ["m3"]
