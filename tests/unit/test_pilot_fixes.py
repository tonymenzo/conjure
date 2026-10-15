"""Fixes from the first pilot study: inbox notices on tool results,
callable vs grantable toolkits, the root serving itself, and naming an
agent's toolkit tools in its prompt (so workers call them instead of
shelling out)."""

from __future__ import annotations

import asyncio

import pytest

from conjure.record import AgentSpec
from conjure.runtime import Runtime
from conjure.toolbase import parse_toolkits, toolkit_brief
from conjure.tools.primitives import ListInboxTool, RecvTool, SendTool, send_impl
from conjure.tools.toolkits import catalog_impl, grant_toolkits_impl
from tests.unit.test_toolbase import FakeServers, _hub


def _call(cls, token, **kw):
    return cls(runtime_token=token, **kw)._run()


def _pair(rt):
    root = rt.record_for(rt.root(AgentSpec(role_prompt="r")))
    kid = rt.record_for(rt._spawn(parent=root.addr, spec=AgentSpec(role_prompt="c", label="worker")))
    return root, kid


def test_tool_results_carry_waiting_messages_until_read():
    rt = Runtime()
    root, kid = _pair(rt)
    assert "inbox" not in _call(SendTool, root.token, to=kid.addr.id, body="go")
    send_impl(token=kid.token, to=root.addr.id, body={"status": "final", "width": 0.0840})
    out = _call(SendTool, root.token, to=kid.addr.id, body="still there?")
    assert out["inbox"]["unread"] == 1
    assert out["inbox"]["latest"][0]["from"] == "worker" and "0.084" in out["inbox"]["latest"][0]["preview"]
    # Tools that already are the inbox don't repeat it.
    assert "inbox" not in _call(ListInboxTool, root.token)
    # Once read, the notice is gone.
    _call(RecvTool, root.token)
    assert "inbox" not in _call(SendTool, root.token, to=kid.addr.id, body="thanks")
    rt.shutdown()


def test_catalog_separates_callable_from_grantable_and_root_can_serve_itself():
    rt = Runtime(toolbase=_hub(FakeServers()))
    root, _ = _pair(rt)
    cat = catalog_impl(token=root.token)
    hep = next(r for r in cat["toolkits"] if r["toolkit"] == "hep")
    assert hep["tools_held"] == 4 and hep["tools_you_can_call"] == 0
    assert 'GrantToolkits(child="self"' in cat["note"]

    out = grant_toolkits_impl(token=root.token, child="self", toolkits=["hep/llp"])
    assert out["ok"], out
    detail = catalog_impl(token=root.token, toolkit="hep")
    by = {t["name"]: t for t in detail["tools"]}
    assert by["Pythia"]["call_as"].endswith("hep__Pythia")
    assert by["MadGraph"]["callable"] is False
    rt.shutdown()


def test_only_the_root_may_grant_itself():
    rt = Runtime(toolbase=_hub(FakeServers()))
    root = rt.record_for(rt.root(AgentSpec(role_prompt="r")))
    kid = rt.record_for(rt._spawn(parent=root.addr, spec=AgentSpec(role_prompt="c", toolkits=parse_toolkits(["calc"]))))
    out = grant_toolkits_impl(token=kid.token, child="self", toolkits=["calc__add"])
    assert out["ok"] is False and out["code"] == "not_permitted"
    rt.shutdown()


@pytest.mark.parametrize("engine,prefix", [("claude_agent", "mcp__toolbase__"), ("codex", "mcp__conjure__"), ("orchestral", "")])
def test_prompt_names_the_exact_tools_to_call(engine, prefix):
    hub = _hub(FakeServers())
    brief = toolkit_brief(hub, AgentSpec(role_prompt="c", toolkits=parse_toolkits(["hep/llp"])), engine)
    assert f"`{prefix}hep__Pythia`" in brief and f"`{prefix}hep__Decay`" in brief
    assert "MadGraph" not in brief  # not granted
    assert "rather than running the underlying" in brief
    assert toolkit_brief(hub, AgentSpec(role_prompt="c"), engine) == ""


def test_codex_prompt_lists_its_tools(tmp_path):
    from conjure.engines.codex import build_system_prompt

    rt = Runtime(store_dir=tmp_path, toolbase=_hub(FakeServers()))
    root = rt.root(AgentSpec(role_prompt="r"))
    kid = rt.record_for(rt._spawn(parent=root, spec=AgentSpec(role_prompt="c", toolkits=parse_toolkits(["hep/llp"]))))
    prompt = build_system_prompt(kid, rt)
    assert "`mcp__conjure__hep__Pythia`" in prompt and "Wait on workers through your inbox" in prompt
    rt.shutdown()


def test_claude_engine_tells_the_agent_about_a_live_grant(tmp_path):
    pytest.importorskip("claude_agent_sdk")
    from tests.unit.test_toolbase import _claude_engine, _record

    rt = Runtime(store_dir=tmp_path, toolbase=_hub(FakeServers()))
    root, rec = _record(rt, ["calc__add"])
    engine, patcher = _claude_engine(rt, rec, tmp_path)
    try:
        asyncio.run(engine._sync_toolkits())
        assert engine._grant_note is None  # the first set is in the system prompt
        assert "`mcp__toolbase__calc__add`" in engine._options.system_prompt
        rt.grant_toolkits(granter=root, target=rec.addr, toolkits=parse_toolkits(["calc__mul"]))
        asyncio.run(engine._sync_toolkits())
        assert "mcp__toolbase__calc__mul" in engine._grant_note
    finally:
        patcher.stop()
        rt.shutdown()
