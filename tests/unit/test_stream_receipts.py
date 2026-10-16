"""A streaming orchestral agent logs its tool results.

With an event log, the engine streams: it drives orchestral's tool loop
itself instead of calling ``Agent.run()``, which is where orchestral
calls the display hook after each round of tool calls. Before 0.2.6 the
streaming loop never called it, so tool results (successes, tools that
raised, unknown tools) never reached the event log, and the web UI and
TUI showed tool calls without their results.
"""

from __future__ import annotations

from types import SimpleNamespace

from orchestral.context.message import Message
from orchestral.llm.base.llm import LLM
from orchestral.llm.base.response import Response
from orchestral.llm.base.tool_call import ToolCall
from orchestral.tools import define_tool

from conjure.engines.orchestral import OrchestralEngine, _build_hook
from conjure.record import AgentSpec
from conjure.runtime import Runtime

from tests.unit.test_orchestral_engine import FakeLLM


@define_tool
def echo(text: str):
    """Return the text."""
    return f"echo: {text}"


@define_tool
def explode():
    """Always fails."""
    raise RuntimeError("boom")


class StreamingToolLLM(FakeLLM):
    """Streams one reply per script entry: a list of (tool, args) calls,
    or a final text."""

    def __init__(self, script: list) -> None:
        super().__init__()
        self.script = list(script)

    def set_tools(self, tools):
        LLM.set_tools(self, tools)  # builds the tool router the agent dispatches through

    def stream_response(self, context, **kwargs):
        step = self.script.pop(0)
        if isinstance(step, str):
            yield step
            return Response(model="fake", message=Message(role="assistant", text=step))
        calls = [ToolCall(id=f"call_{i}", tool_name=name, arguments=args) for i, (name, args) in enumerate(step)]
        yield ""
        return Response(model="fake", message=Message(role="assistant", text="", tool_calls=calls))


class ListLog:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, event: dict) -> None:
        self.events.append(event)


def _streaming_engine(llm, log):
    rt = Runtime()
    record = rt.record_for(rt.root(AgentSpec(role_prompt="r", tools=["primitive"])))
    hook = _build_hook(record=record, display_hook_builder=None, event_log_router=lambda _: log, streaming=True)
    engine = OrchestralEngine(
        record=record, runtime=rt, llm=llm, tools=[echo, explode],
        display_hook=hook, stream_emit=log.emit,
    )
    return rt, engine


def _results(log):
    return [(e["text"], e["failed"]) for e in log.events if e["kind"] == "tool"]


def test_streaming_logs_each_tool_result_once():
    log = ListLog()
    llm = StreamingToolLLM([
        [("echo", {"text": "hi"}), ("explode", {}), ("nope", {})],
        [("echo", {"text": "again"})],
        "done",
    ])
    rt, engine = _streaming_engine(llm, log)
    try:
        assert engine.step("go") == "done"
        results = _results(log)
        assert len(results) == 4
        assert results[0] == ("echo: hi", False)
        assert "boom" in results[1][0]  # orchestral reports a raising tool as an error string
        assert results[2][1] and "not found" in results[2][0]
        assert results[3] == ("echo: again", False)
        # Each round's results are logged before the next reply streams.
        kinds = [e["kind"] for e in log.events if e["kind"] in ("tool", "stream_end")]
        assert kinds == ["stream_end", "tool", "tool", "tool", "stream_end", "tool", "stream_end"]
        # Streamed text isn't logged a second time as a response.
        assert not [e for e in log.events if e["kind"] in ("response", "assistant")]
    finally:
        rt.shutdown()


def test_results_orchestral_already_logged_are_not_repeated():
    # orchestral calls the hook itself when a tool hook rejects a call;
    # the engine's flush after the round must not log that result again.
    class Reject:
        def before_call(self, tool_name, arguments):
            return SimpleNamespace(approved=False, error_message="denied", should_interrupt=False)

    log = ListLog()
    llm = StreamingToolLLM([[("echo", {"text": "hi"})], "done"])
    rt, engine = _streaming_engine(llm, log)
    engine._agent.tool_hooks = [Reject()]
    try:
        assert engine.step("go") == "done"
        assert _results(log) == [("denied", True)]
    finally:
        rt.shutdown()
