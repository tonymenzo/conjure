"""Tests for combinator.runtime — spawn/terminate/shutdown lifecycle."""

from __future__ import annotations

import pytest

from combinator.address import USER
from combinator.errors import (
    CombinatorError,
    MaxDepthExceeded,
    NoSuchAddress,
    Terminated,
)
from combinator.record import AgentSpec
from combinator.runtime import Runtime


def test_root_creates_addressable_agent():
    rt = Runtime()
    addr = rt.root(AgentSpec(role_prompt="root", label="root"))
    assert addr.label == "root"
    rec = rt.record_for(addr)
    assert rec.parent is None
    assert rec.status == "lazy"
    assert addr in rec.capabilities


def test_root_can_only_be_spawned_once():
    rt = Runtime()
    rt.root(AgentSpec(role_prompt="root"))
    with pytest.raises(CombinatorError):
        rt.root(AgentSpec(role_prompt="second-root"))


def test_internal_spawn_links_parent_and_child():
    rt = Runtime()
    root = rt.root(AgentSpec(role_prompt="root"))
    child = rt._spawn(parent=root, spec=AgentSpec(role_prompt="child", label="c"))
    assert child in rt.record_for(root).children
    assert rt.record_for(child).parent == root
    # child's capability set includes parent
    assert root in rt.record_for(child).capabilities


def test_spawn_with_handed_in_capabilities():
    rt = Runtime()
    root = rt.root(AgentSpec(role_prompt="root"))
    sibling = rt._spawn(parent=root, spec=AgentSpec(role_prompt="sib"))
    child = rt._spawn(
        parent=root,
        spec=AgentSpec(role_prompt="child", capabilities=[sibling]),
    )
    caps = rt.record_for(child).capabilities
    assert sibling in caps
    assert root in caps
    assert child in caps


def test_send_external_to_root():
    rt = Runtime()
    addr = rt.root(AgentSpec(role_prompt="root"))
    msg_id = rt.send_external(to=addr, body={"task": "do thing"})
    inbox = rt.read_inbox(addr)
    assert len(inbox) == 1
    assert inbox[0].msg_id == msg_id
    assert inbox[0].from_ == USER
    assert inbox[0].body == {"task": "do thing"}


def test_send_external_to_unknown_address_raises():
    from combinator.address import Address
    rt = Runtime()
    with pytest.raises(NoSuchAddress):
        rt.send_external(to=Address(id="ag-bogus"), body="x")


def test_send_external_to_terminated_raises():
    rt = Runtime()
    addr = rt.root(AgentSpec(role_prompt="root"))
    rt.terminate(addr)
    with pytest.raises(Terminated):
        rt.send_external(to=addr, body="x")


def test_terminate_cascades_to_descendants():
    rt = Runtime()
    root = rt.root(AgentSpec(role_prompt="root"))
    child = rt._spawn(parent=root, spec=AgentSpec(role_prompt="child"))
    grand = rt._spawn(parent=child, spec=AgentSpec(role_prompt="grand"))

    terminated = rt.terminate(child, cascade=True)
    assert set(terminated) == {child, grand}
    assert rt.record_for(child).status == "terminated"
    assert rt.record_for(grand).status == "terminated"
    assert rt.record_for(root).status != "terminated"


def test_terminate_without_cascade_leaves_descendants_alive():
    rt = Runtime()
    root = rt.root(AgentSpec(role_prompt="root"))
    child = rt._spawn(parent=root, spec=AgentSpec(role_prompt="child"))
    grand = rt._spawn(parent=child, spec=AgentSpec(role_prompt="grand"))

    rt.terminate(child, cascade=False)
    assert rt.record_for(child).status == "terminated"
    assert rt.record_for(grand).status == "lazy"


def test_double_terminate_is_noop():
    rt = Runtime()
    addr = rt.root(AgentSpec(role_prompt="root"))
    first = rt.terminate(addr)
    second = rt.terminate(addr)
    assert first == [addr]
    assert second == []


def test_resolve_token_returns_owning_address():
    rt = Runtime()
    addr = rt.root(AgentSpec(role_prompt="root"))
    token = rt.record_for(addr).token
    assert rt.resolve_token(token) == addr


def test_resolve_unknown_token_raises():
    rt = Runtime()
    with pytest.raises(NoSuchAddress):
        rt.resolve_token("not-a-real-token")


def test_shutdown_terminates_all_agents():
    rt = Runtime()
    root = rt.root(AgentSpec(role_prompt="root"))
    child = rt._spawn(parent=root, spec=AgentSpec(role_prompt="c"))
    rt.shutdown()
    assert rt.record_for(root).status == "terminated"
    assert rt.record_for(child).status == "terminated"


def test_operations_after_shutdown_raise():
    rt = Runtime()
    rt.root(AgentSpec(role_prompt="root"))
    rt.shutdown()
    with pytest.raises(CombinatorError):
        rt.root(AgentSpec(role_prompt="another"))


def test_root_is_depth_zero():
    rt = Runtime()
    root = rt.root(AgentSpec(role_prompt="root"))
    assert rt.record_for(root).depth == 0


def test_spawn_increments_depth():
    rt = Runtime(max_depth=4)
    root = rt.root(AgentSpec(role_prompt="root"))
    c1 = rt._spawn(parent=root, spec=AgentSpec(role_prompt="c1"))
    c2 = rt._spawn(parent=c1, spec=AgentSpec(role_prompt="c2"))
    assert rt.record_for(c1).depth == 1
    assert rt.record_for(c2).depth == 2


def test_spawn_beyond_max_depth_raises():
    rt = Runtime(max_depth=2)
    root = rt.root(AgentSpec(role_prompt="root"))         # depth 0
    a = rt._spawn(parent=root, spec=AgentSpec(role_prompt="a"))   # depth 1
    b = rt._spawn(parent=a, spec=AgentSpec(role_prompt="b"))      # depth 2
    with pytest.raises(MaxDepthExceeded):
        rt._spawn(parent=b, spec=AgentSpec(role_prompt="c"))      # would be 3


def test_spawn_at_exact_max_depth_succeeds():
    rt = Runtime(max_depth=2)
    root = rt.root(AgentSpec(role_prompt="root"))
    a = rt._spawn(parent=root, spec=AgentSpec(role_prompt="a"))
    b = rt._spawn(parent=a, spec=AgentSpec(role_prompt="b"))
    assert rt.record_for(b).depth == 2  # at the limit, not over
