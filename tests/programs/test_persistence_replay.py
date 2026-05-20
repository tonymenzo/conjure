"""Run sum_of_squares end-to-end with persistence, then replay the
journal and assert the inbox state survives.
"""

from __future__ import annotations

from pathlib import Path

from combinator.combinators import agent_map
from combinator.record import AgentSpec
from combinator.runtime import Runtime
from combinator.scripted import BehaviorRegistry
from combinator.tools.primitives import send_impl


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


def test_sum_of_squares_then_replay(tmp_path: Path):
    reg = BehaviorRegistry()
    reg.register("idle", lambda *_args, **_kw: "idle")
    reg.register("squarer", squarer)

    rt = Runtime(store_dir=tmp_path, engine_factory=reg.factory())
    root = rt.root(AgentSpec(role_prompt="idle", label="root"))

    result = agent_map(
        rt, root,
        lambda _i: AgentSpec(role_prompt="squarer"),
        [1, 2, 3, 4],
        timeout_s=5.0,
    )
    assert result == [1, 4, 9, 16]
    rt.shutdown(driver_join_timeout=2.0)

    rt2 = Runtime.replay(tmp_path)
    assert rt2.root_addr == root
    # The spawn tree should be restored: root + 4 workers + 1 collector.
    assert len(rt2.record_for(root).children) == 5
    # All workers terminated in the original session.
    for child in rt2.record_for(root).children:
        assert rt2.record_for(child).status == "terminated"
