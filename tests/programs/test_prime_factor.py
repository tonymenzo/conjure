"""Prime factorization via recursive tree decomposition.

A ``factorize`` agent receives an integer. If it finds the smallest
divisor ``d < n``, it spawns a child to factorize ``n // d`` and prepends
``d`` to that child's result. Primes return ``[n]`` directly without
spawning.

The tree shape is unbalanced — depths up to ``log2(n)`` for highly
composite numbers, longer chains for primes-times-small-factors.
"""

from __future__ import annotations

from combinator.record import AgentSpec
from combinator.runtime import Runtime
from combinator.scripted import BehaviorRegistry
from combinator.tools.primitives import send_impl, spawn_impl


def _smallest_divisor(n: int) -> int | None:
    for d in range(2, n):
        if n % d == 0:
            return d
    return None


def factorize_behavior(engine, prompt, envelopes):
    for env in envelopes:
        body = env.body
        if not isinstance(body, dict):
            continue
        if "n" in body:
            n = body["n"]
            reply_to = body["reply_to"]
            d = _smallest_divisor(n)
            if d is None:
                # Prime — done.
                send_impl(
                    token=engine.token, to=reply_to,
                    body={"result": [n]},
                )
                continue
            # Composite — delegate the quotient.
            child = spawn_impl(
                token=engine.token,
                role_prompt="factorize",
                label=f"fac-{n // d}",
            )
            assert child["ok"], child
            send_impl(
                token=engine.token, to=child["address"],
                body={"n": n // d, "reply_to": engine.addr.id},
            )
            engine.state = {
                "d": d,
                "reply_to": reply_to,
                "child_id": child["address"],
            }
        elif "result" in body:
            state = engine.state
            d = state["d"]
            send_impl(
                token=engine.token, to=state["reply_to"],
                body={"result": [d] + body["result"]},
            )
            engine.state = {}
    return "ok"


def _make_runtime() -> Runtime:
    reg = BehaviorRegistry()
    reg.register("idle", lambda *_args, **_kw: "idle")
    reg.register("factorize", factorize_behavior)
    # Recursive factorization can build deeper chains than the default cap.
    return Runtime(engine_factory=reg.factory(), max_depth=64)


def test_factorize_twelve(wait_for_result):
    rt = _make_runtime()
    root = rt.root(AgentSpec(role_prompt="idle"))
    collector = rt._spawn(
        parent=root, spec=AgentSpec(role_prompt="(collector)", lazy=True)
    )
    fac = rt._spawn(parent=root, spec=AgentSpec(role_prompt="factorize", label="fac-12"))
    rt.record_for(fac).capabilities.extend(collector)

    rt.send_external(to=fac, body={"n": 12, "reply_to": collector.id})
    result = wait_for_result(rt, collector, timeout=5.0)
    assert result == [2, 2, 3]
    rt.shutdown()


def test_factorize_prime(wait_for_result):
    rt = _make_runtime()
    root = rt.root(AgentSpec(role_prompt="idle"))
    collector = rt._spawn(
        parent=root, spec=AgentSpec(role_prompt="(collector)", lazy=True)
    )
    fac = rt._spawn(parent=root, spec=AgentSpec(role_prompt="factorize", label="fac-17"))
    rt.record_for(fac).capabilities.extend(collector)

    rt.send_external(to=fac, body={"n": 17, "reply_to": collector.id})
    result = wait_for_result(rt, collector, timeout=5.0)
    assert result == [17]
    # Should not have spawned any children.
    assert not rt.record_for(fac).children
    rt.shutdown()


def test_factorize_large_composite(wait_for_result):
    rt = _make_runtime()
    root = rt.root(AgentSpec(role_prompt="idle"))
    collector = rt._spawn(
        parent=root, spec=AgentSpec(role_prompt="(collector)", lazy=True)
    )
    fac = rt._spawn(parent=root, spec=AgentSpec(role_prompt="factorize", label="fac-360"))
    rt.record_for(fac).capabilities.extend(collector)

    rt.send_external(to=fac, body={"n": 360, "reply_to": collector.id})
    result = wait_for_result(rt, collector, timeout=10.0)
    # 360 = 2^3 * 3^2 * 5
    assert result == [2, 2, 2, 3, 3, 5]
    rt.shutdown()
