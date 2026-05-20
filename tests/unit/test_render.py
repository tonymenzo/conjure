"""Tests for combinator.render.

The renderer is a tail-and-render loop. We test by writing a synthetic
event log to disk and running ``render.main`` in a thread with a
stop_event, then asserting the captured console output. Sticking with
the in-process path avoids subprocess flakiness.
"""

from __future__ import annotations

import io
import json
import sys
import threading
import time
from pathlib import Path

from rich.console import Console

from combinator._ui import render_event


def test_render_event_response_with_tool_calls_uses_panel():
    console = Console(file=io.StringIO(), force_terminal=False, no_color=True, width=200)
    render_event(
        console,
        "iota",
        {
            "kind": "response",
            "text": "I'll spawn a worker.",
            "tool_calls": [{"name": "spawn", "args": {"role_prompt": "x"}}],
        },
    )
    out = console.file.getvalue()
    assert "iota" in out
    assert "I'll spawn" in out
    assert "spawn" in out
    assert "role_prompt" in out


def test_render_event_tool_result_success():
    console = Console(file=io.StringIO(), force_terminal=False, no_color=True, width=200)
    render_event(
        console,
        "iota",
        {"kind": "tool", "text": "{'ok': True, 'address': 'ag-xyz'}", "failed": False},
    )
    out = console.file.getvalue()
    assert "✓" in out
    assert "ag-xyz" in out


def test_render_event_tool_result_failure():
    console = Console(file=io.StringIO(), force_terminal=False, no_color=True, width=200)
    render_event(
        console,
        "iota",
        {"kind": "tool", "text": "{'ok': False, 'code': 'no_runtime', 'error': 'gone'}", "failed": True},
    )
    out = console.file.getvalue()
    assert "✗" in out
    assert "no_runtime" in out


def test_render_event_unknown_kind_is_silent():
    console = Console(file=io.StringIO(), force_terminal=False, no_color=True, width=200)
    render_event(console, "iota", {"kind": "weird", "text": "ignored"})
    assert console.file.getvalue() == ""


def test_render_event_spawned():
    console = Console(file=io.StringIO(), force_terminal=False, no_color=True, width=200)
    render_event(
        console,
        "iota",
        {"kind": "spawned", "addr": "ag-1", "label": "worker-1", "parent": "ag-iota"},
    )
    out = console.file.getvalue()
    assert "worker-1" in out
    assert "ag-iota" in out


def test_render_event_terminated():
    console = Console(file=io.StringIO(), force_terminal=False, no_color=True, width=200)
    render_event(console, "iota", {"kind": "terminated", "addr": "ag-1"})
    out = console.file.getvalue()
    assert "terminated" in out
    assert "ag-1" in out


def test_render_main_loop_processes_log(tmp_path: Path, monkeypatch, capsys):
    """End-to-end: write events to a file, run render.main in a thread,
    stop it, verify the rich-rendered output contains our events."""
    from combinator import render

    log_path = tmp_path / "events.jsonl"
    log_path.write_text(
        json.dumps({"kind": "response", "text": "hi", "tool_calls": []}) + "\n",
        encoding="utf-8",
    )

    # Force rich to render to a capturable file rather than the real tty.
    captured = io.StringIO()

    def fake_console(*args, **kwargs):
        return Console(file=captured, force_terminal=False, no_color=True, width=200)

    monkeypatch.setattr(render, "Console", fake_console)

    rc_box: list[int] = []
    stop_holder: list[threading.Event] = []
    original_tail = render.tail

    def short_tail(path, *, poll_interval=0.05, stop_event=None):
        # Capture the stop_event so the test thread can set it.
        if stop_event is not None:
            stop_holder.append(stop_event)
        yield from original_tail(path, poll_interval=0.01, stop_event=stop_event)

    monkeypatch.setattr(render, "tail", short_tail)

    def runner():
        rc_box.append(render.main(["--log", str(log_path), "--label", "iota"]))

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    # Give it time to read the event then stop it.
    for _ in range(50):
        if "hi" in captured.getvalue():
            break
        time.sleep(0.02)
    if stop_holder:
        stop_holder[0].set()
    t.join(timeout=2.0)
    assert "hi" in captured.getvalue()
    assert "iota" in captured.getvalue()
