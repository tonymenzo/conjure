"""Tests for spawn.env — .env file loading and user-env editing."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from conjure import env as env_mod


def test_redact_passes_through_non_secret_keys():
    assert env_mod.redact("DEBUG", "true") == "true"


def test_redact_redacts_short_secrets():
    assert env_mod.redact("ANTHROPIC_API_KEY", "abc") == "***"


def test_redact_truncates_long_secrets():
    assert env_mod.redact("OPENAI_API_KEY", "sk-1234567890abcdef") == "sk-1…cdef"


def test_load_env_files_no_op_when_dotenv_missing(monkeypatch):
    """If python-dotenv import fails, load_env_files is silent."""
    import builtins
    original_import = builtins.__import__

    def fake_import(name, *args, **kw):
        if name == "dotenv":
            raise ImportError("no dotenv")
        return original_import(name, *args, **kw)

    with mock.patch("builtins.__import__", side_effect=fake_import):
        env_mod.load_env_files()  # should not raise


def test_load_env_files_respects_shell_precedence(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".env").write_text("SHARED=from_project\nFROM_FILE=value1\n", encoding="utf-8")
    monkeypatch.setenv("SHARED", "from_shell")
    monkeypatch.delenv("FROM_FILE", raising=False)
    env_mod.load_env_files(project_dir=project)
    assert os.environ["SHARED"] == "from_shell"
    assert os.environ["FROM_FILE"] == "value1"


def test_load_env_files_project_overrides_user(tmp_path, monkeypatch):
    """Project .env should override values that came from the user .env
    (but never shell)."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".env").write_text("KEY=project\n", encoding="utf-8")

    # Point USER_ENV_PATH at a file we control for this test.
    user_env = tmp_path / "user.env"
    user_env.write_text("KEY=user\n", encoding="utf-8")
    monkeypatch.setattr(env_mod, "USER_ENV_PATH", user_env)
    monkeypatch.delenv("KEY", raising=False)

    env_mod.load_env_files(project_dir=project)
    assert os.environ["KEY"] == "project"


def test_set_user_env_writes_to_file(tmp_path, monkeypatch):
    user_env = tmp_path / ".config" / "combinator" / ".env"
    monkeypatch.setattr(env_mod, "USER_ENV_PATH", user_env)
    path = env_mod.set_user_env("MY_KEY", "my-value")
    assert path == user_env
    content = user_env.read_text(encoding="utf-8")
    assert "MY_KEY" in content
    assert "my-value" in content


def test_list_user_env_returns_empty_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(env_mod, "USER_ENV_PATH", tmp_path / "missing.env")
    assert env_mod.list_user_env() == {}
