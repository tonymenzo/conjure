"""``ClaudeAgentEngine`` — alternate engine using ``claude-agent-sdk``.

Each agent that picks ``engine: "claude_agent"`` in its spec gets a
``ClaudeSDKClient`` of its own, configured with:

- ``cwd`` set to the agent's resolved sandbox directory (same path
  the filesystem tools use).
- ``allowed_tools`` derived from ``spec.tools`` if those are claude-
  code-style tool names; otherwise the SDK's default tool set.
- ``can_use_tool`` callback bridging to ``spec.permissions`` —
  ``deny`` / ``ask`` decisions map to ``PermissionResultDeny`` with
  a clear message; everything else allows the call.
- ``system_prompt`` from the agent's role prompt augmented with the
  same identity + reply-protocol framing the orchestral engine uses.

Sync-from-async bridge: the SDK is async, but combinator's driver
calls ``engine.step`` synchronously. The engine owns a dedicated
event loop running on a daemon thread; ``step`` posts coroutines to
that loop via ``run_coroutine_threadsafe`` and waits for the result.
The client persists across ``step`` calls so the conversation
context is preserved.

The class is intentionally narrow: it mirrors ``OrchestralEngine``'s
interface (``step`` returns the assistant text; ``cost`` returns
USD spent) so the runtime / driver don't need to know which engine
is underneath.
"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

from combinator.llm import api_key_present
from combinator.runtime import PERMISSION_WAIT_S

if TYPE_CHECKING:
    from combinator.record import AgentRecord
    from combinator.runtime import Runtime


_DEFAULT_SYSTEM_FRAME = """You are an agent in the Combinator multi-agent framework.

Your identity:
- Address id:  {addr_id}
- Label:       {label}
- Depth:       {depth} (root is depth 0; max allowed is {max_depth})

Your role:
{role_prompt}

You have access to filesystem tools (Read, Write, Edit, Bash, Grep,
Glob) operating inside your sandbox directory. Be terse and direct.
Once you've completed the task you were asked, STOP — don't send
acknowledgements or status updates."""


class ClaudeAgentEngine:
    """Engine that runs each agent as a ``ClaudeSDKClient`` session."""

    def __init__(
        self,
        *,
        record: "AgentRecord",
        runtime: "Runtime",
        sandbox_dir: Path | None,
        allowed_tools: Sequence[str] | None = None,
        system_prompt: str | None = None,
        stream_emit: Callable[[dict[str, Any]], None] | None = None,
        mcp_socket: Path | None = None,
    ) -> None:
        # Import lazily so the runtime doesn't hard-require
        # claude-agent-sdk for agents that only use the orchestral
        # engine.
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
        from claude_agent_sdk.types import (
            PermissionResultAllow,
            PermissionResultDeny,
        )

        self._record = record
        self._runtime = runtime
        self._stream_emit = stream_emit
        self._cost_used: float = 0.0
        self._uses_subscription: bool = _detect_subscription()

        # Dedicated event loop on a daemon thread so ``step`` (sync)
        # can submit coroutines via ``run_coroutine_threadsafe``.
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name=f"claude-agent-loop-{record.addr.id}",
        )
        self._loop_thread.start()

        async def can_use_tool(tool_name, args, ctx):  # type: ignore[no-untyped-def]
            decision = (record.spec.permissions or {}).get(tool_name, "allow")
            if decision == "deny":
                return PermissionResultDeny(
                    message=f"{tool_name} denied by agent permissions"
                )
            if decision == "ask":
                # Submit a request to the runtime's shared permission
                # queue; the UI banner picks it up. Block this async
                # callback on the underlying Event by running the
                # blocking ``wait`` in a worker thread so we don't
                # stall the event loop.
                req = runtime.submit_permission_request(
                    addr=record.addr,
                    tool_name=tool_name,
                    args=dict(args) if isinstance(args, dict) else {},
                )
                previous = record.status
                record.status = "awaiting_permission"
                try:
                    result = await asyncio.to_thread(
                        req.wait, PERMISSION_WAIT_S
                    )
                finally:
                    record.status = previous
                if result == "allow":
                    return PermissionResultAllow()
                if result == "timeout":
                    runtime._discard_permission(req.req_id)  # noqa: SLF001
                    return PermissionResultDeny(
                        message=f"{tool_name} approval timed out"
                    )
                return PermissionResultDeny(
                    message=f"{tool_name} denied by user"
                )
            return PermissionResultAllow()

        # MCP bridge: expose combinator's orchestration surface
        # (spawn, send, recv, agent_map, ...) to claude_agent. We
        # launch ``combinator-mcp`` as a stdio MCP subprocess; it
        # forwards calls back to the daemon's control socket. The
        # caller passes the daemon's socket path; if absent, the
        # SDK still works but only sees Claude Code's built-in tools.
        mcp_servers: dict[str, Any] = {}
        bridged_tools: list[str] = []
        if mcp_socket is not None:
            import shutil

            from claude_agent_sdk.types import McpStdioServerConfig
            mcp_bin = shutil.which("combinator-mcp") or "combinator-mcp"
            mcp_servers["combinator"] = McpStdioServerConfig(
                command=mcp_bin,
                env={
                    "COMBINATOR_TOKEN": record.token,
                    "COMBINATOR_SOCKET": str(mcp_socket),
                    # Propagate PATH so the subprocess can find python
                    # / mcp lib if it shells out.
                    "PATH": os.environ.get("PATH", ""),
                },
            )
            # Whitelist every bridged tool with the SDK-mandated
            # mcp__<server>__<name> prefix.
            for short in (
                "spawn", "send", "recv", "wait_for",
                "terminate", "introduce", "list_inbox",
                "agent_map", "agent_fold", "agent_filter",
                "agent_fixed_point",
            ):
                bridged_tools.append(f"mcp__combinator__{short}")

        opts = ClaudeAgentOptions(
            system_prompt=system_prompt or self._build_system_prompt(record, runtime),
            cwd=str(sandbox_dir) if sandbox_dir is not None else None,
            allowed_tools=list(allowed_tools or []) + bridged_tools,
            can_use_tool=can_use_tool,
            # ``default`` consults can_use_tool for every tool call.
            permission_mode="default",
            mcp_servers=mcp_servers if mcp_servers else {},
        )
        self._options = opts
        self._client = ClaudeSDKClient(opts)
        self._last_context: tuple[int, int] | None = None
        self._context_fetch_in_flight: bool = False
        try:
            asyncio.run_coroutine_threadsafe(
                self._client.connect(), self._loop
            ).result(timeout=30)
        except BaseException:
            # Without this the daemon loop thread leaks for the
            # lifetime of the process.
            self.shutdown()
            raise

    # ----- interface mirroring OrchestralEngine -----

    def step(self, prompt: str) -> str:
        future = asyncio.run_coroutine_threadsafe(
            self._step_async(prompt), self._loop
        )
        return future.result(timeout=600)

    def cost(self) -> float:
        # Subscription sessions are flat-rate — don't bill them.
        if self._uses_subscription:
            return 0.0
        return self._cost_used

    def model_name(self) -> str | None:
        """Best-effort model identifier from the SDK options. Falls
        back to ``None`` when the SDK uses its CLI default."""
        return getattr(self._options, "model", None) if hasattr(self, "_options") else None

    def context_usage(self) -> tuple[int, int] | None:
        """Non-blocking context fetch. Returns the cached value
        immediately and schedules a background refresh on the engine's
        own event loop. The UI's 500ms tick is never blocked waiting
        for the SDK to respond, even when the SDK is mid-turn or
        rate-limited.

        First-ever call returns ``None`` until the background fetch
        completes; subsequent calls return progressively-fresh
        readings."""
        if not self._context_fetch_in_flight:
            self._context_fetch_in_flight = True
            try:
                asyncio.run_coroutine_threadsafe(
                    self._refresh_context_async(), self._loop
                )
            except Exception:
                self._context_fetch_in_flight = False
        return self._last_context

    async def _refresh_context_async(self) -> None:
        try:
            usage = await self._client.get_context_usage()
        except Exception:
            self._context_fetch_in_flight = False
            return
        used = (
            getattr(usage, "tokens_used", None)
            or getattr(usage, "input_tokens", None)
            or getattr(usage, "used_tokens", None)
        )
        total = (
            getattr(usage, "tokens_max", None)
            or getattr(usage, "context_window", None)
            or getattr(usage, "max_tokens", None)
            or 200_000
        )
        try:
            if used is not None:
                self._last_context = (int(used), int(total))
        except (TypeError, ValueError):
            pass
        finally:
            self._context_fetch_in_flight = False

    def uses_subscription(self) -> bool:
        """Whether the underlying ``claude`` CLI is logged into a
        Max/Pro subscription (vs an API key). The SDK delegates to
        whatever auth the CLI is configured with, but it can't tell us
        which; we infer from the absence of ``ANTHROPIC_API_KEY`` in
        the environment (API-key auth typically requires it). Honest
        but not authoritative — for billing-critical paths, run
        ``claude /status`` and check the user's plan."""
        return self._uses_subscription

    def shutdown(self) -> None:
        """Disconnect the client and stop the event loop. Safe to call
        multiple times — combinator's terminate path may call this
        more than once."""
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._client.disconnect(), self._loop
            )
            fut.result(timeout=5)
        except Exception:
            pass
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass

    # ----- internals -----

    async def _step_async(self, prompt: str) -> str:
        from claude_agent_sdk.types import AssistantMessage, ResultMessage

        accumulated = ""
        try:
            await self._client.query(prompt)
            async for msg in self._client.receive_response():
                text = _extract_text(msg)
                if text:
                    self._emit_chunk(text)
                    accumulated += text
                # ResultMessage carries the per-turn cost.
                if isinstance(msg, ResultMessage):
                    cost = getattr(msg, "total_cost_usd", None)
                    if cost is not None:
                        try:
                            self._cost_used += float(cost)
                        except (TypeError, ValueError):
                            pass
        finally:
            # Emit stream_end so the chat pane finalizes the streaming
            # bubble even when the SDK raises mid-turn. The driver
            # picks up the exception and emits the error event.
            self._emit_stream_end()
        return accumulated

    def _emit_chunk(self, text: str) -> None:
        if self._stream_emit is None or not text:
            return
        try:
            self._stream_emit({"kind": "chunk", "text": text})
        except Exception:
            pass

    def _emit_stream_end(self) -> None:
        if self._stream_emit is None:
            return
        try:
            self._stream_emit({"kind": "stream_end", "tool_calls": []})
        except Exception:
            pass

    @staticmethod
    def _build_system_prompt(record: "AgentRecord", runtime: "Runtime") -> str:
        return _DEFAULT_SYSTEM_FRAME.format(
            addr_id=record.addr.id,
            label=record.addr.label or "(none)",
            role_prompt=record.spec.role_prompt,
            depth=record.depth,
            max_depth=getattr(runtime, "max_depth", 3),
        )


def _detect_subscription() -> bool:
    # A ``claude`` CLI without an Anthropic API key in env is almost
    # certainly running on a Max/Pro subscription. With the key set,
    # the CLI may be using API-key auth — err on the side of "not
    # subscription" so ``cost()`` surfaces the real number.
    return not api_key_present("anthropic")


def _extract_text(msg: Any) -> str:
    """Pull text content out of an SDK message.

    AssistantMessage carries a list of content blocks; TextBlock has
    a ``text`` attribute. Other block kinds (ToolUseBlock, etc.) are
    ignored here — the SDK itself runs the tool and surfaces results
    as separate messages."""
    content = getattr(msg, "content", None)
    if content is None:
        return ""
    parts: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)
