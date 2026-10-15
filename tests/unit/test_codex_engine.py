"""Codex engine: command line, event translation, resume, isolation,
engine inheritance, and the loopback MCP server it talks to.

A fake ``codex`` executable records how it was called and replays a
canned ``--json`` stream, so nothing here needs the real CLI or a
network.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import urllib.request
from pathlib import Path

import pytest

from conjure.engines.codex import CodexEngine, build_system_prompt
from conjure.mcp_http import mcp_http_for
from conjure.record import AgentSpec
from conjure.runtime import Runtime

EVENTS = [
    {"type": "thread.started", "thread_id": "thread-123"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "Checking."}},
    {"type": "item.started", "item": {"id": "i1", "type": "command_execution", "command": "/bin/zsh -lc 'ls'", "status": "in_progress"}},
    {"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "/bin/zsh -lc 'ls'", "aggregated_output": "a.txt\n", "exit_code": 0}},
    {"type": "item.completed", "item": {"id": "i2", "type": "mcp_tool_call", "server": "conjure", "tool": "ListInbox", "arguments": {}, "result": {"content": [{"type": "text", "text": "{\"ok\": true}"}]}}},
    {"type": "item.completed", "item": {"id": "i3", "type": "agent_message", "text": "All done."}},
    {"type": "turn.completed", "usage": {"input_tokens": 1200, "output_tokens": 42}},
]


def _fake_codex(tmp_path: Path, *, exit_code: int = 0) -> tuple[Path, Path]:
    calls = tmp_path / "calls.jsonl"
    script = tmp_path / "codex"
    script.write_text(
        f"""#!{sys.executable}
import json, os, sys
with open({str(calls)!r}, "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(),
        "home": os.environ.get("CODEX_HOME"), "token": os.environ.get("CONJURE_MCP_TOKEN"),
        "openai": os.environ.get("OPENAI_API_KEY")}}) + "\\n")
home = os.environ["CODEX_HOME"]
os.makedirs(os.path.join(home, "sessions", "2026"), exist_ok=True)
with open(os.path.join(home, "sessions", "2026", "rollout-x-thread-123.jsonl"), "w") as f:
    f.write('{{"type":"turn_context","payload":{{"model":"gpt-test-1"}}}}\\n')
    f.write('{{"type":"event_msg","payload":{{"type":"task_started","model_context_window":400000}}}}\\n')
    # A tool call naming another model (a child's) must not be read as ours.
    f.write('{{"type":"response_item","payload":{{"type":"function_call","arguments":"{{\\\\"model\\\\":\\\\"codex/decoy\\\\"}}","model":"decoy"}}}}\\n')
for ev in {json.dumps(EVENTS)!r} and json.loads({json.dumps(json.dumps(EVENTS))}):
    print(json.dumps(ev), flush=True)
sys.stderr.write("boom\\n" if {exit_code} else "")
sys.exit({exit_code})
"""
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script, calls


def _engine(tmp_path: Path, **kw):
    rt = Runtime(store_dir=tmp_path / "store")
    rec = rt.record_for(rt.root(AgentSpec(role_prompt="be terse", label="root")))
    events: list[dict] = []
    script, calls = _fake_codex(tmp_path, exit_code=kw.pop("exit_code", 0))
    sandbox = tmp_path / "sb"
    sandbox.mkdir()
    engine = CodexEngine(
        record=rec,
        runtime=rt,
        sandbox_dir=sandbox,
        codex_home=tmp_path / "home",
        stream_emit=events.append,
        codex_bin=str(script),
        **kw,
    )
    return rt, rec, engine, events, calls


def test_first_turn_starts_a_thread_and_translates_events(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    rt, rec, engine, events, calls = _engine(tmp_path, model="gpt-x", reasoning_effort="high")
    try:
        assert engine.step("hello") == "All done."
        call = json.loads(calls.read_text().splitlines()[0])
        argv = call["argv"]
        assert argv[:2] == ["exec", "-C"] and "resume" not in argv
        assert ["-s", "workspace-write"] == argv[3:5]
        for flag in ("--json", "--skip-git-repo-check", "--ignore-user-config"):
            assert flag in argv
        assert "features.apps=false" in argv and ["-m", "gpt-x"] == argv[argv.index("-m"):argv.index("-m") + 2]
        assert any(a.startswith("mcp_servers.conjure.url=\"http://127.0.0.1:") for a in argv)
        assert 'mcp_servers.conjure.bearer_token_env_var="CONJURE_MCP_TOKEN"' in argv
        assert 'mcp_servers.conjure.default_tools_approval_mode="approve"' in argv
        assert "be terse" in argv[-1] and argv[-1].endswith("hello")  # system prompt folded in
        # Isolation: the token travels by env, the API key doesn't.
        assert call["token"] == rec.token and rec.token not in " ".join(argv)
        assert call["openai"] is None
        assert call["cwd"] == str((tmp_path / "sb").resolve())
        assert call["home"] == str((tmp_path / "home").resolve())

        kinds = [(e["kind"], e.get("text") or e.get("tool_calls")) for e in events]
        assert kinds == [
            ("thinking_start", None),
            ("chunk", "Checking."),
            ("stream_end", [{"name": "shell", "args": {"command": "ls"}}]),
            ("tool", "a.txt\n"),
            ("stream_end", [{"name": "ListInbox", "args": {}}]),
            ("tool", '{"ok": true}'),
            ("chunk", "All done."),
            ("usage", None),
            ("stream_end", []),
            ("thinking_end", None),
        ]
        assert engine.thread_id == "thread-123"
        assert engine.model_info() == ("gpt-test-1", "observed")
        assert engine.context_usage() == (1200, 400000)
        assert engine.cost() == 0.0 and engine.uses_subscription()
    finally:
        engine.shutdown()
        rt.shutdown()


def test_later_turns_resume_the_thread_without_the_system_prompt(tmp_path):
    rt, rec, engine, events, calls = _engine(tmp_path)
    try:
        engine.step("one")
        engine.step("two")
        argv = json.loads(calls.read_text().splitlines()[1])["argv"]
        assert argv[:3] == ["exec", "resume", "thread-123"]
        assert 'sandbox_mode="workspace-write"' in argv
        assert argv[-1] == "two"
    finally:
        engine.shutdown()
        rt.shutdown()


def test_codex_home_links_auth_and_nothing_else(tmp_path, monkeypatch):
    real = tmp_path / "real-codex"
    real.mkdir()
    (real / "auth.json").write_text("{}")
    (real / "AGENTS.md").write_text("personal instructions")
    monkeypatch.setenv("CODEX_HOME", str(real))
    rt, rec, engine, events, calls = _engine(tmp_path)
    try:
        engine.step("hi")
        home = tmp_path / "home"
        assert (home / "auth.json").is_symlink()
        assert not (home / "AGENTS.md").exists()
    finally:
        engine.shutdown()
        rt.shutdown()


def test_a_failed_run_raises_with_stderr(tmp_path):
    rt, rec, engine, events, calls = _engine(tmp_path, exit_code=2)
    try:
        with pytest.raises(RuntimeError, match="codex exited 2: boom"):
            engine.step("hi")
        assert events[-1]["kind"] == "thinking_end"
    finally:
        engine.shutdown()
        rt.shutdown()


def test_system_prompt_is_the_shared_one_for_codex(tmp_path):
    rt = Runtime(store_dir=tmp_path)
    rec = rt.record_for(rt.root(AgentSpec(role_prompt="r")))
    prompt = build_system_prompt(rec, rt)
    assert "(Codex engine)" in prompt and "Codex's shell and file editing" in prompt
    assert "Claude Code's tools" not in prompt
    rt.shutdown()


def test_auto_children_of_a_codex_parent_stay_on_codex():
    from conjure.runner import _engine_for

    rt = Runtime()
    root = rt.root(AgentSpec(role_prompt="r", engine="codex"))
    kid = rt._spawn(parent=root, spec=AgentSpec(role_prompt="c"))
    grandkid = rt._spawn(parent=kid, spec=AgentSpec(role_prompt="g"))
    pinned = rt._spawn(parent=root, spec=AgentSpec(role_prompt="p", engine="orchestral"))
    assert _engine_for(rt.record_for(kid), rt) == "codex"
    assert _engine_for(rt.record_for(grandkid), rt) == "codex"
    assert _engine_for(rt.record_for(pinned), rt) == "orchestral"
    rt.shutdown()


# ---------- loopback MCP server ----------


def _rpc(url: str, token: str, method: str, params: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
            "MCP-Protocol-Version": "2025-06-18",
        },
    )
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def test_mcp_server_scopes_tools_to_the_calling_agent():
    from tests.unit.test_toolbase import _fake_tool

    rt = Runtime()
    root = rt.record_for(rt.root(AgentSpec(role_prompt="r")))
    server = mcp_http_for(rt)
    try:
        url = server.url

        class Mount:
            tools = [_fake_tool("calc__add")]

        server.register_mount(root.token, Mount())
        names = [t["name"] for t in _rpc(url, root.token, "tools/list", {})["result"]["tools"]]
        assert "Spawn" in names and "calc__add" in names
        out = _rpc(url, root.token, "tools/call", {"name": "calc__add", "arguments": {}})["result"]
        assert out["content"][0]["text"] == "ran calc__add" and not out["isError"]
        inbox = _rpc(url, root.token, "tools/call", {"name": "ListInbox", "arguments": {}})["result"]
        assert json.loads(inbox["content"][0]["text"])["ok"] is True

        assert _rpc(url, "not-a-token", "tools/list", {})["result"]["tools"] == []
        refused = _rpc(url, "not-a-token", "tools/call", {"name": "ListInbox", "arguments": {}})["result"]
        assert refused["isError"] and "unauthorized" in refused["content"][0]["text"]
    finally:
        server.close()
        rt.shutdown()


def test_interrupt_kills_the_running_turn_without_an_error(tmp_path):
    import threading
    import time

    script = tmp_path / "codex"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, time\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 't'}), flush=True)\n"
        "time.sleep(30)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    rt = Runtime(store_dir=tmp_path / "store")
    rec = rt.record_for(rt.root(AgentSpec(role_prompt="r")))
    (tmp_path / "sb").mkdir()
    engine = CodexEngine(record=rec, runtime=rt, sandbox_dir=tmp_path / "sb", codex_home=tmp_path / "h", codex_bin=str(script))
    result: list = []
    t = threading.Thread(target=lambda: result.append(engine.step("go")))
    start = time.time()
    t.start()
    while engine._proc is None and time.time() - start < 5:
        time.sleep(0.02)
    time.sleep(0.3)
    engine.interrupt()
    t.join(10)
    assert not t.is_alive() and time.time() - start < 10
    assert result == [""]
    engine.shutdown()
    rt.shutdown()


def test_unpinned_agents_show_codexs_default_before_their_first_reply(tmp_path, monkeypatch):
    real = tmp_path / "real-codex"
    real.mkdir()
    (real / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "gpt-big", "priority": 2, "visibility": "list"},
        {"slug": "gpt-hidden", "priority": 0, "visibility": "hide"},
        {"slug": "gpt-default", "priority": 1, "visibility": "list"},
    ]}))
    monkeypatch.setenv("CODEX_HOME", str(real))
    rt, rec, engine, events, calls = _engine(tmp_path)
    try:
        assert engine.model_info() == ("gpt-default", "resolved")
        engine.step("hi")
        assert engine.model_info() == ("gpt-test-1", "observed")
    finally:
        engine.shutdown()
        rt.shutdown()
    rt, rec, engine, events, calls = _engine(tmp_path / "pinned", model="gpt-big") if (tmp_path / "pinned").mkdir() is None else None
    try:
        assert engine.model_info() == ("gpt-big", "configured")
    finally:
        engine.shutdown()
        rt.shutdown()
