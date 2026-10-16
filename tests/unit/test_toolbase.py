"""Toolbase toolkit curation: grants, attenuation, live grants, requests,
catalog, per-agent serving lifecycle, and both engines' wiring.

Everything runs against an injected catalog and server factory — no
toolbase install, no subprocesses, no LLM calls.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from conjure.errors import ToolkitDenied
from conjure.record import AgentSpec
from conjure.runtime import Runtime
from conjure.toolbase import (
    Catalog,
    SkillInfo,
    ToolbaseHub,
    ToolbaseMount,
    ToolbaseServing,
    ToolInfo,
    ToolkitGrant,
    ToolkitInfo,
    check_subset,
    expand,
    parse_toolkits,
)


# ---------- fixtures ----------


def _catalog() -> Catalog:
    hep = ToolkitInfo(
        name="hep",
        version="3.0.0",
        description="High-energy physics.\nSecond line.",
        tools={
            "MadGraph": ToolInfo("MadGraph", "Run MG5.", ("mg5",)),
            "Pythia": ToolInfo("Pythia", "Shower.", ("llp",)),
            "Decay": ToolInfo("Decay", "LLP decays.", ("llp",)),
            "Units": ToolInfo("Units", "Convert units."),
        },
        skills={
            "mg5": SkillInfo("mg5", "MG5 guide", "mg5"),
            "run-cards": SkillInfo("run-cards", "Run cards", "llp"),
            "start": SkillInfo("start", "Getting started", None),
        },
    )
    calc = ToolkitInfo(
        name="calc",
        description="Arithmetic.",
        tools={"add": ToolInfo("add", "Add."), "mul": ToolInfo("mul", "Multiply.")},
    )
    return Catalog(toolkits={"hep": hep, "calc": calc})


class FakeServers:
    """Server factory that records starts/stops instead of spawning."""

    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.closed: list[int] = []

    def __call__(self, *, toolset, sandbox, **_kw) -> ToolbaseServing:
        idx = len(self.started)
        self.started.append({"toolset": toolset, "sandbox": sandbox})
        catalog = _catalog()
        tools = []
        for kit, grant in sorted(toolset.items()):
            for name in sorted(expand(kit, grant, catalog).tools):
                tools.append(_fake_tool(f"{kit}__{name}"))
        return ToolbaseServing(tools, close=lambda: self.closed.append(idx))


def _fake_tool(name: str) -> Any:
    from orchestral.tools.base.tool import BaseTool

    cls = type(
        f"Fake_{name}",
        (BaseTool,),
        {"__doc__": f"fake {name}", "_run": lambda self: f"ran {name}"},
    )
    tool = cls()
    object.__setattr__(tool, "get_name", lambda: name)
    # Like toolbase's proxies: offered and dispatched under one name.
    spec = cls.get_tool_spec()
    spec.name = name
    object.__setattr__(tool, "get_tool_spec", lambda: spec)
    return tool


def _hub(servers: FakeServers | None = None, authority: Any = "all") -> ToolbaseHub:
    return ToolbaseHub(
        project_root="/nonexistent",
        authority=authority,
        catalog_loader=_catalog,
        server_factory=servers or FakeServers(),
    )


def _rt(tmp_path=None, **kw) -> Runtime:
    return Runtime(store_dir=tmp_path, toolbase=_hub(**kw))


# ---------- parsing ----------


def test_parse_activation_grammar():
    ts = parse_toolkits(["hep/llp", "hep__Units", "calc"])
    assert ts["hep"] == ToolkitGrant(bundles=["llp"], tools=["Units"])
    assert ts["calc"].is_whole


def test_parse_whole_toolkit_absorbs_partial_items():
    ts = parse_toolkits(["hep/llp", "hep"])
    assert ts["hep"].is_whole


def test_parse_mapping_forms():
    ts = parse_toolkits(
        {
            "hep": {"bundles": ["llp"], "skills": ["run-cards"]},
            "calc": ["add", "/basic"],
            "other": None,
        }
    )
    assert ts["hep"] == ToolkitGrant(bundles=["llp"], skills=["run-cards"])
    assert ts["calc"] == ToolkitGrant(bundles=["basic"], tools=["add"])
    assert ts["other"].is_whole


def test_parse_rejects_garbage():
    with pytest.raises(ToolkitDenied):
        parse_toolkits(42)
    with pytest.raises(ToolkitDenied):
        parse_toolkits({"hep": {"bundle": ["typo-key"]}})


# ---------- expansion / attenuation ----------


def test_expand_bundle_covers_its_tools_and_skills():
    e = expand("hep", ToolkitGrant(bundles=["llp"]), _catalog())
    assert e.tools == {"Pythia", "Decay"}
    # Bundle-scoped skills follow the bundle; toolkit-wide skills come too.
    assert e.skills == {"run-cards", "start"}


def test_expand_rejects_unknown_names():
    cat = _catalog()
    for grant, kit in [
        (ToolkitGrant(), "nope"),
        (ToolkitGrant(bundles=["typo"]), "hep"),
        (ToolkitGrant(tools=["Typo"]), "hep"),
        (ToolkitGrant(skills=["typo"]), "hep"),
    ]:
        with pytest.raises(ToolkitDenied):
            expand(kit, grant, cat)


def test_subset_allows_narrowing_and_refuses_widening():
    cat = _catalog()
    parent = parse_toolkits(["hep/llp"])
    check_subset(parse_toolkits(["hep__Decay"]), parent, cat)
    check_subset(parse_toolkits(["hep/llp"]), parent, cat)
    with pytest.raises(ToolkitDenied, match="MadGraph"):
        check_subset(parse_toolkits(["hep"]), parent, cat)
    with pytest.raises(ToolkitDenied, match="do not hold"):
        check_subset(parse_toolkits(["calc"]), parent, cat)
    with pytest.raises(ToolkitDenied, match="skills"):
        check_subset({"hep": ToolkitGrant(bundles=["llp"], skills=["mg5"])}, parent, cat)


# ---------- runtime enforcement ----------


def test_spawn_attenuates_down_the_tree():
    rt = _rt()
    root = rt.root(AgentSpec(role_prompt="r"))
    child = rt._spawn(parent=root, spec=AgentSpec(role_prompt="c", toolkits=parse_toolkits(["hep/llp"])))
    rt._spawn(parent=child, spec=AgentSpec(role_prompt="g", toolkits=parse_toolkits(["hep__Pythia"])))
    with pytest.raises(ToolkitDenied):
        rt._spawn(parent=child, spec=AgentSpec(role_prompt="g", toolkits=parse_toolkits(["hep"])))
    with pytest.raises(ToolkitDenied):
        rt._spawn(parent=child, spec=AgentSpec(role_prompt="g", toolkits=parse_toolkits(["calc"])))
    rt.shutdown()


def test_refused_batch_spawns_nothing():
    rt = _rt()
    root = rt.root(AgentSpec(role_prompt="r"))
    child = rt._spawn(parent=root, spec=AgentSpec(role_prompt="c", toolkits=parse_toolkits(["calc__add"])))
    with pytest.raises(ToolkitDenied):
        rt._spawn_batch(
            parent=child,
            specs=[
                AgentSpec(role_prompt="ok", toolkits=parse_toolkits(["calc__add"])),
                AgentSpec(role_prompt="bad", toolkits=parse_toolkits(["calc__mul"])),
            ],
        )
    assert rt.record_for(child).children == set()
    rt.shutdown()


def test_root_authority_can_be_narrowed_by_config():
    rt = _rt(authority=["calc"])
    root = rt.root(AgentSpec(role_prompt="r"))
    rt._spawn(parent=root, spec=AgentSpec(role_prompt="c", toolkits=parse_toolkits(["calc"])))
    with pytest.raises(ToolkitDenied):
        rt._spawn(parent=root, spec=AgentSpec(role_prompt="c", toolkits=parse_toolkits(["hep"])))
    rt.shutdown()


def test_root_own_toolkits_must_fit_authority():
    rt = _rt(authority=["calc"])
    with pytest.raises(ToolkitDenied):
        rt.root(AgentSpec(role_prompt="r", toolkits=parse_toolkits(["hep"])))
    rt.shutdown()


def test_no_hub_refuses_toolkits():
    rt = Runtime()
    root = rt.root(AgentSpec(role_prompt="r"))
    with pytest.raises(ToolkitDenied, match="not configured"):
        rt._spawn(parent=root, spec=AgentSpec(role_prompt="c", toolkits=parse_toolkits(["calc"])))
    rt.shutdown()


def test_only_root_may_hand_out_a_named_loadout():
    hub = ToolbaseHub(
        authority="all",
        catalog_loader=_catalog,
        server_factory=FakeServers(),
        loadout_resolver=lambda name: parse_toolkits(["calc"]),
    )
    rt = Runtime(toolbase=hub)
    root = rt.root(AgentSpec(role_prompt="r"))
    child = rt._spawn(parent=root, spec=AgentSpec(role_prompt="c", toolbase_loadout="paper"))
    assert hub.toolset_of(rt.record_for(child).spec) == parse_toolkits(["calc"])
    with pytest.raises(ToolkitDenied, match="only the root"):
        rt._spawn(parent=child, spec=AgentSpec(role_prompt="g", toolbase_loadout="paper"))
    rt.shutdown()


# ---------- LLM-facing tools ----------


def test_spawn_tool_reports_toolkit_denied():
    from conjure.tools.primitives import spawn_impl

    rt = _rt()
    root = rt.root(AgentSpec(role_prompt="r"))
    tok = rt.record_for(root).token
    ok = spawn_impl(token=tok, role_prompt="c", toolkits=["calc__add"])
    assert ok["ok"], ok
    child_tok = rt.record_for(rt.address_by_id(ok["address"])).token
    denied = spawn_impl(token=child_tok, role_prompt="g", toolkits=["calc"])
    assert denied["code"] == "toolkit_denied"
    bad = spawn_impl(token=tok, role_prompt="c", toolkits=["nope"])
    assert bad["code"] == "toolkit_denied" and "not installed" in bad["error"]
    rt.shutdown()


def test_catalog_shows_only_what_the_caller_holds():
    from conjure.tools.primitives import spawn_impl
    from conjure.tools.toolkits import catalog_impl

    rt = _rt()
    root = rt.root(AgentSpec(role_prompt="r"))
    tok = rt.record_for(root).token
    summary = catalog_impl(token=tok)
    assert [r["toolkit"] for r in summary["toolkits"]] == ["calc", "hep"]
    hep_row = summary["toolkits"][1]
    assert hep_row["description"] == "High-energy physics."
    assert hep_row["tools_held"] == 4 and hep_row["whole"]

    child = spawn_impl(token=tok, role_prompt="c", toolkits=["hep/llp"])
    ctok = rt.record_for(rt.address_by_id(child["address"])).token
    rows = catalog_impl(token=ctok)["toolkits"]
    assert [(r["toolkit"], r["tools_held"]) for r in rows] == [("hep", 2)]
    detail = catalog_impl(token=ctok, toolkit="hep")
    assert [t["name"] for t in detail["tools"]] == ["Decay", "Pythia"]
    assert [s["slug"] for s in detail["skills"]] == ["run-cards", "start"]
    assert catalog_impl(token=ctok, toolkit="calc")["code"] == "toolkit_denied"
    rt.shutdown()


def test_catalog_refresh_picks_up_new_toolkits():
    from conjure.tools.toolkits import catalog_impl

    state = {"extra": False}

    def loader() -> Catalog:
        cat = _catalog()
        if state["extra"]:
            cat.toolkits["fresh"] = ToolkitInfo(name="fresh", tools={"x": ToolInfo("x")})
        return cat

    rt = Runtime(toolbase=ToolbaseHub(authority="all", catalog_loader=loader, server_factory=FakeServers()))
    root = rt.root(AgentSpec(role_prompt="r"))
    tok = rt.record_for(root).token
    assert "fresh" not in [r["toolkit"] for r in catalog_impl(token=tok)["toolkits"]]
    state["extra"] = True  # e.g. the root authored + installed one
    assert "fresh" not in [r["toolkit"] for r in catalog_impl(token=tok)["toolkits"]]
    assert "fresh" in [r["toolkit"] for r in catalog_impl(token=tok, refresh=True)["toolkits"]]
    rt.shutdown()


def test_toolkit_tools_without_hub():
    from conjure.tools.toolkits import catalog_impl, request_toolkits_impl

    rt = Runtime()
    root = rt.root(AgentSpec(role_prompt="r"))
    tok = rt.record_for(root).token
    assert catalog_impl(token=tok)["code"] == "toolbase_unavailable"
    assert request_toolkits_impl(token=tok, toolkits=["x"])["code"] == "toolbase_unavailable"
    rt.shutdown()


def test_request_then_grant_round_trip(tmp_path):
    from conjure.tools.primitives import spawn_impl
    from conjure.tools.toolkits import grant_toolkits_impl, request_toolkits_impl

    rt = _rt(tmp_path)
    root = rt.root(AgentSpec(role_prompt="r"))
    rtok = rt.record_for(root).token
    child = spawn_impl(token=rtok, role_prompt="c", label="w", toolkits=["calc__add"])
    caddr = rt.address_by_id(child["address"])
    ctok = rt.record_for(caddr).token

    sent = request_toolkits_impl(token=ctok, toolkits=["hep/llp"], reason="need showers")
    assert sent["ok"], sent
    [env] = rt.read_inbox(root)
    assert env.headers["kind"] == "toolkit_request"
    assert env.body["toolkits"] == {"hep": {"bundles": ["llp"]}}
    assert env.body["reason"] == "need showers"

    granted = grant_toolkits_impl(token=rtok, child="w", toolkits=["hep/llp"])
    assert granted["ok"], granted
    assert set(granted["toolkits"]) == {"calc", "hep"}
    assert rt.record_for(caddr).spec.toolkits["calc"].tools == ["add"]

    # A grant survives replay.
    rt.shutdown()
    replayed = Runtime.replay(rt.session_dir)
    spec = replayed.record_for(caddr).spec
    assert set(spec.toolkits) == {"calc", "hep"}


def test_grant_is_bounded_and_ancestor_only():
    from conjure.tools.primitives import spawn_impl
    from conjure.tools.toolkits import grant_toolkits_impl

    rt = _rt()
    root = rt.root(AgentSpec(role_prompt="r"))
    rtok = rt.record_for(root).token
    a = spawn_impl(token=rtok, role_prompt="a", toolkits=["calc__add"])
    b = spawn_impl(token=rtok, role_prompt="b")
    atok = rt.record_for(rt.address_by_id(a["address"])).token
    # a holds only calc__add: can't grant mul to its own child.
    g = spawn_impl(token=atok, role_prompt="g")
    assert grant_toolkits_impl(token=atok, child=g["address"], toolkits=["calc__mul"])["code"] == "toolkit_denied"
    assert grant_toolkits_impl(token=atok, child=g["address"], toolkits=["calc__add"])["ok"]
    # a is not b's ancestor.
    assert grant_toolkits_impl(token=atok, child=b["address"], toolkits=["calc__add"])["code"] == "not_permitted"
    # The root may grant to a grandchild.
    assert grant_toolkits_impl(token=rtok, child=g["address"], toolkits=["hep"])["ok"]
    rt.shutdown()


def test_root_request_has_no_parent():
    from conjure.tools.toolkits import request_toolkits_impl

    rt = _rt()
    root = rt.root(AgentSpec(role_prompt="r"))
    out = request_toolkits_impl(token=rt.record_for(root).token, toolkits=["hep"])
    assert out["code"] == "no_parent"
    rt.shutdown()


def test_combinator_template_toolkits_are_checked():
    from conjure.tools.combinators import AgentMapTool, AgentRaceTool
    from conjure.tools.primitives import spawn_impl

    rt = _rt()
    root = rt.root(AgentSpec(role_prompt="r"))
    child = spawn_impl(token=rt.record_for(root).token, role_prompt="c", toolkits=["calc__add"])
    ctok = rt.record_for(rt.address_by_id(child["address"])).token
    out = AgentMapTool(
        runtime_token=ctok, spec={"role_prompt": "w", "toolkits": ["hep"]}, items=[1], timeout_s=1
    )._run()
    assert out["code"] == "toolkit_denied"
    out = AgentRaceTool(
        runtime_token=ctok, specs=[{"role_prompt": "w", "toolkits": 7}], body="x", timeout_s=1
    )._run()
    assert out["code"] == "toolkit_denied"
    rt.shutdown()


# ---------- mount lifecycle ----------


def _record(rt: Runtime, toolkits: list[str] | None):
    root = rt.root(AgentSpec(role_prompt="r"))
    addr = rt._spawn(
        parent=root,
        spec=AgentSpec(role_prompt="c", toolkits=parse_toolkits(toolkits) or None),
    )
    return root, rt.record_for(addr)


def test_mount_starts_lazily_restarts_on_grant_and_closes():
    servers = FakeServers()
    rt = Runtime(toolbase=_hub(servers))
    root, rec = _record(rt, ["calc__add"])
    mount = ToolbaseMount(rt.toolbase, rec, sandbox=None)
    assert servers.started == []  # nothing until the first step
    first = mount.sync()
    assert [t.get_name() for t in first] == ["calc__add"]
    assert mount.sync() is None  # unchanged → no restart
    rt.grant_toolkits(granter=root, target=rec.addr, toolkits=parse_toolkits(["calc__mul"]))
    second = mount.sync()
    assert [t.get_name() for t in second] == ["calc__add", "calc__mul"]
    assert servers.closed == [0]
    mount.close()
    assert servers.closed == [0, 1]
    rt.shutdown()


def test_empty_mount_starts_nothing():
    servers = FakeServers()
    rt = Runtime(toolbase=_hub(servers))
    _, rec = _record(rt, None)
    mount = ToolbaseMount(rt.toolbase, rec, sandbox=None)
    assert mount.sync() is None
    assert servers.started == []
    rt.shutdown()


# ---------- engines ----------


def test_orchestral_engine_serves_and_swaps_toolkits(tmp_path):
    from tests.unit.test_orchestral_engine import FakeLLM

    from conjure.engines.orchestral import make_orchestral_engine_factory

    servers = FakeServers()
    fake = FakeLLM()
    rt = Runtime(store_dir=tmp_path, toolbase=_hub(servers))
    factory = make_orchestral_engine_factory(llms={"default": fake})
    root, rec = _record(rt, ["calc__add"])
    engine = factory(rec, rt)

    names = lambda: {t.get_name() for t in fake.tools}  # noqa: E731
    assert "ToolCatalog" in {type(t).__name__.removesuffix("Tool") for t in engine._base_tools}
    # Servers start at spawn, in the background (prestart); the first
    # step reuses them rather than starting a second set.
    assert len(servers.started) <= 1
    engine.step("hi")
    assert "calc__add" in names()
    assert len(servers.started) == 1
    # Scoped to the agent's sandbox.
    assert servers.started[0]["sandbox"] == tmp_path / "sandboxes" / rec.addr.id
    context_before = engine._agent.context

    rt.grant_toolkits(granter=root, target=rec.addr, toolkits=parse_toolkits(["hep/llp"]))
    engine.step("again")
    assert {"calc__add", "hep__Pythia", "hep__Decay"} <= names()
    assert engine._agent.context is context_before  # conversation kept
    engine.shutdown()
    assert servers.closed == [0, 1]
    rt.shutdown()


def test_driver_closes_toolkits_when_agent_terminates():
    closed = threading.Event()

    class Engine:
        def step(self, prompt):
            return ""

        def close_toolkits(self):
            closed.set()

    rt = Runtime(engine_factory=lambda record, runtime: Engine())
    root = rt.root(AgentSpec(role_prompt="r"))
    child = rt._spawn(parent=root, spec=AgentSpec(role_prompt="c"))
    rt.terminate(child)
    assert closed.wait(2.0)
    rt.shutdown()


def _claude_engine(rt: Runtime, rec, tmp_path):
    pytest.importorskip("claude_agent_sdk")
    from unittest.mock import AsyncMock, MagicMock, patch

    from conjure.engines.claude_agent import ClaudeAgentEngine

    def make_client(opts):
        client = MagicMock()
        client.options = opts
        client.disconnect = AsyncMock()
        return client

    patcher = patch("claude_agent_sdk.ClaudeSDKClient", side_effect=make_client)
    patcher.start()
    engine = ClaudeAgentEngine(
        record=rec,
        runtime=rt,
        sandbox_dir=tmp_path,
        allowed_tools=["Read"],
        toolbase_mount=ToolbaseMount(rt.toolbase, rec, tmp_path),
    )
    return engine, patcher


def test_claude_engine_wires_toolbase_server_and_resumes_on_grant(tmp_path):
    servers = FakeServers()
    rt = Runtime(store_dir=tmp_path, toolbase=_hub(servers))
    root, rec = _record(rt, ["calc__add"])
    engine, patcher = _claude_engine(rt, rec, tmp_path)
    try:
        allowed = engine._options.allowed_tools
        assert "mcp__conjure__ToolCatalog" in allowed
        assert "toolbase" not in (engine._options.mcp_servers or {})

        asyncio.run(engine._sync_toolkits())
        opts = engine._options
        assert "toolbase" in opts.mcp_servers
        assert "mcp__toolbase__calc__add" in opts.allowed_tools
        assert opts.resume is None
        assert engine._client.options is opts

        # Simulate a live session, then a grant.
        engine._connected = True
        engine._session_id = "sess-1"
        old_client = engine._client
        rt.grant_toolkits(granter=root, target=rec.addr, toolkits=parse_toolkits(["calc__mul"]))
        asyncio.run(engine._sync_toolkits())
        old_client.disconnect.assert_awaited_once()
        opts = engine._options
        assert opts.resume == "sess-1"
        assert {"mcp__toolbase__calc__add", "mcp__toolbase__calc__mul"} <= set(opts.allowed_tools)
        assert engine._connected is False

        # Nothing changed → no reconnect.
        engine._connected = True
        client = engine._client
        asyncio.run(engine._sync_toolkits())
        assert engine._client is client
    finally:
        patcher.stop()
        rt.shutdown()


def test_claude_engine_respects_explicit_permissions_on_toolkit_tools(tmp_path):
    rt = Runtime(store_dir=tmp_path, toolbase=_hub())
    root = rt.root(AgentSpec(role_prompt="r"))
    addr = rt._spawn(
        parent=root,
        spec=AgentSpec(
            role_prompt="c",
            toolkits=parse_toolkits(["calc"]),
            permissions={"mcp__toolbase__calc__mul": "ask"},
        ),
    )
    engine, patcher = _claude_engine(rt, rt.record_for(addr), tmp_path)
    try:
        asyncio.run(engine._sync_toolkits())
        assert "mcp__toolbase__calc__add" in engine._options.allowed_tools
        assert "mcp__toolbase__calc__mul" not in engine._options.allowed_tools
    finally:
        patcher.stop()
        rt.shutdown()


def test_toolbase_sdk_server_forwards_calls():
    pytest.importorskip("claude_agent_sdk")
    from conjure.mcp_in_process import _make_proxy_handler, build_toolbase_sdk_server

    calls = []
    tool = SimpleNamespace(
        get_tool_spec=lambda: SimpleNamespace(name="calc__add", description="Add."),
        get_input_schema=lambda: {"type": "object", "properties": {"a": {"type": "number"}}},
        execute=lambda **kw: calls.append(kw) or "3",
    )
    server = build_toolbase_sdk_server([tool])
    assert server["name"] == "toolbase"
    out = asyncio.run(_make_proxy_handler(tool)({"a": 1}))
    assert out == {"content": [{"type": "text", "text": "3"}]}
    assert calls == [{"a": 1}]

    boom = SimpleNamespace(execute=lambda **kw: 1 / 0)
    err = asyncio.run(_make_proxy_handler(boom)({}))
    assert err["is_error"] and "ZeroDivisionError" in err["content"][0]["text"]


def test_config_builds_root_toolkits(monkeypatch):
    import conjure.runner as runner
    from conjure.config import load_config_from_mapping

    monkeypatch.setattr(runner, "build_hub_from_config", lambda cfg: _hub())
    cfg = load_config_from_mapping(
        {
            "llms": {"default": {"provider": "anthropic", "model": "x"}},
            "root": {"role_prompt": "r", "engine": "orchestral", "toolkits": ["calc"]},
            "toolbase": {"authority": ["calc", "hep/llp"]},
        }
    )
    assert cfg.toolbase.authority == ["calc", "hep/llp"]
    rt = Runtime(toolbase=_hub())  # sanity: config-shaped grant parses
    rt.root(AgentSpec(role_prompt="r", toolkits=parse_toolkits(cfg.root.toolkits)))
    rt.shutdown()


def test_legacy_toolbase_profile_key_still_validates():
    spec = AgentSpec.model_validate({"role_prompt": "r", "toolbase_profile": "old"})
    assert spec.toolbase_loadout == "old"


def test_kit_config_resolves_against_the_session_project(tmp_path, monkeypatch):
    """Toolkit config (and so bundle gating) comes from the session's
    project, not the process's cwd; session overrides win."""
    pytest.importorskip("toolbase.envs.config")
    from conjure.toolbase import resolve_kit_config

    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # no user layer
    project = tmp_path / "proj"
    (project / ".toolbase" / "config").mkdir(parents=True)
    (project / ".toolbase" / "config" / "hep.yaml").write_text("mg5_path: /opt/mg5\ncache: true\n")
    monkeypatch.chdir(tmp_path)  # cwd is NOT the project

    assert resolve_kit_config("hep", project) == {"mg5_path": "/opt/mg5", "cache": True}
    assert resolve_kit_config("hep", project, {"hep": {"mg5_path": "/other"}})["mg5_path"] == "/other"
    assert resolve_kit_config("other", project, {"hep": {"x": 1}}) == {}


# ---------- feedback from a live root agent ----------


@pytest.mark.parametrize(
    "raw",
    [
        ["hep/llp", "hep__Units"],
        '["hep/llp", "hep__Units"]',            # list sent as JSON text
        '{"hep": {"bundles": ["llp"], "tools": ["Units"]}}',
        "hep/llp, hep__Units",
        "hep/llp hep__Units",
    ],
)
def test_spawn_toolkits_accepts_every_shape_a_client_sends(raw):
    from conjure.tools.primitives import spawn_impl

    rt = _rt()
    root = rt.root(AgentSpec(role_prompt="r"))
    out = spawn_impl(token=rt.record_for(root).token, role_prompt="c", toolkits=raw)
    assert out["ok"], out
    # The grant is echoed back.
    assert out["toolkits"] == {"hep": {"bundles": ["llp"], "tools": ["Units"]}}
    rt.shutdown()


def test_unreadable_toolkits_say_what_arrived_and_what_was_expected():
    from conjure.tools.primitives import spawn_impl

    rt = _rt()
    root = rt.root(AgentSpec(role_prompt="r"))
    tok = rt.record_for(root).token
    for raw in ['["hep/llp", ', ['["hep/llp'], {"hep": 3}]:
        out = spawn_impl(token=tok, role_prompt="c", toolkits=raw)
        assert out["code"] == "toolkit_denied"
        assert "couldn't read toolkits" in out["error"] and "expected" in out["error"]
        assert "not installed" not in out["error"]
    rt.shutdown()


def test_toolkits_schema_is_typed_for_clients():
    from conjure.mcp_in_process import _build_input_schema
    from conjure.tools.primitives import SpawnTool
    from conjure.tools.toolkits import GrantToolkitsTool, RequestToolkitsTool

    for cls in (SpawnTool, GrantToolkitsTool, RequestToolkitsTool):
        types = {a.get("type") for a in _build_input_schema(cls)["properties"]["toolkits"]["anyOf"]}
        assert {"string", "array", "object"} <= types, cls


def test_default_authority_is_the_active_loadout_not_everything_installed():
    """Installing a toolkit isn't activating it: by default the root may
    grant only what the project's active loadout selects."""
    def hub(**kw):
        return ToolbaseHub(catalog_loader=_catalog, server_factory=FakeServers(), **kw)

    active = hub(loadout_resolver=lambda name: parse_toolkits(["hep/llp"]))
    assert active.authority_mode == "loadout"
    rt = Runtime(toolbase=active)
    root = rt.root(AgentSpec(role_prompt="r"))
    rt._spawn(parent=root, spec=AgentSpec(role_prompt="c", toolkits=parse_toolkits(["hep__Pythia"])))
    with pytest.raises(ToolkitDenied):  # installed, not activated
        rt._spawn(parent=root, spec=AgentSpec(role_prompt="c", toolkits=parse_toolkits(["calc"])))
    with pytest.raises(ToolkitDenied):  # activated bundle only
        rt._spawn(parent=root, spec=AgentSpec(role_prompt="c", toolkits=parse_toolkits(["hep/mg5"])))
    rt.shutdown()

    def no_loadout(name):
        raise RuntimeError("no active loadout")

    assert hub(loadout_resolver=no_loadout).root_authority() == {}
    assert set(hub(authority="all").root_authority()) == {"hep", "calc"}
    assert set(hub(authority=["hep"]).root_authority()) == {"hep"}


def test_config_default_authority_is_loadout():
    from conjure.config import ToolbaseConfig

    assert ToolbaseConfig().authority == "loadout"
