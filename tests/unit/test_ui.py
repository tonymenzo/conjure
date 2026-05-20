"""Tests for combinator._ui — display hook renderer."""

from __future__ import annotations

import io

from orchestral.context.message import Message
from orchestral.llm.base.response import Response
from orchestral.llm.base.tool_call import ToolCall
from rich.console import Console

from combinator._ui import make_display_hook_builder
from combinator.record import AgentSpec
from combinator.runtime import Runtime


class _Ctx:
    """Stand-in for orchestral.context.Context — just exposes ``messages``."""

    def __init__(self, messages):
        self.messages = messages


def _captured_console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False, no_color=True, width=200)


def _make_record():
    rt = Runtime()
    addr = rt.root(AgentSpec(role_prompt="r", label="iota"))
    rec = rt.record_for(addr)
    return rt, rec


def test_hook_renders_assistant_text_with_label():
    console = _captured_console()
    builder = make_display_hook_builder(console)
    rt, rec = _make_record()

    hook = builder(rec)
    response = Response(
        model="fake",
        message=Message(role="assistant", text="hello there"),
    )
    hook(_Ctx([response]))

    out = console.file.getvalue()
    assert "iota" in out
    assert "hello there" in out
    rt.shutdown()


def test_hook_renders_tool_call_then_result():
    console = _captured_console()
    builder = make_display_hook_builder(console)
    rt, rec = _make_record()
    hook = builder(rec)

    tc = ToolCall(id="tc-1", tool_name="spawn", arguments={"role_prompt": "hi"})
    response = Response(
        model="fake",
        message=Message(role="assistant", text=None, tool_calls=[tc]),
    )
    tool_msg = Message(role="tool", text='{"ok": true}', tool_call_id="tc-1")
    hook(_Ctx([response, tool_msg]))

    out = console.file.getvalue()
    assert "spawn" in out
    assert "role_prompt" in out
    assert "ok" in out
    rt.shutdown()


def test_hook_does_not_redraw_seen_messages():
    console = _captured_console()
    builder = make_display_hook_builder(console)
    rt, rec = _make_record()
    hook = builder(rec)

    r1 = Response(model="fake", message=Message(role="assistant", text="first"))
    r2 = Response(model="fake", message=Message(role="assistant", text="second"))
    hook(_Ctx([r1]))
    intermediate = console.file.getvalue()
    hook(_Ctx([r1, r2]))
    final = console.file.getvalue()

    # The second call should append `second` but not duplicate `first`.
    assert final.count("first") == 1
    assert "second" in final
    rt.shutdown()


def test_hook_skips_user_echo():
    """The REPL prints the user prompt itself; the hook should ignore
    the user message that orchestral adds to its context."""
    console = _captured_console()
    builder = make_display_hook_builder(console)
    rt, rec = _make_record()
    hook = builder(rec)

    user_msg = Message(role="user", text="hi iota")
    hook(_Ctx([user_msg]))
    out = console.file.getvalue()
    assert "hi iota" not in out
    rt.shutdown()


def test_hook_renders_failed_tool_in_red():
    console = _captured_console()
    builder = make_display_hook_builder(console)
    rt, rec = _make_record()
    hook = builder(rec)

    failed = Message(role="tool", text="capability missing", tool_call_id="tc-1", failed=True)
    hook(_Ctx([failed]))
    out = console.file.getvalue()
    assert "capability missing" in out
    rt.shutdown()


def test_hook_summarizes_ok_dict_result():
    """A success result with an 'address' key collapses to 'address=...'."""
    console = _captured_console()
    builder = make_display_hook_builder(console)
    rt, rec = _make_record()
    hook = builder(rec)

    tool_msg = Message(
        role="tool",
        text="{'ok': True, 'address': 'ag-abc', 'label': 'sub'}",
        tool_call_id="tc-1",
    )
    hook(_Ctx([tool_msg]))
    out = console.file.getvalue()
    assert "address" in out
    assert "ag-abc" in out
    rt.shutdown()


def test_hook_summarizes_failed_dict_result():
    """A failure with code/error becomes 'code: error'."""
    console = _captured_console()
    builder = make_display_hook_builder(console)
    rt, rec = _make_record()
    hook = builder(rec)

    tool_msg = Message(
        role="tool",
        text="{'ok': False, 'code': 'not_permitted', 'error': 'caller cannot send to ag-xyz'}",
        tool_call_id="tc-1",
        failed=True,
    )
    hook(_Ctx([tool_msg]))
    out = console.file.getvalue()
    assert "not_permitted" in out
    assert "caller cannot send" in out
    rt.shutdown()
