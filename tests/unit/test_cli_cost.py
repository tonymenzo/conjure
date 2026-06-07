"""Smoke test for the :cost REPL command."""

from __future__ import annotations

import io

from rich.console import Console

from spawn.cli import _handle_command, _print_cost
from spawn.record import AgentSpec
from spawn.runtime import Runtime
from spawn.scripted import BehaviorRegistry


def _captured_console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False, no_color=True, width=200)


def _runtime():
    reg = BehaviorRegistry()
    reg.register("idle", lambda *_a, **_kw: "ok")
    return Runtime(engine_factory=reg.factory())


def test_cost_command_prints_per_agent_and_total():
    rt = _runtime()
    addr = rt.root(AgentSpec(role_prompt="idle", label="iota"))
    rt.record_for(addr).agent.engine.cost = lambda: 0.0042
    console = _captured_console()
    _print_cost(rt, console=console)
    out = console.file.getvalue()
    assert "iota" in out
    assert "0.0042" in out
    assert "total" in out.lower()
    rt.shutdown()


def test_cost_command_via_handle_command():
    rt = _runtime()
    root = rt.root(AgentSpec(role_prompt="idle", label="iota"))
    rt.record_for(root).agent.engine.cost = lambda: 0.001
    console = _captured_console()
    should_exit = _handle_command(":cost", console=console, runtime=rt, root=root)
    assert should_exit is False
    out = console.file.getvalue()
    assert "iota" in out
    assert "$0" in out
    rt.shutdown()


def test_cost_command_zero_when_no_llm_calls():
    rt = _runtime()
    rt.root(AgentSpec(role_prompt="idle", label="iota"))
    console = _captured_console()
    _print_cost(rt, console=console)
    out = console.file.getvalue()
    assert "$0" in out
    rt.shutdown()
