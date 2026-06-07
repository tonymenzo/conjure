"""In-process MCP bridge: schema generation + handler invocation.

The engine relies on ``build_in_process_mcp_server`` to replace the
``spawn-mcp`` subprocess with direct Python calls. These tests
exercise the schema-shaping (state fields excluded, runtime fields
preserved) and the handler path (Spawn / Send round-trip through the
runtime) without booting the SDK.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

pytest.importorskip(
    "claude_agent_sdk",
    reason="claude-agent-sdk required for the in-process MCP bridge",
)

from spawn.mcp_in_process import (
    _HIDDEN_FIELDS,
    _build_input_schema,
    _make_handler,
    build_in_process_mcp_server,
)
from spawn.record import AgentSpec
from spawn.runtime import Runtime
from spawn.tools.primitives import (
    CallTool,
    SendTool,
    SpawnTool,
    WaitForTool,
)


def test_schema_strips_state_fields():
    """``runtime_token`` and ``cost`` are internal — the LLM must
    never see them in the input schema."""
    schema = _build_input_schema(SpawnTool)
    props = schema["properties"]
    for hidden in _HIDDEN_FIELDS:
        assert hidden not in props
    # Real runtime fields are kept.
    assert "role_prompt" in props
    assert "label" in props
    assert "oneshot" in props


def test_handler_round_trips_through_runtime():
    """A Spawn handler call must hit the runtime and produce a real
    child agent with a working capability back-edge."""
    with tempfile.TemporaryDirectory() as d:
        rt = Runtime(store_dir=Path(d))
        try:
            root = rt.root(AgentSpec(role_prompt="root", label="root"))
            token = rt.record_for(root).token

            spawn = _make_handler(SpawnTool, token)
            send = _make_handler(SendTool, token)

            async def go():
                res = await spawn(
                    {"role_prompt": "child", "label": "kid", "lazy": True}
                )
                body = json.loads(res["content"][0]["text"])
                assert body["ok"], body
                child_addr = body["address"]
                res = await send({"to": child_addr, "body": "hello"})
                body = json.loads(res["content"][0]["text"])
                assert body["ok"], body
                return child_addr

            child = asyncio.run(go())
            addr = rt.address_by_id(child)
            assert addr is not None
            envs = rt.read_inbox(addr)
            assert len(envs) == 1
            assert envs[0].body == "hello"
        finally:
            rt.shutdown()


def test_handler_surfaces_bad_args_as_error():
    """Pydantic validation failures come back as ``is_error: True``
    rather than crashing the engine."""
    handler = _make_handler(SendTool, token="no-such-token")

    async def go():
        # ``to`` is required; omitting it should produce a structured error.
        return await handler({})

    res = asyncio.run(go())
    assert res.get("is_error") is True
    payload = json.loads(res["content"][0]["text"])
    assert payload["ok"] is False


def test_build_in_process_server_returns_config():
    """Smoke test: ``build_in_process_mcp_server`` returns the SDK's
    server-config dict shape — the engine plugs that straight into
    ``ClaudeAgentOptions.mcp_servers``."""
    cfg = build_in_process_mcp_server("test-token")
    assert cfg is not None
    assert isinstance(cfg, dict)
    assert cfg.get("name") == "spawn"
    assert "instance" in cfg


def test_call_tool_exposed_with_required_fields():
    """The Call primitive must be in the in-process schema (it was
    added to the bridge alongside the original primitives)."""
    schema = _build_input_schema(CallTool)
    assert "spec" in schema["properties"]
    assert "body" in schema["properties"]
    assert "timeout_s" in schema["properties"]


def test_wait_for_schema_preserves_timeout_default():
    """``WaitFor`` declares a 30s default; if the schema drops defaults
    the LLM has to specify the timeout every call, which breaks the
    natural fan-in shape."""
    schema = _build_input_schema(WaitForTool)
    assert schema["properties"]["timeout_s"].get("default") == 30.0
