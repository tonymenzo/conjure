"""End-to-end tests for ControlServer + ControlClient.

The server is started in-process on an ephemeral Unix socket. Each
test issues real socket-level requests through the client and asserts
on responses. The runtime is real but uses no engine factory — agents
stay ``lazy`` — which is fine because the control plane operates on
runtime metadata, not engine output.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from conjure.control import ControlClient, ControlServer
from conjure.record import AgentSpec
from conjure.runtime import Runtime


@pytest.fixture
def server_and_client(tmp_path: Path):
    rt = Runtime()
    sock_path = tmp_path / "control.sock"
    server = ControlServer(runtime=rt, socket_path=sock_path)
    server.start()
    # Wait briefly for the accept loop to be ready.
    time.sleep(0.05)
    client = ControlClient(sock_path)
    yield rt, server, client
    server.stop()
    rt.shutdown()


def test_tree_with_no_root(server_and_client):
    _, _, client = server_and_client
    reply = client.call("tree")
    assert reply == {"ok": True, "tree": None}


def test_tree_with_root_and_children(server_and_client):
    rt, _, client = server_and_client
    root = rt.root(AgentSpec(role_prompt="r", label="iota"))
    rt._spawn(parent=root, spec=AgentSpec(role_prompt="c", label="worker-1"))
    rt._spawn(parent=root, spec=AgentSpec(role_prompt="c", label="worker-2"))

    reply = client.call("tree")
    assert reply["ok"] is True
    tree = reply["tree"]
    assert tree["label"] == "iota"
    labels = {c["label"] for c in tree["children"]}
    assert labels == {"worker-1", "worker-2"}


def test_status_lists_all_agents(server_and_client):
    rt, _, client = server_and_client
    rt.root(AgentSpec(role_prompt="r", label="iota"))
    reply = client.call("status")
    assert reply["ok"] is True
    labels = {a["label"] for a in reply["agents"]}
    assert "iota" in labels


def test_cost_returns_zero_for_scripted(server_and_client):
    rt, _, client = server_and_client
    rt.root(AgentSpec(role_prompt="r", label="iota"))
    reply = client.call("cost")
    assert reply["ok"] is True
    assert reply["total"] == 0.0


def test_inbox_for_known_addr(server_and_client):
    rt, _, client = server_and_client
    root = rt.root(AgentSpec(role_prompt="r"))
    rt.send_external(to=root, body="hello")

    reply = client.call("inbox", addr=root.id)
    assert reply["ok"] is True
    assert len(reply["envelopes"]) == 1
    assert reply["envelopes"][0]["body"] == "hello"


def test_inbox_unknown_addr(server_and_client):
    _, _, client = server_and_client
    reply = client.call("inbox", addr="ag-not-real")
    assert reply["ok"] is False
    assert "unknown" in reply["error"]


def test_send_injects_message(server_and_client):
    rt, _, client = server_and_client
    root = rt.root(AgentSpec(role_prompt="r"))
    reply = client.call("send", addr=root.id, body="from control")
    assert reply["ok"] is True
    inbox = rt.read_inbox(root)
    assert any(e.body == "from control" for e in inbox)


def test_terminate_via_control(server_and_client):
    rt, _, client = server_and_client
    root = rt.root(AgentSpec(role_prompt="r"))
    child = rt._spawn(parent=root, spec=AgentSpec(role_prompt="c"))
    reply = client.call("terminate", addr=child.id)
    assert reply["ok"] is True
    assert child.id in reply["terminated"]
    assert rt.record_for(child).status == "terminated"


def test_unknown_method(server_and_client):
    _, _, client = server_and_client
    reply = client.call("doesnotexist")
    assert reply["ok"] is False
    assert "unknown method" in reply["error"]


def test_missing_addr_error(server_and_client):
    _, _, client = server_and_client
    reply = client.call("inbox")
    assert reply["ok"] is False
    assert "missing addr" in reply["error"]
