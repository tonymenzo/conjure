"""Sum of squares via ``agent_map``.

The root agent invokes ``agent_map`` (Python combinator) to compute the
square of each item in parallel, then sums the results. Tree shape: a
flat fan-out of ``len(items)`` workers.
"""

from __future__ import annotations

from spawn.combinators import agent_map
from spawn.record import AgentSpec
from spawn.runtime import Runtime
from spawn.scripted import BehaviorRegistry
from spawn.tools.primitives import send_impl


def squarer(engine, prompt, envelopes):
    for env in envelopes:
        body = env.body
        if isinstance(body, dict) and "item" in body:
            send_impl(
                token=engine.token,
                to=body["reply_to"],
                body=body["item"] ** 2,
            )
    return "ok"


def test_sum_of_squares():
    reg = BehaviorRegistry()
    reg.register("idle", lambda *_args, **_kw: "idle")
    reg.register("squarer", squarer)
    rt = Runtime(engine_factory=reg.factory())
    root = rt.root(AgentSpec(role_prompt="idle"))

    items = [1, 2, 3, 4, 5]
    squares = agent_map(
        rt,
        root,
        lambda _i: AgentSpec(role_prompt="squarer"),
        items,
        timeout_s=5.0,
    )
    assert squares == [1, 4, 9, 16, 25]
    assert sum(squares) == 55

    # All workers (and the collector) terminated.
    for child in rt.record_for(root).children:
        assert rt.record_for(child).status == "terminated"
    rt.shutdown()


def test_sum_of_squares_breadth_first_tree():
    """``agent_map`` should spawn ``len(items) + 1`` direct children of
    the root (n workers + 1 collector). Each worker is a leaf."""
    reg = BehaviorRegistry()
    reg.register("idle", lambda *_args, **_kw: "idle")
    reg.register("squarer", squarer)
    rt = Runtime(engine_factory=reg.factory())
    root = rt.root(AgentSpec(role_prompt="idle"))

    items = list(range(10))
    agent_map(
        rt, root,
        lambda _i: AgentSpec(role_prompt="squarer"),
        items,
        timeout_s=5.0,
    )

    direct_children = rt.record_for(root).children
    # 10 workers + 1 collector = 11
    assert len(direct_children) == 11
    # Every direct child is a leaf — workers don't spawn anything.
    for child in direct_children:
        assert not rt.record_for(child).children
    rt.shutdown()
