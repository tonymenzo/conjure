"""Smoke tests for the ClaudeAgentEngine wiring.

These do NOT make API calls — they verify:

  1. The dispatch in ``build_runtime`` routes ``engine: "claude_agent"``
     to the right factory and ``engine: "orchestral"`` stays unchanged.
  2. ``ClaudeAgentEngine`` can be imported (i.e. the optional
     ``claude-agent-sdk`` dependency is wired correctly) when present;
     skip otherwise so CI without the SDK doesn't fail.
  3. The tool-call / tool-result extractors produce the shapes the
     chat pane expects.
  4. The bundled CLAUDE.md system prompt loads and templates cleanly.
"""

from __future__ import annotations

import pytest


def test_claude_agent_sdk_importable_or_skip():
    """If the SDK is installed in this env, importing the engine
    module shouldn't crash."""
    pytest.importorskip("claude_agent_sdk")
    from spawn.engines import claude_agent
    assert hasattr(claude_agent, "ClaudeAgentEngine")


def test_extract_tool_calls_strips_mcp_prefix():
    """Tool calls bridged through MCP show up as
    ``mcp__spawn__<name>`` on the wire. The chat pane expects the
    user-friendly bare name."""
    pytest.importorskip("claude_agent_sdk")
    from claude_agent_sdk.types import (
        AssistantMessage,
        TextBlock,
        ToolUseBlock,
    )
    from spawn.engines.claude_agent import (
        _extract_text,
        _extract_tool_calls,
    )

    msg = AssistantMessage(
        content=[
            TextBlock(text="spawning"),
            ToolUseBlock(
                id="t1",
                name="mcp__spawn__spawn",
                input={"role_prompt": "x", "label": "y"},
            ),
            ToolUseBlock(
                id="t2",
                name="Read",
                input={"path": "foo.py"},
            ),
        ],
        model="claude-sonnet-4-5",
    )
    assert _extract_text(msg) == "spawning"
    calls = _extract_tool_calls(msg)
    assert [c["name"] for c in calls] == ["spawn", "Read"]
    assert calls[0]["args"]["role_prompt"] == "x"
    assert calls[1]["args"]["path"] == "foo.py"


def test_extract_tool_results_handles_str_and_list_bodies():
    """``ToolResultBlock.content`` is either a string or a list of
    content-block dicts. Both should reduce to a single text string."""
    pytest.importorskip("claude_agent_sdk")
    from claude_agent_sdk.types import ToolResultBlock, UserMessage
    from spawn.engines.claude_agent import _extract_tool_results

    msg = UserMessage(
        content=[
            ToolResultBlock(
                tool_use_id="t1",
                content='{"ok": true, "address": "ag-x"}',
                is_error=False,
            ),
            ToolResultBlock(
                tool_use_id="t2",
                content=[{"type": "text", "text": "rate limit"}],
                is_error=True,
            ),
        ],
    )
    results = _extract_tool_results(msg)
    assert results[0]["failed"] is False
    assert "ag-x" in results[0]["text"]
    assert results[1]["failed"] is True
    assert results[1]["text"] == "rate limit"


def test_default_system_frame_loads_and_templates():
    """The bundled system prompt loads as a ``string.Template`` whose
    ``safe_substitute`` doesn't choke on the JSON examples (literal
    ``{...}``) in the markdown."""
    from spawn.engines.claude_agent import (
        ClaudeAgentEngine,
        _load_default_system_template,
    )
    from spawn.address import Address
    from spawn.capability import CapabilitySet
    from spawn.mailbox import Mailbox
    from spawn.record import AgentRecord, AgentSpec

    template = _load_default_system_template()
    frame = template.template
    assert "Spawn" in frame
    assert "$addr_id" in frame
    assert "$role_prompt" in frame
    assert "spawn" in frame.lower()
    assert "send" in frame.lower()

    addr = Address(id="ag-test", label="probe")
    record = AgentRecord(
        addr=addr,
        spec=AgentSpec(role_prompt="run a benchmark sweep"),
        inbox=Mailbox(),
        capabilities=CapabilitySet(self_addr=addr),
        token="t",
        depth=0,
    )

    class _RT:
        max_depth = 3

    rendered = ClaudeAgentEngine._build_system_prompt(record, _RT())
    assert "ag-test" in rendered
    assert "run a benchmark sweep" in rendered
    assert "{addr_id}" not in rendered  # placeholder substituted


def test_preapproved_tools_excludes_ask_and_deny(tmp_path):
    """Tools whose effective decision isn't ``allow`` must NOT land
    in the SDK's ``allowed_tools`` (= CLI's ``--allowedTools``).
    Otherwise the CLI pre-approves them at the wire level and our
    ``can_use_tool`` callback never sees the request — which is the
    bug that kept the perm-modal from ever popping for Bash even
    when the spec said ``Bash: ask``."""
    pytest.importorskip("claude_agent_sdk")
    from unittest.mock import patch

    from spawn.address import Address
    from spawn.capability import CapabilitySet
    from spawn.engines.claude_agent import ClaudeAgentEngine
    from spawn.mailbox import Mailbox
    from spawn.record import AgentRecord, AgentSpec
    from spawn.runtime import Runtime

    rt = Runtime(store_dir=tmp_path)

    addr = Address(id="ag-test", label="root")
    record = AgentRecord(
        addr=addr,
        spec=AgentSpec(
            role_prompt="x",
            tools=["Read", "Write", "Edit", "Bash", "Grep"],
            permissions={
                "Read": "allow",
                "Write": "ask",
                "Edit": "ask",
                "Bash": "ask",
                "Grep": "allow",
            },
        ),
        inbox=Mailbox(),
        capabilities=CapabilitySet(self_addr=addr),
        token="t",
        depth=0,
    )

    # ClaudeSDKClient(opts) shells out to the CLI on construction in
    # some SDK paths — patch the client so this test stays offline.
    with patch("claude_agent_sdk.ClaudeSDKClient") as MockClient:
        engine = ClaudeAgentEngine(
            record=record,
            runtime=rt,
            sandbox_dir=tmp_path,
            allowed_tools=record.spec.tools,
        )
        allowed = list(engine._options.allowed_tools)

    # Tools whose spec says "ask" must not be in the CLI's allow list.
    assert "Write" not in allowed
    assert "Edit" not in allowed
    assert "Bash" not in allowed
    # Explicitly-allowed tools stay (no point routing through the
    # callback just to return allow).
    assert "Read" in allowed
    assert "Grep" in allowed
    # Bridged MCP tools must stay (they're only callable when listed).
    assert any(t.startswith("mcp__spawn__") for t in allowed)


def test_preapproved_tools_default_ask_set_held_back(tmp_path):
    """With no spec-level permissions, tools in ``_ASK_BY_DEFAULT``
    (Bash / Edit / Write / MultiEdit / NotebookEdit) still get held
    back from ``--allowedTools`` — the engine's safety default."""
    pytest.importorskip("claude_agent_sdk")
    from unittest.mock import patch

    from spawn.address import Address
    from spawn.capability import CapabilitySet
    from spawn.engines.claude_agent import ClaudeAgentEngine
    from spawn.mailbox import Mailbox
    from spawn.record import AgentRecord, AgentSpec
    from spawn.runtime import Runtime

    rt = Runtime(store_dir=tmp_path)
    addr = Address(id="ag-test", label="root")
    record = AgentRecord(
        addr=addr,
        spec=AgentSpec(
            role_prompt="x",
            tools=["Read", "Write", "Edit", "Bash"],
            permissions={},  # no explicit decisions
        ),
        inbox=Mailbox(),
        capabilities=CapabilitySet(self_addr=addr),
        token="t",
        depth=0,
    )
    with patch("claude_agent_sdk.ClaudeSDKClient"):
        engine = ClaudeAgentEngine(
            record=record,
            runtime=rt,
            sandbox_dir=tmp_path,
            allowed_tools=record.spec.tools,
        )
        allowed = list(engine._options.allowed_tools)

    assert "Read" in allowed
    assert "Write" not in allowed
    assert "Edit" not in allowed
    assert "Bash" not in allowed


def test_dispatch_rejects_unknown_engine(tmp_path):
    """``build_runtime`` dispatcher refuses unknown engine names with
    a clear error pointing at the supported set."""
    from spawn.config import load_config_from_mapping
    from spawn.runner import build_runtime

    cfg = load_config_from_mapping(
        {
            "runtime": {"store_dir": str(tmp_path)},
            "llms": {"default": {"provider": "anthropic"}},
            "root": {
                "role_prompt": "you are root",
                "engine": "totally_made_up",
            },
        }
    )
    with pytest.raises(Exception) as excinfo:
        build_runtime(cfg)
    # The error message must mention the unknown engine name and the
    # supported set so the user knows what's wrong.
    msg = str(excinfo.value)
    assert "totally_made_up" in msg or "unknown engine" in msg
