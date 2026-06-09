"""Tests for OrchestralEngine using a fake LLM.

The fake LLM satisfies orchestral's ``LLM`` interface: ``set_tools`` and
``get_response``. It returns a canned ``Response`` (no tool calls), so
``Agent.run`` finishes after one round.
"""

from __future__ import annotations

from orchestral.context.message import Message
from orchestral.llm.base.llm import LLM
from orchestral.llm.base.response import Response

from conjure.engines.orchestral import (
    OrchestralEngine,
    make_orchestral_engine_factory,
)
from conjure.record import AgentSpec
from conjure.runtime import Runtime


class FakeLLM(LLM):
    """LLM that returns a canned reply, no tool calls.

    Implements just the methods orchestral's ``Agent.run`` actually
    invokes; the other abstract members are stubbed.
    """

    def __init__(self, reply: str = "fake-reply") -> None:
        super().__init__(tools=None)
        self.reply = reply
        self.calls: list = []
        self.tools: list = []

    def set_tools(self, tools):
        self.tools = list(tools)

    def get_response(self, context, **kwargs):
        self.calls.append(context)
        msg = Message(role="assistant", text=self.reply, tool_calls=None)
        return Response(model="fake", message=msg)

    # ---- Unused abstract surface ----
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

    def _format_tool_choice(self, tool_choice):
        return None


def test_orchestral_engine_returns_llm_text():
    rt = Runtime()
    addr = rt.root(AgentSpec(role_prompt="root", tools=["primitive"]))
    record = rt.record_for(addr)

    fake = FakeLLM(reply="hello from fake")
    engine = OrchestralEngine(
        record=record,
        runtime=rt,
        llm=fake,
        tools=[],  # No combinator tools in this minimal test.
    )
    out = engine.step("user prompt")
    assert out == "hello from fake"
    assert len(fake.calls) == 1
    rt.shutdown()


def test_orchestral_engine_factory_resolves_named_llm():
    fake_default = FakeLLM(reply="default-llm")
    fake_cheap = FakeLLM(reply="cheap-llm")

    factory = make_orchestral_engine_factory(
        llms={"default": fake_default, "cheap": fake_cheap}
    )
    rt = Runtime(engine_factory=factory)
    root = rt.root(AgentSpec(role_prompt="root", tools=["primitive"], llm="default"))

    # The root's engine should have been built with fake_default.
    engine = rt.record_for(root).agent.engine
    assert isinstance(engine, OrchestralEngine)

    rt.send_external(to=root, body="hi")
    import time
    time.sleep(0.2)
    assert any(c is not None for c in fake_default.calls)
    rt.shutdown()


def test_orchestral_engine_factory_rejects_unknown_llm():
    factory = make_orchestral_engine_factory(llms={"default": FakeLLM()})
    rt = Runtime(engine_factory=factory)
    import pytest
    with pytest.raises(KeyError, match="not configured"):
        rt.root(AgentSpec(role_prompt="r", tools=["primitive"], llm="nonexistent"))
    rt.shutdown()


def test_orchestral_engine_builds_with_primitive_tools():
    """Spawning a real agent through the runtime gives it
    properly-tokenized primitive tools that don't error on construction."""
    fake = FakeLLM(reply="ok")
    factory = make_orchestral_engine_factory(llms={"default": fake})
    rt = Runtime(engine_factory=factory)
    root = rt.root(AgentSpec(role_prompt="r", tools=["primitive"], llm="default"))
    # The engine should hold an orchestral.Agent whose llm has tools set.
    engine = rt.record_for(root).agent.engine
    assert isinstance(engine, OrchestralEngine)
    # Orchestral stores tools on llm.tools after Agent.__init__.
    assert len(fake.tools) > 0
    rt.shutdown()
