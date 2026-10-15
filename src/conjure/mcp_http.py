"""Loopback streamable-HTTP MCP server: conjure's tools for CLI engines.

Engines that drive an external agent CLI (Codex) can't take an
in-process SDK server the way ``claude_agent`` does, and launching the
stdio ``conjure-mcp`` bridge would cost a Python start per turn plus a
daemon socket the REPL doesn't have. Instead one server per runtime
listens on ``127.0.0.1:<ephemeral>/mcp``, started on first use.

Each request authenticates with the calling agent's runtime token
(``Authorization: Bearer <token>``, which the CLI reads from an env var
so it never appears on a command line). What an agent sees is scoped
to it:

- conjure's orchestration tools (``Spawn``, ``Send``, ... and, with
  toolbase configured, ``ToolCatalog`` / ``GrantToolkits`` /
  ``RequestToolkits``), run as that agent: capability checks,
  journaling, everything as for in-process callers;
- its own toolbase toolkit tools, ``<toolkit>__<tool>``, from the
  ``ToolbaseMount`` its engine registered. A CLI starts a fresh MCP
  session per turn, so a live grant shows up on the next turn.

A token the runtime doesn't know gets an empty tool list and refused
calls. DNS-rebinding protection (Host / Origin checks) is on.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from conjure.runtime import Runtime
    from conjure.toolbase import ToolbaseMount


class McpHttpServer:
    """One per runtime; see the module docstring. Thread-safe."""

    def __init__(self, runtime: "Runtime", *, host: str = "127.0.0.1") -> None:
        self._runtime = runtime
        self._host = host
        self._mounts: dict[str, "ToolbaseMount"] = {}
        self._lock = threading.Lock()
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._url: str | None = None

    # ----- lifecycle -----

    @property
    def url(self) -> str:
        """The endpoint, starting the server on first use."""
        with self._lock:
            if self._url is None:
                self._start()
            assert self._url is not None
            return self._url

    def register_mount(self, token: str, mount: "ToolbaseMount | None") -> None:
        with self._lock:
            if mount is None:
                self._mounts.pop(token, None)
            else:
                self._mounts[token] = mount

    def close(self) -> None:
        with self._lock:
            server, thread = self._server, self._thread
            self._server = self._thread = None
            self._url = None
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=5)

    def _start(self) -> None:
        import uvicorn

        app = self._build_app()
        config = uvicorn.Config(
            app, host=self._host, port=0, log_level="warning", lifespan="on"
        )
        server = uvicorn.Server(config)
        started = threading.Event()

        def run() -> None:
            async def serve() -> None:
                task = asyncio.create_task(server.serve())
                while not server.started and not task.done():
                    await asyncio.sleep(0.02)
                started.set()
                await task

            asyncio.run(serve())

        thread = threading.Thread(target=run, daemon=True, name="conjure-mcp-http")
        thread.start()
        if not started.wait(10) or not server.started:
            server.should_exit = True
            raise RuntimeError("conjure MCP HTTP server failed to start")
        port = server.servers[0].sockets[0].getsockname()[1]
        self._server, self._thread = server, thread
        self._url = f"http://{self._host}:{port}/mcp"

    # ----- MCP -----

    def _build_app(self) -> Any:
        import mcp_types as types
        from mcp.server.lowlevel import Server

        from conjure.mcp_in_process import (
            _HIDDEN_FIELDS,
            _TOOL_TARGETS,
            _TOOLKIT_TARGETS,
            _build_input_schema,
        )

        conjure_tools: dict[str, type] = {}
        for name, cls in _TOOL_TARGETS:
            conjure_tools.setdefault(name, cls)
        if getattr(self._runtime, "toolbase", None) is not None:
            for name, cls in _TOOLKIT_TARGETS:
                conjure_tools[name] = cls
        conjure_specs = [
            types.Tool(
                name=name,
                description=(cls.__doc__ or name).strip(),
                input_schema=_build_input_schema(cls),
            )
            for name, cls in conjure_tools.items()
        ]

        def caller_token(ctx: Any) -> str | None:
            request = getattr(ctx, "request", None)
            header = request.headers.get("authorization", "") if request is not None else ""
            scheme, _, token = header.partition(" ")
            if scheme.lower() != "bearer" or not token:
                return None
            from conjure.tools._base import resolve_token

            return token if resolve_token(token) is not None else None

        def text_result(payload: Any, *, is_error: bool) -> Any:
            text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=text)], is_error=is_error
            )

        async def list_tools(ctx: Any, params: Any) -> Any:
            token = caller_token(ctx)
            if token is None:
                return types.ListToolsResult(tools=[])
            tools = list(conjure_specs)
            mount = self._mounts.get(token)
            for tool in mount.tools if mount is not None else []:
                spec = tool.get_tool_spec()
                tools.append(
                    types.Tool(
                        # The name calls are matched on (``<toolkit>__<tool>``).
                        name=tool.get_name(),
                        description=spec.description or spec.name,
                        input_schema=tool.get_input_schema() or {"type": "object", "properties": {}},
                    )
                )
            return types.ListToolsResult(tools=tools)

        async def call_tool(ctx: Any, params: Any) -> Any:
            token = caller_token(ctx)
            if token is None:
                return text_result(
                    {"ok": False, "code": "unauthorized", "error": "unknown agent token"},
                    is_error=True,
                )
            name = params.name
            args = dict(params.arguments or {})
            cls = conjure_tools.get(name)
            if cls is not None:
                clean = {k: v for k, v in args.items() if k not in _HIDDEN_FIELDS}
                try:
                    instance = cls(runtime_token=token, **clean)
                except Exception as exc:
                    return text_result(
                        {"ok": False, "code": "bad_args", "error": f"{type(exc).__name__}: {exc}"},
                        is_error=True,
                    )
                try:
                    result = await asyncio.to_thread(instance._run)  # noqa: SLF001
                except Exception as exc:
                    return text_result(
                        {"ok": False, "code": "exec_error", "error": f"{type(exc).__name__}: {exc}"},
                        is_error=True,
                    )
                if not isinstance(result, dict):
                    result = {"ok": True, "result": result}
                return text_result(result, is_error=not result.get("ok", True))
            mount = self._mounts.get(token)
            for tool in mount.tools if mount is not None else []:
                if tool.get_name() == name:
                    try:
                        result = await asyncio.to_thread(tool.execute, **args)
                    except Exception as exc:
                        return text_result(f"{type(exc).__name__}: {exc}", is_error=True)
                    return text_result(result, is_error=False)
            return text_result(
                {"ok": False, "code": "unknown_tool", "error": f"no tool {name!r} for this agent"},
                is_error=True,
            )

        server = Server(
            "conjure",
            instructions="conjure orchestration tools and this agent's toolbase toolkits.",
            on_list_tools=list_tools,
            on_call_tool=call_tool,
        )
        return server.streamable_http_app(host=self._host, stateless_http=True, json_response=True)


def mcp_http_for(runtime: "Runtime") -> McpHttpServer:
    """The runtime's server, created (not yet started) on first call."""
    server = getattr(runtime, "_mcp_http", None)
    if server is None:
        with _CREATE_LOCK:
            server = getattr(runtime, "_mcp_http", None)
            if server is None:
                server = McpHttpServer(runtime)
                runtime._mcp_http = server  # noqa: SLF001
    return server


_CREATE_LOCK = threading.Lock()
