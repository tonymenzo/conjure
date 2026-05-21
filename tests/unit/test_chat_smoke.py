"""Smoke tests for combinator.chat — verify the module imports and
constructs an app without launching the TUI event loop.

The textual runtime needs a TTY and an event loop, so we can't really
exercise the app's interactive behavior in unit tests. We can verify
the entry points wire up correctly and the app instantiates.
"""

from __future__ import annotations

from pathlib import Path


def test_chat_app_constructs(tmp_path: Path):
    """``ChatApp(...)`` should construct cleanly given valid args."""
    from combinator.chat import ChatApp

    app = ChatApp(
        log_path=tmp_path / "log.jsonl",
        addr="ag-test",
        label="iota",
        socket_path=tmp_path / "ctl.sock",
    )
    assert app.title.endswith("iota")
    assert "ag-test" in app.sub_title


def test_chat_main_help_does_not_raise():
    """``combinator-chat --help`` exits cleanly with usage info."""
    import sys

    from combinator import chat

    with __import__("contextlib").suppress(SystemExit):
        chat.main(["--help"])
