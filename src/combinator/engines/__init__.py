"""Engine adapters — concrete ``Engine`` implementations.

The driver loop in ``combinator.driver`` interacts only with the
``Engine`` protocol; this package houses the production engines:

- ``orchestral`` — wraps ``orchestral.Agent`` for direct LLM use.
- (future) ``claude_code``, ``codex`` — subprocess-driven engines that
  put each agent in its own tmux pane.
"""

from combinator.engines.orchestral import OrchestralEngine, make_orchestral_engine_factory
from combinator.engines.registry import (
    DEFAULT_TOOL_GROUPS,
    ToolGroupRegistry,
    build_tools,
)

__all__ = [
    "OrchestralEngine",
    "make_orchestral_engine_factory",
    "build_tools",
    "ToolGroupRegistry",
    "DEFAULT_TOOL_GROUPS",
]
