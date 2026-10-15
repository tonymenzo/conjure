"""Toolkit skills reach the agents granted them, and toolkit servers
start at spawn.

Skills are surfaced from a real on-disk toolkit (toolbase's own
``surface_skills`` writes them) into each agent's harness skill root;
servers come from a fake factory. No subprocesses, no LLM calls.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("toolbase.skills")

from conjure.record import AgentSpec
from conjure.runtime import Runtime
from conjure.toolbase import (
    Catalog,
    SkillInfo,
    ToolbaseHub,
    ToolbaseMount,
    ToolbaseServing,
    ToolInfo,
    ToolkitInfo,
    parse_toolkits,
)
from conjure.tools.toolkits import load_skill_impl


def _write_toolkit(root: Path) -> Path:
    kit = root / "hep-kit"
    skills = kit / "skills"
    (skills / "mg5" / "references").mkdir(parents=True)
    (skills / "mg5" / "SKILL.md").write_text(
        "---\nname: mg5\ndescription: Driving MadGraph\nbundle: mg5\n---\n\nUse run cards.\n"
    )
    (skills / "mg5" / "references" / "cards.md").write_text("cards reference\n")
    (skills / "pythia.md").write_text(
        "---\nname: pythia\ndescription: Showering\nbundle: llp\n---\n\nShower it.\n"
    )
    (skills / "start.md").write_text(
        "---\nname: start\ndescription: Getting started\n---\n\nStart here.\n"
    )
    (skills / "delphes.md").write_text(
        "---\nname: delphes\ndescription: Detector sim\nbundle: det\n---\n\nNeeds delphes_path.\n"
    )
    return kit


def _catalog(kit_dir: Path) -> Catalog:
    s = kit_dir / "skills"
    hep = ToolkitInfo(
        name="hep",
        tools={
            "MadGraph": ToolInfo("MadGraph", "Run MG5.", ("mg5",)),
            "Pythia": ToolInfo("Pythia", "Shower.", ("llp",)),
            "Delphes": ToolInfo("Delphes", "Detector.", ("det",)),
        },
        skills={
            "mg5": SkillInfo("mg5", "Driving MadGraph", "mg5", s / "mg5" / "SKILL.md", s / "mg5"),
            "pythia": SkillInfo("pythia", "Showering", "llp", s / "pythia.md"),
            "start": SkillInfo("start", "Getting started", None, s / "start.md"),
            "delphes": SkillInfo("delphes", "Detector sim", "det", s / "delphes.md"),
        },
        unavailable_bundles={"det": ["delphes_path"]},
        path=kit_dir,
    )
    return Catalog(toolkits={"hep": hep})


class FakeServers:
    def __init__(self, delay: threading.Event | None = None) -> None:
        self.started: list[dict[str, Any]] = []
        self.closed: list[int] = []
        self.delay = delay

    def __call__(self, *, toolset, sandbox, **_kw) -> ToolbaseServing:
        if self.delay is not None:
            self.delay.wait(5)
        idx = len(self.started)
        self.started.append({"toolset": toolset, "sandbox": sandbox, "thread": threading.current_thread().name})
        return ToolbaseServing([], close=lambda: self.closed.append(idx))


def _hub(kit_dir: Path, servers: FakeServers | None = None, **kw) -> ToolbaseHub:
    return ToolbaseHub(
        project_root="/nonexistent",
        authority="all",
        catalog_loader=lambda: _catalog(kit_dir),
        server_factory=servers or FakeServers(),
        **kw,
    )


def _child(rt: Runtime, toolkits: list[str]):
    root = rt.root(AgentSpec(role_prompt="r"))
    addr = rt._spawn(parent=root, spec=AgentSpec(role_prompt="c", toolkits=parse_toolkits(toolkits)))
    return root, rt.record_for(addr)


# ---------- surfacing ----------


def test_grant_surfaces_exactly_its_skills_and_tracks_grants(tmp_path):
    kit = _write_toolkit(tmp_path / "src")
    rt = Runtime(toolbase=_hub(kit))
    root, rec = _child(rt, ["hep/mg5"])
    skills_dir = tmp_path / "sb" / ".claude" / "skills"
    mount = ToolbaseMount(rt.toolbase, rec, None, skills_dir=skills_dir)
    mount.sync()

    # The bundle's skill plus toolkit-wide ones; not another bundle's.
    assert sorted(mount.skill_names()) == ["hep__mg5", "hep__start"]
    assert sorted(p.name for p in skills_dir.iterdir()) == ["hep__mg5", "hep__start"]
    doc = (skills_dir / "hep__mg5" / "SKILL.md").read_text()
    assert "name: hep__mg5" in doc and "Use run cards." in doc
    assert (skills_dir / "hep__mg5" / "references" / "cards.md").is_file()

    rt.grant_toolkits(granter=root, target=rec.addr, toolkits=parse_toolkits(["hep/llp"]))
    mount.sync()
    assert sorted(mount.skill_names()) == ["hep__mg5", "hep__pythia", "hep__start"]

    mount.close()
    assert list(skills_dir.iterdir()) == []
    rt.shutdown()


def test_skills_of_bundles_the_session_cannot_serve_are_withheld(tmp_path):
    kit = _write_toolkit(tmp_path / "src")
    rt = Runtime(toolbase=_hub(kit))
    _, rec = _child(rt, ["hep"])
    skills_dir = tmp_path / "skills"
    mount = ToolbaseMount(rt.toolbase, rec, None, skills_dir=skills_dir)
    mount.sync()
    assert "hep__delphes" not in mount.skill_names()
    assert not (skills_dir / "hep__delphes").exists()
    rt.shutdown()


def test_a_skill_the_user_put_there_is_never_overwritten_or_removed(tmp_path):
    kit = _write_toolkit(tmp_path / "src")
    rt = Runtime(toolbase=_hub(kit))
    _, rec = _child(rt, ["hep/mg5"])
    skills_dir = tmp_path / "skills"
    mine = skills_dir / "hep__start"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("my own notes\n")
    mount = ToolbaseMount(rt.toolbase, rec, None, skills_dir=skills_dir)
    mount.sync()
    assert (mine / "SKILL.md").read_text() == "my own notes\n"
    mount.close()
    assert (mine / "SKILL.md").read_text() == "my own notes\n"
    assert not (skills_dir / "hep__mg5").exists()
    rt.shutdown()


# ---------- engines ----------


def test_claude_engine_names_granted_skills_and_loads_only_the_sandbox_project(tmp_path):
    pytest.importorskip("claude_agent_sdk")
    from unittest.mock import AsyncMock, MagicMock, patch

    from conjure.engines.claude_agent import ClaudeAgentEngine

    kit = _write_toolkit(tmp_path / "src")
    rt = Runtime(store_dir=tmp_path, toolbase=_hub(kit))
    _, rec = _child(rt, ["hep/mg5"])

    def make_client(opts):
        client = MagicMock()
        client.options = opts
        client.disconnect = AsyncMock()
        return client

    sandbox = tmp_path / "sb"
    with patch("claude_agent_sdk.ClaudeSDKClient", side_effect=make_client):
        engine = ClaudeAgentEngine(
            record=rec,
            runtime=rt,
            sandbox_dir=sandbox,
            allowed_tools=["Read"],
            toolbase_mount=ToolbaseMount(
                rt.toolbase, rec, sandbox, skills_dir=sandbox / ".claude" / "skills"
            ),
        )
        assert engine._options.setting_sources is None
        asyncio.run(engine._sync_toolkits())
    opts = engine._options
    assert sorted(opts.skills) == ["hep__mg5", "hep__start"]
    assert opts.setting_sources == ["project"]
    assert (sandbox / ".claude" / "skills" / "hep__mg5" / "SKILL.md").is_file()
    rt.shutdown()


def test_in_process_server_is_named_to_match_its_allowlist(tmp_path):
    pytest.importorskip("claude_agent_sdk")
    from unittest.mock import MagicMock, patch

    from conjure.engines.claude_agent import ClaudeAgentEngine

    rt = Runtime(store_dir=tmp_path)
    rec = rt.record_for(rt.root(AgentSpec(role_prompt="r")))
    with patch("claude_agent_sdk.ClaudeSDKClient", side_effect=lambda o: MagicMock(options=o)):
        engine = ClaudeAgentEngine(record=rec, runtime=rt, sandbox_dir=tmp_path)
    servers = engine._options.mcp_servers
    assert "conjure" in servers and "spawn" not in servers
    assert "mcp__conjure__Spawn" in engine._options.allowed_tools
    rt.shutdown()


# ---------- LoadSkill ----------


def test_load_skill_reads_held_skills_and_their_files_only(tmp_path):
    kit = _write_toolkit(tmp_path / "src")
    rt = Runtime(toolbase=_hub(kit))
    _, rec = _child(rt, ["hep/mg5"])
    tok = rec.token

    out = load_skill_impl(token=tok, name="hep__mg5")
    assert out["ok"] and "Use run cards." in out["content"]
    assert out["files"] == ["references/cards.md"]
    assert load_skill_impl(token=tok, name="mg5")["name"] == "hep__mg5"  # bare, unique
    ref = load_skill_impl(token=tok, name="hep__mg5", file="references/cards.md")
    assert ref["content"] == "cards reference\n"

    escape = load_skill_impl(token=tok, name="hep__mg5", file="../pythia.md")
    assert escape["ok"] is False and escape["code"] == "bad_args"
    not_held = load_skill_impl(token=tok, name="hep__pythia")
    assert not_held["ok"] is False and not_held["code"] == "no_such_skill"
    assert "hep__mg5" in not_held["error"]
    rt.shutdown()


# ---------- prestart ----------


def test_prestart_boots_servers_off_thread_and_first_sync_still_reports_them(tmp_path):
    kit = _write_toolkit(tmp_path / "src")
    gate = threading.Event()
    servers = FakeServers(delay=gate)
    rt = Runtime(toolbase=_hub(kit, servers))
    _, rec = _child(rt, ["hep/mg5"])
    mount = ToolbaseMount(rt.toolbase, rec, None)

    t = mount.prestart()
    assert t is not None  # returned at once; the boot runs in the background
    gate.set()
    t.join(5)
    assert len(servers.started) == 1
    assert servers.started[0]["thread"].startswith("toolbase-warm-")

    # The engine's first sync gets the tools without a second start.
    assert mount.sync() == []
    assert mount.sync() is None
    assert len(servers.started) == 1
    mount.close()
    rt.shutdown()


def test_prestart_is_a_noop_without_toolkits_or_when_disabled(tmp_path):
    kit = _write_toolkit(tmp_path / "src")
    servers = FakeServers()
    rt = Runtime(toolbase=_hub(kit, servers))
    root = rt.root(AgentSpec(role_prompt="r"))
    bare = rt.record_for(rt._spawn(parent=root, spec=AgentSpec(role_prompt="c")))
    assert ToolbaseMount(rt.toolbase, bare, None).prestart() is None

    off = Runtime(toolbase=_hub(kit, servers, prestart=False))
    _, rec = _child(off, ["hep/mg5"])
    assert ToolbaseMount(off.toolbase, rec, None).prestart() is None
    assert servers.started == []
    rt.shutdown()
    off.shutdown()


def test_spawning_a_claude_child_with_toolkits_starts_its_servers(tmp_path):
    pytest.importorskip("claude_agent_sdk")
    from unittest.mock import MagicMock, patch

    from conjure.runner import _build_claude_agent_engine

    kit = _write_toolkit(tmp_path / "src")
    servers = FakeServers()
    rt = Runtime(store_dir=tmp_path, toolbase=_hub(kit, servers))
    _, rec = _child(rt, ["hep/mg5"])
    with patch("claude_agent_sdk.ClaudeSDKClient", side_effect=lambda o: MagicMock(options=o)):
        engine = _build_claude_agent_engine(
            record=rec, runtime=rt, event_log_router=None, control_socket=None
        )
    sandbox = tmp_path / "sandboxes" / rec.addr.id
    skill = sandbox / ".claude" / "skills" / "hep__mg5" / "SKILL.md"
    for _ in range(250):
        if skill.is_file():
            break
        threading.Event().wait(0.02)
    assert len(servers.started) == 1  # before any step
    assert skill.is_file()
    engine.close_toolkits()
    rt.shutdown()
