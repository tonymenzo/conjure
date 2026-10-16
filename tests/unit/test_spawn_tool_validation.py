"""Unknown tool groups are refused before a child exists.

Tool groups used to be checked only while the child's engine was built, after
the spawn had been recorded and journaled. A refused spawn therefore left a
terminated "spawn-failed" child and a ``spawn`` journal entry, and a benchmark
counting workers from the journal counted it (seen with an agent that passed
Claude Code tool names, ``Read``/``Bash``, to an orchestral child, then retried
with groups).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conjure.engines.orchestral import make_orchestral_engine_factory
from conjure.record import AgentSpec
from conjure.runtime import Runtime

from tests.unit.test_spawn_listener import _FakeLLM


def _journal_spawns(rt: Runtime) -> list[dict]:
    rt._journal.flush()
    path = Path(rt.session_dir) / "journal.jsonl"
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    return [r["payload"] for r in rows if r["kind"] == "spawn"]


@pytest.fixture
def rt(tmp_path):
    factory = make_orchestral_engine_factory(llms={"default": _FakeLLM()})
    runtime = Runtime(store_dir=tmp_path, engine_factory=factory)
    yield runtime
    runtime.shutdown()


def test_bad_tool_group_refused_without_a_trace(rt):
    root = rt.root(AgentSpec(role_prompt="r", label="root", tools=["primitive"]))
    with pytest.raises(ValueError, match="unknown tool group: 'Read'"):
        rt._spawn(parent=root, spec=AgentSpec(
            role_prompt="w", label="worker", tools=["Read", "Bash"]))
    assert rt.record_for(root).children == set()
    assert [s["addr"]["label"] for s in _journal_spawns(rt)] == ["root"]


def test_retry_with_groups_yields_exactly_one_journaled_child(rt):
    root = rt.root(AgentSpec(role_prompt="r", label="root", tools=["primitive"]))
    with pytest.raises(ValueError):
        rt._spawn(parent=root, spec=AgentSpec(role_prompt="w", label="worker", tools=["Read"]))
    child = rt._spawn(parent=root, spec=AgentSpec(
        role_prompt="w", label="worker", tools=["filesystem", "primitive"]))
    assert rt.record_for(root).children == {child}
    children = [s for s in _journal_spawns(rt) if s.get("parent")]
    assert [c["addr"]["id"] for c in children] == [child.id]


def test_one_bad_spec_refuses_the_whole_batch(rt):
    root = rt.root(AgentSpec(role_prompt="r", label="root", tools=["primitive"]))
    with pytest.raises(ValueError, match="unknown tool group"):
        rt._spawn_batch(parent=root, specs=[
            AgentSpec(role_prompt="a", label="a", tools=["primitive"]),
            AgentSpec(role_prompt="b", label="b", tools=["nope"]),
        ])
    assert rt.record_for(root).children == set()
    assert len(_journal_spawns(rt)) == 1


def test_default_tools_still_spawn(rt):
    root = rt.root(AgentSpec(role_prompt="r", label="root"))
    child = rt._spawn(parent=root, spec=AgentSpec(role_prompt="w", label="worker"))
    assert rt.record_for(root).children == {child}


def test_runner_dispatch_validates_orchestral_children_only(tmp_path, monkeypatch):
    """build_runtime's per-engine dispatcher routes the check: orchestral
    children are held to tool groups; claude_agent tools are Claude Code
    tool names and are left for that engine."""
    from conjure.config import load_config_from_mapping
    from conjure.runner import build_runtime

    monkeypatch.setenv("FAKE_KEY", "x")
    cfg = load_config_from_mapping({
        "runtime": {"store_dir": str(tmp_path), "max_depth": 1},
        "llms": {"gw": {"provider": "openai_compatible", "base_url": "http://127.0.0.1:9/v1",
                        "api_key_env": "FAKE_KEY", "model": "m"}},
        "root": {"role_prompt": "r", "engine": "orchestral", "llm": "gw", "tools": ["primitive"]},
    })
    runtime, root = build_runtime(cfg)
    try:
        validate = runtime._engine_factory.validate_spec
        with pytest.raises(ValueError, match="unknown tool group: 'Read'"):
            validate(AgentSpec(role_prompt="w", engine="orchestral", llm="gw", tools=["Read"]), root)
        validate(AgentSpec(role_prompt="w", engine="orchestral", llm="gw", tools=["filesystem"]), root)
        validate(AgentSpec(role_prompt="w", engine="claude_agent", tools=["Read", "Bash"]), root)
        with pytest.raises(ValueError, match="unknown tool group"):
            runtime._spawn(parent=root, spec=AgentSpec(
                role_prompt="w", engine="orchestral", llm="gw", tools=["Bash"]))
        assert runtime.record_for(root).children == set()
    finally:
        runtime.shutdown()
