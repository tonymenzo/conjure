"""Tests for combinator.meta socket resolution logic.

The textual app itself isn't unit-tested (textual's runtime is hard to
exercise from pytest without a TTY). The socket-resolution function is
pure plumbing and is exercised directly.
"""

from __future__ import annotations

from pathlib import Path

from combinator import meta


def test_resolve_explicit_socket_takes_precedence(tmp_path: Path):
    p = tmp_path / "explicit.sock"
    p.touch()
    assert meta._resolve_socket(p, None) == p


def test_resolve_session_name(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(meta, "socket_path_for", lambda name: tmp_path / f"{name}.sock")
    out = meta._resolve_socket(None, "combinator-test")
    assert out == tmp_path / "combinator-test.sock"


def test_resolve_returns_none_when_nothing_to_find(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(meta, "list_session_names", lambda: [])
    assert meta._resolve_socket(None, None) is None


def test_current_tmux_session_no_env(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    assert meta._current_tmux_session() is None
