"""Declarative configuration for ``conjure run``.

A YAML file (or any mapping) is loaded into a ``Config`` pydantic
model. The model declares the runtime, named LLMs, and the root
agent's spec.

Example YAML:

.. code-block:: yaml

    runtime:
      store_dir: ./.conjure/store
      max_workers: 32

    llms:
      default:
        provider: anthropic
        model: claude-sonnet-4-6
        api_key_env: ANTHROPIC_API_KEY

    root:
      role_prompt: |
        You are the root agent of this conjure session.
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
    # See ``conjure.engines.resolve_engine_name`` for ``"auto"``
    # semantics. Override with ``"orchestral"``, ``"claude_agent"`` or
    # ``"codex"`` to pin a specific surface.
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
    # Model for a claude_agent root — alias (``opus`` / ``sonnet`` /
    # ``haiku``) or full id. ``None`` uses the ``claude`` CLI's default,
    # which isn't known until the first reply names it.
    model: str | None = None
    # Toolbase toolkits the root itself is served, in ``tb activate``
    # grammar (``[heptapod/llp, calculator]``) or the mapping form.
    # Must fall within ``toolbase.authority``.
    toolkits: list[str] | dict[str, Any] | None = None
    # An existing toolbase loadout to serve the root, merged with
    # ``toolkits``.
    toolbase_loadout: str | None = None


class ToolbaseConfig(BaseModel):
    """``toolbase:`` — lets agents curate toolkits for the agents they
    spawn. Needs ``conjure-agents[toolbase]`` (Python >= 3.12)."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    # Project whose toolbase loadout supplies version pins and the
    # ``serve.yaml`` blocklists. ``None`` discovers from the cwd, like
    # ``tb serve``.
    project_root: str | None = None
    loadout: str | None = None
    # What the root may grant: ``loadout`` (default — the project's
    # active toolbase loadout, i.e. what you've ``tb activate``-d),
    # ``all`` (every installed toolkit; opt-in), or a toolset in the same
    # grammar as ``root.toolkits`` (e.g. ``[heptapod]``).
    authority: str | list[str] | dict[str, Any] = "loadout"
    call_timeout_s: float = 60.0
    # Start a child's toolkit servers when it's spawned, in the
    # background, so its first turn doesn't wait for them to boot.
    prestart: bool = True
    # Per-toolkit settings for this session only, layered over toolbase's
    # own config (user → project → private), e.g.
    # ``{heptapod: {mg5_path: /opt/MG5_aMC}}``. Unlocks config-gated
    # bundles without touching ~/.toolbase.
    config: dict[str, dict[str, Any]] = Field(default_factory=dict)


class CodexConfig(BaseModel):
    """``codex:`` — settings for agents on ``engine: codex`` (the Codex
    CLI, logged in with ``codex login``)."""

    model_config = ConfigDict(extra="forbid")
    # Codex's sandbox for the agent's own shell commands and edits:
    # ``workspace-write`` (writes confined to the agent's sandbox dir),
    # ``read-only``, or ``danger-full-access``.
    sandbox: Literal["read-only", "workspace-write", "danger-full-access"] = "workspace-write"
    # ``-m`` for children that don't name a model (the root uses
    # ``root.model``). ``None``: Codex's default.
    child_model: str | None = None
    reasoning_effort: str | None = None
    bin: str | None = None  # path to ``codex``; default: from PATH


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    llms: dict[str, LLMConfig]
    root: RootConfig
    toolbase: ToolbaseConfig | None = None
    codex: CodexConfig = Field(default_factory=CodexConfig)
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
