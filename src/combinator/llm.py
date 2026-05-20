"""LLM client construction from declarative config.

Maps a small provider-agnostic config dict to an ``orchestral.llm``
client instance. Used by the CLI / YAML loader to turn user
configuration into live clients.

Config shape (one entry per named LLM):

.. code-block:: yaml

    default:
      provider: anthropic               # one of: anthropic, openai, google, groq, ollama
      model: claude-sonnet-4-6          # optional; provider default if omitted
      api_key_env: ANTHROPIC_API_KEY    # env var; falls back to provider default

API keys are NEVER stored in the config — they're read from the named
environment variable at construction time.
"""

from __future__ import annotations

import os
from typing import Any


SUPPORTED_PROVIDERS = ("anthropic", "openai", "google", "groq", "ollama")


def build_llm(config: dict[str, Any]) -> Any:
    """Construct one LLM client from one config dict.

    Returns the live client (an ``orchestral.llm.LLM`` subclass instance).
    Raises ``ValueError`` for unknown providers or missing required
    fields.
    """
    if "provider" not in config:
        raise ValueError("LLM config missing required field: provider")
    provider = config["provider"].lower()
    model = config.get("model")
    api_key_env = config.get("api_key_env")
    api_key = os.environ.get(api_key_env) if api_key_env else None

    if provider == "anthropic":
        from orchestral.llm import Claude
        return Claude(**_kw_with_optional(model=model, api_key=api_key))
    if provider == "openai":
        from orchestral.llm import GPT
        return GPT(**_kw_with_optional(model=model, api_key=api_key))
    if provider == "google":
        from orchestral.llm import Gemini
        return Gemini(**_kw_with_optional(model=model, api_key=api_key))
    if provider == "groq":
        from orchestral.llm import Groq
        return Groq(**_kw_with_optional(model=model, api_key=api_key))
    if provider == "ollama":
        from orchestral.llm import Ollama
        return Ollama(**_kw_with_optional(model=model, api_key=api_key))

    raise ValueError(
        f"unsupported provider: {provider!r} (supported: {SUPPORTED_PROVIDERS})"
    )


def build_llms(configs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Build a registry of named LLM clients from a dict-of-configs."""
    return {name: build_llm(cfg) for name, cfg in configs.items()}


def _kw_with_optional(**kwargs: Any) -> dict[str, Any]:
    """Drop kwargs whose value is None — orchestral constructors prefer
    omitted args (defaults kick in) over explicit ``None``."""
    return {k: v for k, v in kwargs.items() if v is not None}
