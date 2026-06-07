"""Compose a runtime from a ``Config``.

``build_runtime(config) -> (Runtime, root_addr)`` is the bridge between
declarative configuration (``spawn.config``) and the live runtime.

Engine dispatch: the per-agent ``spec.engine`` field selects which
engine builds that agent. ``orchestral`` (default) uses an LLM
client + spawn tool surface. ``claude_agent`` uses the
``claude-agent-sdk`` with Claude Code's tool surface, configured
against the agent's sandbox dir.
"""

from __future__ import annotations

from pathlib import Path

from typing import Callable, Any

from spawn.address import Address
from spawn.config import Config
from spawn.engines import resolve_engine_name
from spawn.engines.orchestral import make_orchestral_engine_factory
from spawn.llm import build_llm
from spawn.record import AgentRecord, AgentSpec
from spawn.runtime import Runtime


def build_runtime(
    config: Config,
    *,
    session_id: str | None = None,
    display_hook_builder: Callable[[AgentRecord], Callable[[Any], None]] | None = None,
    event_log_router: Callable[[AgentRecord], Any] | None = None,
    spawn_listener: Callable[[AgentRecord], None] | None = None,
    stream: bool = False,
    control_socket: "Path | None" = None,
) -> tuple[Runtime, Address]:
    """Build a ``Runtime`` and spawn the root agent per ``config``.

    Each spawned agent gets its own freshly-constructed engine. Engine
    selection is per-agent via ``spec.engine``.

    ``display_hook_builder`` (REPL path) — returns a console-rendering
    display hook per agent.

    ``event_log_router`` (tmux path) — returns an EventLog per agent;
    when present, engines emit events to the log instead of rendering
    directly.

    ``spawn_listener`` — called synchronously after every spawn (root
    or child); used by the tmux orchestrator to set up per-agent state.
    """
    # Capture each LLM config as a default arg so the lambda closes
    # over the right value (avoid the classic late-binding bug).
    llm_factories = {
        name: (lambda c=cfg.model_dump(): build_llm(c))
        for name, cfg in config.llms.items()
    }

    orchestral_factory = make_orchestral_engine_factory(
        llm_factories=llm_factories,
        display_hook_builder=display_hook_builder,
        event_log_router=event_log_router,
        stream=stream,
    )

    def dispatch(record: AgentRecord, runtime: Runtime):
        engine_name = resolve_engine_name(record.spec.engine)
        if engine_name == "orchestral":
            return orchestral_factory(record, runtime)
        if engine_name == "claude_agent":
            return _build_claude_agent_engine(
                record=record,
                runtime=runtime,
                event_log_router=event_log_router,
                control_socket=control_socket,
            )
        raise ValueError(
            f"unknown engine {engine_name!r} on agent {record.addr.id}; "
            f"supported: 'orchestral', 'claude_agent', 'auto'"
        )

    store_dir = Path(config.runtime.store_dir) if config.runtime.store_dir else None
    runtime = Runtime(
        store_dir=store_dir,
        session_id=session_id,
        engine_factory=dispatch,
        max_workers=config.runtime.max_workers,
        max_depth=config.runtime.max_depth,
        spawn_listener=spawn_listener,
    )

    root_spec = AgentSpec(
        role_prompt=config.root.role_prompt,
        engine=config.root.engine,
        llm=config.root.llm,
        tools=config.root.tools,
        label=config.root.label,
        sandbox_dir=config.root.sandbox_dir,
        permissions=config.root.permissions,
    )
    root_addr = runtime.root(root_spec)
    return runtime, root_addr


def _build_claude_agent_engine(
    *,
    record: AgentRecord,
    runtime: Runtime,
    event_log_router: Callable[[AgentRecord], Any] | None,
    control_socket: "Path | None",
):
    """Construct a ``ClaudeAgentEngine`` for this record. Resolves
    the agent's sandbox (same logic the FS tools use) and routes
    streaming chunks to the agent's event log if one is configured.
    Also passes the daemon's control socket so the engine can
    register the spawn-mcp bridge — giving the SDK access to
    spawn/send/recv/agent_map/... via MCP."""
    from spawn.engines.claude_agent import ClaudeAgentEngine
    from spawn.tools.filesystem import _sandbox_for

    stream_emit = None
    if event_log_router is not None:
        event_log = event_log_router(record)
        if event_log is not None:
            stream_emit = event_log.emit
    sandbox = _sandbox_for(record, runtime)
    model = _resolve_model(record)
    return ClaudeAgentEngine(
        record=record,
        runtime=runtime,
        sandbox_dir=sandbox,
        allowed_tools=record.spec.tools,
        stream_emit=stream_emit,
        mcp_socket=control_socket,
        model=model,
    )


def _resolve_model(record: AgentRecord) -> str | None:
    """Pick the model for this agent's claude_agent session.

    Resolution order:
    1. ``spec.model`` if the caller (user config or ``Spawn`` tool
       call) set it explicitly.
    2. ``"haiku"`` for any non-root agent — children default to a
       cheap model so the substrate doesn't burn Opus on every
       helper. Override with an explicit ``model=`` on ``Spawn``
       when the child genuinely needs more capability.
    3. ``None`` for the root, letting the ``claude`` CLI use its
       configured default (typically what the user is paying for).
    """
    if record.spec.model:
        return record.spec.model
    if record.parent is not None:
        return "haiku"
    return None
