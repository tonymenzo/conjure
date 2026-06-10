"""Hierarchical cost ceilings — ``AgentSpec.budget`` enforcement."""

from __future__ import annotations

import time

import pytest

from conjure.errors import BudgetExceeded
from conjure.record import AgentSpec
from conjure.runtime import Runtime
from conjure.scripted import BehaviorRegistry
from conjure.tools.primitives import spawn_impl


def _costing_factory(reg: BehaviorRegistry, costs: dict[str, float]):
    """Engine factory whose engines report ``costs[label]`` as their
    spend. Tests mutate ``costs`` to simulate tokens burning."""
    base = reg.factory()

    def make(record, runtime):
        engine = base(record, runtime)
        engine.cost = lambda: costs.get(record.spec.label, 0.0)
        return engine

    return make


def _build(costs: dict[str, float], *, root_budget: float | None = None):
    reg = BehaviorRegistry()
    reg.register("idle", lambda *_a, **_k: "idle")
    rt = Runtime(engine_factory=_costing_factory(reg, costs))
    root = rt.root(
        AgentSpec(role_prompt="idle", label="root", budget=root_budget)
    )
    return rt, root


def test_subtree_cost_sums_descendants():
    costs = {"root": 0.10, "kid": 0.25, "grandkid": 0.05}
    rt, root = _build(costs)
    kid = rt._spawn(parent=root, spec=AgentSpec(role_prompt="idle", label="kid"))
    rt._spawn(parent=kid, spec=AgentSpec(role_prompt="idle", label="grandkid"))
    assert rt.subtree_cost(root) == pytest.approx(0.40)
    assert rt.subtree_cost(kid) == pytest.approx(0.30)
    rt.shutdown()


def test_budget_exceeded_reports_nearest_exhausted_holder():
    costs = {"root": 0.0, "kid": 0.0, "grandkid": 0.0}
    rt, root = _build(costs, root_budget=1.0)
    kid = rt._spawn(
        parent=root, spec=AgentSpec(role_prompt="idle", label="kid", budget=0.2)
    )
    grandkid = rt._spawn(
        parent=kid, spec=AgentSpec(role_prompt="idle", label="grandkid")
    )
    assert rt.budget_exceeded(grandkid) is None
    costs["grandkid"] = 0.3  # blows the kid's 0.2 ceiling, not the root's 1.0
    assert rt.budget_exceeded(grandkid) == kid.id
    assert rt.budget_exceeded(root) is None
    rt.shutdown()


def test_spawn_refused_under_exhausted_budget():
    costs = {"root": 1.5}
    rt, root = _build(costs, root_budget=1.0)
    with pytest.raises(BudgetExceeded):
        rt._spawn(parent=root, spec=AgentSpec(role_prompt="idle", label="kid"))
    rt.shutdown()


def test_spawn_tool_returns_budget_exceeded_code():
    costs = {"root": 1.5}
    rt, root = _build(costs, root_budget=1.0)
    token = rt.record_for(root).token
    out = spawn_impl(token=token, role_prompt="idle", label="kid")
    assert out["ok"] is False
    assert out["code"] == "budget_exceeded"
    rt.shutdown()


def test_driver_skips_step_and_notifies_parent_when_exhausted():
    costs = {"root": 0.0, "kid": 2.0}
    rt, root = _build(costs)
    kid = rt._spawn(
        parent=root, spec=AgentSpec(role_prompt="idle", label="kid", budget=1.0)
    )
    rt.send_external(to=kid, body="do work")

    deadline = time.monotonic() + 5.0
    kid_record = rt.record_for(kid)
    while time.monotonic() < deadline and kid_record.status != "error":
        time.sleep(0.02)
    assert kid_record.status == "error"

    # The engine never ran — the driver consumed the message but
    # skipped the step.
    assert kid_record.agent.engine.calls == 0

    # The parent received exactly one budget_exceeded supervision event.
    events = [
        e for e in rt.read_inbox(root)
        if isinstance(e.body, dict)
        and e.body.get("kind") == "child_event"
        and e.body.get("event") == "budget_exceeded"
    ]
    assert len(events) == 1
    assert kid.id in events[0].body["reason"] or "kid" in events[0].body["reason"]
    rt.shutdown()


def test_no_budget_means_no_enforcement():
    costs = {"root": 99.0, "kid": 99.0}
    rt, root = _build(costs)  # no budgets anywhere
    kid = rt._spawn(parent=root, spec=AgentSpec(role_prompt="idle", label="kid"))
    assert rt.budget_exceeded(kid) is None
    rt.send_external(to=kid, body="hello")
    deadline = time.monotonic() + 5.0
    kid_record = rt.record_for(kid)
    while time.monotonic() < deadline and kid_record.agent.engine.calls == 0:
        time.sleep(0.02)
    assert kid_record.agent.engine.calls == 1  # step ran normally
    rt.shutdown()
