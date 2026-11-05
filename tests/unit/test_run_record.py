"""The run record: what a run is, kept in the root's sandbox."""

from __future__ import annotations

import json

from conjure import __version__
from conjure.record import AgentSpec
from conjure.runtime import Runtime


def _read(rt, root):
    return json.loads((rt.sandbox_of(root) / ".conjure" / "run.json").read_text())


def test_root_spawn_writes_the_record(tmp_path):
    rt = Runtime(store_dir=tmp_path / "store")
    try:
        root = rt.root(AgentSpec(role_prompt="r", label="lead", engine="claude", model="opus"))
        rec = _read(rt, root)
        assert rec["harness"] == "Conjure"
        assert rec["version"] == __version__
        assert [a["label"] for a in rec["agents"]] == ["lead"]
        assert rec["agents"][0]["engine"] == "claude"
        assert rec["models"] == ["opus"]
    finally:
        rt.shutdown()


def test_children_and_reported_models_are_added(tmp_path):
    rt = Runtime(store_dir=tmp_path / "store")
    try:
        root = rt.root(AgentSpec(role_prompt="r", label="lead", engine="claude"))
        child = rt._spawn(parent=root, spec=AgentSpec(role_prompt="w", label="worker", engine="codex"))
        rt.note_agent_meta(root, {"engine": "claude", "model": "claude-opus-5-5"})
        rt.note_agent_meta(child, {"model": "gpt-5.5"})
        rec = _read(rt, root)
        by = {a["label"]: a for a in rec["agents"]}
        assert by["worker"]["parent"] == root.id
        assert by["lead"]["model"] == "claude-opus-5-5"
        assert rec["models"] == ["claude-opus-5-5", "gpt-5.5"]
        assert "user" not in by and "system" not in by
    finally:
        rt.shutdown()


def test_no_sandbox_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rt = Runtime()
    try:
        root = rt.root(AgentSpec(role_prompt="r"))
        assert rt.sandbox_of(root) is None
        assert rt.run_record()["harness"] == "Conjure"
        assert not list(tmp_path.rglob("run.json"))
    finally:
        rt.shutdown()
