"""Tests for spawn.combinators — agent_map / fold / filter / fixed_point.

Each test wires up a BehaviorRegistry with worker behaviors that read
their inbox, compute a result, and send it to the ``reply_to`` address
extracted from the message body. The combinator (running in the test's
own Python flow) collects these.

The "parent" address used by the combinator is the root agent in each
test. The root has no behavior — its driver is idle while the
combinator runs in the test thread.
"""

from __future__ import annotations

import operator
import time

import pytest

from spawn.combinators import (
    agent_filter,
    agent_fixed_point,
    agent_fold,
    agent_map,
)
from spawn.errors import Timeout
from spawn.record import AgentSpec
from spawn.runtime import Runtime
from spawn.scripted import BehaviorRegistry
from spawn.tools.primitives import call_impl, send_impl


def _idle_root(prompt, envelopes):
    return "idle"


def _reply_with(transform):
    """Build a worker behavior that, for each envelope, sends
    ``transform(body)`` back to body['reply_to']."""

    def behavior(engine, prompt, envelopes):
        for env in envelopes:
            body = env.body
            result = transform(body)
            send_impl(
                token=engine.token,
                to=body["reply_to"],
                body=result,
            )
        return "ok"

    return behavior


def _make_runtime(behaviors: dict[str, callable]) -> Runtime:
    registry = BehaviorRegistry()
    registry.register("root", _idle_root)
    for role, behavior in behaviors.items():
        registry.register(role, behavior)
    return Runtime(engine_factory=registry.factory())


# ----- agent_map -----

def test_agent_map_squares_a_list():
    rt = _make_runtime({"squarer": _reply_with(lambda b: b["item"] ** 2)})
    root = rt.root(AgentSpec(role_prompt="root"))
    result = agent_map(
        rt, root,
        lambda item: AgentSpec(role_prompt="squarer"),
        [1, 2, 3, 4, 5],
        timeout_s=5.0,
    )
    assert result == [1, 4, 9, 16, 25]
    rt.shutdown()


def test_agent_map_empty_input():
    rt = _make_runtime({})
    root = rt.root(AgentSpec(role_prompt="root"))
    result = agent_map(rt, root, lambda i: AgentSpec(role_prompt="x"), [])
    assert result == []
    rt.shutdown()


def test_agent_map_preserves_order_under_concurrency():
    """Workers reply in arbitrary order; agent_map must return in input order."""

    def slow_double(engine, prompt, envelopes):
        for env in envelopes:
            # Vary the delay so replies arrive out of order.
            time.sleep(0.05 * (5 - env.body["item"]))
            send_impl(
                token=engine.token,
                to=env.body["reply_to"],
                body=env.body["item"] * 2,
            )
        return "ok"

    rt = _make_runtime({"doubler": slow_double})
    root = rt.root(AgentSpec(role_prompt="root"))
    result = agent_map(
        rt, root,
        lambda i: AgentSpec(role_prompt="doubler"),
        [1, 2, 3, 4, 5],
        timeout_s=10.0,
    )
    assert result == [2, 4, 6, 8, 10]
    rt.shutdown()


def test_agent_map_cleans_up_workers():
    rt = _make_runtime({"squarer": _reply_with(lambda b: b["item"] ** 2)})
    root = rt.root(AgentSpec(role_prompt="root"))
    agent_map(
        rt, root,
        lambda i: AgentSpec(role_prompt="squarer"),
        [1, 2, 3],
        timeout_s=5.0,
    )
    # All children of root should be terminated.
    record = rt.record_for(root)
    for child in record.children:
        assert rt.record_for(child).status == "terminated"
    rt.shutdown()


def test_agent_map_timeout_when_worker_silent():
    """A worker that never replies should cause the combinator to time out."""

    def silent(engine, prompt, envelopes):
        return "ok"  # don't reply

    rt = _make_runtime({"silent": silent})
    root = rt.root(AgentSpec(role_prompt="root"))
    with pytest.raises(Timeout):
        agent_map(
            rt, root,
            lambda i: AgentSpec(role_prompt="silent"),
            [1, 2],
            timeout_s=0.3,
        )
    rt.shutdown()


# ----- agent_fold -----

def test_agent_fold_accumulates_sum():
    """Each worker receives acc and item; replies with acc + item."""

    def adder(engine, prompt, envelopes):
        for env in envelopes:
            body = env.body
            send_impl(
                token=engine.token,
                to=body["reply_to"],
                body=body["acc"] + body["item"],
            )
        return "ok"

    rt = _make_runtime({"adder": adder})
    root = rt.root(AgentSpec(role_prompt="root"))
    total = agent_fold(
        rt, root,
        lambda i: AgentSpec(role_prompt="adder"),
        [1, 2, 3, 4, 5],
        init=0,
        timeout_s=5.0,
    )
    assert total == 15
    rt.shutdown()


def test_agent_fold_empty_returns_init():
    rt = _make_runtime({})
    root = rt.root(AgentSpec(role_prompt="root"))
    result = agent_fold(
        rt, root, lambda i: AgentSpec(role_prompt="x"), [], init=42
    )
    assert result == 42
    rt.shutdown()


def test_agent_fold_trace_returns_full_history():
    """``trace=True`` returns ``{"result": final, "trace": [init, ...,
    final]}`` so callers can see every intermediate accumulator. The
    chain is what makes telephone-game / drift-detection / progress-UI
    use cases work."""

    def adder(engine, prompt, envelopes):
        for env in envelopes:
            body = env.body
            send_impl(
                token=engine.token,
                to=body["reply_to"],
                body=body["acc"] + body["item"],
            )
        return "ok"

    rt = _make_runtime({"adder": adder})
    root = rt.root(AgentSpec(role_prompt="root"))
    out = agent_fold(
        rt, root,
        lambda i: AgentSpec(role_prompt="adder"),
        [1, 2, 3],
        init=0,
        timeout_s=5.0,
        trace=True,
    )
    assert isinstance(out, dict)
    assert out["result"] == 6
    assert out["trace"] == [0, 1, 3, 6]
    rt.shutdown()


def test_agent_fold_trace_empty_items_returns_singleton_trace():
    rt = _make_runtime({})
    root = rt.root(AgentSpec(role_prompt="root"))
    out = agent_fold(
        rt, root, lambda i: AgentSpec(role_prompt="x"), [],
        init=99, trace=True,
    )
    assert out == {"result": 99, "trace": [99]}
    rt.shutdown()


# ----- agent_filter -----

def test_agent_filter_keeps_truthy():
    rt = _make_runtime({"is_even": _reply_with(lambda b: b["item"] % 2 == 0)})
    root = rt.root(AgentSpec(role_prompt="root"))
    result = agent_filter(
        rt, root,
        lambda i: AgentSpec(role_prompt="is_even"),
        [1, 2, 3, 4, 5, 6],
        timeout_s=5.0,
    )
    assert result == [2, 4, 6]
    rt.shutdown()


# ----- agent_fixed_point -----

def test_agent_fixed_point_converges_to_eq():
    """Each iteration halves; eventually stabilizes at 0 (integer division)."""

    def halver(engine, prompt, envelopes):
        for env in envelopes:
            v = env.body["value"]
            send_impl(
                token=engine.token,
                to=env.body["reply_to"],
                body=v // 2,
            )
        return "ok"

    rt = _make_runtime({"halver": halver})
    root = rt.root(AgentSpec(role_prompt="root"))
    final, converged = agent_fixed_point(
        rt, root,
        lambda v: AgentSpec(role_prompt="halver"),
        seed=10,
        max_iters=20,
        timeout_s=5.0,
    )
    assert final == 0
    assert converged is True
    rt.shutdown()


def test_agent_fixed_point_max_iters_when_non_converging():
    """An incrementer never converges; should hit max_iters."""

    def incrementer(engine, prompt, envelopes):
        for env in envelopes:
            v = env.body["value"]
            send_impl(
                token=engine.token,
                to=env.body["reply_to"],
                body=v + 1,
            )
        return "ok"

    rt = _make_runtime({"inc": incrementer})
    root = rt.root(AgentSpec(role_prompt="root"))
    final, converged = agent_fixed_point(
        rt, root,
        lambda v: AgentSpec(role_prompt="inc"),
        seed=0,
        max_iters=5,
        timeout_s=5.0,
    )
    assert final == 5
    assert converged is False
    rt.shutdown()


def test_agent_fixed_point_terminates_collector():
    rt = _make_runtime({"halver": _reply_with(lambda b: b["value"] // 2)})
    root = rt.root(AgentSpec(role_prompt="root"))
    agent_fixed_point(
        rt, root,
        lambda v: AgentSpec(role_prompt="halver"),
        seed=4,
        max_iters=10,
    )
    # All children of root should be terminated.
    record = rt.record_for(root)
    for child in record.children:
        assert rt.record_for(child).status == "terminated"
    rt.shutdown()


# ----- call_impl -----

def test_call_returns_worker_reply_via_caller_shortcut():
    """``Call`` spawns a oneshot worker that replies via
    ``Send(to="caller", body=...)``. The reply is routed through a
    private collector so the parent's inbox stays clean; ``call_impl``
    returns that body under ``result``."""

    def echo_caller(engine, prompt, envelopes):
        for env in envelopes:
            # Reply via the "caller" shortcut — which resolves to the
            # collector (the dispatcher's apparent sender), giving the
            # parent a private return channel.
            send_impl(
                token=engine.token,
                to="caller",
                body={"echo": env.body},
            )
        return "ok"

    rt = _make_runtime({"echoer": echo_caller})
    root = rt.root(AgentSpec(role_prompt="root"))
    root_token = rt.record_for(root).token

    out = call_impl(
        token=root_token,
        spec={"role_prompt": "echoer", "label": "echo"},
        body="hello world",
        timeout_s=5.0,
    )
    assert out["ok"] is True
    assert out["result"] == {"echo": "hello world"}
    # Worker should have auto-terminated (oneshot) + been cleaned up.
    worker_addr = rt.address_by_id(out["worker"])
    if worker_addr is not None:
        assert rt.record_for(worker_addr).status == "terminated"
    # Parent's inbox should NOT contain the reply — the collector
    # absorbed it.
    parent_envs = rt.read_inbox(root)
    assert all(
        not (isinstance(e.body, dict) and "echo" in e.body)
        for e in parent_envs
    )
    rt.shutdown()


def test_call_cleanup_does_not_spam_parent_supervision():
    """``Call``'s finally-block tears down both the worker and its
    private collector. Both teardowns must be tagged
    ``requested_by="oneshot"`` so the parent doesn't wake to a tail of
    ``child_event terminated`` envelopes right after consuming the
    reply — the reply itself is the only signal the caller needs."""

    def echo_caller(engine, prompt, envelopes):
        for env in envelopes:
            send_impl(token=engine.token, to="caller", body=env.body)
        return "ok"

    rt = _make_runtime({"echoer": echo_caller})
    root = rt.root(AgentSpec(role_prompt="root"))
    root_token = rt.record_for(root).token
    root_inbox = rt.record_for(root).inbox
    base_seq = root_inbox.latest_seq()

    out = call_impl(
        token=root_token,
        spec={"role_prompt": "echoer"},
        body="ping",
        timeout_s=5.0,
    )
    assert out["ok"] is True
    # Give the driver a beat to run the oneshot auto-terminate too.
    time.sleep(0.1)
    new = root_inbox.read(since_seq=base_seq, max_n=50)
    events = [
        e for e in new
        if isinstance(e.body, dict) and e.body.get("kind") == "child_event"
    ]
    assert events == [], (
        f"Call cleanup should not produce supervision events for the "
        f"parent; saw: {[e.body for e in events]}"
    )
    rt.shutdown()


def test_fixed_point_does_not_spam_parent_per_iteration():
    """Each loop turn in ``agent_fixed_point`` terminates that turn's
    worker. With ``requested_by="oneshot"`` the parent should see no
    supervision envelopes — without the tag, the parent would receive
    one ``child_event terminated`` per iteration, flooding the inbox
    on long-running convergence loops."""
    rt = _make_runtime({"halver": _reply_with(lambda b: b["value"] // 2)})
    root = rt.root(AgentSpec(role_prompt="root"))
    root_inbox = rt.record_for(root).inbox
    base_seq = root_inbox.latest_seq()

    value, converged = agent_fixed_point(
        rt, root,
        lambda v: AgentSpec(role_prompt="halver"),
        seed=64,  # 64 → 32 → 16 → 8 → 4 → 2 → 1 → 0 → 0 (8 iters)
        max_iters=20,
    )
    assert converged is True
    assert value == 0
    new = root_inbox.read(since_seq=base_seq, max_n=50)
    events = [
        e for e in new
        if isinstance(e.body, dict) and e.body.get("kind") == "child_event"
    ]
    assert events == [], (
        f"agent_fixed_point per-iteration teardown should not produce "
        f"supervision events; saw: {[e.body for e in events]}"
    )
    rt.shutdown()


def test_call_timeout_returns_structured_error():
    """If the worker never replies, ``call_impl`` returns
    ``code=timeout`` with the dead worker's address for inspection."""

    def never_reply(engine, prompt, envelopes):
        # Just sit on the message — never send back.
        return "ok"

    rt = _make_runtime({"silent": never_reply})
    root = rt.root(AgentSpec(role_prompt="root"))
    root_token = rt.record_for(root).token

    out = call_impl(
        token=root_token,
        spec={"role_prompt": "silent"},
        body="anything",
        timeout_s=0.3,
    )
    assert out["ok"] is False
    assert out["code"] == "timeout"
    assert "worker" in out
    rt.shutdown()
