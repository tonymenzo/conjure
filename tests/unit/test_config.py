"""Tests for combinator.config — YAML loader."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from combinator.config import Config, load_config, load_config_from_mapping


_MINIMAL = {
    "llms": {
        "default": {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "api_key_env": "ANTHROPIC_API_KEY",
        },
    },
    "root": {
        "role_prompt": "you are iota",
    },
}


def test_minimal_config_loads_with_defaults():
    cfg = load_config_from_mapping(_MINIMAL)
    assert cfg.mode == "repl"
    assert cfg.root.label == "iota"
    assert cfg.root.engine == "orchestral"
    assert cfg.root.llm == "default"
    assert cfg.root.tools == ["primitive", "combinator"]
    assert cfg.runtime.max_workers == 32


def test_unknown_field_rejected():
    bad = dict(_MINIMAL)
    bad["unknown_top_level"] = 1
    with pytest.raises(Exception):
        load_config_from_mapping(bad)


def test_one_shot_mode_with_task():
    data = dict(_MINIMAL)
    data["mode"] = "one-shot"
    data["initial_task"] = "do the thing"
    cfg = load_config_from_mapping(data)
    assert cfg.mode == "one-shot"
    assert cfg.initial_task == "do the thing"


def test_load_from_yaml_file(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(_MINIMAL), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.root.role_prompt == "you are iota"
    assert "default" in cfg.llms


def test_multiple_llms():
    data = dict(_MINIMAL)
    data["llms"] = {
        "default": {"provider": "anthropic"},
        "cheap": {"provider": "openai", "model": "gpt-4o-mini"},
    }
    cfg = load_config_from_mapping(data)
    assert set(cfg.llms) == {"default", "cheap"}
    assert cfg.llms["cheap"].model == "gpt-4o-mini"
