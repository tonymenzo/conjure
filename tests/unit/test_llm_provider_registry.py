"""Tests for spawn.llm's provider registry."""

from __future__ import annotations

import pytest

from spawn.llm import (
    SUPPORTED_PROVIDERS,
    api_key_present,
    key_env_for,
    provider_spec,
)


def test_known_providers_have_key_env():
    for provider in SUPPORTED_PROVIDERS:
        spec = provider_spec(provider)
        assert "key_env" in spec
        assert "orchestral_attr" in spec
        assert "default_model" in spec


def test_provider_spec_lowercases():
    assert provider_spec("Anthropic") == provider_spec("anthropic")


def test_provider_spec_unknown_raises():
    with pytest.raises(ValueError, match="unsupported"):
        provider_spec("nonexistent")


def test_key_env_for_known_providers():
    assert key_env_for("anthropic") == "ANTHROPIC_API_KEY"
    assert key_env_for("openai") == "OPENAI_API_KEY"
    assert key_env_for("google") == "GOOGLE_API_KEY"
    assert key_env_for("ollama") == ""


def test_api_key_present_for_keyless_provider():
    """Ollama needs no key, so api_key_present is always True."""
    assert api_key_present("ollama") is True


def test_api_key_present_reads_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-abc")
    assert api_key_present("anthropic") is True
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert api_key_present("anthropic") is False
