"""Compose a runtime from a ``Config``.

``build_runtime(config) -> (Runtime, root_addr)`` is the bridge between
declarative configuration (``combinator.config``) and the live runtime.
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
) -> tuple[Runtime, Address]:
    """Build a ``Runtime`` and spawn the root agent per ``config``.

    Each spawned agent gets its own freshly-constructed LLM client —
    orchestral mutates the client's tool router on Agent construction,
    so sharing clients across agents cross-wires their tools.

    ``display_hook_builder`` (REPL path) — returns a console-rendering
    display hook per agent.

    ``event_log_router`` (tmux path) — returns an EventLog per agent;
    when present, the engine emits events to the log instead of
    rendering directly.

    ``spawn_listener`` — called synchronously after every spawn (root
    or child); used by the tmux orchestrator to set up an event log
    and a tmux window for the newly-created agent.
    """
    # Capture each LLM config as a default arg so the lambda closes
    # over the right value (avoid the classic late-binding bug).
    llm_factories = {
        name: (lambda c=cfg.model_dump(): build_llm(c))
        for name, cfg in config.llms.items()
    }

    if config.root.engine != "orchestral":
        raise NotImplementedError(
            f"engine {config.root.engine!r} not yet implemented "
            "(only 'orchestral' is supported in v0.1)"
        )

    engine_factory = make_orchestral_engine_factory(
        llm_factories=llm_factories,
        display_hook_builder=display_hook_builder,
        event_log_router=event_log_router,
    )

    store_dir = Path(config.runtime.store_dir) if config.runtime.store_dir else None
    runtime = Runtime(
        store_dir=store_dir,
        engine_factory=engine_factory,
        max_workers=config.runtime.max_workers,
        spawn_listener=spawn_listener,
    )

    root_spec = AgentSpec(
        role_prompt=config.root.role_prompt,
        llm=config.root.llm,
        tools=config.root.tools,
        label=config.root.label,
    )
    root_addr = runtime.root(root_spec)
    return runtime, root_addr
