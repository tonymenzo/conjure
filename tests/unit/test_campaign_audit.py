"""Regressions from auditing a 24-trial benchmark campaign (feyntable-01).

1. A script printed 16M characters; Bash returned all of it, the next model
   call failed ("prompt is too long: 12930458 tokens") and the run died.
2. Bash ran under /bin/sh, so ``<(...)`` failed with "syntax error near
   unexpected token `('".
3. A spawned worker's task came from ``@system``, so its
   ``Send(to="caller")`` went to @system and the parent waited forever.
4. ``Spawn(sandbox_dir="work/worker")`` resolved against the process cwd,
   outside the parent's workspace; the worker found none of its inputs.
5. ``WaitFor(timeout_s=7200)`` under a 6000 s harness wall clock outlived
   the trial.
6. A trial cut off mid-turn recorded no cost: agent meta (cost, tokens) was
   only noted at turn end.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path

import pytest

from conjure.record import AgentRecord, AgentSpec
from conjure.runtime import DEFAULT_MAX_WAIT_S, DEFAULT_TOOL_OUTPUT_MAX_CHARS, Runtime
from conjure.tool_output import TOOL_OUTPUT_DIR, ToolOutputCap, cap_tool_output
from conjure.tools.filesystem import bash_impl, read_impl, write_impl
from conjure.tools.primitives import recv_impl, send_impl, spawn_impl, wait_for_impl

from tests.unit.test_orchestral_tool_calls import ToolCallingGateway


def _journal(rt: Runtime) -> list[dict]:
    rt._journal.flush()
    path = Path(rt.session_dir) / "journal.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


@pytest.fixture
def rt(tmp_path):
    runtime = Runtime(store_dir=tmp_path / "store")
    runtime.auto_mode = True
    yield runtime
    runtime.shutdown()


# ---------- 1. tool-result cap ----------


def test_cap_keeps_head_and_tail_and_saves_the_full_output(tmp_path):
    text = "HEAD" + "x" * 200_000 + "TAIL"
    out, info = cap_tool_output(text, max_chars=10_000, sandbox=tmp_path, tool_name="Bash", call_id="call_1")
    assert out.startswith("HEAD") and out.endswith("TAIL")
    assert len(out) < 11_000
    assert info["chars"] == len(text) and info["truncated"] == len(text) - 10_000
    saved = Path(info["path"])
    assert saved.read_text() == text
    assert saved.parent == tmp_path / TOOL_OUTPUT_DIR
    assert f"[... {len(text) - 10_000} chars truncated" in out
    assert str(saved.relative_to(tmp_path)) in out


def test_short_output_and_disabled_cap_pass_through(tmp_path):
    assert cap_tool_output("short", max_chars=10_000, sandbox=tmp_path) == ("short", None)
    big = "y" * 100_000
    assert cap_tool_output(big, max_chars=0, sandbox=tmp_path) == (big, None)
    assert cap_tool_output(big, max_chars=None, sandbox=tmp_path) == (big, None)
    assert not (tmp_path / TOOL_OUTPUT_DIR).exists()


def test_default_cap_is_on():
    assert Runtime().tool_output_max_chars == DEFAULT_TOOL_OUTPUT_MAX_CHARS
    from conjure.config import RuntimeConfig

    assert RuntimeConfig().tool_output_max_chars == DEFAULT_TOOL_OUTPUT_MAX_CHARS


def test_cap_hook_replaces_the_message_and_logs_the_cut(rt):
    from orchestral.context.message import Message

    root = rt.root(AgentSpec(role_prompt="r", label="root"))
    record = rt.record_for(root)
    events: list[dict] = []
    hook = ToolOutputCap(record, rt, emit=events.append)
    msg = Message(role="tool", text="z" * 100_000, tool_call_id="call_9")
    out = hook.after_call("Bash", msg).apply_to(msg)
    assert out is not msg and out.tool_call_id == "call_9"
    assert len(out.text) < DEFAULT_TOOL_OUTPUT_MAX_CHARS + 1_000
    [event] = events
    assert event["kind"] == "tool_output_truncated" and event["chars"] == 100_000
    assert Path(event["path"]).read_text() == "z" * 100_000
    assert Path(event["path"]).is_relative_to(rt.sandbox_of(root))
    # Under the cap: untouched, nothing logged.
    small = Message(role="tool", text="ok", tool_call_id="call_10")
    assert hook.after_call("Bash", small).apply_to(small) is small
    assert len(events) == 1


def test_huge_bash_output_never_reaches_the_model(monkeypatch, tmp_path):
    """End to end through orchestral: the model's next request carries the
    capped result, and the full output is in the sandbox."""
    from conjure.config import load_config_from_mapping
    from conjure.runner import build_runtime

    gw = ToolCallingGateway("Bash")
    real = gw.server.RequestHandlerClass.do_POST

    def bash_once(handler):
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length)
        body = json.loads(raw)
        if not any(m.get("role") == "tool" for m in body.get("messages", [])):
            gw.requests.append(body)
            out = json.dumps({"id": "c", "object": "chat.completion", "created": 0, "model": "m",
                              "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                                  "role": "assistant", "content": None, "tool_calls": [{
                                      "id": "call_1", "type": "function", "function": {
                                          "name": "Bash",
                                          "arguments": json.dumps({"command": "python3 -c \"print('A'*300000)\""})}}]}}],
                              "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(out)))
            handler.end_headers()
            handler.wfile.write(out)
            return
        # Replay the consumed body for the default handler.
        import io

        handler.rfile = io.BytesIO(raw)
        real(handler)

    gw.server.RequestHandlerClass.do_POST = bash_once
    monkeypatch.setenv("GW_KEY", "k")
    ws = tmp_path / "ws"
    cfg = load_config_from_mapping({
        "runtime": {"auto_approve": True, "store_dir": str(tmp_path / "store"), "tool_output_max_chars": 5_000},
        "llms": {"gw": {"provider": "openai_compatible", "base_url": gw.url, "api_key_env": "GW_KEY", "model": "m"}},
        "root": {"role_prompt": "r", "engine": "orchestral", "llm": "gw", "tools": ["filesystem"],
                 "sandbox_dir": str(ws)},
    })
    rt, root = build_runtime(cfg)
    try:
        rt.send_external(to=root, body="run it")
        deadline = time.time() + 20
        while time.time() < deadline and len(gw.requests) < 2:
            time.sleep(0.05)
        assert len(gw.requests) >= 2, "the agent never called the model after its Bash call"
        result = [m for m in gw.requests[1]["messages"] if m.get("role") == "tool"][-1]["content"]
        assert len(str(result)) < 7_000
        assert "chars truncated" in str(result)
        [saved] = list((ws / TOOL_OUTPUT_DIR).iterdir())
        assert saved.read_text().count("A") >= 300_000
    finally:
        rt.shutdown()
        gw.server.shutdown()


def test_read_pages_a_long_file(rt):
    token = rt.record_for(rt.root(AgentSpec(role_prompt="r", label="root"))).token
    write_impl(token=token, path="big.txt", content="".join(f"line {i}\n" for i in range(100)))
    out = read_impl(token=token, path="big.txt", offset=10, limit=3)
    assert out["content"] == "line 10\nline 11\nline 12\n"
    assert out["total_lines"] == 100 and out["lines"] == 3
    assert read_impl(token=token, path="big.txt")["content"].startswith("line 0\n")


# ---------- 2. bash, not sh ----------


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_bash_runs_bash_syntax(rt):
    token = rt.record_for(rt.root(AgentSpec(role_prompt="r", label="root"))).token
    out = bash_impl(token=token, command="diff <(echo a) <(echo a) && [[ 1 == 1 ]] && echo same-$BASH_VERSINFO")
    assert out["ok"] is True, out
    assert out["exit_code"] == 0, out["stderr"]
    assert out["stdout"].startswith("same-")


def test_bash_shell_override(monkeypatch):
    from conjure.tools import filesystem

    monkeypatch.setenv("CONJURE_SHELL", "/bin/sh")
    assert filesystem._shell() == "/bin/sh"


# ---------- 3. a worker's reply reaches its parent ----------


def test_spawn_task_comes_from_the_parent_and_caller_replies_to_it(rt):
    root = rt.root(AgentSpec(role_prompt="r", label="root"))
    out = spawn_impl(token=rt.record_for(root).token, role_prompt="w", label="enumerator",
                     initial_message='TASK: ... reply with Send(to="caller")')
    child = rt.address_by_id(out["address"])
    [task] = rt.read_inbox(child)
    assert task.from_ == root
    sends = [r for r in _journal(rt) if r["kind"] == "send"]
    assert sends[-1]["payload"]["envelope"]["from"]["id"] == root.id
    # The driver sets ``last_received_from`` from the pushed task.
    rt.record_for(child).last_received_from = task.from_
    reply = send_impl(token=rt.record_for(child).token, to="caller", body={"handoff": "done"})
    assert reply["ok"] is True
    got = wait_for_impl(token=rt.record_for(root).token, predicate_kind="from", value="enumerator", timeout_s=1)
    assert [e["body"] for e in got["envelopes"]] == [{"handoff": "done"}]


def test_caller_after_a_system_notice_is_the_parent(rt):
    from conjure.address import SYSTEM

    root = rt.root(AgentSpec(role_prompt="r", label="root"))
    child = rt._spawn(parent=root, spec=AgentSpec(role_prompt="w", label="w"))
    rec = rt.record_for(child)
    rec.last_received_from = SYSTEM
    assert send_impl(token=rec.token, to="caller", body="result")["ok"] is True
    assert [e.body for e in rt.read_inbox(root)] == ["result"]
    rec.last_received_from = None
    assert send_impl(token=rec.token, to="caller", body="again")["ok"] is True
    assert [e.body for e in rt.read_inbox(root)][-1] == "again"


def test_send_to_system_is_refused_not_lost(rt):
    root = rt.root(AgentSpec(role_prompt="r", label="root"))
    child = rt._spawn(parent=root, spec=AgentSpec(role_prompt="w", label="w"))
    out = send_impl(token=rt.record_for(child).token, to="@system", body={"handoff": 1})
    assert out["ok"] is False and out["code"] == "not_deliverable"
    assert 'to="parent"' in out["error"]


def test_system_prompt_tells_workers_how_to_reply(rt):
    from conjure.prompts import build_system_prompt

    root = rt.root(AgentSpec(role_prompt="r", label="root"))
    child = rt._spawn(parent=root, spec=AgentSpec(role_prompt="w", label="w"))
    prompt = build_system_prompt(rt.record_for(child), rt, "orchestral")
    assert 'Send(to="parent"' in prompt and "your task came from your parent" in prompt


# ---------- 4. sandbox_dir is inside the parent's sandbox ----------


def test_relative_sandbox_resolves_against_the_parents_sandbox(tmp_path):
    rt = Runtime(store_dir=tmp_path / "store")
    try:
        ws = tmp_path / "run" / "workspace"
        root = rt.root(AgentSpec(role_prompt="r", label="root", sandbox_dir=str(ws)))
        out = spawn_impl(token=rt.record_for(root).token, role_prompt="w", label="worker",
                         sandbox_dir="work/worker")
        assert out["ok"] is True, out
        child = rt.address_by_id(out["address"])
        assert rt.record_for(child).spec.sandbox_dir == str((ws / "work" / "worker").resolve())
        spawn = [r for r in _journal(rt) if r["kind"] == "spawn"][-1]
        assert spawn["payload"]["spec"]["sandbox_dir"] == str((ws / "work" / "worker").resolve())
        # An absolute path inside is fine too.
        inside = spawn_impl(token=rt.record_for(root).token, role_prompt="w", label="w2",
                            sandbox_dir=str(ws / "w2"))
        assert inside["ok"] is True
    finally:
        rt.shutdown()


@pytest.mark.parametrize("bad", ["../worker", "/elsewhere/worker", "work/../../x"])
def test_escaping_sandbox_is_refused_before_the_spawn_is_journaled(tmp_path, bad):
    rt = Runtime(store_dir=tmp_path / "store")
    try:
        ws = tmp_path / "run" / "workspace"
        root = rt.root(AgentSpec(role_prompt="r", label="root", sandbox_dir=str(ws)))
        out = spawn_impl(token=rt.record_for(root).token, role_prompt="w", label="worker",
                         sandbox_dir=bad, initial_message="go")
        assert out["ok"] is False and out["code"] == "invalid_sandbox", out
        assert rt.record_for(root).children == set()
        assert [r["payload"]["addr"]["label"] for r in _journal(rt) if r["kind"] == "spawn"] == ["root"]
    finally:
        rt.shutdown()


def test_auto_sandbox_parent_contains_children(rt):
    root = rt.root(AgentSpec(role_prompt="r", label="root"))
    out = spawn_impl(token=rt.record_for(root).token, role_prompt="w", sandbox_dir="sub")
    assert out["ok"] is True
    child = rt.address_by_id(out["address"])
    assert Path(rt.record_for(child).spec.sandbox_dir) == rt.sandbox_of(root) / "sub"


# ---------- 5. waits are bounded ----------


def test_waitfor_is_cut_to_max_wait_with_a_note(rt):
    rt.max_wait_s = 0.3
    root = rt.root(AgentSpec(role_prompt="r", label="root"))
    rt._spawn(parent=root, spec=AgentSpec(role_prompt="w", label="enumerator"))
    t0 = time.monotonic()
    out = wait_for_impl(token=rt.record_for(root).token, predicate_kind="from", value="enumerator",
                        timeout_s=7200)
    assert time.monotonic() - t0 < 3
    assert out["ok"] is True and out["timed_out"] is True
    assert "Peek" in out["note"] and "max_wait_s" in out["note"]


def test_waitfor_never_outlives_the_deadline(rt):
    rt.deadline = time.time() + 0.3
    root = rt.root(AgentSpec(role_prompt="r", label="root"))
    t0 = time.monotonic()
    out = wait_for_impl(token=rt.record_for(root).token, timeout_s=7200)
    assert time.monotonic() - t0 < 3
    assert out["timed_out"] is True and "deadline" in out["note"]
    # Past the deadline: returns at once, saying so.
    out = recv_impl(token=rt.record_for(root).token, timeout_s=60)
    assert out["timed_out"] is True and "deadline has passed" in out["note"]


def test_a_plain_poll_is_not_a_timeout(rt):
    root = rt.root(AgentSpec(role_prompt="r", label="root"))
    out = recv_impl(token=rt.record_for(root).token, timeout_s=0)
    assert "timed_out" not in out


def test_bash_timeout_is_clipped_to_the_deadline(rt):
    rt.deadline = time.time() + 0.5
    token = rt.record_for(rt.root(AgentSpec(role_prompt="r", label="root"))).token
    t0 = time.monotonic()
    out = bash_impl(token=token, command="sleep 20", timeout_s=600)
    assert time.monotonic() - t0 < 8
    assert out["code"] == "timeout"


def test_config_sets_the_deadline_and_max_wait(monkeypatch):
    from conjure.config import load_config_from_mapping
    from conjure.runner import _deadline

    base = {"llms": {"d": {"provider": "anthropic"}}, "root": {"role_prompt": "r", "llm": "d"}}
    cfg = load_config_from_mapping({**base, "runtime": {"wall_clock_s": 100}})
    assert abs(_deadline(cfg) - (time.time() + 100)) < 5
    cfg = load_config_from_mapping({**base, "runtime": {"wall_clock_s": 100, "deadline": time.time() + 10}})
    assert _deadline(cfg) - time.time() < 11
    cfg = load_config_from_mapping(base)
    assert _deadline(cfg) is None and cfg.runtime.max_wait_s == DEFAULT_MAX_WAIT_S


# ---------- 6. cost survives a run cut off mid-turn ----------


class _StuckEngine:
    """An engine whose turn never ends (an agent blocked in WaitFor)."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def step(self, prompt: str) -> str:
        self.entered.set()
        self.release.wait(10)
        return ""

    def cost(self) -> float:
        return 12.5

    def usage(self) -> dict:
        return {"tokens_in": 1000, "tokens_out": 50, "unpriced_tokens": 0}

    def model_name(self) -> str:
        return "claude-opus-5-5"


def test_shutdown_mid_turn_records_cost(tmp_path):
    engines: list[_StuckEngine] = []

    def factory(record: AgentRecord, runtime: Runtime):
        engines.append(_StuckEngine())
        return engines[-1]

    rt = Runtime(store_dir=tmp_path, engine_factory=factory)
    root = rt.root(AgentSpec(role_prompt="r", label="root"))
    rt.send_external(to=root, body="go")
    assert engines[0].entered.wait(5)
    assert rt.record_for(root).meta == {}
    try:
        rt.shutdown(driver_join_timeout=0.1)
        meta = rt.record_for(root).meta
        assert meta["cost_usd"] == 12.5 and meta["tokens"] == 1050
        rows = [r for r in _journal(rt) if r["kind"] == "agent_meta"]
        assert rows and rows[-1]["payload"]["meta"]["cost_usd"] == 12.5
    finally:
        engines[0].release.set()
