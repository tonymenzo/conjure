"""Smoke tests for spawn.chat — verify the module imports and
constructs an app without launching the TUI event loop.

The textual runtime needs a TTY and an event loop, so we can't really
exercise the app's interactive behavior in unit tests. We can verify
the entry points wire up correctly and the app instantiates.
"""

from __future__ import annotations

from pathlib import Path


def test_chat_app_constructs(tmp_path: Path):
    """``ChatApp(...)`` should construct cleanly given valid args."""
    from spawn.chat import ChatApp

    app = ChatApp(
        log_path=tmp_path / "log.jsonl",
        addr="ag-test",
        label="iota",
        socket_path=tmp_path / "ctl.sock",
    )
    assert app.title.endswith("iota")
    assert "ag-test" in app.sub_title


def test_chat_main_help_does_not_raise():
    """``spawn-chat --help`` exits cleanly with usage info."""
    import sys

    from spawn import chat

    with __import__("contextlib").suppress(SystemExit):
        chat.main(["--help"])


def test_format_event_system_prompt_renders_panel():
    """``system_prompt`` events should yield a Rich Panel under the
    ``system-block`` class so the chat pane opens with the agent's
    initialization context as the first message."""
    from rich.panel import Panel

    from spawn.chat import _format_event

    block, classes = _format_event(
        {"kind": "system_prompt", "text": "you are a worker", "label": "alpha"}
    )
    assert isinstance(block, Panel)
    assert "system-block" in classes


def _render(renderable) -> str:
    import io

    from rich.console import Console

    buf = io.StringIO()
    Console(file=buf, width=80, force_terminal=False).print(renderable)
    return buf.getvalue()


def test_format_event_send_uses_response_body_layout():
    """A ``Send`` tool call should render its body as visible text
    rows (rather than only the compact ``● Send(args)`` line) so the
    user sees what the agent sent without going to the meta view."""
    from spawn.chat import _format_event

    block, _ = _format_event(
        {
            "kind": "response",
            "text": "",
            "tool_calls": [
                {
                    "name": "Send",
                    "args": {"to": "caller", "body": "here is my reply"},
                }
            ],
        }
    )
    assert block is not None
    captured = _render(block)
    assert "Send" in captured
    assert "caller" in captured
    assert "here is my reply" in captured


def test_format_event_non_send_tool_call_keeps_compact_form():
    """Non-``Send`` tool calls retain the existing compact form so
    we don't accidentally explode every tool's args into the chat."""
    from spawn.chat import _format_event

    block, _ = _format_event(
        {
            "kind": "response",
            "text": "thinking",
            "tool_calls": [
                {"name": "Spawn", "args": {"role_prompt": "x" * 200}},
            ],
        }
    )
    assert block is not None
    captured = _render(block)
    assert "Spawn(" in captured
    # Truncation marker confirms args weren't fully expanded.
    assert "…" in captured
