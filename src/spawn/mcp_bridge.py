"""``spawn-mcp`` — stdio MCP server that exposes spawn's
orchestration tools (spawn, send, recv, agent_map, ...) to the
``claude-agent-sdk`` running in a ``claude`` subprocess.

Architecture:

  claude subprocess
       │  MCP stdio
       ▼
  spawn-mcp (this script)
       │  Unix socket
       ▼
  spawn daemon (control.tool_call)
       │
       ▼
  spawn.tools.{primitives,combinators} ← actual runtime work

The bridge is intentionally thin: each MCP tool inherits the
corresponding spawn tool's field declarations (so the SDK sees
the same arg surface — types, descriptions, defaults), then
overrides ``_run`` to forward the call to the daemon's
``tool_call`` RPC. The daemon does the actual work — capability
checks, journaling, runtime mutation — exactly as it does for
orchestral agents.

Required env vars:
  SPAWN_TOKEN   — caller's runtime_token (per-agent identity).
  SPAWN_SOCKET  — daemon control socket path.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Sequence

from orchestral.mcp.server import MCPServer
from orchestral.tools.base.field_utils import is_state_field

from spawn.control import ControlClient
from spawn.tools._base import StateField
from spawn.tools.combinators import (
    AgentFilterTool,
    AgentFixedPointTool,
    AgentFoldTool,
    AgentMapTool,
)
from spawn.tools.primitives import (
    CallTool,
    IntroduceTool,
    ListInboxTool,
    PeekTool,
    RecvTool,
    SendTool,
    SpawnTool,
    TerminateTool,
    WaitForTool,
)


# Map each MCP tool's short name to (spawn tool class,
# PascalCase MCP display name). The short name is the snake_case
# token the daemon's ``tool_call`` registry expects (capability
# checks happen by that name); the display name is what the SDK
# exposes to the LLM (mcp__spawn__<DisplayName>).
#
# We can't rely on orchestral's auto-PascalCase here because
# ``tool.get_name()`` returns the already-cleaned class name (e.g.
# ``WaitFor``), which the converter then title-cases to ``Waitfor``.
# Spelling each display name out keeps multi-word tools readable.
_BRIDGE_TARGETS: dict[str, tuple[type, str]] = {
    "spawn": (SpawnTool, "Spawn"),
    "send": (SendTool, "Send"),
    "recv": (RecvTool, "Recv"),
    "wait_for": (WaitForTool, "WaitFor"),
    "terminate": (TerminateTool, "Terminate"),
    "introduce": (IntroduceTool, "Introduce"),
    "list_inbox": (ListInboxTool, "ListInbox"),
    "peek": (PeekTool, "Peek"),
    "call": (CallTool, "Call"),
    "agent_map": (AgentMapTool, "AgentMap"),
    "agent_fold": (AgentFoldTool, "AgentFold"),
    "agent_filter": (AgentFilterTool, "AgentFilter"),
    "agent_fixed_point": (AgentFixedPointTool, "AgentFixedPoint"),
}


def _make_bridge_class(
    short_name: str, target_cls: type, display_name: str
) -> type:
    """Build a subclass of ``target_cls`` that overrides ``_run`` to
    forward the call to the daemon over the control socket. Keeps
    the parent's runtime fields (and their descriptions) intact so
    the MCP-exposed schema matches what the LLM would see if it
    were calling the tool directly.

    ``display_name`` sets the PascalCase MCP-side name (orchestral's
    MCPServer reads ``_mcp_display_name`` when
    ``use_display_names=True``). We pass it explicitly because the
    auto-PascalCase'r collapses multi-word names from
    ``tool.get_name()`` into a single capitalized token."""

    def _run(self) -> dict[str, Any]:
        runtime_args: dict[str, Any] = {}
        for field_name, field_info in type(self).model_fields.items():
            if is_state_field(field_info):
                continue
            value = getattr(self, field_name, None)
            if value is None:
                continue
            runtime_args[field_name] = value
        # Wait at least as long as the tool's own ``timeout_s`` (plus
        # headroom) before declaring the RPC dead. Without this the
        # ControlClient's default 10s cap fires while a combinator
        # like ``AgentMap`` is still legitimately spawning workers
        # and gathering replies — the daemon returns ok asynchronously
        # but the bridge has already given up.
        tool_timeout = runtime_args.get("timeout_s")
        rpc_timeout = (
            float(tool_timeout) + 30.0
            if isinstance(tool_timeout, (int, float))
            else 600.0
        )
        client = ControlClient(Path(self.bridge_socket))
        return client.call(
            "tool_call",
            token=self.bridge_token,
            name=short_name,
            args=runtime_args,
            timeout=rpc_timeout,
        )

    bridge = type(
        target_cls.__name__,
        (target_cls,),
        {
            "_run": _run,
            "_mcp_display_name": display_name,
            "__module__": __name__,
            "__doc__": (target_cls.__doc__ or "")
            + "\n\n(Bridged: forwards to the spawn daemon over MCP.)",
            # StateField annotations — invisible to MCP clients (the
            # schema generator skips state fields) but pydantic
            # requires they don't start with an underscore.
            "__annotations__": {
                "bridge_token": str,
                "bridge_socket": str,
            },
            "bridge_token": StateField(default=""),
            "bridge_socket": StateField(default=""),
        },
    )
    return bridge


def _build_bridge_tools(token: str, socket_path: str) -> list:
    """Instantiate one bridge tool per spawn tool, bound to
    this agent's token + the daemon socket."""
    tools = []
    for short_name, (target_cls, display_name) in _BRIDGE_TARGETS.items():
        cls = _make_bridge_class(short_name, target_cls, display_name)
        # The bridged class still has ``runtime_token`` (inherited);
        # set it to "" so pydantic doesn't object. The real token
        # lives on the private bridge field.
        tool = cls(
            runtime_token="",
            bridge_token=token,
            bridge_socket=socket_path,
        )
        tools.append(tool)
    return tools


def main(argv: Sequence[str] | None = None) -> int:
    token = os.environ.get("SPAWN_TOKEN", "")
    socket_path = os.environ.get("SPAWN_SOCKET", "")
    if not token or not socket_path:
        print(
            "spawn-mcp requires SPAWN_TOKEN and SPAWN_SOCKET "
            "env vars (set by ClaudeAgentEngine when it launches this "
            "subprocess).",
            file=sys.stderr,
        )
        return 2
    if not Path(socket_path).exists():
        print(
            f"spawn-mcp: control socket not found: {socket_path}",
            file=sys.stderr,
        )
        return 2

    tools = _build_bridge_tools(token=token, socket_path=socket_path)
    server = MCPServer(
        tools=tools,
        name="spawn",
        # PascalCase display names so the spawn tools read the
        # same as Claude Code's built-ins (Read, Write, Bash, …) when
        # they appear in the SDK's tool list and in the chat pane.
        # Orchestral's adapter auto-converts: spawn → Spawn,
        # agent_map → AgentMap, agent_fixed_point → AgentFixedPoint.
        use_display_names=True,
    )
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
