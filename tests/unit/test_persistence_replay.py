"""Tests for spawn.persistence and Runtime.replay."""

from __future__ import annotations

from pathlib import Path

import pytest

from spawn.address import Address
from spawn.persistence import Journal
from spawn.record import AgentSpec
from spawn.runtime import Runtime


def test_journal_writes_and_reads_back(tmp_path: Path):
    j = Journal(tmp_path)
    j.write("spawn", {"addr": {"id": "ag-1", "label": "x"}})
    j.write("send", {"envelope": {"msg_id": "msg-1"}})
    j.close()

    entries = list(Journal.read_all(tmp_path))
    assert [e["kind"] for e in entries] == ["spawn", "send"]
    assert entries[0]["payload"]["addr"]["id"] == "ag-1"


def test_no_journal_when_store_dir_none():
    j = Journal(None)
    assert not j.is_active
    j.write("spawn", {})
    j.close()


def test_replay_reconstructs_spawn_tree(tmp_path: Path):
    rt = Runtime(store_dir=tmp_path)
    session_dir = rt.session_dir
    root = rt.root(AgentSpec(role_prompt="root", label="root"))
    child_a = rt._spawn(parent=root, spec=AgentSpec(role_prompt="a", label="a"))
    child_b = rt._spawn(parent=root, spec=AgentSpec(role_prompt="b", label="b"))
    grand = rt._spawn(parent=child_a, spec=AgentSpec(role_prompt="g", label="g"))
    rt.shutdown()

    assert session_dir is not None
    rt2 = Runtime.replay(session_dir)
    # All four addresses are restored
    assert rt2.root_addr == root
    rec_root = rt2.record_for(root)
    assert child_a in rec_root.children
    assert child_b in rec_root.children
    assert grand in rt2.record_for(child_a).children


def test_replay_preserves_inbox_contents(tmp_path: Path):
    rt = Runtime(store_dir=tmp_path)
    session_dir = rt.session_dir
    root = rt.root(AgentSpec(role_prompt="root"))
    child = rt._spawn(parent=root, spec=AgentSpec(role_prompt="c"))
    rt.send_external(to=child, body={"task": "alpha"})
    rt.send_external(to=child, body={"task": "beta"})
    rt.send_external(to=root, body={"task": "gamma"})
    rt.shutdown()

    assert session_dir is not None
    rt2 = Runtime.replay(session_dir)
    child_inbox = rt2.read_inbox(child)
    root_inbox = rt2.read_inbox(root)
    assert [e.body for e in child_inbox] == [{"task": "alpha"}, {"task": "beta"}]
    assert [e.body for e in root_inbox] == [{"task": "gamma"}]
    # Seq numbers preserved
    assert [e.seq for e in child_inbox] == [1, 2]
    assert [e.seq for e in root_inbox] == [1]


def test_replay_preserves_termination(tmp_path: Path):
    rt = Runtime(store_dir=tmp_path)
    session_dir = rt.session_dir
    root = rt.root(AgentSpec(role_prompt="root"))
    child = rt._spawn(parent=root, spec=AgentSpec(role_prompt="c"))
    grand = rt._spawn(parent=child, spec=AgentSpec(role_prompt="g"))
    rt.terminate(child, cascade=True)
    rt.shutdown()

    assert session_dir is not None
    rt2 = Runtime.replay(session_dir)
    assert rt2.record_for(child).status == "terminated"
    assert rt2.record_for(grand).status == "terminated"
    assert rt2.record_for(root).status != "terminated"


def test_replay_runtime_has_no_journal(tmp_path: Path):
    rt = Runtime(store_dir=tmp_path)
    session_dir = rt.session_dir
    rt.root(AgentSpec(role_prompt="root"))
    rt.shutdown()

    assert session_dir is not None
    rt2 = Runtime.replay(session_dir)
    # Replay runtime should not be writing to the original journal —
    # additional ops must not append to the on-disk file.
    rt2.send_external(to=rt2.root_addr, body="post-replay")  # type: ignore[arg-type]
    entries_after = list(Journal.read_all(session_dir))
    # We had one spawn + one terminate (from shutdown? no, shutdown
    # itself doesn't journal — only explicit terminate does).
    kinds = [e["kind"] for e in entries_after]
    # Original session only had a spawn — the post-replay send must NOT
    # appear in the original journal.
    assert "send" not in kinds
