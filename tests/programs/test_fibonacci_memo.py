"""Memoized Fibonacci with a shared cache agent.

A ``cache`` agent stores ``{n: fib(n)}``. Each ``fib`` agent, when given
``n``, first queries the cache. On hit it replies immediately; on miss
it spawns ``fib(n-1)`` and ``fib(n-2)``, introduces the cache to each
(capability passing), waits for both to reply, sums, writes the result
to the cache, and replies upward.

Total cache hits should grow as ``n`` increases — empirically O(n)
spawns rather than O(phi^n).
"""

from __future__ import annotations

from spawn.record import AgentSpec
from spawn.runtime import Runtime
from spawn.scripted import BehaviorRegistry
from spawn.tools.primitives import introduce_impl, send_impl, spawn_impl


def cache_behavior(engine, prompt, envelopes):
    cache = engine.state.setdefault("cache", {})
    engine.state.setdefault("hits", 0)
    engine.state.setdefault("misses", 0)
    for env in envelopes:
        body = env.body
        if not isinstance(body, dict):
            continue
        op = body.get("op")
        if op == "get":
            n = body["n"]
            value = cache.get(n)
            if value is not None:
                engine.state["hits"] += 1
            else:
                engine.state["misses"] += 1
            send_impl(
                token=engine.token, to=body["reply_to"],
                body={"value": value, "n": n},
            )
        elif op == "set":
            cache[body["n"]] = body["value"]
    return "ok"


def fib_behavior(engine, prompt, envelopes):
    for env in envelopes:
        body = env.body
        if not isinstance(body, dict):
            continue
        if "compute" in body:
            n = body["compute"]
            engine.state = {
                "n": n,
                "cache_id": body["cache_id"],
                "reply_to": body["reply_to"],
                "phase": "checking_cache",
            }
            send_impl(
                token=engine.token, to=body["cache_id"],
                body={"op": "get", "n": n, "reply_to": engine.addr.id},
            )
        elif "value" in body:
            state = engine.state
            if state.get("phase") != "checking_cache":
                continue
            n = state["n"]
            if body["value"] is not None:
                send_impl(
                    token=engine.token, to=state["reply_to"],
                    body={"result": body["value"]},
                )
                engine.state = {}
            elif n <= 1:
                send_impl(
                    token=engine.token, to=state["cache_id"],
                    body={"op": "set", "n": n, "value": n},
                )
                send_impl(
                    token=engine.token, to=state["reply_to"],
                    body={"result": n},
                )
                engine.state = {}
            else:
                a = spawn_impl(
                    token=engine.token, role_prompt="fib", label=f"fib-{n - 1}",
                )["address"]
                b = spawn_impl(
                    token=engine.token, role_prompt="fib", label=f"fib-{n - 2}",
                )["address"]
                introduce_impl(
                    token=engine.token, child=a, capability=state["cache_id"]
                )
                introduce_impl(
                    token=engine.token, child=b, capability=state["cache_id"]
                )
                send_impl(
                    token=engine.token, to=a,
                    body={"compute": n - 1, "cache_id": state["cache_id"],
                          "reply_to": engine.addr.id},
                )
                send_impl(
                    token=engine.token, to=b,
                    body={"compute": n - 2, "cache_id": state["cache_id"],
                          "reply_to": engine.addr.id},
                )
                state["children"] = {a: None, b: None}
                state["phase"] = "waiting"
        elif "result" in body:
            state = engine.state
            if state.get("phase") != "waiting":
                continue
            state["children"][env.from_.id] = body["result"]
            if all(v is not None for v in state["children"].values()):
                total = sum(state["children"].values())
                send_impl(
                    token=engine.token, to=state["cache_id"],
                    body={"op": "set", "n": state["n"], "value": total},
                )
                send_impl(
                    token=engine.token, to=state["reply_to"],
                    body={"result": total},
                )
                engine.state = {}
    return "ok"


def _make_runtime() -> Runtime:
    reg = BehaviorRegistry()
    reg.register("idle", lambda *_args, **_kw: "idle")
    reg.register("cache", cache_behavior)
    reg.register("fib", fib_behavior)
    # Recursive memoization exceeds the default depth cap.
    return Runtime(engine_factory=reg.factory(), max_depth=64)


def test_fib_small(wait_for_result):
    rt = _make_runtime()
    root = rt.root(AgentSpec(role_prompt="idle"))
    collector = rt._spawn(
        parent=root, spec=AgentSpec(role_prompt="(collector)", lazy=True)
    )
    cache = rt._spawn(parent=root, spec=AgentSpec(role_prompt="cache"))
    fib = rt._spawn(parent=root, spec=AgentSpec(role_prompt="fib", label="fib-7"))
    rt.record_for(fib).capabilities.extend(cache)
    rt.record_for(fib).capabilities.extend(collector)

    rt.send_external(to=fib, body={
        "compute": 7,
        "cache_id": cache.id,
        "reply_to": collector.id,
    })

    result = wait_for_result(rt, collector, timeout=10.0)
    # fib: 0,1,1,2,3,5,8,13
    assert result == 13
    rt.shutdown()


def test_fib_cache_records_hits(wait_for_result):
    rt = _make_runtime()
    root = rt.root(AgentSpec(role_prompt="idle"))
    collector = rt._spawn(
        parent=root, spec=AgentSpec(role_prompt="(collector)", lazy=True)
    )
    cache = rt._spawn(parent=root, spec=AgentSpec(role_prompt="cache"))
    fib = rt._spawn(parent=root, spec=AgentSpec(role_prompt="fib", label="fib-6"))
    rt.record_for(fib).capabilities.extend(cache)
    rt.record_for(fib).capabilities.extend(collector)

    rt.send_external(to=fib, body={
        "compute": 6,
        "cache_id": cache.id,
        "reply_to": collector.id,
    })
    result = wait_for_result(rt, collector, timeout=10.0)
    assert result == 8

    # The cache should report some hits — repeated subproblems do occur.
    cache_record = rt.record_for(cache)
    # The cache agent's engine has state["hits"] tracking hits.
    engine = cache_record.agent.engine  # ScriptedEngine
    assert engine.state.get("hits", 0) >= 1, "cache should record hits for repeated subproblems"
    rt.shutdown()
