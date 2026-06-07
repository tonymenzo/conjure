"""Tests for spawn.events — orchestral-message serializer."""

from __future__ import annotations

from orchestral.context.message import Message
from orchestral.llm.base.response import Response
from orchestral.llm.base.tool_call import ToolCall

from spawn.events import (
    make_spawned_event,
    make_system_prompt_event,
    make_terminated_event,
    serialize_message,
)


def test_response_with_text_and_tool_calls():
    tc = ToolCall(id="t1", tool_name="spawn", arguments={"role_prompt": "hi"})
    resp = Response(model="m", message=Message(role="assistant", text="hello", tool_calls=[tc]))
    event = serialize_message(resp)
    assert event["kind"] == "response"
    assert event["text"] == "hello"
    assert event["tool_calls"] == [{"name": "spawn", "args": {"role_prompt": "hi"}}]


def test_response_with_text_only():
    resp = Response(model="m", message=Message(role="assistant", text="done"))
    event = serialize_message(resp)
    assert event == {"kind": "response", "text": "done", "tool_calls": []}


def test_tool_message_serialized():
    msg = Message(role="tool", text="{'ok': True}", tool_call_id="t1")
    event = serialize_message(msg)
    assert event["kind"] == "tool"
    assert event["failed"] is False
    assert event["text"] == "{'ok': True}"


def test_failed_tool_message_flagged():
    msg = Message(role="tool", text="boom", tool_call_id="t1", failed=True)
    event = serialize_message(msg)
    assert event["kind"] == "tool"
    assert event["failed"] is True


def test_user_message_serialized():
    msg = Message(role="user", text="hi iota")
    assert serialize_message(msg) == {"kind": "user", "text": "hi iota"}


def test_bare_assistant_serialized():
    msg = Message(role="assistant", text="ok")
    assert serialize_message(msg) == {"kind": "assistant", "text": "ok"}


def test_unknown_role_is_captured_not_dropped():
    msg = Message(role="something_weird", text="weird")
    event = serialize_message(msg)
    assert event["kind"] == "unknown"
    assert event["text"] == "weird"


def test_make_spawned_event():
    event = make_spawned_event(addr="ag-1", label="iota", parent=None)
    assert event == {"kind": "spawned", "addr": "ag-1", "label": "iota", "parent": None}


def test_make_terminated_event():
    event = make_terminated_event(addr="ag-1")
    assert event == {"kind": "terminated", "addr": "ag-1"}


def test_make_system_prompt_event():
    event = make_system_prompt_event(text="you are a worker", label="alpha")
    assert event == {
        "kind": "system_prompt",
        "text": "you are a worker",
        "label": "alpha",
    }


def test_make_system_prompt_event_defaults():
    event = make_system_prompt_event(text="")
    assert event == {"kind": "system_prompt", "text": "", "label": ""}
