"""Tool argument schemas for the UI's call previews: compacted from the
tools' JSON schemas, journaled with the agent's provenance, and served
live and in a replay."""

from __future__ import annotations

import time

from conjure import AgentSpec, BehaviorRegistry, Runtime
from conjure.control import ControlServer
from conjure.scripted import ScriptedEngine
from conjure.tool_schemas import compact, from_tools
from conjure.tools.filesystem import ReadTool
from conjure.web import SessionView


def test_compact_keeps_order_required_and_small_defaults():
    schema = {
        "properties": {
            "initial": {"type": "array"},
            "final": {"type": "array"},
            "visualize": {"type": "boolean", "default": False},
            "options": {"type": "object", "default": {"x": "y" * 500}},
        },
        "required": ["initial", "final", "not_a_property"],
    }
    out = compact(schema)
    assert out["order"] == ["initial", "final", "visualize", "options"]
    assert out["required"] == ["initial", "final"]
    assert out["defaults"] == {"visualize": False}   # a large default isn't journaled
    assert compact(None) == {"order": [], "required": [], "defaults": {}}


def test_from_tools_reads_orchestral_tools():
    out = from_tools([ReadTool])
    [(name, schema)] = out.items()
    assert schema["required"] == ["path"]
    assert schema["defaults"] == {"offset": 0, "limit": 0}
    # The caller's token is internal state, not an argument.
    assert "runtime_token" not in schema["order"]
    renamed = from_tools([ReadTool], name=lambda spec, tool: f"mcp__toolbase__{spec.name}")
    assert list(renamed) == [f"mcp__toolbase__{name}"]


SCHEMAS = {"heptapod__EnumerateDiagrams": {
    "order": ["initial", "final", "model", "visualize"],
    "required": ["initial", "final"],
    "defaults": {"visualize": False},
}}


class _Engine(ScriptedEngine):
    def tool_schemas(self):
        return SCHEMAS


def test_schemas_are_journaled_and_served_in_a_replay(tmp_path):
    reg = BehaviorRegistry()
    reg.register("work", lambda *a, **kw: "done")
    base = reg.factory()

    def factory(record, runtime):
        engine = base(record, runtime)
        return _Engine(record=record, runtime=runtime, behavior=engine.behavior)

    rt = Runtime(store_dir=tmp_path, session_id="s1", engine_factory=factory)
    root = rt.root(AgentSpec(role_prompt="work", label="root"))
    rt.send_external(to=root, body="go")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and rt.record_for(root).meta.get("tool_schemas") is None:
        time.sleep(0.02)
    live = SessionView.live_session(rt, ControlServer(runtime=rt, socket_path=tmp_path / "x.sock"), name="s1")
    assert live.agent(root.id)["tool_schemas"] == SCHEMAS
    rt.shutdown()

    replay = SessionView.replay_session(rt.session_dir)
    assert replay.agent(root.id)["tool_schemas"] == SCHEMAS
