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
import functools
import json
import os
import shutil
import string
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

from combinator.llm import api_key_present
from combinator.runtime import PERMISSION_WAIT_S

if TYPE_CHECKING:
    from combinator.record import AgentRecord
    from combinator.runtime import Runtime


_SYSTEM_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "system_prompts" / "claude_agent.md"
)


# Tools whose side effects can mutate the user's environment — file
# writes, shell commands, system-state-changing operations. Default
# permission is ``ask`` so the perm-banner surfaces them; explicit
# per-agent ``spec.permissions[tool_name]`` always wins. Read-only
# tools (Read, Glob, Grep, NotebookRead, WebFetch, WebSearch, etc.)
# stay default-allow so navigation isn't interrupted.
_ASK_BY_DEFAULT: frozenset[str] = frozenset({
    "Bash",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Write",
})


@functools.cache
def _load_default_system_template() -> string.Template:
    """Compile the templated system prompt from
    ``system_prompts/claude_agent.md`` into a ``string.Template``.

    Cached so we don't re-read the file or recompile the template on
    every spawn — once at process start, all subsequent calls are an
    O(1) dict lookup. The template uses ``$name`` / ``${name}`` syntax
    (which doesn't collide with the curly braces in the prompt's JSON
    examples the way ``str.format`` would). Falls back to a minimal
    inline frame if the file is missing (shouldn't happen in a proper
    install, but keeps the engine robust to packaging accidents)."""
    try:
        text = _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    except OSError:
        text = (
            "You are an agent in the Combinator multi-agent framework.\n\n"
            "Identity: address $addr_id, label $label, depth $depth "
            "(max $max_depth).\n\nRole: $role_prompt"
        )
    return string.Template(text)


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
        model: str | None = None,
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
        # Actual model the SDK reports on the last AssistantMessage —
        # populated lazily from the response stream so we surface the
        # *real* id (e.g. ``claude-haiku-4-5-20251001``) rather than
        # the alias (``"haiku"``) or ``None`` (when the root falls
        # through to the CLI default).
        self._observed_model: str | None = None

        # Shared sync-from-async bridge: every claude_agent engine in
        # the runtime posts onto the same event loop. Cuts N children's
        # OS-thread + event-loop overhead down to one of each; uvloop
        # picks itself up via the runtime's loop factory when present.
        self._loop = runtime.get_shared_async_loop()

        async def can_use_tool(tool_name, args, ctx):  # type: ignore[no-untyped-def]
            # Tools whose side effects can change the user's environment
            # (filesystem writes, shell commands) default to ``ask`` so
            # the perm-banner surfaces them — anyone who configured an
            # explicit decision per agent in ``spec.permissions``
            # overrides this. Read-only tools (Read, Glob, Grep, ...)
            # keep the default-allow.
            explicit = (record.spec.permissions or {}).get(tool_name)
            decision = explicit or (
                "ask" if tool_name in _ASK_BY_DEFAULT else "allow"
            )
            if decision == "deny":
                return PermissionResultDeny(
                    message=f"{tool_name} denied by agent permissions"
                )
            # Auto-mode: ``ask`` decisions become silent allows. Deny
            # is still honored — auto-mode opens gates, never overrides
            # an explicit deny.
            if decision == "ask" and runtime.auto_mode:
                return PermissionResultAllow()
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
        # (spawn, send, recv, agent_map, ...) to claude_agent.
        #
        # Preferred path is the in-process SDK MCP server — tools run
        # as direct Python calls inside this engine's own process,
        # which eliminates the per-child ``combinator-mcp`` subprocess
        # startup (200–500ms cold) and the daemon socket round-trip.
        # For an N-way fan-out that's the difference between several
        # seconds of warmup and milliseconds.
        #
        # Stdio subprocess remains the fallback for when the installed
        # SDK predates ``create_sdk_mcp_server`` or when no in-process
        # server can be constructed for any other reason — pinning the
        # MCP bridge to the daemon socket still works.
        mcp_servers: dict[str, Any] = {}
        bridged_tools: list[str] = []
        in_process_server = None
        if os.environ.get("COMBINATOR_MCP_INPROC", "1") != "0":
            try:
                from combinator.mcp_in_process import (
                    build_in_process_mcp_server,
                )

                in_process_server = build_in_process_mcp_server(record.token)
            except Exception:
                in_process_server = None
        if in_process_server is not None:
            mcp_servers["combinator"] = in_process_server
        elif mcp_socket is not None:
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
        if mcp_servers:
            # Whitelist every bridged tool with the SDK-mandated
            # mcp__<server>__<name> prefix. Names are PascalCase
            # because both bridges expose them in PascalCase to match
            # Claude Code's built-in tool naming convention.
            for short in (
                "Spawn", "Send", "Recv", "WaitFor",
                "Terminate", "Introduce", "ListInbox",
                "Peek", "Call",
                "AgentMap", "AgentFold", "AgentFilter",
                "AgentFixedPoint",
            ):
                bridged_tools.append(f"mcp__combinator__{short}")

        # When the user is on a subscription, shadow ANTHROPIC_API_KEY
        # in the subprocess env so the CLI doesn't flip to per-token
        # API billing. Without this the env var (often loaded from
        # ~/.config/combinator/.env for the orchestral engine) leaks
        # through and the CLI prefers it over the OAuth subscription.
        sdk_env: dict[str, str] = {}
        if self._uses_subscription and os.environ.get("ANTHROPIC_API_KEY"):
            sdk_env["ANTHROPIC_API_KEY"] = ""

        opts = ClaudeAgentOptions(
            system_prompt=system_prompt or self._build_system_prompt(record, runtime),
            cwd=str(sandbox_dir) if sandbox_dir is not None else None,
            allowed_tools=list(allowed_tools or []) + bridged_tools,
            can_use_tool=can_use_tool,
            # ``default`` consults can_use_tool for every tool call.
            permission_mode="default",
            mcp_servers=mcp_servers if mcp_servers else {},
            env=sdk_env,
            model=model,
        )
        self._options = opts
        self._client = ClaudeSDKClient(opts)
        self._last_context: tuple[int, int] | None = None
        self._context_fetch_in_flight: bool = False
        # Lazy connect: ``_client.connect()`` shells out to the ``claude``
        # CLI and can take 3–30s. Deferring it to the first ``step``
        # means ``Spawn`` over MCP returns immediately, multiple spawns
        # connect concurrently (each child on its own first turn), and
        # the parent agent isn't held hostage by child CLI startup.
        # Single-step-at-a-time is enforced by the driver, so a bare
        # bool here is enough — no lock needed.
        self._connected = False

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
        """Best-effort model identifier. Prefers the value the SDK
        actually reports on its last ``AssistantMessage`` (the real
        model id, including version suffix) over the alias passed
        to the options (e.g. ``"haiku"``). Falls back to the option
        value, and finally ``None`` when neither is known."""
        observed = getattr(self, "_observed_model", None)
        if isinstance(observed, str) and observed:
            return observed
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
        # Skip the fetch entirely until the client has connected
        # (which now happens lazily on first step). No usage to read
        # before then.
        if not self._connected:
            return None
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
        # The SDK returns a ``TypedDict`` (i.e. a plain ``dict``), so
        # we read keys with ``.get()``. The older code used
        # ``getattr`` which always returned ``None`` on a dict — that
        # was the bug that kept the context bar empty.
        def _pick(*keys: str) -> Any:
            if isinstance(usage, dict):
                for k in keys:
                    v = usage.get(k)
                    if v is not None:
                        return v
                return None
            for k in keys:
                v = getattr(usage, k, None)
                if v is not None:
                    return v
            return None

        used = _pick("totalTokens", "tokens_used", "input_tokens", "used_tokens")
        total = _pick(
            "maxTokens", "tokens_max", "context_window", "max_tokens"
        ) or 200_000
        try:
            if used is not None:
                self._last_context = (int(used), int(total))
        except (TypeError, ValueError):
            pass
        finally:
            self._context_fetch_in_flight = False

    def uses_subscription(self) -> bool:
        """Whether the underlying ``claude`` CLI is logged into a
        Max/Pro subscription. Resolved at engine construction time via
        ``claude auth status``; falls back to the env-var heuristic
        (no ``ANTHROPIC_API_KEY`` ⇒ probably subscription) only when
        the CLI is unreachable."""
        return self._uses_subscription

    def shutdown(self) -> None:
        """Disconnect the SDK client (if it was ever connected). The
        event loop itself is owned by the runtime; we never stop it
        here. Safe to call multiple times — combinator's terminate
        path may call this more than once."""
        if not self._connected:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._client.disconnect(), self._loop
            )
            fut.result(timeout=5)
        except Exception:
            pass
        self._connected = False

    # ----- internals -----

    async def _step_async(self, prompt: str) -> str:
        from claude_agent_sdk.types import (
            AssistantMessage,
            ResultMessage,
            UserMessage,
        )

        if not self._connected:
            await self._client.connect()
            self._connected = True

        accumulated = ""
        pending_tool_calls: list[dict[str, Any]] = []
        stream_open = False  # text/tool_calls emitted since last stream_end
        # Powers the chat pane's "thinking…" status line. ``thinking_start``
        # carries the turn's start ts (so the widget can show elapsed
        # time); ``usage`` events update the cumulative token meter as
        # AssistantMessages stream in; ``thinking_end`` removes the
        # status widget when the turn finishes (success or failure).
        self._emit_event({"kind": "thinking_start"})
        # ``turn_tokens_out`` accumulates *output* tokens across all
        # AssistantMessages in this turn (each one is unique new
        # generation, so summing is meaningful). ``turn_tokens_in``
        # is intentionally *not* accumulated — Anthropic's API
        # echoes the whole conversation as input_tokens on each
        # subsequent call within a turn, so summing them double-
        # counts. The per-turn UI shows only output tokens; the
        # bottom gutter's context-window meter covers input.
        turn_tokens_out = 0
        try:
            await self._client.query(prompt)
            async for msg in self._client.receive_response():
                if isinstance(msg, AssistantMessage):
                    observed = getattr(msg, "model", None)
                    if isinstance(observed, str) and observed:
                        self._observed_model = observed
                    usage = getattr(msg, "usage", None) or {}
                    _, delta_out = _extract_usage(usage)
                    if delta_out:
                        turn_tokens_out += delta_out
                        self._emit_event(
                            {
                                "kind": "usage",
                                "tokens_in": 0,
                                "tokens_out": turn_tokens_out,
                            }
                        )
                    text = _extract_text(msg)
                    if text:
                        self._emit_chunk(text)
                        accumulated += text
                        stream_open = True
                    tool_calls = _extract_tool_calls(msg)
                    if tool_calls:
                        pending_tool_calls.extend(tool_calls)
                    if text or tool_calls:
                        # Close the response block: text on top, tool
                        # calls beneath (chat.py:_response_block).
                        self._emit_stream_end(pending_tool_calls)
                        pending_tool_calls = []
                        stream_open = False
                elif isinstance(msg, UserMessage):
                    for result in _extract_tool_results(msg):
                        self._emit_tool_result(result)
                elif isinstance(msg, ResultMessage):
                    cost = getattr(msg, "total_cost_usd", None)
                    if cost is not None:
                        try:
                            self._cost_used += float(cost)
                        except (TypeError, ValueError):
                            pass
        finally:
            # Close any block left dangling — exception mid-turn, or a
            # final assistant message we never saw the close of.
            if stream_open or pending_tool_calls:
                self._emit_stream_end(pending_tool_calls)
            self._emit_event({"kind": "thinking_end"})
        return accumulated

    def _emit_event(self, event: dict[str, Any]) -> None:
        if self._stream_emit is None:
            return
        try:
            self._stream_emit(event)
        except Exception:
            pass

    def _emit_chunk(self, text: str) -> None:
        if self._stream_emit is None or not text:
            return
        try:
            self._stream_emit({"kind": "chunk", "text": text})
        except Exception:
            pass

    def _emit_stream_end(self, tool_calls: list[dict[str, Any]] | None = None) -> None:
        if self._stream_emit is None:
            return
        try:
            self._stream_emit(
                {"kind": "stream_end", "tool_calls": list(tool_calls or [])}
            )
        except Exception:
            pass

    def _emit_tool_result(self, result: dict[str, Any]) -> None:
        if self._stream_emit is None:
            return
        try:
            self._stream_emit(
                {
                    "kind": "tool",
                    "text": result.get("text", ""),
                    "failed": bool(result.get("failed")),
                }
            )
        except Exception:
            pass

    @staticmethod
    def _build_system_prompt(record: "AgentRecord", runtime: "Runtime") -> str:
        template = _load_default_system_template()
        return template.safe_substitute(
            addr_id=record.addr.id,
            label=record.addr.label or "(none)",
            role_prompt=record.spec.role_prompt,
            depth=str(record.depth),
            max_depth=str(getattr(runtime, "max_depth", 3)),
        )


@functools.cache
def _detect_subscription() -> bool:
    # Authoritative probe: ``claude auth status`` reports the actual
    # auth mode the CLI is using (which is what the SDK delegates to),
    # not whatever ``ANTHROPIC_API_KEY`` happens to be in env. Cached
    # for the process lifetime because auth doesn't shift mid-run.
    info = _claude_auth_status()
    if info is not None:
        sub = info.get("subscriptionType")
        if isinstance(sub, str) and sub and sub.lower() != "none":
            return True
        if info.get("authMethod") == "claude.ai":
            return True
        return False
    # CLI couldn't tell us — fall back to the env-var heuristic.
    return not api_key_present("anthropic")


def _claude_auth_status() -> dict[str, Any] | None:
    """Return parsed JSON from ``claude auth status``, or ``None`` if
    the CLI is missing, hangs, or emits non-JSON."""
    claude_bin = shutil.which("claude")
    if claude_bin is None:
        return None
    try:
        proc = subprocess.run(
            [claude_bin, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        info = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return info if isinstance(info, dict) else None


def _extract_usage(usage: dict[str, Any] | None) -> tuple[int, int]:
    """``AssistantMessage.usage`` is a free-form dict; the SDK uses
    Anthropic-API key names. Read the standard slots, fall back to
    zeros when absent. Returns ``(input_tokens, output_tokens)`` —
    counts that have *just* been billed against this AssistantMessage
    (the caller accumulates across the turn)."""
    if not isinstance(usage, dict):
        return (0, 0)
    inp = usage.get("input_tokens", 0) or 0
    out = usage.get("output_tokens", 0) or 0
    # Cache reads, when present, also count as input tokens that the
    # model "saw" for this turn.
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    cache_create = usage.get("cache_creation_input_tokens", 0) or 0
    try:
        return (int(inp) + int(cache_read) + int(cache_create), int(out))
    except (TypeError, ValueError):
        return (0, 0)


def _extract_text(msg: Any) -> str:
    """Pull plain text out of an SDK message's content blocks.

    Walks the content list and concatenates ``TextBlock.text`` entries.
    Other block kinds (``ToolUseBlock``, ``ThinkingBlock``, etc.)
    contribute nothing — they're surfaced via dedicated extractors."""
    content = getattr(msg, "content", None)
    if content is None:
        return ""
    parts: list[str] = []
    for block in content:
        # ``ToolUseBlock`` also has a (callable) ``name`` attribute and
        # no ``text``; gate on the block's class name so we don't
        # accidentally pick up non-text blocks that happen to expose a
        # ``text`` attribute via duck typing.
        cls_name = type(block).__name__
        if cls_name not in ("TextBlock",):
            continue
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


_MCP_PREFIX = "mcp__combinator__"


def _extract_tool_calls(msg: Any) -> list[dict[str, Any]]:
    """Serialize each ``ToolUseBlock`` on ``msg`` for stream_end.

    The ``mcp__combinator__<name>`` prefix is stripped so the chat
    pane shows the user-meaningful name (``spawn``, ``send``, ...)
    rather than the full MCP-wire identifier."""
    out: list[dict[str, Any]] = []
    for block in getattr(msg, "content", None) or []:
        if type(block).__name__ not in ("ToolUseBlock", "ServerToolUseBlock"):
            continue
        name = getattr(block, "name", "") or "?"
        if name.startswith(_MCP_PREFIX):
            name = name[len(_MCP_PREFIX):]
        args = getattr(block, "input", None) or {}
        out.append({"name": name, "args": args})
    return out


def _extract_tool_results(msg: Any) -> list[dict[str, Any]]:
    """Serialize each ``ToolResultBlock`` on ``msg`` for ``tool``
    events. ``failed`` reflects ``is_error``; ``text`` is the result
    body coerced to a string (the chat pane's ``_summarize_tool_result``
    parses ``{"ok": False, ...}`` shapes back into a code:reason
    summary)."""
    out: list[dict[str, Any]] = []
    content = getattr(msg, "content", None)
    if not isinstance(content, list):
        return out
    for block in content:
        if type(block).__name__ not in ("ToolResultBlock", "ServerToolResultBlock"):
            continue
        body = getattr(block, "content", None)
        text = _stringify_tool_result(body)
        out.append(
            {
                "text": text,
                "failed": bool(getattr(block, "is_error", False)),
            }
        )
    return out


def _stringify_tool_result(body: Any) -> str:
    """Coerce a ``ToolResultBlock.content`` value to a chat-renderable
    string. The SDK delivers either a bare string or a list of
    content-block dicts (with ``"type": "text"`` etc.)."""
    if body is None:
        return ""
    if isinstance(body, str):
        return body
    if isinstance(body, list):
        parts: list[str] = []
        for entry in body:
            if isinstance(entry, dict) and entry.get("type") == "text":
                t = entry.get("text", "")
                if isinstance(t, str):
                    parts.append(t)
            elif isinstance(entry, str):
                parts.append(entry)
        return "".join(parts)
    return str(body)
