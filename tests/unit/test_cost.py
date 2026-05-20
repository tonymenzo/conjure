"""Tests for cost reporting on engines and runtime."""

from __future__ import annotations

from combinator.record import AgentSpec
from combinator.runtime import Runtime
from combinator.scripted import BehaviorRegistry


def test_scripted_engine_reports_zero_cost():
    reg = BehaviorRegistry()
    reg.register("idle", lambda *_a, **_kw: "ok")
    rt = Runtime(engine_factory=reg.factory())
    rt.root(AgentSpec(role_prompt="idle"))
    assert rt.total_cost() == 0.0
    rt.shutdown()


def test_total_cost_sums_across_agents():
    """Two agents with engines that report cost: total is the sum."""
    reg = BehaviorRegistry()
    reg.register("a", lambda *_a, **_kw: "ok")
    rt = Runtime(engine_factory=reg.factory())
    addr = rt.root(AgentSpec(role_prompt="a"))

    # Inject a fixed cost on the root's engine so we can assert.
    engine = rt.record_for(addr).agent.engine
    engine.cost = lambda: 0.0123  # type: ignore[assignment]
    assert rt.total_cost() == 0.0123
    rt.shutdown()


def test_costs_by_agent_is_per_address():
    reg = BehaviorRegistry()
    reg.register("a", lambda *_a, **_kw: "ok")
    reg.register("b", lambda *_a, **_kw: "ok")
    rt = Runtime(engine_factory=reg.factory())
    root = rt.root(AgentSpec(role_prompt="a"))
    child = rt._spawn(parent=root, spec=AgentSpec(role_prompt="b"))

    rt.record_for(root).agent.engine.cost = lambda: 0.01
    rt.record_for(child).agent.engine.cost = lambda: 0.005

    rows = dict(rt.costs_by_agent())
    assert rows[root] == 0.01
    assert rows[child] == 0.005
    assert rt.total_cost() == 0.015
    rt.shutdown()


def test_engine_without_cost_method_contributes_zero():
    """Engines that don't expose .cost() shouldn't break total_cost."""
    reg = BehaviorRegistry()
    reg.register("a", lambda *_a, **_kw: "ok")
    rt = Runtime(engine_factory=reg.factory())
    addr = rt.root(AgentSpec(role_prompt="a"))
    # Replace the underlying engine with a bare object that has no .cost.
    rt.record_for(addr).agent._engine = object()  # noqa: SLF001
    assert rt.total_cost() == 0.0
    rt.shutdown()


def test_engine_raising_in_cost_treated_as_zero():
    reg = BehaviorRegistry()
    reg.register("a", lambda *_a, **_kw: "ok")
    rt = Runtime(engine_factory=reg.factory())
    addr = rt.root(AgentSpec(role_prompt="a"))

    def boom() -> float:
        raise RuntimeError("bad cost source")

    rt.record_for(addr).agent.engine.cost = boom  # type: ignore[assignment]
    assert rt.total_cost() == 0.0
    rt.shutdown()
