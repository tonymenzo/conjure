"""Provenance trace: Conjure registers agents, hands each agent's
orchestrators its own ``Trace`` handle, and records the trace and the
served toolkits in ``.conjure/run.json``. Fake catalog and server
factory: no toolkit servers start."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("toolbase.trace")

from conjure.record import AgentSpec
from conjure.runtime import Runtime
from conjure.toolbase import ToolbaseHub, ToolbaseMount, ToolbaseServing, parse_toolkits

from tests.unit.test_toolbase import _catalog

RUN_KEYS = {"harness", "version", "session", "agents", "models"}


@pytest.fixture(autouse=True)
def _no_trace_env(monkeypatch):
    monkeypatch.delenv("TOOLBASE_TRACE", raising=False)
    monkeypatch.delenv("TOOLBASE_TRACE_DIR", raising=False)


class Servers:
    """Records each start's keyword arguments (``trace`` among them)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kw) -> ToolbaseServing:
        self.calls.append(kw)
        return ToolbaseServing([])


def _hub(tmp_path: Path, servers: Servers, **kw) -> ToolbaseHub:
    return ToolbaseHub(
        project_root=tmp_path / "proj",
        authority="all",
        catalog_loader=_catalog,
        server_factory=servers,
        version_pins=lambda: {"calc": "1.2.0"},
        **kw,
    )


def _tree(tmp_path: Path, hub: ToolbaseHub, *, session_id: str = "sess-1"):
    rt = Runtime(store_dir=tmp_path / "store", session_id=session_id, toolbase=hub)
    root = rt.root(AgentSpec(role_prompt="lead brief", label="lead", engine="codex",
                             model="gpt-5.5", toolkits=parse_toolkits(["hep", "calc"]),
                             sandbox_dir=str(tmp_path / "run")))
    child = rt._spawn(parent=root, spec=AgentSpec(
        role_prompt="worker brief", label="worker", engine="codex", model="gpt-5.5-mini",
        initial_message="do the thing", toolkits=parse_toolkits(["hep/llp"])))
    return rt, root, child


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _run_json(rt: Runtime, root) -> dict[str, Any]:
    return json.loads((rt.sandbox_of(root) / ".conjure" / "run.json").read_text())


def test_off_by_default_changes_nothing(tmp_path):
    servers = Servers()
    hub = _hub(tmp_path, servers)
    rt, root, child = _tree(tmp_path, hub)
    try:
        for addr in (root, child):
            ToolbaseMount(hub, rt.record_for(addr), sandbox=rt.sandbox_of(addr)).sync()
        assert len(servers.calls) == 2
        assert all("trace" not in c for c in servers.calls)
        assert set(_run_json(rt, root)) == RUN_KEYS
        assert hub.session_trace is None and hub.served_toolkits == {}
        assert not list(tmp_path.rglob("agents.jsonl"))
        assert not (tmp_path / "proj" / ".toolbase" / "traces").exists()
    finally:
        rt.shutdown()


def test_on_registers_agents_hands_out_handles_and_records_the_run(tmp_path):
    servers = Servers()
    hub = _hub(tmp_path, servers, trace=True)
    rt, root, child = _tree(tmp_path, hub)
    trace_dir = tmp_path / "proj" / ".toolbase" / "traces" / "sess-1"
    try:
        mounts = {a: ToolbaseMount(hub, rt.record_for(a), sandbox=rt.sandbox_of(a)) for a in (root, child)}
        for m in mounts.values():
            m.sync()
        # Each agent's orchestrators get that agent's own handle.
        handles = {c["trace"].agent: c["trace"] for c in servers.calls}
        assert set(handles) == {root.id, child.id}
        assert all(Path(h.dir) == trace_dir for h in handles.values())

        # A re-serve (a live grant) reuses the same identity and handle.
        rt.grant_toolkits(granter=root, target=child, toolkits=parse_toolkits(["calc__add"]))
        mounts[child].sync()
        assert len(servers.calls) == 3
        assert servers.calls[-1]["trace"] is handles[child.id]

        started = [e for e in _jsonl(trace_dir / "agents.jsonl") if e["event"] == "agent_started"]
        by = {e["id"]: e for e in started}
        assert len(started) == 2
        assert by[root.id]["parent"] is None and by[root.id]["label"] == "lead"
        assert by[child.id]["parent"] == root.id
        assert by[child.id]["model"] == "gpt-5.5-mini" and by[child.id]["engine"] == "codex"
        assert {e["harness"] for e in started} == {"conjure"}

        run = _run_json(rt, root)
        assert RUN_KEYS <= set(run)
        # The run directory is not under the root's sandbox here: absolute.
        assert Path(run["trace"]) == trace_dir
        assert run["toolkits"] == {"calc": "1.2.0", "hep": "3.0.0"}  # pin, else catalog

        rt.terminate(child)
        ended = [e["id"] for e in _jsonl(trace_dir / "agents.jsonl") if e["event"] == "agent_ended"]
        assert ended == [child.id]
    finally:
        rt.shutdown()
    ended = [e["id"] for e in _jsonl(trace_dir / "agents.jsonl") if e["event"] == "agent_ended"]
    assert ended == [child.id, root.id]  # the session's end ends the rest, once each


def test_messages_log_spawns_sends_and_terminations(tmp_path):
    hub = _hub(tmp_path, Servers(), trace=True)
    rt, root, child = _tree(tmp_path, hub)
    try:
        rt.dispatch_batch([(root, child, {"task": "go"})])
        rt.terminate(child, requested_by="lead")
    finally:
        rt.shutdown()
    msgs = _jsonl(hub.session_trace.dir / "messages.jsonl")
    spawns = [m for m in msgs if m["event"] == "spawn"]
    assert [(m["from"], m["to"]) for m in spawns] == [(None, root.id), (root.id, child.id)]
    assert spawns[1]["role_prompt"] == "worker brief"
    assert spawns[1]["initial_message"] == "do the thing"
    send = next(m for m in msgs if m["event"] == "send")
    assert (send["from"], send["to"], send["body"]) == (root.id, child.id, {"task": "go"})
    term = next(m for m in msgs if m["event"] == "terminate")
    assert (term["from"], term["to"]) == ("lead", child.id)
    assert all("ts" in m for m in msgs)


def test_trace_inside_the_root_sandbox_is_recorded_relative(tmp_path):
    # A phenomaton trial: the run directory is both the toolbase project
    # and the root's sandbox.
    hub = ToolbaseHub(project_root=tmp_path / "run", authority="all", catalog_loader=_catalog,
                      server_factory=Servers(), version_pins=dict, trace=True)
    rt = Runtime(store_dir=tmp_path / "store", session_id="s2", toolbase=hub)
    try:
        root = rt.root(AgentSpec(role_prompt="r", sandbox_dir=str(tmp_path / "run")))
        assert _run_json(rt, root)["trace"] == str(Path(".toolbase") / "traces" / "s2")
        assert _run_json(rt, root)["toolkits"] == {}
    finally:
        rt.shutdown()


def test_environment_overrides_config_as_for_tb_serve(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOLBASE_TRACE", "1")
    monkeypatch.setenv("TOOLBASE_TRACE_DIR", str(tmp_path / "elsewhere"))
    hub = _hub(tmp_path, Servers())  # config off
    rt = Runtime(store_dir=tmp_path / "store", toolbase=hub)
    try:
        rt.root(AgentSpec(role_prompt="r"))
        assert hub.session_trace.dir == tmp_path / "elsewhere"
        assert (tmp_path / "elsewhere" / "agents.jsonl").is_file()
    finally:
        rt.shutdown()

    monkeypatch.setenv("TOOLBASE_TRACE", "0")
    hub = _hub(tmp_path, Servers(), trace=True)  # config on, env forces off
    rt = Runtime(store_dir=tmp_path / "store", toolbase=hub)
    try:
        assert hub.session_trace is None
    finally:
        rt.shutdown()


def test_configured_trace_dir_is_under_the_project(tmp_path):
    hub = _hub(tmp_path, Servers(), trace=True, trace_dir="tr/here")
    rt = Runtime(store_dir=tmp_path / "store", toolbase=hub)
    try:
        assert hub.session_trace.dir == tmp_path / "proj" / "tr" / "here"
    finally:
        rt.shutdown()


def test_toolbase_without_trace_runs_untraced_with_a_message(tmp_path, monkeypatch, capsys):
    import conjure.trace as ct

    def missing():
        raise ct.TraceUnavailable("toolbase.trace is requested but the installed toolbase has none")

    monkeypatch.setattr(ct, "trace_api", missing)
    servers = Servers()
    hub = _hub(tmp_path, servers, trace=True)
    rt, root, child = _tree(tmp_path, hub)
    try:
        ToolbaseMount(hub, rt.record_for(child), sandbox=None).sync()
        assert "trace" not in servers.calls[0]
        assert "toolbase.trace is requested" in capsys.readouterr().err
        run = _run_json(rt, root)
        assert run["trace"] is None and "toolbase.trace" in run["trace_error"]
    finally:
        rt.shutdown()


def test_default_server_factory_passes_the_handle_to_each_orchestrator(tmp_path, monkeypatch):
    import toolbase.serve.loadouts as loadouts
    import toolbase.serve.orchestrator as orchestrator
    from toolbase.trace import Trace

    from conjure.toolbase import ToolkitGrant, _default_server_factory

    built: list[dict[str, Any]] = []

    class FakeOrchestrator:
        def __init__(self, *, console=None, loadout=None, call_timeout_s=None,
                     config_overrides=None, trace=None):
            built.append({"kit": next(iter(loadout.toolkits)), "trace": trace,
                          "base": (config_overrides or {}).get("base_directory")})

        def start(self):
            return []

        def shutdown(self):
            pass

    def no_loadout(*a, **k):
        raise loadouts.NoActiveLoadoutError("none")

    monkeypatch.setattr(orchestrator, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(loadouts, "resolve_loadout", no_loadout)
    assert "trace" in inspect.signature(_default_server_factory).parameters
    handle = Trace(tmp_path / "t", agent="ag-x")
    serving = _default_server_factory(
        toolset={"hep": ToolkitGrant(), "calc": ToolkitGrant()}, sandbox=tmp_path,
        project_root=tmp_path, loadout=None, call_timeout_s=5.0, trace=handle,
    )
    serving.close()
    assert sorted(b["kit"] for b in built) == ["calc", "hep"]
    assert all(b["trace"] is handle and b["base"] == str(tmp_path) for b in built)


def test_runner_registers_each_engines_default_model(tmp_path):
    from types import SimpleNamespace

    from conjure.runner import _trace_agent_info

    cfg = SimpleNamespace(codex=SimpleNamespace(child_model="gpt-child"),
                          llms={"default": SimpleNamespace(model="llm-model")})
    rt = Runtime(store_dir=tmp_path / "store")
    try:
        root = rt.root(AgentSpec(role_prompt="r", engine="codex"))
        kids = {e: rt._spawn(parent=root, spec=AgentSpec(role_prompt="c", engine=e))
                for e in ("codex", "claude_agent", "orchestral")}
        info = {e: _trace_agent_info(rt.record_for(a), rt, cfg) for e, a in kids.items()}
        assert info["codex"] == {"engine": "codex", "model": "gpt-child"}
        assert info["claude_agent"] == {"engine": "claude_agent", "model": "haiku"}
        assert info["orchestral"] == {"engine": "orchestral", "model": "llm-model"}
        assert _trace_agent_info(rt.record_for(root), rt, cfg)["model"] is None
    finally:
        rt.shutdown()


def test_a_passive_collector_is_registered_without_a_model(tmp_path):
    hub = _hub(tmp_path, Servers(), trace=True)
    rt, root, child = _tree(tmp_path, hub)
    try:
        col = rt._spawn(parent=root, spec=AgentSpec(role_prompt="(collector)",
                                                    label="call-collector", model="sonnet", lazy=True))
    finally:
        rt.shutdown()
    started = {e["id"]: e for e in _jsonl(hub.session_trace.dir / "agents.jsonl")
               if e["event"] == "agent_started"}
    assert started[col.id]["model"] is None and started[col.id]["engine"] is None


def test_shell_commands_are_logged_with_tooltraces_ids(tmp_path):
    import hashlib

    hub = _hub(tmp_path, Servers(), trace=True)
    rt, root, child = _tree(tmp_path, hub)
    seen = []
    try:
        sink = rt._trace.stream(child.id, seen.append)
        sink({"kind": "stream_end", "tool_calls": [
            {"id": "toolu_A", "name": "Bash", "args": {"command": "python3 scripts/reach.py"}},
            {"id": "toolu_B", "name": "Read", "args": {"file_path": "x"}}]})
        sink({"kind": "tool", "tool_call_id": "toolu_B", "text": "", "failed": False})
        sink({"kind": "tool", "tool_call_id": "toolu_A", "text": "ok", "failed": True})
        sink({"kind": "stream_end", "tool_calls": [{"id": "it_1", "name": "shell", "args": {"command": "ls"}}]})
        sink({"kind": "tool", "tool_call_id": "it_1", "text": "", "failed": False})
    finally:
        rt.shutdown()
    assert len(seen) == 5                                  # every event still reaches the log
    cmds = _jsonl(hub.session_trace.dir / "commands.jsonl")
    assert [(c["agent"], c["command"], c["ok"]) for c in cmds] == [
        (child.id, "python3 scripts/reach.py", False), (child.id, "ls", True)]
    assert cmds[0]["id"] == "cmd-" + hashlib.sha256(b"toolu_A").hexdigest()[:10]
    assert cmds[0]["ts_start"] <= cmds[0]["ts_end"]
    assert cmds[1]["id"] != "cmd-" + hashlib.sha256(b"it_1").hexdigest()[:10]   # codex ids are salted


def test_codex_item_ids_that_repeat_give_distinct_commands(tmp_path):
    hub = _hub(tmp_path, Servers(), trace=True)
    rt, root, child = _tree(tmp_path, hub)
    try:
        for agent in (root.id, child.id, child.id):
            sink = rt._trace.stream(agent, None)
            sink({"kind": "stream_end", "tool_calls": [{"id": "item_5", "name": "shell", "args": {"command": "ls"}}]})
            sink({"kind": "tool", "tool_call_id": "item_5", "failed": False})
    finally:
        rt.shutdown()
    cmds = _jsonl(hub.session_trace.dir / "commands.jsonl")
    assert len({c["id"] for c in cmds}) == 3


def test_file_edits_are_logged_with_the_paths_they_write(tmp_path):
    hub = _hub(tmp_path, Servers(), trace=True)
    rt, root, child = _tree(tmp_path, hub)
    try:
        sink = rt._trace.stream(child.id, None)
        sink({"kind": "stream_end", "tool_calls": [
            {"id": "toolu_W", "name": "Write", "args": {"file_path": "/w/scripts/x.py", "content": "print(1)"}},
            {"id": "item_3", "name": "edit", "args": {"changes": [{"path": "/w/a.tex", "kind": "update"}]}}]})
        sink({"kind": "tool", "tool_call_id": "toolu_W", "failed": False})
        sink({"kind": "tool", "tool_call_id": "item_3", "failed": False})
    finally:
        rt.shutdown()
    cmds = _jsonl(hub.session_trace.dir / "commands.jsonl")
    assert [(c["tool"], c["writes"]) for c in cmds] == [("Write", ["/w/scripts/x.py"]), ("edit", ["/w/a.tex"])]
