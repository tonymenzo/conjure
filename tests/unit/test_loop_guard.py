"""Safeguards: a turn looping on failing tool calls stops with a notice,
and an agent whose tools would be offered and dispatched under different
names refuses to start."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from conjure.engines.orchestral import check_tool_names, loop_guard


def ctx(*msgs):
    return SimpleNamespace(messages=[SimpleNamespace(role=r, text=t) for r, t in msgs])


def test_repeated_calls_to_a_missing_tool_stop_the_turn():
    nf = "Tool 'catalog' not found! Available tools: ['ToolCatalog']"
    assert loop_guard(ctx(("user", "go"), ("tool", nf), ("tool", nf))) is None
    reason = loop_guard(ctx(("user", "go"), ("tool", nf), ("tool", nf), ("tool", nf)))
    assert reason and "don't exist" in reason and "catalog" in reason


def test_the_same_error_back_to_back_stops_the_turn_but_varied_work_does_not():
    err = "{'ok': False, 'code': 'timeout', 'error': 'took 30s'}"
    looping = ctx(("user", "go"), *[("tool", err)] * 4)
    assert "same tool error" in loop_guard(looping)
    mixed = ctx(("user", "go"), ("tool", err), ("tool", "{'ok': True}"), ("tool", err), ("tool", err))
    assert loop_guard(mixed) is None


def test_only_the_current_turn_counts():
    nf = "Tool 'x' not found! Available tools: []"
    earlier = [("tool", nf)] * 3
    assert loop_guard(ctx(("user", "first"), *earlier, ("user", "second"), ("tool", nf))) is None


def test_mismatched_or_duplicate_tool_names_refuse_to_start():
    def tool(offered, routed):
        return SimpleNamespace(get_tool_spec=lambda: SimpleNamespace(name=offered), get_name=lambda: routed)

    check_tool_names([tool("Spawn", "Spawn"), tool("calc__add", "calc__add")])
    with pytest.raises(RuntimeError, match="don't match"):
        check_tool_names([tool("catalog", "ToolCatalog")])
    with pytest.raises(RuntimeError, match="duplicate"):
        check_tool_names([tool("Spawn", "Spawn"), tool("Spawn", "Spawn")])
