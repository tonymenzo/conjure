"""Tests for Runtime.spawn_listener and the engine factory's
event_log_router — the two pieces that let the tmux orchestrator
attach state to each spawned agent."""

from __future__ import annotations

import json
from pathlib import Path

from orchestral.context.message import Message
from orchestral.llm.base.llm import LLM
from orchestral.llm.base.response import Response

from spawn.engines.orchestral import make_orchestral_engine_factory
from spawn.event_log import EventLog
from spawn.record import AgentRecord, AgentSpec
from spawn.runtime import Runtime


class _FakeLLM(LLM):
    def __init__(self) -> None:
        super().__init__(tools=None)
        self.tools: list = []

    def set_tools(self, tools):
        self.tools = list(tools)

    def get_response(self, context, **kwargs):
        return Response(
            model="fake",
            message=Message(role="assistant", text="hi", tool_calls=None),
        )

    def call_api(self, formatted_input, **kwargs):
        raise NotImplementedError

    def call_streaming_api(self, formatted_input, **kwargs):
        raise NotImplementedError

    def extract_text_from_chunk(self, chunk) -> str:
        return ""

    def process_api_input(self, context):
        return None

    def process_api_response(self, api_response):
        raise NotImplementedError

    def process_streaming_response(self, accumulated_chunks, accumulated_text, final_chunk):
        raise NotImplementedError

    def _convert_tools_to_provider_format(self):
        return []


def test_spawn_listener_fires_for_root():
    seen: list[AgentRecord] = []
    rt = Runtime(spawn_listener=lambda r: seen.append(r))
    rt.root(AgentSpec(role_prompt="r", label="iota"))
    assert len(seen) == 1
    assert seen[0].addr.label == "iota"
    rt.shutdown()


def test_spawn_listener_fires_for_children():
    seen: list[AgentRecord] = []
    rt = Runtime(spawn_listener=lambda r: seen.append(r))
    root = rt.root(AgentSpec(role_prompt="r", label="iota"))
    rt._spawn(parent=root, spec=AgentSpec(role_prompt="c", label="child"))
    rt._spawn(parent=root, spec=AgentSpec(role_prompt="c2", label="child2"))
    assert [r.addr.label for r in seen] == ["iota", "child", "child2"]
    rt.shutdown()


def test_event_log_router_emits_events(tmp_path: Path):
    """The engine factory plumbs an event log onto the engine's display
    hook; orchestral context updates show up as JSONL events."""

    def event_log_router(record: AgentRecord) -> EventLog | None:
        return record.event_log

    def spawn_listener(record: AgentRecord) -> None:
        path = tmp_path / f"{record.addr.id}.jsonl"
        record.event_log = EventLog(path)

    factory = make_orchestral_engine_factory(
        llms={"default": _FakeLLM()},
        event_log_router=event_log_router,
    )
    rt = Runtime(engine_factory=factory, spawn_listener=spawn_listener)
    root = rt.root(AgentSpec(role_prompt="r", label="iota", tools=["primitive"]))

    # Send a message and let the driver process it.
    rt.send_external(to=root, body="hello")
    target_seq = rt.record_for(root).inbox.latest_seq()
    rt.wait_for_idle(root, target_seq, timeout_s=5.0)

    # Find the agent's log file — it's the per-agent .jsonl we created.
    log_path = next(tmp_path.glob("*.jsonl"))
    rt.record_for(root).event_log.close()

    events = [
        json.loads(line)
        for line in log_path.read_text().splitlines()
        if line.strip()
    ]
    # We expect at least one response event from the FakeLLM ("hi").
    response_events = [e for e in events if e["kind"] == "response"]
    assert response_events, f"no response events found in {events}"
    assert response_events[0]["text"] == "hi"
    rt.shutdown()


def test_event_log_router_falls_back_when_returning_none():
    """If event_log_router returns None, the engine falls back to the
    display_hook_builder (or no hook)."""
    factory = make_orchestral_engine_factory(
        llms={"default": _FakeLLM()},
        event_log_router=lambda _r: None,
        display_hook_builder=lambda _r: lambda _ctx: None,
    )
    rt = Runtime(engine_factory=factory)
    rt.root(AgentSpec(role_prompt="r", tools=["primitive"]))
    # No assertion needed — we just want construction to not throw.
    rt.shutdown()
