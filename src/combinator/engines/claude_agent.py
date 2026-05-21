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
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

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
                # Phase 2 follow-up will surface an interactive prompt;
                # for now treat as a hard deny so the agent gets a
                # clear signal instead of hanging.
                return PermissionResultDeny(
                    message=f"{tool_name} requires approval"
                )
            return PermissionResultAllow()

        opts = ClaudeAgentOptions(
            system_prompt=system_prompt or self._build_system_prompt(record, runtime),
            cwd=str(sandbox_dir) if sandbox_dir is not None else None,
            allowed_tools=list(allowed_tools or []),
            can_use_tool=can_use_tool,
            # ``default`` consults can_use_tool for every tool call.
            permission_mode="default",
        )
        self._client = ClaudeSDKClient(opts)
        asyncio.run_coroutine_threadsafe(
            self._client.connect(), self._loop
        ).result(timeout=30)

    # ----- interface mirroring OrchestralEngine -----

    def step(self, prompt: str) -> str:
        future = asyncio.run_coroutine_threadsafe(
            self._step_async(prompt), self._loop
        )
        return future.result(timeout=600)

    def cost(self) -> float:
        return self._cost_used

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

        await self._client.query(prompt)
        accumulated = ""
        try:
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
