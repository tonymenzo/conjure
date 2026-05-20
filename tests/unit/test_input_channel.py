"""Tests for the input channel + input reader loop.

The CLI's ``_start_input_reader`` tails a JSONL file appended by the
``combinator-input`` window and dispatches each line. We test it
in-process by writing lines manually and verifying the reader's
behavior.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from combinator.cli import _start_input_reader
from combinator.record import AgentSpec
from combinator.runtime import Runtime


def _append(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")
        f.flush()


def test_plain_line_dispatched_to_root(tmp_path: Path):
    rt = Runtime()
    root = rt.root(AgentSpec(role_prompt="r"))
    input_path = tmp_path / "input.jsonl"
    input_path.touch()
    stop = threading.Event()

    _start_input_reader(
        input_path=input_path,
        runtime=rt,
        root=root,
        shutdown_event=stop,
    )

    _append(input_path, {"line": "hello world"})

    # Poll for the message landing in root's inbox.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        inbox = rt.read_inbox(root)
        if any(env.body == "hello world" for env in inbox):
            break
        time.sleep(0.05)
    stop.set()
    rt.shutdown()
    assert any(env.body == "hello world" for env in rt.read_inbox(root))


def test_quit_sets_shutdown_event(tmp_path: Path):
    rt = Runtime()
    root = rt.root(AgentSpec(role_prompt="r"))
    input_path = tmp_path / "input.jsonl"
    input_path.touch()
    stop = threading.Event()

    _start_input_reader(
        input_path=input_path,
        runtime=rt,
        root=root,
        shutdown_event=stop,
    )

    _append(input_path, {"line": ":quit"})

    assert stop.wait(timeout=2.0), "shutdown_event not set after :quit"
    rt.shutdown()


def test_malformed_json_skipped(tmp_path: Path):
    rt = Runtime()
    root = rt.root(AgentSpec(role_prompt="r"))
    input_path = tmp_path / "input.jsonl"
    input_path.write_text("not json\n", encoding="utf-8")
    stop = threading.Event()

    _start_input_reader(
        input_path=input_path,
        runtime=rt,
        root=root,
        shutdown_event=stop,
    )

    _append(input_path, {"line": "valid one"})

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        inbox = rt.read_inbox(root)
        if any(env.body == "valid one" for env in inbox):
            break
        time.sleep(0.05)
    stop.set()
    rt.shutdown()
    assert any(env.body == "valid one" for env in rt.read_inbox(root))
