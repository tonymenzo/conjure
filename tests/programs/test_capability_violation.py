"""Capability violation: sibling agents can't message each other
without an introduction.

Two siblings ``A`` and ``B`` are spawned under the same root. Without
``introduce``, ``A`` cannot send to ``B`` — the send tool returns
``not_permitted``. After ``introduce``, the same send succeeds.
"""

from __future__ import annotations

from combinator.record import AgentSpec
from combinator.runtime import Runtime
from combinator.tools.primitives import introduce_impl, send_impl, spawn_impl


def test_siblings_cannot_send_without_introduction():
    rt = Runtime()
    root = rt.root(AgentSpec(role_prompt="root"))
    root_token = rt.record_for(root).token

    a_out = spawn_impl(token=root_token, role_prompt="a", label="a")
    b_out = spawn_impl(token=root_token, role_prompt="b", label="b")
    a_token = rt.record_for(rt.address_by_id(a_out["address"])).token

    blocked = send_impl(token=a_token, to=b_out["address"], body="hi")
    assert blocked["ok"] is False
    assert blocked["code"] == "not_permitted"

    # Root introduces b to a.
    intro = introduce_impl(
        token=root_token,
        child=a_out["address"],
        capability=b_out["address"],
    )
    assert intro["ok"] is True

    # Now a can send to b.
    ok = send_impl(token=a_token, to=b_out["address"], body="hi-now")
    assert ok["ok"] is True

    inbox = rt.read_inbox(rt.address_by_id(b_out["address"]))
    assert any(env.body == "hi-now" for env in inbox)
    rt.shutdown()
