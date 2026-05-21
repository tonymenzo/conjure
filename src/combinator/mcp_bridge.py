"""``combinator-mcp`` — stdio MCP server that exposes combinator's
orchestration tools (spawn, send, recv, agent_map, ...) to the
``claude-agent-sdk`` running in a ``claude`` subprocess.

Architecture:

  claude subprocess
       │  MCP stdio
       ▼
  combinator-mcp (this script)
       │  Unix socket
       ▼
  combinator daemon (control.tool_call)
       │
       ▼
  combinator.tools.{primitives,combinators} ← actual runtime work

The bridge is intentionally thin: each MCP tool inherits the
corresponding combinator tool's field declarations (so the SDK sees
the same arg surface — types, descriptions, defaults), then
overrides ``_run`` to forward the call to the daemon's
``tool_call`` RPC. The daemon does the actual work — capability
checks, journaling, runtime mutation — exactly as it does for
orchestral agents.

Required env vars:
  COMBINATOR_TOKEN   — caller's runtime_token (per-agent identity).
  COMBINATOR_SOCKET  — daemon control socket path.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Sequence

from orchestral.mcp.server import MCPServer
from orchestral.tools.base.field_utils import is_state_field

from combinator.control import ControlClient
from combinator.tools._base import StateField
from combinator.tools.combinators import (
    AgentFilterTool,
    AgentFixedPointTool,
    AgentFoldTool,
    AgentMapTool,
)
from combinator.tools.primitives import (
    IntroduceTool,
    ListInboxTool,
    RecvTool,
    SendTool,
    SpawnTool,
    TerminateTool,
    WaitForTool,
)


# Map each MCP tool's short name to the combinator tool class whose
# fields we inherit. The class name (with ``Tool`` suffix dropped) is
# also what the SDK sees as the MCP tool's display name.
_BRIDGE_TARGETS: dict[str, type] = {
    "spawn": SpawnTool,
    "send": SendTool,
    "recv": RecvTool,
    "wait_for": WaitForTool,
    "terminate": TerminateTool,
    "introduce": IntroduceTool,
    "list_inbox": ListInboxTool,
    "agent_map": AgentMapTool,
    "agent_fold": AgentFoldTool,
    "agent_filter": AgentFilterTool,
    "agent_fixed_point": AgentFixedPointTool,
}


def _make_bridge_class(short_name: str, target_cls: type) -> type:
    """Build a subclass of ``target_cls`` that overrides ``_run`` to
    forward the call to the daemon over the control socket. Keeps
    the parent's runtime fields (and their descriptions) intact so
    the MCP-exposed schema matches what the LLM would see if it
    were calling the tool directly."""

    def _run(self) -> dict[str, Any]:
        runtime_args: dict[str, Any] = {}
        for field_name, field_info in type(self).model_fields.items():
            if is_state_field(field_info):
                continue
            value = getattr(self, field_name, None)
            if value is None:
                continue
            runtime_args[field_name] = value
        client = ControlClient(Path(self.bridge_socket))
        return client.call(
            "tool_call",
            token=self.bridge_token,
            name=short_name,
            args=runtime_args,
        )

    bridge = type(
        target_cls.__name__,
        (target_cls,),
        {
            "_run": _run,
            "__module__": __name__,
            "__doc__": (target_cls.__doc__ or "")
            + "\n\n(Bridged: forwards to the combinator daemon over MCP.)",
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
    """Instantiate one bridge tool per combinator tool, bound to
    this agent's token + the daemon socket."""
    tools = []
    for short_name, target_cls in _BRIDGE_TARGETS.items():
        cls = _make_bridge_class(short_name, target_cls)
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
    token = os.environ.get("COMBINATOR_TOKEN", "")
    socket_path = os.environ.get("COMBINATOR_SOCKET", "")
    if not token or not socket_path:
        print(
            "combinator-mcp requires COMBINATOR_TOKEN and COMBINATOR_SOCKET "
            "env vars (set by ClaudeAgentEngine when it launches this "
            "subprocess).",
            file=sys.stderr,
        )
        return 2
    if not Path(socket_path).exists():
        print(
            f"combinator-mcp: control socket not found: {socket_path}",
            file=sys.stderr,
        )
        return 2

    tools = _build_bridge_tools(token=token, socket_path=socket_path)
    server = MCPServer(
        tools=tools,
        name="combinator",
        # Keep the original snake_case names; the SDK lists tools as
        # ``mcp__combinator__<name>`` so the prefix is already
        # distinctive.
        use_display_names=False,
    )
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
