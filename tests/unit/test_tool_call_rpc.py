"""End-to-end tests for the ``tool_call`` control RPC — the bridge
the combinator-mcp subprocess uses to forward claude_agent's MCP
calls into the daemon's tool surface."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

from combinator.control import ControlClient, ControlServer
from combinator.record import AgentSpec
from combinator.runtime import Runtime


@pytest.fixture
def runtime_and_server(tmp_path):
    rt = Runtime(store_dir=tmp_path)
    server = ControlServer(runtime=rt, socket_path=tmp_path / "control.sock")
    server.start()
    # Wait for accept loop to be ready.
    for _ in range(50):
        if (tmp_path / "control.sock").exists():
            break
        time.sleep(0.01)
    yield rt, server, tmp_path / "control.sock"
    server.stop()
    rt.shutdown()


def test_tool_call_spawn_then_send(runtime_and_server):
    rt, _server, sock = runtime_and_server
    root = rt.root(AgentSpec(role_prompt="r", label="root"))
    root_token = rt.record_for(root).token
    client = ControlClient(sock)

    spawned = client.call(
        "tool_call",
        token=root_token,
        name="spawn",
        args={"role_prompt": "c", "label": "child"},
    )
    assert spawned["ok"] is True
    child_id = spawned["address"]

    sent = client.call(
        "tool_call",
        token=root_token,
        name="send",
        args={"to": child_id, "body": "hello"},
    )
    assert sent["ok"] is True
    inbox = rt.read_inbox(rt.address_by_id(child_id))
    assert len(inbox) == 1
    assert inbox[0].body == "hello"


def test_tool_call_unknown_tool_returns_error(runtime_and_server):
    rt, _server, sock = runtime_and_server
    root = rt.root(AgentSpec(role_prompt="r", label="root"))
    token = rt.record_for(root).token
    client = ControlClient(sock)
    out = client.call(
        "tool_call", token=token, name="not_a_real_tool", args={}
    )
    assert out["ok"] is False
    assert "unknown tool" in out.get("error", "")


def test_tool_call_bad_args_returns_clear_error(runtime_and_server):
    """Missing required arg surfaces a structured ``bad_args``
    response — the MCP subprocess can pass it straight back to the
    LLM so it knows to retry with proper args."""
    rt, _server, sock = runtime_and_server
    root = rt.root(AgentSpec(role_prompt="r", label="root"))
    token = rt.record_for(root).token
    client = ControlClient(sock)
    # ``spawn`` requires role_prompt; omit it.
    out = client.call("tool_call", token=token, name="spawn", args={})
    assert out["ok"] is False
    # bad_args from pydantic missing-field, or exec_error from
    # something else — either way structured + non-throwing.
    assert "code" in out


def test_tool_call_spawn_with_claude_agent_engine(runtime_and_server):
    """Verify ``spawn`` now accepts engine + sandbox + permissions
    so a claude_agent can spawn a claude_agent child."""
    rt, _server, sock = runtime_and_server
    root = rt.root(AgentSpec(role_prompt="r", label="root"))
    token = rt.record_for(root).token
    client = ControlClient(sock)
    out = client.call(
        "tool_call",
        token=token,
        name="spawn",
        args={
            "role_prompt": "child agent",
            "label": "coder",
            "engine": "claude_agent",
            "tools": ["Read", "Write"],
            "permissions": {"Bash": "ask"},
            "lazy": True,
        },
    )
    assert out["ok"] is True
    child = rt.record_for(rt.address_by_id(out["address"]))
    assert child.spec.engine == "claude_agent"
    assert child.spec.tools == ["Read", "Write"]
    assert child.spec.permissions == {"Bash": "ask"}
