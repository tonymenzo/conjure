"""Compose a runtime from a ``Config``.

``build_runtime(config) -> (Runtime, root_addr)`` is the bridge between
declarative configuration (``combinator.config``) and the live runtime.

Engine dispatch: the per-agent ``spec.engine`` field selects which
engine builds that agent. ``orchestral`` (default) uses an LLM
client + combinator tool surface. ``claude_agent`` uses the
``claude-agent-sdk`` with Claude Code's tool surface, configured
against the agent's sandbox dir.
"""

from __future__ import annotations

from pathlib import Path

from typing import Callable, Any

from combinator.address import Address
from combinator.config import Config
from combinator.engines.orchestral import make_orchestral_engine_factory
from combinator.llm import build_llm
from combinator.record import AgentRecord, AgentSpec
from combinator.runtime import Runtime


def build_runtime(
    config: Config,
    *,
    display_hook_builder: Callable[[AgentRecord], Callable[[Any], None]] | None = None,
    event_log_router: Callable[[AgentRecord], Any] | None = None,
    spawn_listener: Callable[[AgentRecord], None] | None = None,
    stream: bool = False,
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
        engine_name = record.spec.engine or "orchestral"
        if engine_name == "orchestral":
            return orchestral_factory(record, runtime)
        if engine_name == "claude_agent":
            return _build_claude_agent_engine(
                record=record,
                runtime=runtime,
                event_log_router=event_log_router,
            )
        raise ValueError(
            f"unknown engine {engine_name!r} on agent {record.addr.id}; "
            f"supported: 'orchestral', 'claude_agent'"
        )

    store_dir = Path(config.runtime.store_dir) if config.runtime.store_dir else None
    runtime = Runtime(
        store_dir=store_dir,
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
):
    """Construct a ``ClaudeAgentEngine`` for this record. Resolves
    the agent's sandbox (same logic the FS tools use) and routes
    streaming chunks to the agent's event log if one is configured."""
    from combinator.engines.claude_agent import ClaudeAgentEngine
    from combinator.tools.filesystem import _sandbox_for

    stream_emit = None
    if event_log_router is not None:
        event_log = event_log_router(record)
        if event_log is not None:
            stream_emit = event_log.emit
    sandbox = _sandbox_for(record, runtime)
    return ClaudeAgentEngine(
        record=record,
        runtime=runtime,
        sandbox_dir=sandbox,
        allowed_tools=record.spec.tools,
        stream_emit=stream_emit,
    )
