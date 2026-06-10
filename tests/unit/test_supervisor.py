"""``agent_supervisor`` — supervised fan-out with one-for-one restarts."""

from __future__ import annotations

import pytest

from conjure.combinators import agent_supervisor
from conjure.errors import Timeout
from conjure.record import AgentSpec
from conjure.runtime import Runtime
from conjure.scripted import BehaviorRegistry
from conjure.tools.primitives import send_impl


def _replier(engine, prompt, envelopes):
    for env in envelopes:
        body = env.body
        if isinstance(body, dict) and "item" in body:
            send_impl(
                token=engine.token,
                to=body["reply_to"],
                body=body["item"] * 10,
            )
    return "ok"


def test_supervisor_happy_path_matches_map_semantics():
    reg = BehaviorRegistry()
    reg.register("idle", lambda *_a, **_k: "idle")
    reg.register("replier", _replier)
    rt = Runtime(engine_factory=reg.factory())
    root = rt.root(AgentSpec(role_prompt="idle"))

    out = agent_supervisor(
        rt, root,
        lambda _i: AgentSpec(role_prompt="replier"),
        [1, 2, 3],
        timeout_s=10.0,
    )
    assert out == {"results": [10, 20, 30], "restarts": 0, "failed": []}
    for child in rt.record_for(root).children:
        assert rt.record_for(child).status == "terminated"
    rt.shutdown()


def test_supervisor_restarts_flaky_worker():
    attempts: dict[int, int] = {}

    def flaky(engine, prompt, envelopes):
        for env in envelopes:
            body = env.body
            if isinstance(body, dict) and "item" in body:
                item = body["item"]
                attempts[item] = attempts.get(item, 0) + 1
                # Item 2 fails on its first attempt only.
                if item == 2 and attempts[item] == 1:
                    raise RuntimeError("transient failure")
                send_impl(
                    token=engine.token,
                    to=body["reply_to"],
                    body=item * 10,
                )
        return "ok"

    reg = BehaviorRegistry()
    reg.register("idle", lambda *_a, **_k: "idle")
    reg.register("flaky", flaky)
    rt = Runtime(engine_factory=reg.factory())
    root = rt.root(AgentSpec(role_prompt="idle"))

    out = agent_supervisor(
        rt, root,
        lambda _i: AgentSpec(role_prompt="flaky"),
        [1, 2, 3],
        max_restarts=2,
        timeout_s=10.0,
    )
    assert out["results"] == [10, 20, 30]
    assert out["restarts"] == 1
    assert out["failed"] == []
    assert attempts[2] == 2
    rt.shutdown()


def test_supervisor_marks_item_failed_after_restart_budget():
    def always_broken(engine, prompt, envelopes):
        for env in envelopes:
            body = env.body
            if isinstance(body, dict) and "item" in body:
                if body["item"] == "bad":
                    raise RuntimeError("permanent failure")
                send_impl(
                    token=engine.token,
                    to=body["reply_to"],
                    body="done",
                )
        return "ok"

    reg = BehaviorRegistry()
    reg.register("idle", lambda *_a, **_k: "idle")
    reg.register("broken", always_broken)
    rt = Runtime(engine_factory=reg.factory())
    root = rt.root(AgentSpec(role_prompt="idle"))

    out = agent_supervisor(
        rt, root,
        lambda _i: AgentSpec(role_prompt="broken"),
        ["good", "bad"],
        max_restarts=1,
        timeout_s=10.0,
    )
    assert out["results"] == ["done", None]
    assert out["failed"] == [1]
    assert out["restarts"] == 1  # one retry attempted before giving up
    rt.shutdown()


def test_supervisor_times_out_with_partial_results():
    def silent(engine, prompt, envelopes):
        for env in envelopes:
            body = env.body
            if isinstance(body, dict) and body.get("item") == "fast":
                send_impl(
                    token=engine.token, to=body["reply_to"], body="fast-done"
                )
        return "ok"  # the "slow" item never replies, never errors

    reg = BehaviorRegistry()
    reg.register("idle", lambda *_a, **_k: "idle")
    reg.register("silent", silent)
    rt = Runtime(engine_factory=reg.factory())
    root = rt.root(AgentSpec(role_prompt="idle"))

    with pytest.raises(Timeout) as excinfo:
        agent_supervisor(
            rt, root,
            lambda _i: AgentSpec(role_prompt="silent"),
            ["fast", "slow"],
            timeout_s=1.0,
        )
    assert excinfo.value.received == 1
    assert excinfo.value.expected == 2
    assert excinfo.value.partial == ["fast-done", None]
    rt.shutdown()


def test_supervisor_empty_items():
    rt = Runtime()
    root = rt.root(AgentSpec(role_prompt="idle"))
    out = agent_supervisor(
        rt, root, lambda _i: AgentSpec(role_prompt="x"), [], timeout_s=1.0
    )
    assert out == {"results": [], "restarts": 0, "failed": []}
    rt.shutdown()
