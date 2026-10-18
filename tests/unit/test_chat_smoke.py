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
    from conjure.chat import ChatApp

    app = ChatApp(
        log_path=tmp_path / "log.jsonl",
        addr="ag-test",
        label="iota",
        socket_path=tmp_path / "ctl.sock",
    )
    assert app.title.endswith("iota")
    assert "ag-test" in app.sub_title


def test_chat_main_help_does_not_raise():
    """``conjure-chat --help`` exits cleanly with usage info."""
    import sys

    from conjure import chat

    with __import__("contextlib").suppress(SystemExit):
        chat.main(["--help"])


def test_format_event_system_prompt_renders_panel():
    """``system_prompt`` events should yield a Rich Panel under the
    ``system-block`` class so the chat pane opens with the agent's
    initialization context as the first message."""
    from rich.panel import Panel

    from conjure.chat import _format_event

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
    from conjure.chat import _format_event

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
    from conjure.chat import _format_event

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


def _call_block(name, args):
    from conjure.chat import _format_event

    block, _ = _format_event({"kind": "response", "text": "", "tool_calls": [{"name": name, "args": args}]})
    return _render(block)


def test_bash_call_shows_its_command_as_code():
    out = _call_block("Bash", {"command": "set -e\npython3 - <<'EOF'\nprint('hi')\nEOF"})
    assert "$ set -e" in out and "+3 lines" in out
    assert "print('hi')" in out


def test_write_call_previews_the_file_with_line_numbers():
    content = "".join(f"x{i} = {i}\n" for i in range(30))
    out = _call_block("Write", {"path": "params.py", "content": content})
    assert "params.py" in out and "30 lines" in out
    assert "x11 = 11" in out and "x12 = 12" not in out
    assert "18 more lines" in out


def test_edit_call_shows_a_diff():
    out = _call_block("Edit", {"file_path": "a.py", "old_string": "n = 1\n", "new_string": "n = 2\n"})
    assert "-n = 1" in out and "+n = 2" in out and "@@" not in out


def test_shell_result_shows_exit_status_and_output_tail():
    from conjure.chat import _format_event

    payload = {"ok": True, "exit_code": 2, "stdout": "".join(f"line {i}\n" for i in range(20)), "stderr": "boom\n"}
    block, _ = _format_event({"kind": "tool", "text": str(payload), "failed": False})
    out = _render(block)
    assert "exit 2" in out and "21 lines" in out
    assert "boom" in out and "line 19" in out and "line 3\n" not in out


def test_capped_shell_result_still_parses():
    """A capped result has raw newlines from its truncation notice
    inside the repr; it should still render as a command result."""
    from conjure.chat import _format_event
    from conjure.tool_output import cap_tool_output

    payload = {"ok": True, "exit_code": 0, "stdout": "y" * 5000, "stderr": ""}
    capped, info = cap_tool_output(str(payload), max_chars=1000, tool_name="Bash")
    assert info is not None
    block, _ = _format_event({"kind": "tool", "text": capped, "failed": False})
    out = _render(block)
    assert "exit 0" in out and "chars cut" in out
