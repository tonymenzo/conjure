"""In-process MCP bridge for the claude_agent engine.

The ``combinator-mcp`` subprocess (see :mod:`combinator.mcp_bridge`) was
the v0.1 way to expose combinator's orchestration tools to a
``ClaudeSDKClient`` — every child engine launched a fresh Python
interpreter just to forward tool calls back to the daemon over a Unix
socket. For ``AgentMap`` over N items that's N × interpreter-start
seconds of warm-up before the first LLM token flows.

This module replaces the subprocess with an ``McpSdkServerConfig`` that
runs the tools directly in the engine's own process. Each engine builds
its own server (bound to its agent's ``runtime_token``) and hands it to
``ClaudeAgentOptions.mcp_servers``. Tool calls become a function call,
not an IPC round-trip.

Returns ``None`` from :func:`build_in_process_mcp_server` when the SDK
isn't new enough to expose ``create_sdk_mcp_server``; callers can fall
back to the stdio path in that case.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from combinator.tools.combinators import (
    AgentFilterTool,
    AgentFixedPointTool,
    AgentFoldTool,
    AgentMapTool,
)
from combinator.tools.primitives import (
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


# Display-name → tool-class map. The PascalCase name is what the LLM
# sees as ``mcp__combinator__<DisplayName>`` and matches the names the
# stdio bridge uses, so the two paths are interchangeable from the
# model's point of view.
_TOOL_TARGETS: list[tuple[str, type]] = [
    ("Spawn", SpawnTool),
    ("Send", SendTool),
    ("Recv", RecvTool),
    ("WaitFor", WaitForTool),
    ("Terminate", TerminateTool),
    ("Introduce", IntroduceTool),
    ("ListInbox", ListInboxTool),
    ("Peek", PeekTool),
    ("Call", CallTool),
    ("AgentMap", AgentMapTool),
    ("AgentFold", AgentFoldTool),
    ("AgentFilter", AgentFilterTool),
    ("AgentFixedPoint", AgentFixedPointTool),
]


# Fields present on every combinator tool that the LLM must NOT see —
# state fields (token plumbing) plus BaseTool's accounting hooks. Kept
# in one place so adding a new shared field doesn't silently leak into
# the schema.
_HIDDEN_FIELDS = frozenset({"runtime_token", "cost"})


def build_in_process_mcp_server(token: str) -> Any | None:
    """Return an ``McpSdkServerConfig`` (the SDK's in-process server
    handle) for combinator's orchestration tools, bound to ``token``.

    Returns ``None`` if the SDK doesn't expose ``create_sdk_mcp_server``
    — the caller should fall back to the stdio bridge in that case.
    """
    try:
        from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server
    except ImportError:
        return None

    tools: list[Any] = []
    for display_name, tool_cls in _TOOL_TARGETS:
        schema = _build_input_schema(tool_cls)
        description = (tool_cls.__doc__ or display_name).strip()
        handler = _make_handler(tool_cls, token)
        tools.append(
            SdkMcpTool(
                name=display_name,
                description=description,
                input_schema=schema,
                handler=handler,
            )
        )
    return create_sdk_mcp_server(name="combinator", tools=tools)


def _build_input_schema(tool_cls: type) -> dict[str, Any]:
    """Derive the MCP input schema from a combinator tool's pydantic
    fields. Strips state / accounting fields the LLM shouldn't see and
    flattens to a clean JSON Schema."""
    full = tool_cls.model_json_schema()
    properties = {
        name: spec
        for name, spec in (full.get("properties") or {}).items()
        if name not in _HIDDEN_FIELDS
    }
    required = [
        name
        for name in (full.get("required") or [])
        if name not in _HIDDEN_FIELDS
    ]
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


def _make_handler(tool_cls: type, token: str):
    """Build the async handler the SDK invokes on each tool call.

    The combinator tool implementations are synchronous (mailbox ops,
    capability checks, runtime mutations) so we hop to a worker thread
    via ``asyncio.to_thread`` — keeps the engine's shared event loop
    free to service other engines' SDK traffic concurrently.
    """

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        clean = {k: v for k, v in (args or {}).items() if k not in _HIDDEN_FIELDS}
        try:
            instance = tool_cls(runtime_token=token, **clean)
        except Exception as exc:
            payload = {
                "ok": False,
                "code": "bad_args",
                "error": f"{type(exc).__name__}: {exc}",
            }
            return _content(payload, is_error=True)
        try:
            result = await asyncio.to_thread(instance._run)  # noqa: SLF001
        except Exception as exc:
            payload = {
                "ok": False,
                "code": "exec_error",
                "error": f"{type(exc).__name__}: {exc}",
            }
            return _content(payload, is_error=True)
        if not isinstance(result, dict):
            result = {"ok": True, "result": result}
        return _content(result, is_error=not result.get("ok", True))

    return handler


def _content(payload: dict[str, Any], *, is_error: bool) -> dict[str, Any]:
    """Wrap a combinator tool's dict result as an MCP tool-call response."""
    text = json.dumps(payload, default=str)
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        out["is_error"] = True
    return out
