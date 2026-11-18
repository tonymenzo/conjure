"""When live-granted tools become callable, and who is told.

No engine adds tools to a turn already running: Codex lists the hylo
MCP server's tools when its ``codex exec`` process opens a session,
Claude when its SDK client connects, orchestral when the step starts.
So a grant is callable from the grantee's next turn. These tests pin
that down (for Codex, through a fake ``codex`` that talks to the real
loopback MCP server mid-turn) and check that the grantee, the granter
and a self-granting root are each told so, and that the grantee's next
turn actually follows.
"""

from __future__ import annotations

import json
import stat
import sys
import threading
from pathlib import Path

from hylo.address import SYSTEM
from hylo.engines.codex import CodexEngine
from hylo.record import AgentSpec
from hylo.runtime import Runtime
from hylo.toolbase import ToolbaseMount, parse_toolkits
from hylo.tools.primitives import recv_impl
from hylo.tools.toolkits import grant_toolkits_impl
from tests.unit.test_toolbase import FakeServers, _hub


def _tree(engine_factory=None):
    rt = Runtime(toolbase=_hub(FakeServers()), engine_factory=engine_factory)
    root = rt.record_for(rt.root(AgentSpec(role_prompt="r", label="root")))
    kid = rt.record_for(rt._spawn(
        parent=root.addr,
        spec=AgentSpec(role_prompt="c", label="review", engine="codex",
                       toolkits=parse_toolkits(["calc__add"])),
    ))
    return rt, root, kid


def _notices(rt, rec):
    return [e for e in rt.read_inbox(rec.addr) if (e.headers or {}).get("kind") == "toolkit_grant"]


class _Driver:
    """Stands in for the grantee's driver: only ``busy`` is read."""

    def __init__(self, busy: bool) -> None:
        self.busy = busy

    def stop(self, **_kw) -> None:
        pass

    def join(self, timeout=None) -> bool:
        return True


# ---------- what each side is told ----------


def test_grantee_mid_turn_gets_a_notice_and_the_granter_is_told_next_turn():
    rt, root, kid = _tree()
    kid.driver = _Driver(busy=True)
    out = grant_toolkits_impl(token=root.token, child="review", toolkits=["calc__mul"])
    assert out["ok"], out
    assert out["new_tools"] == ["mcp__hylo__calc__mul"]  # as Codex shows it
    assert "mid-turn" in out["note"] and "next turn, not this one" in out["note"]
    assert "don't need to message it" in out["note"]

    [env] = _notices(rt, kid)
    assert env.from_ == SYSTEM
    assert env.body["tools"] == ["mcp__hylo__calc__mul"]
    assert env.body["granted_by"] == "root"
    note = env.body["note"]
    assert "root granted you mcp__hylo__calc__mul" in note
    assert "can't gain tools" in note and "next turn" in note
    # The note leads the body, so the inbox preview on the grantee's tool
    # results (first 200 characters) carries the timing.
    assert json.dumps(env.body).startswith('{"kind": "toolkit_grant", "note": "[hylo] root granted you')
    rt.shutdown()


def test_idle_grantee_is_woken_by_the_notice():
    rt, root, kid = _tree()
    kid.driver = _Driver(busy=False)
    kid.wakeup.clear()
    out = grant_toolkits_impl(token=root.token, child="review", toolkits=["calc__mul"])
    assert "was idle" in out["note"] and "starts its next turn now" in out["note"]
    assert kid.wakeup.is_set()
    rt.shutdown()


def test_self_grant_tells_the_root_to_end_its_turn():
    rt, root, _ = _tree()
    out = grant_toolkits_impl(token=root.token, child="self", toolkits=["hep/llp"])
    assert out["ok"], out
    assert "NOT this one" in out["note"] and "End this turn" in out["note"]
    [env] = _notices(rt, root)
    assert "you granted yourself" in env.body["note"]
    assert sorted(t.split("__", 2)[-1] for t in env.body["tools"]) == ["hep__Decay", "hep__Pythia"]
    rt.shutdown()


def test_a_grant_that_adds_nothing_sends_no_notice():
    rt, root, kid = _tree()
    first = grant_toolkits_impl(token=root.token, child="review", toolkits=["hep/llp"])
    assert first["new_tools"]
    # Naming tools already covered by the bundle changes nothing served:
    # the trial's second, redundant grant.
    again = grant_toolkits_impl(token=root.token, child="review",
                                toolkits=["hep__Pythia", "hep__Decay"])
    assert again["ok"] and again["new_tools"] == []
    assert again["note"].startswith("nothing new")
    assert len(_notices(rt, kid)) == 1
    rt.shutdown()


def test_the_notice_survives_replay(tmp_path):
    rt = Runtime(store_dir=tmp_path, toolbase=_hub(FakeServers()))
    root = rt.record_for(rt.root(AgentSpec(role_prompt="r", label="root")))
    kid = rt.record_for(rt._spawn(parent=root.addr, spec=AgentSpec(role_prompt="c", label="w")))
    grant_toolkits_impl(token=root.token, child="w", toolkits=["calc__mul"])
    rt.shutdown()
    replayed = Runtime.replay(rt.session_dir)
    [env] = _notices(replayed, replayed.record_for(kid.addr))
    assert env.body["tools"][0].endswith("calc__mul")


# ---------- the next turn follows ----------


class _GrantedMidTurn:
    """An engine whose first turn is the trial's reviewer: its parent
    grants it tools mid-turn, it reads the notice with Recv, and ends
    the turn. Later turns just record their prompts."""

    def __init__(self, record, runtime) -> None:
        self.record, self.runtime = record, runtime
        self.prompts: list[str] = []
        self.second = threading.Event()

    def step(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            root = self.runtime.record_for(self.record.parent)
            grant_toolkits_impl(token=root.token, child=self.record.addr.id, toolkits=["calc__mul"])
            got = recv_impl(token=self.record.token)
            assert [e["headers"].get("kind") for e in got["envelopes"]] == ["toolkit_grant"]
        else:
            self.second.set()
        return "ok"


def test_a_grant_read_mid_turn_still_starts_the_next_turn():
    engines: list[_GrantedMidTurn] = []

    def factory(record, runtime):
        if record.parent is None:
            from tests.unit.test_driver import MockEngine

            return MockEngine()
        engines.append(_GrantedMidTurn(record, runtime))
        return engines[-1]

    rt = Runtime(toolbase=_hub(FakeServers()), engine_factory=factory)
    root = rt.root(AgentSpec(role_prompt="r", label="root"))
    kid = rt._spawn(parent=root, spec=AgentSpec(role_prompt="c", label="review"))
    rt.send_external(kid, "check the inputs")
    # Recv consumed the notice during turn 1, yet the driver pushes it:
    # nothing else would wake the agent for the turn that has the tools.
    assert engines and engines[0].second.wait(5), "the grant did not start a next turn"
    assert "toolkit_grant" in engines[0].prompts[1] and "calc__mul" in engines[0].prompts[1]
    rt.shutdown()


# ---------- Codex: not callable mid-turn, callable the next turn ----------


def _fake_codex(tmp_path: Path) -> tuple[Path, Path]:
    """A ``codex`` that, like the real one, lists the hylo MCP tools
    when its process starts. On its first run (the reviewer's turn) it
    has the root grant it ``calc__mul``, then lists and calls again in
    the same process; later runs list and call once."""
    log = tmp_path / "mcp.jsonl"
    script = tmp_path / "codex"
    script.write_text(
        f"""#!{sys.executable}
import json, os, sys, urllib.request
argv = sys.argv[1:]
url = next(json.loads(a.split("=", 1)[1]) for a in argv if a.startswith("mcp_servers.hylo.url="))
token = os.environ["HYLO_MCP_TOKEN"]
def rpc(tok, method, params):
    req = urllib.request.Request(url, data=json.dumps({{"jsonrpc": "2.0", "id": 1,
        "method": method, "params": params}}).encode(), headers={{
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
        "Authorization": "Bearer " + tok, "MCP-Protocol-Version": "2025-06-18"}})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())["result"]
def names():
    return [t["name"] for t in rpc(token, "tools/list", {{}})["tools"]]
def call(name, tok=None, **args):
    out = rpc(tok or token, "tools/call", {{"name": name, "arguments": args}})
    return {{"error": out["isError"], "text": out["content"][0]["text"]}}
rec = {{"resume": "resume" in argv, "prompt": argv[-1], "start": names()}}
if not rec["resume"]:
    root_token, child = open({str(tmp_path / "root")!r}).read().split()
    rec["grant"] = json.loads(call("GrantToolkits", root_token, child=child, toolkits=["calc__mul"])["text"])
    rec["after_grant"] = names()
rec["mul"] = call("calc__mul")
with open({str(log)!r}, "a") as f:
    f.write(json.dumps(rec) + "\\n")
for ev in [{{"type": "thread.started", "thread_id": "t-1"}},
           {{"type": "item.completed", "item": {{"id": "m", "type": "agent_message", "text": "done"}}}},
           {{"type": "turn.completed", "usage": {{"output_tokens": 1}}}}]:
    print(json.dumps(ev), flush=True)
"""
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script, log


def test_codex_grantee_calls_granted_tools_from_its_next_turn(tmp_path):
    rt, root, kid = _tree()
    script, log = _fake_codex(tmp_path)
    (tmp_path / "root").write_text(f"{root.token} {kid.addr.id}")
    sandbox = tmp_path / "sb"
    sandbox.mkdir()
    engine = CodexEngine(
        record=kid, runtime=rt, sandbox_dir=sandbox, codex_home=tmp_path / "home",
        codex_bin=str(script), toolbase_mount=ToolbaseMount(rt.toolbase, kid, sandbox),
    )
    try:
        engine.step("review the inputs")
        engine.step("continue")
    finally:
        engine.shutdown()
        rt.shutdown()
    turn1, turn2 = [json.loads(line) for line in log.read_text().splitlines()]

    # Turn 1: the grant succeeds and says when the tools arrive; within
    # this codex process the tool is neither listed nor callable.
    assert "calc__add" in turn1["start"] and "calc__mul" not in turn1["start"]
    assert turn1["grant"]["ok"] and turn1["grant"]["new_tools"] == ["mcp__hylo__calc__mul"]
    assert "next turn" in turn1["grant"]["note"]
    assert "calc__mul" not in turn1["after_grant"]
    assert turn1["mul"]["error"] and "unknown_tool" in turn1["mul"]["text"]

    # Turn 2 (``codex exec resume``): listed and callable, and the prompt
    # names it.
    assert turn2["resume"] and "calc__mul" in turn2["start"]
    assert turn2["mul"] == {"error": False, "text": "ran calc__mul"}
    assert "mcp__hylo__calc__mul" in turn2["prompt"].split("\n\n")[0]
