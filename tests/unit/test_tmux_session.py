"""Tests for spawn.tmux_session.

Each test creates a uniquely-named ephemeral session and tears it down
in a finalizer, so test runs don't collide with each other or with a
user's existing tmux sessions. Tests are skipped wholesale when tmux
isn't installed.
"""

from __future__ import annotations

import uuid

import pytest

from conjure.tmux_session import TmuxSession, TmuxSessionError, tmux_available


pytestmark = pytest.mark.skipif(not tmux_available(), reason="tmux not installed")


@pytest.fixture
def session_name() -> str:
    return f"conjure-test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def session(session_name: str):
    s = TmuxSession.attach_or_create(session_name)
    yield s
    s.kill()


def test_attach_or_create_creates_a_session(session_name: str):
    s = TmuxSession.attach_or_create(session_name)
    try:
        assert s.has_session()
    finally:
        s.kill()


def test_kill_tears_down_the_session(session_name: str):
    s = TmuxSession.attach_or_create(session_name)
    assert s.has_session()
    s.kill()
    assert not s.has_session()


def test_attach_or_create_reuses_existing(session_name: str):
    s1 = TmuxSession.attach_or_create(session_name)
    try:
        s2 = TmuxSession.attach_or_create(session_name)
        assert s2.has_session()
        # Both wrappers reference the same underlying tmux session.
        assert s1.has_session()
    finally:
        s1.kill()


def test_new_window_appears_in_listing(session: TmuxSession):
    session.new_window(name="alpha", command="cat")
    names = {w["name"] for w in session.list_windows()}
    assert "alpha" in names


def test_kill_window_removes_it(session: TmuxSession):
    session.new_window(name="alpha", command="cat")
    session.kill_window("alpha")
    names = {w["name"] for w in session.list_windows()}
    assert "alpha" not in names


def test_kill_window_is_idempotent(session: TmuxSession):
    session.kill_window("ghost")  # never existed; should not raise


def test_rename_window(session: TmuxSession):
    session.new_window(name="alpha", command="cat")
    session.rename_window(current_name="alpha", new_name="beta")
    names = {w["name"] for w in session.list_windows()}
    assert "beta" in names
    assert "alpha" not in names


def test_kill_session_is_idempotent(session_name: str):
    s = TmuxSession.attach_or_create(session_name)
    s.kill()
    s.kill()  # no error on second kill


def test_init_raises_without_tmux(monkeypatch):
    """If tmux is missing, the constructor raises a clean error."""
    monkeypatch.setattr("spawn.tmux_session.tmux_available", lambda: False)
    with pytest.raises(TmuxSessionError, match="tmux binary"):
        TmuxSession("anything")
