"""LLM client construction from declarative config.

Maps a small provider-agnostic config dict to an ``orchestral.llm``
client instance. Each supported provider has a default model and a
default ``key_env`` (env var holding the API key); both can be
overridden per-LLM in the config.

API keys are NEVER stored in the config — they're read from the named
environment variable at construction time. ``conjure.env`` provides
``.env`` file autoloading so users can keep keys in
``~/.config/conjure/.env`` or a project-local ``./.env``.
"""

from __future__ import annotations

import os
from typing import Any


_PROVIDERS: dict[str, dict[str, str]] = {
    "anthropic": {
        "orchestral_attr": "Claude",
        "default_model": "claude-sonnet-4-5",
        "key_env": "ANTHROPIC_API_KEY",
    },
    "openai": {
        "orchestral_attr": "GPT",
        "default_model": "gpt-4o-mini",
        "key_env": "OPENAI_API_KEY",
    },
    "google": {
        "orchestral_attr": "Gemini",
        "default_model": "gemini-2.0-flash-exp",
        "key_env": "GOOGLE_API_KEY",
    },
    "groq": {
        "orchestral_attr": "Groq",
        "default_model": "llama-3.3-70b-versatile",  # fast, tool-use capable
        "key_env": "GROQ_API_KEY",
    },
    "ollama": {
        "orchestral_attr": "Ollama",
        "default_model": "llama3.1",
        "key_env": "",  # local, no API key needed
    },
    # Any OpenAI-compatible chat-completions endpoint: a model access
    # gateway (MAG), vLLM, LiteLLM, an institutional proxy. Needs
    # ``base_url``; ``model`` may be omitted when ``discover`` lists
    # the endpoint's models (``GET <base_url>/models``).
    "openai_compatible": {
        "orchestral_attr": "",
        "default_model": "",
        "key_env": "",
    },
}


SUPPORTED_PROVIDERS = tuple(_PROVIDERS)


def provider_spec(provider: str) -> dict[str, str]:
    """Return the registry entry for ``provider`` (lowercased)."""
    key = provider.lower()
    if key not in _PROVIDERS:
        raise ValueError(
            f"unsupported provider: {provider!r} (supported: {SUPPORTED_PROVIDERS})"
        )
    return _PROVIDERS[key]


def key_env_for(provider: str) -> str:
    """Return the default env-var name holding the API key for
    ``provider``. Empty string when the provider needs no key
    (e.g. Ollama)."""
    return provider_spec(provider)["key_env"]


def api_key_present(provider: str) -> bool:
    """Whether the env var for ``provider``'s key is set and non-empty."""
    env_name = key_env_for(provider)
    if not env_name:
        return True
    return bool(os.environ.get(env_name))


def build_llm(config: dict[str, Any]) -> Any:
    """Construct one LLM client from one config dict.

    The dict accepts:

    - ``provider`` (required): one of ``SUPPORTED_PROVIDERS``.
    - ``model`` (optional): provider-default when omitted.
    - ``api_key_env`` (optional): override the default key env-var name.
    - ``openai_compatible`` only: ``base_url`` (required), ``headers``
      (values may name env vars as ``${VAR}``), ``timeout_s``.
    """
    if "provider" not in config:
        raise ValueError("LLM config missing required field: provider")
    if config["provider"].lower() == "openai_compatible":
        return build_openai_compatible(config)

    spec = provider_spec(config["provider"])
    model = config.get("model") or spec["default_model"]
    key_env = config.get("api_key_env") or spec["key_env"]
    api_key = os.environ.get(key_env) if key_env else None

    from orchestral import llm as orchestral_llm
    cls = getattr(orchestral_llm, spec["orchestral_attr"])
    return cls(**_kw_with_optional(model=model, api_key=api_key))


def gateway_auth(config: dict[str, Any]) -> tuple[str | None, dict[str, str]]:
    """``(api_key, extra headers)`` for an OpenAI-compatible endpoint,
    read from the environment at call time (never from the config)."""
    key_env = config.get("api_key_env")
    api_key = os.environ.get(key_env) if key_env else None
    headers = {
        str(k): _expand_env(str(v)) for k, v in (config.get("headers") or {}).items()
    }
    return api_key, headers


def _expand_env(value: str) -> str:
    import re

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: os.environ.get(m.group(1), ""), value)


def build_openai_compatible(config: dict[str, Any]) -> Any:
    """An orchestral client for an OpenAI-compatible endpoint.

    Built on orchestral's ``VLLM`` client (OpenAI chat completions with a
    custom host), with the exact ``base_url`` (VLLM would append ``/v1``),
    the gateway's headers, and a timeout suited to long generations."""
    base_url = config.get("base_url")
    if not base_url:
        raise ValueError("openai_compatible LLM needs base_url")
    model = config.get("model")
    if not model:
        raise ValueError(
            "openai_compatible LLM needs a model (set `model`, or spawn with "
            "one from ModelCatalog)"
        )
    import openai
    from orchestral.llm.vllm.client import VLLM

    api_key, headers = gateway_auth(config)
    llm = VLLM(model=model, host=base_url.rstrip("/"), api_key=api_key or "EMPTY")
    llm.base_url = base_url.rstrip("/")
    llm.client = openai.Client(
        api_key=api_key or "EMPTY",
        base_url=llm.base_url,
        default_headers=headers or None,
        timeout=float(config.get("timeout_s") or 600.0),
        # conjure retries (llm_resilience): one layer, visible in the UI,
        # honoring the gateway's reset time.
        max_retries=0,
    )
    return llm


def build_llms(configs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Build a registry of named LLM clients from a dict-of-configs."""
    return {name: build_llm(cfg) for name, cfg in configs.items()}


def _kw_with_optional(**kwargs: Any) -> dict[str, Any]:
    """Drop kwargs whose value is None — orchestral constructors prefer
    omitted args (defaults kick in) over explicit ``None``."""
    return {k: v for k, v in kwargs.items() if v is not None}
