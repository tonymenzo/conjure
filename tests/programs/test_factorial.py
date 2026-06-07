"""Recursive factorial via chain of agents.

Each ``fact`` agent receives ``{"n": n, "reply_to": addr}``. If ``n <= 1``
it replies with ``{"result": 1}``; otherwise it spawns a child
``fact(n-1)`` and stashes (n, reply_to, child_id) in engine state. When
the child replies with ``{"result": k}``, the parent computes ``n*k``
and replies to its own reply_to.

The tree shape is a linear chain of depth ``n``.
"""

from __future__ import annotations

import math

from spawn.record import AgentSpec
from spawn.runtime import Runtime
from spawn.scripted import BehaviorRegistry
from spawn.tools.primitives import send_impl, spawn_impl


def fact_behavior(engine, prompt, envelopes):
    for env in envelopes:
        body = env.body
        if not isinstance(body, dict):
            continue
        if "n" in body:
            n = body["n"]
            reply_to = body["reply_to"]
            if n <= 1:
                send_impl(token=engine.token, to=reply_to, body={"result": 1})
                continue
            child = spawn_impl(
                token=engine.token,
                role_prompt="fact",
                label=f"fact-{n - 1}",
            )
            assert child["ok"], child
            send_impl(
                token=engine.token,
                to=child["address"],
                body={"n": n - 1, "reply_to": engine.addr.id},
            )
            engine.state = {"n": n, "reply_to": reply_to, "child_id": child["address"]}
        elif "result" in body:
            state = engine.state
            send_impl(
                token=engine.token,
                to=state["reply_to"],
                body={"result": state["n"] * body["result"]},
            )
            engine.state = {}
    return "ok"


def _make_runtime() -> Runtime:
    reg = BehaviorRegistry()
    reg.register("idle", lambda *_args, **_kw: "idle")
    reg.register("fact", fact_behavior)
    # Factorial recursion needs more depth than the default cap.
    return Runtime(engine_factory=reg.factory(), max_depth=64)


def test_factorial_of_five(wait_for_result):
    rt = _make_runtime()
    root = rt.root(AgentSpec(role_prompt="idle"))
    collector = rt._spawn(
        parent=root, spec=AgentSpec(role_prompt="(collector)", lazy=True)
    )
    fact = rt._spawn(parent=root, spec=AgentSpec(role_prompt="fact", label="fact-5"))
    rt.record_for(fact).capabilities.extend(collector)

    rt.send_external(to=fact, body={"n": 5, "reply_to": collector.id})

    result = wait_for_result(rt, collector, timeout=5.0)
    assert result == math.factorial(5)
    rt.shutdown()


def test_factorial_base_case(wait_for_result):
    rt = _make_runtime()
    root = rt.root(AgentSpec(role_prompt="idle"))
    collector = rt._spawn(
        parent=root, spec=AgentSpec(role_prompt="(collector)", lazy=True)
    )
    fact = rt._spawn(parent=root, spec=AgentSpec(role_prompt="fact", label="fact-0"))
    rt.record_for(fact).capabilities.extend(collector)

    rt.send_external(to=fact, body={"n": 0, "reply_to": collector.id})
    result = wait_for_result(rt, collector, timeout=5.0)
    assert result == 1
    rt.shutdown()


def test_factorial_builds_linear_chain(wait_for_result):
    """A chain of depth n means each fact agent has exactly one child."""
    rt = _make_runtime()
    root = rt.root(AgentSpec(role_prompt="idle"))
    collector = rt._spawn(
        parent=root, spec=AgentSpec(role_prompt="(collector)", lazy=True)
    )
    fact = rt._spawn(parent=root, spec=AgentSpec(role_prompt="fact", label="fact-4"))
    rt.record_for(fact).capabilities.extend(collector)

    rt.send_external(to=fact, body={"n": 4, "reply_to": collector.id})
    wait_for_result(rt, collector, timeout=5.0)

    # Walk the chain rooted at ``fact``. fact-4 -> fact-3 -> fact-2 -> fact-1.
    depth = 0
    current = fact
    while rt.record_for(current).children:
        children = rt.record_for(current).children
        assert len(children) == 1, f"fact at depth {depth} has {len(children)} children"
        current = next(iter(children))
        depth += 1
    assert depth == 3  # fact-4 has 3 descendants (fact-3, fact-2, fact-1)
    rt.shutdown()
