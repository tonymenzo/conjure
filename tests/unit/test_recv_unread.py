"""Recv / WaitFor return what the agent hasn't seen, return promptly,
and accept labels. Regressions for a root that hung: ``Recv()`` kept
returning its original task (message #1), and ``WaitFor()`` sat out its
whole timeout even after the reply had arrived."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from conjure.record import AgentSpec
from conjure.runtime import Runtime
from conjure.tools.primitives import recv_impl, send_impl, wait_for_impl


def _session():
    rt = Runtime()
    root = rt.record_for(rt.root(AgentSpec(role_prompt="r", label="root")))
    kid = rt.record_for(rt._spawn(parent=root.addr, spec=AgentSpec(role_prompt="c", label="worker")))
    rt.send_external(to=root.addr, body="the original task")       # seq 1
    root.driver = SimpleNamespace(_cursor=1, stop=lambda *a, **k: None, join=lambda *a, **k: None)                       # pushed: the turn's prompt
    return rt, root, kid


def bodies(out):
    return [e["body"] for e in out["envelopes"]]


def test_recv_returns_new_messages_not_the_original_task():
    rt, root, kid = _session()
    assert bodies(recv_impl(token=root.token)) == []
    send_impl(token=kid.token, to="parent", body="result 1")
    send_impl(token=kid.token, to="parent", body="result 2")
    assert bodies(recv_impl(token=root.token)) == ["result 1", "result 2"]
    assert bodies(recv_impl(token=root.token)) == []  # read once
    # Paging the whole inbox is still possible.
    assert bodies(recv_impl(token=root.token, since_seq=0, max_n=10))[0] == "the original task"
    rt.shutdown()


def test_wait_for_returns_when_the_reply_arrives_not_at_the_timeout():
    rt, root, kid = _session()
    threading.Timer(0.1, lambda: send_impl(token=kid.token, to="parent", body="done")).start()
    t0 = time.monotonic()
    out = wait_for_impl(token=root.token, predicate_kind="from", value="worker", timeout_s=10)
    assert bodies(out) == ["done"] and time.monotonic() - t0 < 2
    rt.shutdown()


def test_wait_for_ignores_messages_already_seen():
    rt, root, kid = _session()
    t0 = time.monotonic()
    out = wait_for_impl(token=root.token, timeout_s=0.2)
    assert bodies(out) == [] and time.monotonic() - t0 >= 0.19  # not the original task
    rt.shutdown()


def test_labels_resolve_and_unknown_names_fail_fast():
    rt, root, kid = _session()
    send_impl(token=kid.token, to="parent", body="hi")
    assert bodies(recv_impl(token=root.token, from_="worker")) == ["hi"]
    t0 = time.monotonic()
    bad = wait_for_impl(token=root.token, predicate_kind="from", value="nobody", timeout_s=30)
    assert bad["ok"] is False and bad["code"] == "no_such_address" and time.monotonic() - t0 < 1
    rt.shutdown()


def test_fan_in_with_max_n_still_waits_for_all():
    rt = Runtime()
    root = rt.record_for(rt.root(AgentSpec(role_prompt="r")))
    kids = [rt.record_for(rt._spawn(parent=root.addr, spec=AgentSpec(role_prompt="c", label=f"w{i}"))) for i in range(3)]
    for i, k in enumerate(kids):
        threading.Timer(0.05 * (i + 1), lambda k=k: send_impl(token=k.token, to="parent", body=k.addr.label)).start()
    out = wait_for_impl(token=root.token, max_n=3, timeout_s=5)
    assert sorted(bodies(out)) == ["w0", "w1", "w2"]
    rt.shutdown()


def test_wait_for_a_child_ends_when_it_goes_idle_without_replying():
    rt, root, kid = _session()
    kid.status = "idle"
    kid.driver = SimpleNamespace(busy=False, _cursor=kid.inbox.latest_seq(), stop=lambda *a, **k: None, join=lambda *a, **k: None)
    t0 = time.monotonic()
    out = wait_for_impl(token=root.token, predicate_kind="from", value="worker", timeout_s=30)
    assert out["child_idle"] and out["timed_out"] and time.monotonic() - t0 < 5
    # A child with an unread message is not idle: the wait runs its course.
    rt.send_external(to=kid.addr, body="next task")
    out = wait_for_impl(token=root.token, predicate_kind="from", value="worker", timeout_s=2.5)
    assert "child_idle" not in out and out["timed_out"]
    rt.shutdown()


def test_waiting_parent_learns_a_live_child_stopped_after_a_progress_note():
    from conjure.runtime import Runtime as LiveRuntime
    from conjure.scripted import BehaviorRegistry
    seen = {}

    def slow(engine, prompt, envelopes):
        time.sleep(2.5)  # mid-turn for longer than a wait slice
        send_impl(token=engine.token, to="parent", body="done")

    def progress(engine, prompt, envelopes):
        send_impl(token=engine.token, to="parent", body="in progress")

    def parent(engine, prompt, envelopes):
        rt = engine.runtime
        for label in ["slow", "progress"]:
            kid = rt._spawn(parent=engine.record.addr, spec=AgentSpec(role_prompt=label, label=label))
            send_impl(token=engine.token, to=kid.id, body="go")
            seen[label] = [wait_for_impl(token=engine.token, predicate_kind="from", value=label, timeout_s=20)
                           for _ in range(2 if label == "progress" else 1)]

    reg = BehaviorRegistry()
    for name, fn in [("slow", slow), ("progress", progress), ("parent", parent)]:
        reg.register(name, fn)
    rt = LiveRuntime(engine_factory=reg.factory())
    root = rt.root(AgentSpec(role_prompt="parent", label="root"))
    t0 = time.monotonic()
    rt.send_external(to=root, body="start")
    while "progress" not in seen or len(seen["progress"]) < 2:
        assert time.monotonic() - t0 < 15
        time.sleep(0.05)
    assert bodies(seen["slow"][0]) == ["done"]  # busy is not idle
    first, second = seen["progress"]
    assert bodies(first) == ["in progress"] and second["child_idle"] and not second["envelopes"]
    rt.shutdown()


def _stub(record, busy=False):
    record.status = "idle"
    record.driver = SimpleNamespace(busy=busy, _cursor=record.inbox.latest_seq(),
                                    stop=lambda *a, **k: None, join=lambda *a, **k: None)


def test_fan_in_ends_once_every_child_has_stopped_and_says_so_once():
    rt, root, kid = _session()
    other = rt.record_for(rt._spawn(parent=root.addr, spec=AgentSpec(role_prompt="c", label="other")))
    helper = rt.record_for(rt._spawn(parent=other.addr, spec=AgentSpec(role_prompt="c", label="helper")))
    send_impl(token=kid.token, to="parent", body="done")
    for r in (kid, other):
        _stub(r)
    _stub(helper, busy=True)  # other's worker is still running: other may yet reply
    out = wait_for_impl(token=root.token, max_n=2, timeout_s=2.5)
    assert bodies(out) == ["done"] and "child_idle" not in out
    helper.driver.busy = False
    t0 = time.monotonic()
    out = wait_for_impl(token=root.token, max_n=2, timeout_s=30)
    assert out["child_idle"] == ["worker", "other"] or out["child_idle"] == ["other", "worker"]
    assert time.monotonic() - t0 < 5
    out = wait_for_impl(token=root.token, timeout_s=2.5)  # already told: block as usual
    assert "child_idle" not in out and out["timed_out"]
    rt.shutdown()
