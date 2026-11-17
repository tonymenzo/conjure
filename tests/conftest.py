"""Shared pytest fixtures.

Concrete fixtures (ephemeral runtime, scripted agents, temp stores) are
added as the corresponding modules land.
"""

import os

# Never start a real ``claude`` CLI to read its model table in tests.
os.environ.setdefault("HYLO_MODEL_PROBE", "0")


import pytest


@pytest.fixture(autouse=True)
def _no_legacy_user_env(tmp_path_factory, monkeypatch):
    """Tests never read the machine's ~/.config/conjure/.env (the pre-0.3.0 name)."""
    from hylo import env as env_mod

    monkeypatch.setattr(env_mod, "LEGACY_USER_ENV_PATH", tmp_path_factory.mktemp("legacy") / "absent.env")
