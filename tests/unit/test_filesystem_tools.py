"""Tests for the filesystem tool group — sandbox enforcement, basic
read/write/edit/bash/grep/glob behavior, permission gating."""

from __future__ import annotations

from pathlib import Path

import pytest

from combinator.record import AgentSpec
from combinator.runtime import Runtime
from combinator.tools._base import register_token, unregister_token
from combinator.tools.filesystem import (
    bash_impl,
    edit_impl,
    glob_impl,
    grep_impl,
    read_impl,
    write_impl,
)


@pytest.fixture
def rt(tmp_path):
    rt = Runtime(store_dir=tmp_path)
    yield rt
    rt.shutdown()


@pytest.fixture
def agent_token(rt):
    """Spawn a single agent and return its token. Sandbox auto-allocates
    under ``rt.store_dir/sandboxes/<id>``."""
    root = rt.root(AgentSpec(role_prompt="r", label="root"))
    return rt.record_for(root).token


def test_write_then_read_round_trip(agent_token):
    w = write_impl(token=agent_token, path="hello.txt", content="hi\nworld\n")
    assert w["ok"] is True
    r = read_impl(token=agent_token, path="hello.txt")
    assert r["ok"] is True
    assert r["content"] == "hi\nworld\n"


def test_read_missing_file(agent_token):
    out = read_impl(token=agent_token, path="nope.txt")
    assert out["ok"] is False
    assert out["code"] == "not_found"


def test_path_escape_is_blocked(agent_token):
    out = write_impl(token=agent_token, path="../outside.txt", content="x")
    assert out["ok"] is False
    assert out["code"] == "escape"


def test_edit_replaces_unique_occurrence(agent_token):
    write_impl(token=agent_token, path="f.txt", content="foo bar baz")
    out = edit_impl(
        token=agent_token, path="f.txt", old_string="bar", new_string="qux"
    )
    assert out["ok"] is True
    assert read_impl(token=agent_token, path="f.txt")["content"] == "foo qux baz"


def test_edit_refuses_ambiguous_old_string(agent_token):
    write_impl(token=agent_token, path="f.txt", content="x x x")
    out = edit_impl(
        token=agent_token, path="f.txt", old_string="x", new_string="y"
    )
    assert out["ok"] is False
    assert out["code"] == "ambiguous"


def test_bash_runs_in_sandbox(agent_token):
    write_impl(token=agent_token, path="sub/a.txt", content="1")
    write_impl(token=agent_token, path="sub/b.txt", content="2")
    out = bash_impl(token=agent_token, command="ls sub")
    assert out["ok"] is True
    assert out["exit_code"] == 0
    assert "a.txt" in out["stdout"]
    assert "b.txt" in out["stdout"]


def test_bash_timeout(agent_token):
    out = bash_impl(token=agent_token, command="sleep 2", timeout_s=0.2)
    assert out["ok"] is False
    assert out["code"] == "timeout"


def test_grep_finds_substring(agent_token):
    write_impl(token=agent_token, path="a.py", content="def foo():\n    pass\n")
    write_impl(token=agent_token, path="b.py", content="def bar(): pass\n")
    out = grep_impl(token=agent_token, pattern="def foo")
    assert out["ok"] is True
    assert len(out["matches"]) == 1
    assert out["matches"][0]["path"] == "a.py"
    assert out["matches"][0]["line"] == 1


def test_glob_matches_recursive(agent_token):
    write_impl(token=agent_token, path="src/x.py", content="")
    write_impl(token=agent_token, path="src/sub/y.py", content="")
    write_impl(token=agent_token, path="src/z.md", content="")
    out = glob_impl(token=agent_token, pattern="**/*.py")
    assert out["ok"] is True
    assert set(out["paths"]) == {"src/x.py", "src/sub/y.py"}


def test_permission_deny_blocks_write(rt):
    """Deny Write via ``spec.permissions``; Read still works."""
    addr = rt.root(
        AgentSpec(
            role_prompt="r",
            label="root",
            permissions={"Write": "deny"},
        )
    )
    token = rt.record_for(addr).token
    out = write_impl(token=token, path="x.txt", content="x")
    assert out["ok"] is False
    assert out["code"] == "permission_denied"
    # Read remains allowed.
    assert read_impl(token=token, path="x.txt")["code"] == "not_found"


def test_permission_ask_blocks_and_resolves_to_allow(rt):
    """An ``ask`` decision submits a PermissionRequest and blocks the
    tool. When the UI resolves it to ``allow``, the tool proceeds."""
    import threading

    addr = rt.root(
        AgentSpec(
            role_prompt="r",
            label="root",
            permissions={"Bash": "ask"},
        )
    )
    token = rt.record_for(addr).token

    result: dict = {}

    def runner():
        result["out"] = bash_impl(token=token, command="echo hi")

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    # Wait until the request appears in the queue.
    import time
    for _ in range(50):
        pending = rt.list_pending_permissions(addr=addr)
        if pending:
            break
        time.sleep(0.05)
    pending = rt.list_pending_permissions(addr=addr)
    assert len(pending) == 1
    assert pending[0].tool_name == "Bash"
    assert pending[0].args == {"command": "echo hi"}
    # Resolve → allow.
    assert rt.resolve_permission(pending[0].req_id, "allow") is True
    t.join(timeout=5)
    assert result["out"]["ok"] is True
    assert result["out"]["stdout"].strip() == "hi"


def test_permission_ask_resolved_to_deny(rt):
    """``deny`` resolution returns a clean ``permission_denied``."""
    import threading

    addr = rt.root(
        AgentSpec(
            role_prompt="r",
            label="root",
            permissions={"Write": "ask"},
        )
    )
    token = rt.record_for(addr).token
    result: dict = {}

    def runner():
        result["out"] = write_impl(
            token=token, path="x.txt", content="hi"
        )

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    import time
    for _ in range(50):
        pending = rt.list_pending_permissions(addr=addr)
        if pending:
            break
        time.sleep(0.05)
    rt.resolve_permission(pending[0].req_id, "deny")
    t.join(timeout=5)
    assert result["out"]["ok"] is False
    assert result["out"]["code"] == "permission_denied"


def test_no_sandbox_when_no_store_dir():
    """Without a runtime store_dir and no explicit sandbox_dir, FS
    tools refuse to run."""
    rt = Runtime()  # no store_dir
    addr = rt.root(AgentSpec(role_prompt="r", label="root"))
    token = rt.record_for(addr).token
    try:
        out = read_impl(token=token, path="x")
        assert out["ok"] is False
        assert out["code"] == "no_sandbox"
    finally:
        rt.shutdown()


def test_explicit_sandbox_dir(tmp_path):
    """Spec-level ``sandbox_dir`` wins over the auto-allocated path."""
    custom = tmp_path / "custom-sandbox"
    rt = Runtime()  # no store_dir → would normally be no_sandbox
    addr = rt.root(
        AgentSpec(
            role_prompt="r",
            label="root",
            sandbox_dir=str(custom),
        )
    )
    token = rt.record_for(addr).token
    try:
        w = write_impl(token=token, path="ok.txt", content="ok")
        assert w["ok"] is True
        assert (custom / "ok.txt").read_text() == "ok"
    finally:
        rt.shutdown()
