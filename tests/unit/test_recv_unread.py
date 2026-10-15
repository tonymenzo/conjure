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
