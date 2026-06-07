"""Fixed-point convergence via ``agent_fixed_point``.

A whitespace-normalizer agent receives a string and emits the same
string with consecutive whitespace collapsed and outer whitespace
trimmed. After enough iterations, the output equals the input — a
genuine fixed point.
"""

from __future__ import annotations

import re

from conjure.combinators import agent_fixed_point
from conjure.record import AgentSpec
from conjure.runtime import Runtime
from conjure.scripted import BehaviorRegistry
from conjure.tools.primitives import send_impl


def normalize_step(engine, prompt, envelopes):
    """One step of whitespace normalization: collapse runs of whitespace
    to single spaces and strip outer whitespace. Idempotent after one
    pass — so the fixed point arrives after two iterations on any
    non-fixed input."""
    for env in envelopes:
        body = env.body
        s = body["value"]
        normalized = re.sub(r"\s+", " ", s).strip()
        send_impl(token=engine.token, to=body["reply_to"], body=normalized)
    return "ok"


def test_fixed_point_converges_on_whitespace_string():
    reg = BehaviorRegistry()
    reg.register("idle", lambda *_args, **_kw: "idle")
    reg.register("normalize", normalize_step)
    rt = Runtime(engine_factory=reg.factory())
    root = rt.root(AgentSpec(role_prompt="idle"))

    value, converged = agent_fixed_point(
        rt, root,
        lambda v: AgentSpec(role_prompt="normalize"),
        seed="   hello   world  ",
        max_iters=5,
        timeout_s=5.0,
    )
    assert value == "hello world"
    assert converged is True
    rt.shutdown()


def test_fixed_point_already_at_fixed_point_does_not_iterate_more():
    reg = BehaviorRegistry()
    reg.register("idle", lambda *_args, **_kw: "idle")
    reg.register("normalize", normalize_step)
    rt = Runtime(engine_factory=reg.factory())
    root = rt.root(AgentSpec(role_prompt="idle"))

    # An already-normalized string. The first iteration returns itself,
    # converging immediately (one worker spawn).
    value, converged = agent_fixed_point(
        rt, root,
        lambda v: AgentSpec(role_prompt="normalize"),
        seed="already normalized",
        max_iters=5,
        timeout_s=5.0,
    )
    assert value == "already normalized"
    assert converged is True
    # Root should have a few descendants: the normalizers + the collector.
    # But all should be terminated.
    for child in rt.record_for(root).children:
        assert rt.record_for(child).status == "terminated"
    rt.shutdown()
