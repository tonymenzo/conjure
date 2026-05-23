"""Declarative configuration for ``combinator run``.

A YAML file (or any mapping) is loaded into a ``Config`` pydantic
model. The model declares the runtime, named LLMs, and the root
agent's spec.

Example YAML:

.. code-block:: yaml

    runtime:
      store_dir: ./.combinator/store
      max_workers: 32

    llms:
      default:
        provider: anthropic
        model: claude-sonnet-4-6
        api_key_env: ANTHROPIC_API_KEY

    root:
      role_prompt: |
        You are the root agent of this combinator session.
      engine: orchestral
      llm: default
      tools: [primitive, combinator]
      label: root

    mode: repl
    # initial_task: "your task here"   # one-shot mode only
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    store_dir: str | None = None
    max_workers: int = 32
    # Hard cap on the spawn-tree depth. The root agent is depth 0;
    # children are depth 1, etc. ``max_depth = 3`` allows root + two
    # generations of descendants and rejects further spawns with a
    # ``depth_exceeded`` error so the agent's LLM can fall back to
    # answering directly. Tune up for genuinely deep workflows.
    max_depth: int = 3


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    model: str | None = None
    api_key_env: str | None = None


class RootConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role_prompt: str
    # See ``combinator.engines.resolve_engine_name`` for ``"auto"``
    # semantics. Override with ``"orchestral"`` or ``"claude_agent"``
    # to pin a specific surface.
    engine: str = "auto"
    llm: str = "default"
    tools: list[str] = Field(default_factory=lambda: ["primitive", "combinator"])
    label: str = "root"
    # Filesystem sandbox path (used by the filesystem tool group and
    # by ``engine: claude_agent``). ``None`` auto-allocates under
    # ``{runtime.store_dir}/sandboxes/{agent_id}/``.
    sandbox_dir: str | None = None
    # Per-tool permission decisions, e.g. ``{Bash: ask, Write: allow}``.
    # ``ask`` blocks the tool until the UI banner resolves it.
    permissions: dict[str, str] = Field(default_factory=dict)


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    llms: dict[str, LLMConfig]
    root: RootConfig
    mode: Literal["repl", "one-shot"] = "repl"
    initial_task: str | None = None


def load_config(path: str | Path) -> Config:
    """Load a YAML config file into a ``Config`` model."""
    import yaml

    text = Path(path).read_text(encoding="utf-8")
    raw = yaml.safe_load(text) or {}
    return Config.model_validate(raw)


def load_config_from_mapping(data: dict[str, Any]) -> Config:
    """Build a ``Config`` from an already-parsed dict (skip YAML)."""
    return Config.model_validate(data)
