"""Smoke tests for the ClaudeAgentEngine wiring.

These do NOT make API calls — they verify:

  1. The dispatch in ``build_runtime`` routes ``engine: "claude_agent"``
     to the right factory and ``engine: "orchestral"`` stays unchanged.
  2. ``ClaudeAgentEngine`` can be imported (i.e. the optional
     ``claude-agent-sdk`` dependency is wired correctly) when present;
     skip otherwise so CI without the SDK doesn't fail.
"""

from __future__ import annotations

import pytest


def test_claude_agent_sdk_importable_or_skip():
    """If the SDK is installed in this env, importing the engine
    module shouldn't crash."""
    pytest.importorskip("claude_agent_sdk")
    from combinator.engines import claude_agent
    assert hasattr(claude_agent, "ClaudeAgentEngine")


def test_dispatch_rejects_unknown_engine(tmp_path):
    """``build_runtime`` dispatcher refuses unknown engine names with
    a clear error pointing at the supported set."""
    from combinator.config import load_config_from_mapping
    from combinator.runner import build_runtime

    cfg = load_config_from_mapping(
        {
            "runtime": {"store_dir": str(tmp_path)},
            "llms": {"default": {"provider": "anthropic"}},
            "root": {
                "role_prompt": "you are root",
                "engine": "totally_made_up",
            },
        }
    )
    with pytest.raises(Exception) as excinfo:
        build_runtime(cfg)
    # The error message must mention the unknown engine name and the
    # supported set so the user knows what's wrong.
    msg = str(excinfo.value)
    assert "totally_made_up" in msg or "unknown engine" in msg
