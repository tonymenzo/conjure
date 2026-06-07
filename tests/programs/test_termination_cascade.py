"""Terminating a middle node cascades to descendants but leaves the
root and any non-descendants untouched.
"""

from __future__ import annotations

from spawn.record import AgentSpec
from spawn.runtime import Runtime
from spawn.tools.primitives import spawn_impl, terminate_impl


def test_terminate_middle_cascades_to_grandchild():
    rt = Runtime()
    root = rt.root(AgentSpec(role_prompt="root"))
    root_token = rt.record_for(root).token

    child = spawn_impl(token=root_token, role_prompt="c", label="child")
    child_addr = rt.address_by_id(child["address"])
    child_token = rt.record_for(child_addr).token

    grand = spawn_impl(token=child_token, role_prompt="g", label="grand")
    grand_addr = rt.address_by_id(grand["address"])

    # Root terminates child with cascade.
    out = terminate_impl(token=root_token, address=child["address"], cascade=True)
    assert out["ok"] is True
    assert set(out["terminated"]) == {child["address"], grand["address"]}

    assert rt.record_for(child_addr).status == "terminated"
    assert rt.record_for(grand_addr).status == "terminated"
    assert rt.record_for(root).status != "terminated"
    rt.shutdown()


def test_terminate_without_cascade_leaves_descendants_alive():
    rt = Runtime()
    root = rt.root(AgentSpec(role_prompt="root"))
    root_token = rt.record_for(root).token

    child = spawn_impl(token=root_token, role_prompt="c", label="child")
    child_token = rt.record_for(rt.address_by_id(child["address"])).token
    grand = spawn_impl(token=child_token, role_prompt="g", label="grand")

    terminate_impl(token=root_token, address=child["address"], cascade=False)
    assert rt.record_for(rt.address_by_id(child["address"])).status == "terminated"
    assert rt.record_for(rt.address_by_id(grand["address"])).status != "terminated"
    rt.shutdown()


def test_non_descendant_cannot_terminate():
    rt = Runtime()
    root = rt.root(AgentSpec(role_prompt="root"))
    root_token = rt.record_for(root).token

    a = spawn_impl(token=root_token, role_prompt="a")
    b = spawn_impl(token=root_token, role_prompt="b")
    a_token = rt.record_for(rt.address_by_id(a["address"])).token

    out = terminate_impl(token=a_token, address=b["address"])
    assert out["ok"] is False
    assert out["code"] == "not_permitted"
    assert rt.record_for(rt.address_by_id(b["address"])).status != "terminated"
    rt.shutdown()
