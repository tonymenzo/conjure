"""Tests for spawn.driver — uses an inline mock engine so the
driver loop can be exercised without orchestral.
"""

from __future__ import annotations

import threading
import time

import pytest

from spawn.agent import Agent
from spawn.record import AgentRecord, AgentSpec
from spawn.runtime import Runtime


class MockEngine:
    """Records every prompt it receives. Optionally raises on the first
    call to exercise the driver's exception path."""

    def __init__(self, *, raise_once: bool = False) -> None:
        self.prompts: list[str] = []
        self._raise_once = raise_once
        self.signal = threading.Event()

    def step(self, prompt: str) -> str:
        self.prompts.append(prompt)
        self.signal.set()
        if self._raise_once:
            self._raise_once = False
            raise RuntimeError("boom")
        return "ok"


def _factory(*, capture: list[MockEngine], raise_first: bool = False):
    def make(record: AgentRecord, runtime: Runtime) -> MockEngine:
        engine = MockEngine(raise_once=raise_first and not capture)
        capture.append(engine)
        return engine
    return make


def _wait_for_call(engine: MockEngine, *, timeout: float = 2.0) -> None:
    assert engine.signal.wait(timeout=timeout), "engine.step was not called in time"
    engine.signal.clear()


def test_driver_starts_idle_with_no_messages():
    engines: list[MockEngine] = []
    rt = Runtime(engine_factory=_factory(capture=engines))
    addr = rt.root(AgentSpec(role_prompt="root"))
    record = rt.record_for(addr)
    assert record.status == "idle"
    assert record.driver is not None
    rt.shutdown()
    assert record.status == "terminated"


def test_driver_wakes_on_send_external():
    engines: list[MockEngine] = []
    rt = Runtime(engine_factory=_factory(capture=engines))
    addr = rt.root(AgentSpec(role_prompt="root"))

    rt.send_external(to=addr, body={"task": "hello"})
    _wait_for_call(engines[0])
    assert any("hello" in p for p in engines[0].prompts)
    rt.shutdown()


def test_driver_advances_cursor_across_calls():
    engines: list[MockEngine] = []
    rt = Runtime(engine_factory=_factory(capture=engines))
    addr = rt.root(AgentSpec(role_prompt="root"))
    engine = engines[0]

    rt.send_external(to=addr, body="m1")
    _wait_for_call(engine)
    rt.send_external(to=addr, body="m2")
    _wait_for_call(engine)

    # Each prompt should contain only the new message, not both.
    last = engine.prompts[-1]
    assert "m2" in last
    assert "m1" not in last
    rt.shutdown()


def test_driver_handles_engine_exception_and_continues():
    engines: list[MockEngine] = []

    def factory(record: AgentRecord, runtime: Runtime) -> MockEngine:
        e = MockEngine(raise_once=True)
        engines.append(e)
        return e

    rt = Runtime(engine_factory=factory)
    addr = rt.root(AgentSpec(role_prompt="root"))
    engine = engines[0]

    rt.send_external(to=addr, body="will-raise")
    _wait_for_call(engine)
    # First step raised; status is sticky ``error`` so the tree's red
    # dot persists until the agent successfully processes more work.
    time.sleep(0.05)
    assert rt.record_for(addr).status == "error"

    rt.send_external(to=addr, body="will-succeed")
    _wait_for_call(engine)
    # Successful step clears the error.
    time.sleep(0.05)
    assert rt.record_for(addr).status == "idle"
    assert len(engine.prompts) == 2
    rt.shutdown()


def test_engine_exception_notifies_parent_via_child_event():
    """Supervision: a child whose engine raises shows up in the
    parent's inbox as a ``@system → parent`` ``child_event`` with
    ``event: "errored"``. Lets the parent react without polling."""
    engines: list[MockEngine] = []

    def factory(record: AgentRecord, runtime: Runtime) -> MockEngine:
        # Only the *child* should raise; the root is a quiet observer.
        raise_first = bool(engines)  # second-built engine is the child
        e = MockEngine(raise_once=raise_first)
        engines.append(e)
        return e

    rt = Runtime(engine_factory=factory)
    root = rt.root(AgentSpec(role_prompt="root"))
    child = rt._spawn(parent=root, spec=AgentSpec(role_prompt="child", label="c"))
    root_inbox = rt.record_for(root).inbox
    base_seq = root_inbox.latest_seq()

    rt.send_external(to=child, body="will-raise")
    _wait_for_call(engines[1])
    # Wait for status flip + notification to settle.
    time.sleep(0.1)

    new = root_inbox.read(since_seq=base_seq, max_n=10)
    events = [
        e for e in new
        if isinstance(e.body, dict) and e.body.get("kind") == "child_event"
    ]
    assert events, f"no child_event delivered; saw: {[e.body for e in new]}"
    assert events[0].body["event"] == "errored"
    assert events[0].body["child_addr"] == child.id
    assert "RuntimeError" in events[0].body.get("reason", "")
    assert events[0].from_.id == "@system"
    rt.shutdown()


def test_driver_emits_engine_error_to_event_log():
    """Engine exceptions surface as ``{"kind": "error", "text": ...}``
    events on the agent's event log so the chat pane can render them.
    Without this the user just sees the agent stop replying."""
    emitted: list[dict] = []

    class _CaptureLog:
        def emit(self, event: dict) -> None:
            emitted.append(event)

    engines: list[MockEngine] = []

    def factory(record: AgentRecord, runtime: Runtime) -> MockEngine:
        record.event_log = _CaptureLog()
        e = MockEngine(raise_once=True)
        engines.append(e)
        return e

    rt = Runtime(engine_factory=factory)
    addr = rt.root(AgentSpec(role_prompt="root"))
    engine = engines[0]

    rt.send_external(to=addr, body="boom")
    _wait_for_call(engine)
    time.sleep(0.05)

    error_events = [e for e in emitted if e.get("kind") == "error"]
    assert error_events, f"no error event emitted; saw: {emitted}"
    assert "RuntimeError" in error_events[0]["text"]
    assert "boom" in error_events[0]["text"]
    rt.shutdown()


def test_oneshot_auto_terminates_after_successful_step():
    """``spec.oneshot=True`` makes the runtime tear the agent down
    as soon as its first step returns cleanly. An errored step
    leaves it in ``status="error"`` for inspection / retry."""
    engines: list[MockEngine] = []
    rt = Runtime(engine_factory=_factory(capture=engines))
    root = rt.root(AgentSpec(role_prompt="root"))
    child = rt._spawn(
        parent=root,
        spec=AgentSpec(role_prompt="oneshot", label="ow", oneshot=True),
    )
    engine = engines[1]

    rt.send_external(to=child, body="do the thing")
    _wait_for_call(engine)
    # Give the driver a moment to flip status + invoke terminate.
    time.sleep(0.1)
    assert rt.record_for(child).status == "terminated"
    rt.shutdown()


def test_oneshot_termination_does_not_spam_parent_supervision():
    """Supervision events for ``requested_by=oneshot`` are suppressed
    — the parent already collected the worker's reply, so the
    ``child_event terminated`` envelope adds no signal and just
    floods the inbox at the tail of a fan-out."""
    engines: list[MockEngine] = []
    rt = Runtime(engine_factory=_factory(capture=engines))
    root = rt.root(AgentSpec(role_prompt="root"))
    child = rt._spawn(
        parent=root,
        spec=AgentSpec(role_prompt="oneshot", oneshot=True),
    )
    root_inbox = rt.record_for(root).inbox
    base_seq = root_inbox.latest_seq()

    rt.send_external(to=child, body="go")
    _wait_for_call(engines[1])
    time.sleep(0.1)
    assert rt.record_for(child).status == "terminated"

    new = root_inbox.read(since_seq=base_seq, max_n=10)
    events = [
        e for e in new
        if isinstance(e.body, dict) and e.body.get("kind") == "child_event"
    ]
    assert events == [], (
        f"oneshot teardown should not produce supervision events; "
        f"saw: {[e.body for e in events]}"
    )
    rt.shutdown()


def test_oneshot_does_not_terminate_on_errored_step():
    """A oneshot agent whose engine raises should NOT be auto-
    terminated — leave it in ``status="error"`` so the parent can
    inspect or retry."""
    engines: list[MockEngine] = []

    def factory(record: AgentRecord, runtime: Runtime) -> MockEngine:
        raise_first = bool(engines)
        e = MockEngine(raise_once=raise_first)
        engines.append(e)
        return e

    rt = Runtime(engine_factory=factory)
    root = rt.root(AgentSpec(role_prompt="root"))
    child = rt._spawn(
        parent=root,
        spec=AgentSpec(role_prompt="oneshot", oneshot=True),
    )

    rt.send_external(to=child, body="boom")
    _wait_for_call(engines[1])
    time.sleep(0.1)
    assert rt.record_for(child).status == "error"  # not terminated
    rt.shutdown()


def test_driver_stops_cleanly_on_terminate():
    engines: list[MockEngine] = []
    rt = Runtime(engine_factory=_factory(capture=engines))
    addr = rt.root(AgentSpec(role_prompt="root"))
    record = rt.record_for(addr)
    driver = record.driver
    assert driver is not None

    rt.terminate(addr)
    # The driver should exit; give it a moment.
    assert driver.join(timeout=2.0)
    assert record.status == "terminated"
    rt.shutdown()


def test_lazy_spawn_skips_driver():
    engines: list[MockEngine] = []
    rt = Runtime(engine_factory=_factory(capture=engines))
    addr = rt.root(AgentSpec(role_prompt="root", lazy=True))
    record = rt.record_for(addr)
    assert record.status == "lazy"
    assert record.driver is None
    assert len(engines) == 0
    rt.shutdown()


def test_runtime_without_engine_factory_stays_lazy():
    rt = Runtime()  # no engine_factory
    addr = rt.root(AgentSpec(role_prompt="root"))
    record = rt.record_for(addr)
    assert record.status == "lazy"
    assert record.driver is None
    rt.shutdown()


def test_shutdown_stops_all_drivers():
    engines: list[MockEngine] = []
    rt = Runtime(engine_factory=_factory(capture=engines))
    root = rt.root(AgentSpec(role_prompt="root"))
    child = rt._spawn(parent=root, spec=AgentSpec(role_prompt="c"))
    grand = rt._spawn(parent=child, spec=AgentSpec(role_prompt="g"))

    rt.shutdown()
    for addr in (root, child, grand):
        record = rt.record_for(addr)
        assert record.status == "terminated"
        assert record.driver is not None
        assert record.driver.join(timeout=2.0)
