"""Tests for spawn.events — orchestral-message serializer."""

from __future__ import annotations

from orchestral.context.message import Message
from orchestral.llm.base.response import Response
from orchestral.llm.base.tool_call import ToolCall

from conjure.events import (
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
    assert event["tool_calls"] == [{"id": "t1", "name": "spawn", "args": {"role_prompt": "hi"}}]


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
    assert event["tool_call_id"] == "t1"


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


def test_token_usage_is_provider_neutral():
    from conjure.provenance import add_usage, token_usage

    # Anthropic: the cache is reported beside the input, so it is added back.
    anthropic = token_usage({"input_tokens": 10, "output_tokens": 5,
                             "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20})
    assert anthropic == {"input": 130, "output": 5, "cache_read": 100, "cache_creation": 20}
    # OpenAI style: cached tokens are already part of the prompt count.
    openai = token_usage({"prompt_tokens": 130, "completion_tokens": 5, "cached_prompt_tokens": 100})
    assert openai == {"input": 130, "output": 5, "cache_read": 100, "cache_creation": 0}
    codex = token_usage({"input_tokens": 1200, "cached_input_tokens": 1000, "output_tokens": 42})
    assert codex == {"input": 1200, "output": 42, "cache_read": 1000, "cache_creation": 0}
    assert token_usage(None) == {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    total = add_usage(dict(anthropic), openai)
    assert total == {"input": 260, "output": 10, "cache_read": 200, "cache_creation": 20}
