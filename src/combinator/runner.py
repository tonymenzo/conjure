"""Compose a runtime from a ``Config``.

``build_runtime(config) -> (Runtime, root_addr)`` is the bridge between
declarative configuration (``combinator.config``) and the live runtime.
"""

from __future__ import annotations

from pathlib import Path

from combinator.address import Address
from combinator.config import Config
from combinator.engines.orchestral import make_orchestral_engine_factory
from combinator.llm import build_llms
from combinator.record import AgentSpec
from combinator.runtime import Runtime


def build_runtime(config: Config) -> tuple[Runtime, Address]:
    """Build a ``Runtime`` and spawn the root agent per ``config``."""
    llms = build_llms({name: c.model_dump() for name, c in config.llms.items()})

    if config.root.engine != "orchestral":
        raise NotImplementedError(
            f"engine {config.root.engine!r} not yet implemented "
            "(only 'orchestral' is supported in v0.1)"
        )

    engine_factory = make_orchestral_engine_factory(llms=llms)

    store_dir = Path(config.runtime.store_dir) if config.runtime.store_dir else None
    runtime = Runtime(
        store_dir=store_dir,
        engine_factory=engine_factory,
        max_workers=config.runtime.max_workers,
    )

    root_spec = AgentSpec(
        role_prompt=config.root.role_prompt,
        llm=config.root.llm,
        tools=config.root.tools,
        label=config.root.label,
    )
    root_addr = runtime.root(root_spec)
    return runtime, root_addr
