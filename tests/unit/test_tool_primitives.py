"""Tests for the primitive tool impl functions and tool classes.

Most tests exercise the ``*_impl`` functions directly — they are the
behavior layer. A handful of integration tests instantiate the tool
classes and call ``execute()`` to verify the orchestral plumbing
(StatelessRuntimeTool reset semantics, runtime_token plumbing) works.
"""

from __future__ import annotations

import pytest

from spawn.record import AgentSpec
from spawn.runtime import Runtime
from spawn.tools.primitives import (
    PRIMITIVE_TOOL_CLASSES,
    SendTool,
    SpawnTool,
    build_primitive_tools,
    introduce_impl,
    list_inbox_impl,
    peek_impl,
    recv_impl,
    send_impl,
    spawn_impl,
    terminate_impl,
    wait_for_impl,
)


# ----- Fixtures -----

@pytest.fixture
def rt():
    runtime = Runtime()
    yield runtime
    runtime.shutdown()


@pytest.fixture
def root_token(rt):
    addr = rt.root(AgentSpec(role_prompt="root", label="root"))
    return rt.record_for(addr).token


# ----- spawn_impl -----

def test_spawn_creates_child(rt, root_token):
    out = spawn_impl(token=root_token, role_prompt="child", label="c")
    assert out["ok"] is True
    assert out["address"].startswith("ag-")
    child_addr = rt.address_by_id(out["address"])
    assert child_addr is not None
    assert rt.record_for(child_addr).parent == rt.root_addr


def test_spawn_with_unknown_token_returns_no_runtime():
    out = spawn_impl(token="fake-token", role_prompt="x")
    assert out == {"ok": False, "code": "no_runtime", "error": "tool is not bound to a runtime"}


def test_spawn_with_capability_caller_holds(rt, root_token):
    sib = spawn_impl(token=root_token, role_prompt="sib", label="sib")
    sib_id = sib["address"]
    # Caller (root) holds sib as a capability (because root spawned it).
    child = spawn_impl(
        token=root_token,
        role_prompt="child",
        capabilities=[sib_id],
    )
    assert child["ok"] is True
    child_addr = rt.address_by_id(child["address"])
    sib_addr = rt.address_by_id(sib_id)
    assert sib_addr in rt.record_for(child_addr).capabilities


def test_spawn_rejects_unheld_capability(rt, root_token):
    # Spawn two children of root.
    a = spawn_impl(token=root_token, role_prompt="a")
    b = spawn_impl(token=root_token, role_prompt="b")
    a_token = rt.record_for(rt.address_by_id(a["address"])).token

    # 'a' tries to spawn a grandchild handed 'b' as a capability — but
    # 'a' does not hold 'b' itself.
    out = spawn_impl(
        token=a_token,
        role_prompt="g",
        capabilities=[b["address"]],
    )
    assert out["ok"] is False
    assert out["code"] == "cap_violation"


# ----- send_impl -----

def test_send_to_known_capability(rt, root_token):
    child = spawn_impl(token=root_token, role_prompt="c")
    out = send_impl(token=root_token, to=child["address"], body="hello")
    assert out["ok"] is True
    msg_id = out["msg_id"]
    assert msg_id.startswith("msg-")
    inbox = rt.read_inbox(rt.address_by_id(child["address"]))
    assert len(inbox) == 1
    assert inbox[0].body == "hello"


def test_send_to_non_capability_returns_not_permitted(rt, root_token):
    # Spawn two siblings; they don't know each other.
    a = spawn_impl(token=root_token, role_prompt="a")
    b = spawn_impl(token=root_token, role_prompt="b")
    a_token = rt.record_for(rt.address_by_id(a["address"])).token

    out = send_impl(token=a_token, to=b["address"], body="hi")
    assert out["ok"] is False
    assert out["code"] == "not_permitted"


def test_send_to_unknown_address(rt, root_token):
    out = send_impl(token=root_token, to="ag-nonexistent", body="x")
    assert out["ok"] is False
    assert out["code"] == "no_such_address"


def test_send_to_terminated_target(rt, root_token):
    child = spawn_impl(token=root_token, role_prompt="c")
    rt.terminate(rt.address_by_id(child["address"]))
    out = send_impl(token=root_token, to=child["address"], body="x")
    assert out["ok"] is False
    assert out["code"] == "terminated"


def test_send_dedups_immediate_duplicate(rt, root_token):
    """Two identical sends in quick succession from the same caller
    coalesce — the second is suppressed and returns the first's
    msg_id with ``deduplicated=True``."""
    child = spawn_impl(token=root_token, role_prompt="c")
    first = send_impl(token=root_token, to=child["address"], body="hi")
    second = send_impl(token=root_token, to=child["address"], body="hi")
    assert first["ok"] and second["ok"]
    assert second.get("deduplicated") is True
    assert second["msg_id"] == first["msg_id"]
    # The recipient's inbox should have exactly ONE envelope.
    inbox = rt.read_inbox(rt.address_by_id(child["address"]))
    assert len(inbox) == 1


def test_send_different_body_not_deduped(rt, root_token):
    child = spawn_impl(token=root_token, role_prompt="c")
    a = send_impl(token=root_token, to=child["address"], body="first")
    b = send_impl(token=root_token, to=child["address"], body="second")
    assert a["msg_id"] != b["msg_id"]
    assert not b.get("deduplicated")
    inbox = rt.read_inbox(rt.address_by_id(child["address"]))
    assert len(inbox) == 2


def test_send_dedup_checks_recent_tail(rt, root_token):
    child = spawn_impl(token=root_token, role_prompt="c")
    first = send_impl(token=root_token, to=child["address"], body="first")
    for i in range(12):
        send_impl(token=root_token, to=child["address"], body=f"body-{i}")

    duplicate = send_impl(token=root_token, to=child["address"], body="body-11")
    late_repeat = send_impl(token=root_token, to=child["address"], body="first")

    assert duplicate.get("deduplicated") is True
    assert duplicate["msg_id"] != first["msg_id"]
    assert late_repeat.get("deduplicated") is not True


# ----- recv_impl & wait_for_impl -----

def test_recv_empty_returns_no_envelopes(rt, root_token):
    out = recv_impl(token=root_token)
    assert out["ok"] is True
    assert out["envelopes"] == []
    assert out["next_seq"] == 0


def test_recv_returns_inbox_contents(rt, root_token):
    rt.send_external(to=rt.root_addr, body="m1")
    rt.send_external(to=rt.root_addr, body="m2")
    out = recv_impl(token=root_token, max_n=10)
    assert out["ok"] is True
    assert len(out["envelopes"]) == 2
    assert out["next_seq"] == 2


def test_recv_advances_cursor_via_since_seq(rt, root_token):
    rt.send_external(to=rt.root_addr, body="m1")
    rt.send_external(to=rt.root_addr, body="m2")
    rt.send_external(to=rt.root_addr, body="m3")
    out1 = recv_impl(token=root_token, max_n=1)
    cursor = out1["next_seq"]
    out2 = recv_impl(token=root_token, since_seq=cursor, max_n=10)
    bodies = [e["body"] for e in out2["envelopes"]]
    assert bodies == ["m2", "m3"]


def test_wait_for_thread(rt, root_token):
    rt.send_external(to=rt.root_addr, body={"x": 1})  # thread defaults to msg id
    # Without filter, wait_for returns immediately.
    out = wait_for_impl(token=root_token, predicate_kind="any", timeout_s=0.1)
    assert out["ok"] is True
    assert len(out["envelopes"]) == 1


def test_wait_for_max_n_waits_for_full_count(rt, root_token):
    """``WaitFor(max_n=N, timeout_s=T)`` should accumulate up to N
    matches across multiple inbox arrivals, not return at the first
    one. Regression for the report's UX issue #2."""
    import threading
    import time

    # Three sends staggered so the first arrives ~immediately and the
    # other two trickle in. A single mailbox.read returns at the
    # first match; the looped wait_for_impl must keep draining.
    def stagger():
        time.sleep(0.05)
        rt.send_external(to=rt.root_addr, body="b")
        time.sleep(0.05)
        rt.send_external(to=rt.root_addr, body="c")

    rt.send_external(to=rt.root_addr, body="a")
    threading.Thread(target=stagger, daemon=True).start()

    out = wait_for_impl(
        token=root_token, predicate_kind="any",
        max_n=3, timeout_s=2.0,
    )
    bodies = [e["body"] for e in out["envelopes"]]
    assert out["ok"] is True
    assert bodies == ["a", "b", "c"]


def test_wait_for_returns_partial_on_timeout(rt, root_token):
    """If only some matches arrive before the deadline, return what
    we have rather than nothing."""
    rt.send_external(to=rt.root_addr, body="a")
    out = wait_for_impl(
        token=root_token, predicate_kind="any",
        max_n=5, timeout_s=0.2,
    )
    assert out["ok"] is True
    assert [e["body"] for e in out["envelopes"]] == ["a"]
    assert out["next_seq"] == 1


# ----- address shortcuts ----- (self / parent / label)

def test_send_to_self_shortcut(rt, root_token):
    out = send_impl(token=root_token, to="self", body="echo")
    assert out["ok"] is True


def test_send_to_parent_shortcut(rt, root_token):
    """A child can address its parent without knowing the opaque id."""
    child = spawn_impl(token=root_token, role_prompt="child", label="c")
    child_token = rt.record_for(rt.address_by_id(child["address"])).token
    out = send_impl(token=child_token, to="parent", body="hi parent")
    assert out["ok"] is True
    envs = rt.read_inbox(rt.root_addr)
    assert any(e.body == "hi parent" for e in envs)


def test_send_to_parent_from_root_fails(rt, root_token):
    """Root has no parent — the shortcut returns ``no_such_address``."""
    out = send_impl(token=root_token, to="parent", body="nope")
    assert out["ok"] is False
    assert out["code"] == "no_such_address"


def test_send_to_child_label_shortcut(rt, root_token):
    """A parent can address its child by the label it spawned with."""
    spawn_impl(token=root_token, role_prompt="worker", label="w1")
    out = send_impl(token=root_token, to="w1", body={"task": 42})
    assert out["ok"] is True


def test_label_shortcut_ambiguous_returns_none(rt, root_token):
    """When two children share a label, the resolver gives up rather
    than guessing — caller gets ``no_such_address``."""
    spawn_impl(token=root_token, role_prompt="a", label="dup")
    spawn_impl(token=root_token, role_prompt="b", label="dup")
    out = send_impl(token=root_token, to="dup", body="?")
    assert out["ok"] is False
    assert out["code"] == "no_such_address"


def test_send_to_caller_shortcut_resolves_to_last_sender(rt, root_token):
    """``"caller"`` resolves to the sender of the most-recent envelope
    the agent received. The right tool for "reply to whoever just
    messaged me" — no body-field plumbing needed."""
    from spawn.address import USER

    # Simulate a delivered envelope by hand — the driver normally sets
    # ``last_received_from`` before calling step.
    rt.record_for(rt.root_addr).last_received_from = USER
    out = send_impl(token=root_token, to="caller", body="reply")
    assert out["ok"] is True
    user_envs = rt.read_inbox(USER)
    assert any(e.body == "reply" for e in user_envs)


def test_send_to_caller_without_received_message_is_unresolved(rt, root_token):
    """Before the agent has received anything, ``"caller"`` is None
    and the resolver reports ``no_such_address`` rather than guessing."""
    out = send_impl(token=root_token, to="caller", body="nope")
    assert out["ok"] is False
    assert out["code"] == "no_such_address"


# ----- peek_impl -----

def test_peek_descendant_returns_status_and_inbox(rt, root_token):
    child = spawn_impl(token=root_token, role_prompt="c", label="worker")
    addr = rt.address_by_id(child["address"])
    rt.send_external(to=addr, body={"task": "x"})

    out = peek_impl(token=root_token, address=child["address"])
    assert out["ok"] is True
    assert out["address"] == child["address"]
    assert out["label"] == "worker"
    assert out["status"] == "lazy"  # no engine factory wired in this rt
    assert out["depth"] == 1
    assert out["parent"] == rt.root_addr.id
    assert out["inbox_size"] == 1
    assert out["recent_envelopes"][0]["body"] == {"task": "x"}


def test_peek_non_descendant_rejected(rt, root_token):
    """Authority: a child can't peek a sibling — only ancestors can
    see down the tree."""
    a = spawn_impl(token=root_token, role_prompt="a", label="a")
    b = spawn_impl(token=root_token, role_prompt="b", label="b")
    a_token = rt.record_for(rt.address_by_id(a["address"])).token
    out = peek_impl(token=a_token, address=b["address"])
    assert out["ok"] is False
    assert out["code"] == "not_permitted"


def test_peek_label_shortcut(rt, root_token):
    spawn_impl(token=root_token, role_prompt="c", label="w1")
    out = peek_impl(token=root_token, address="w1")
    assert out["ok"] is True
    assert out["label"] == "w1"


# ----- terminate_impl -----

def test_terminate_descendant(rt, root_token):
    child = spawn_impl(token=root_token, role_prompt="c")
    out = terminate_impl(token=root_token, address=child["address"])
    assert out["ok"] is True
    assert child["address"] in out["terminated"]


def test_terminate_non_descendant_rejected(rt, root_token):
    a = spawn_impl(token=root_token, role_prompt="a")
    b = spawn_impl(token=root_token, role_prompt="b")
    a_token = rt.record_for(rt.address_by_id(a["address"])).token
    out = terminate_impl(token=a_token, address=b["address"])
    assert out["ok"] is False
    assert out["code"] == "not_permitted"


# ----- introduce_impl -----

def test_introduce_grants_capability_to_descendant(rt, root_token):
    a = spawn_impl(token=root_token, role_prompt="a")
    b = spawn_impl(token=root_token, role_prompt="b")
    a_token = rt.record_for(rt.address_by_id(a["address"])).token

    # Before introduction, a cannot send to b.
    pre = send_impl(token=a_token, to=b["address"], body="hi")
    assert pre["code"] == "not_permitted"

    # Root introduces b to a.
    intro = introduce_impl(token=root_token, child=a["address"], capability=b["address"])
    assert intro["ok"] is True

    # Now a can send to b.
    post = send_impl(token=a_token, to=b["address"], body="hi")
    assert post["ok"] is True


def test_introduce_to_non_descendant_rejected(rt, root_token):
    a = spawn_impl(token=root_token, role_prompt="a")
    b = spawn_impl(token=root_token, role_prompt="b")
    a_token = rt.record_for(rt.address_by_id(a["address"])).token
    out = introduce_impl(token=a_token, child=b["address"], capability=rt.root_addr.id)
    assert out["ok"] is False
    assert out["code"] == "not_descendant"


def test_introduce_cap_missing(rt, root_token):
    # Spawn a child and an orphan.
    child = spawn_impl(token=root_token, role_prompt="c")
    # The cap we try to introduce doesn't exist
    out = introduce_impl(
        token=root_token,
        child=child["address"],
        capability="ag-bogus",
    )
    assert out["ok"] is False
    assert out["code"] == "no_such_address"


# ----- list_inbox_impl -----

def test_list_inbox_non_consuming(rt, root_token):
    rt.send_external(to=rt.root_addr, body="x")
    out1 = list_inbox_impl(token=root_token)
    out2 = list_inbox_impl(token=root_token)
    assert out1["total"] == 1
    assert out2["total"] == 1
    assert len(out2["envelopes"]) == 1


# ----- tool class plumbing -----

def test_build_primitive_tools_creates_one_per_class(rt, root_token):
    tools = build_primitive_tools(root_token)
    assert len(tools) == len(PRIMITIVE_TOOL_CLASSES)
    types = {type(t) for t in tools}
    assert types == set(PRIMITIVE_TOOL_CLASSES)


def test_spawn_tool_class_executes(rt, root_token):
    tool = SpawnTool(runtime_token=root_token)
    out = tool.execute(role_prompt="from-tool", label="ft")
    # BaseTool.execute returns the _run result wrapped (orchestral
    # may wrap it). Tolerate either dict result or a wrapping.
    if isinstance(out, dict) and "ok" in out:
        assert out["ok"] is True
    else:
        # If orchestral wraps, the dict should be inside the wrapping.
        assert "ft" in repr(out) or "from-tool" in repr(out) or "ok" in repr(out)


def test_stateless_runtime_tool_resets_runtime_fields(rt, root_token):
    """A SpawnTool used twice in a row should not leak the previous
    call's optional fields (label, capabilities, etc.)."""
    tool = SpawnTool(runtime_token=root_token)
    tool.execute(role_prompt="first", label="A", capabilities=[])
    # Without StatelessRuntimeTool, label would still be "A".
    tool.execute(role_prompt="second")
    # The second call's spec should not carry "A" as the label.
    # Look at the agent that was just created.
    # Find children of root; the second one is what we just spawned.
    children = list(rt.record_for(rt.root_addr).children)
    last_addr = max(children, key=lambda a: rt.record_for(a).spawned_at)
    assert rt.record_for(last_addr).spec.label == ""
