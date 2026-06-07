"""Engine adapters — concrete ``Engine`` implementations.

The driver loop in ``spawn.driver`` interacts only with the
``Engine`` protocol; this package houses the production engines:

- ``orchestral`` — wraps ``orchestral.Agent`` for direct LLM use.
- ``claude_agent`` — runs each agent under ``claude-agent-sdk`` so
  it can talk to a logged-in ``claude`` CLI (subscription or API key).

The ``"auto"`` engine name (the default on root + spawn specs) is
resolved at engine-build time to ``claude_agent`` when both the SDK
and the ``claude`` CLI are available, else to ``orchestral``. Prefer
the subscription when the user has one wired up; charge the API key
only when they don't.
"""

from __future__ import annotations

import functools
import importlib.util
import shutil

from spawn.engines.orchestral import OrchestralEngine, make_orchestral_engine_factory
from spawn.engines.registry import (
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
    "resolve_engine_name",
]


@functools.cache
def _claude_agent_available() -> bool:
    # Cheap PATH check first; only walk sys.path for the SDK if the
    # CLI is present. ``find_spec`` can raise on partially-installed
    # namespace packages — treat any failure as "not available."
    if shutil.which("claude") is None:
        return False
    try:
        return importlib.util.find_spec("claude_agent_sdk") is not None
    except (ImportError, ValueError):
        return False


def resolve_engine_name(name: str | None) -> str:
    """Map ``"auto"`` (or empty) to a concrete engine name.

    When both the ``claude`` CLI and ``claude_agent_sdk`` are present
    we pick ``claude_agent`` so the session reuses the CLI's existing
    auth (typically a Max/Pro subscription); else fall back to
    ``orchestral`` (LLM API key).
    """
    if not name or name == "auto":
        return "claude_agent" if _claude_agent_available() else "orchestral"
    return name
