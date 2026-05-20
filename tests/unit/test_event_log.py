"""Tests for combinator.event_log."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from combinator.event_log import EventLog, tail


def test_emit_appends_jsonl_line(tmp_path: Path):
    log = EventLog(tmp_path / "events.jsonl")
    log.emit({"kind": "response", "text": "hello"})
    log.emit({"kind": "tool", "text": "ok"})
    log.close()

    content = (tmp_path / "events.jsonl").read_text()
    lines = [json.loads(l) for l in content.splitlines() if l.strip()]
    assert lines == [
        {"kind": "response", "text": "hello"},
        {"kind": "tool", "text": "ok"},
    ]


def test_emit_after_close_is_noop(tmp_path: Path):
    log = EventLog(tmp_path / "events.jsonl")
    log.close()
    log.emit({"kind": "anything"})  # must not raise
    assert log.is_closed


def test_close_is_idempotent(tmp_path: Path):
    log = EventLog(tmp_path / "events.jsonl")
    log.close()
    log.close()  # no error


def test_tail_yields_existing_events(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    log.emit({"kind": "a", "v": 1})
    log.emit({"kind": "b", "v": 2})
    log.close()

    stop = threading.Event()
    gen = tail(path, poll_interval=0.01, stop_event=stop)
    a = next(gen)
    b = next(gen)
    stop.set()
    assert a == {"kind": "a", "v": 1}
    assert b == {"kind": "b", "v": 2}


def test_tail_waits_for_file_creation(tmp_path: Path):
    path = tmp_path / "later.jsonl"
    stop = threading.Event()
    results: list[dict] = []

    def reader():
        for event in tail(path, poll_interval=0.02, stop_event=stop):
            results.append(event)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    time.sleep(0.1)
    # File doesn't exist yet.
    assert results == []

    log = EventLog(path)
    log.emit({"kind": "late", "v": 42})
    time.sleep(0.2)
    stop.set()
    t.join(timeout=2.0)
    assert {"kind": "late", "v": 42} in results
    log.close()


def test_tail_skips_malformed_lines(tmp_path: Path):
    """Manually write a broken line and a good one; the bad line is
    silently skipped."""
    path = tmp_path / "events.jsonl"
    path.write_text('not valid json\n{"kind": "good"}\n', encoding="utf-8")

    stop = threading.Event()
    gen = tail(path, poll_interval=0.01, stop_event=stop)
    good = next(gen)
    stop.set()
    assert good == {"kind": "good"}


def test_concurrent_emits_are_atomic(tmp_path: Path):
    """Two threads emitting to one EventLog produce well-formed JSONL."""
    path = tmp_path / "events.jsonl"
    log = EventLog(path)

    def writer(tag: str):
        for i in range(50):
            log.emit({"kind": "x", "tag": tag, "i": i})

    threads = [threading.Thread(target=writer, args=(t,)) for t in ("a", "b", "c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log.close()

    content = path.read_text()
    parsed = [json.loads(l) for l in content.splitlines() if l.strip()]
    assert len(parsed) == 150  # all 150 events arrived intact
