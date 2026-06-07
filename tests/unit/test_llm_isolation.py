"""Regression test: each spawned agent must get its own LLM client.

orchestral.Agent mutates the LLM client's tool router on construction
(``self.llm.set_tools(...)``). If two agents share the same LLM
instance, the second agent's tools clobber the first's. Subsequent
tool calls from the first agent would then route through the second
agent's identity — leading to symptoms like "this message was
addressed to a different agent" or runtime-token mismatches.
"""

from __future__ import annotations

from orchestral.context.message import Message
from orchestral.llm.base.llm import LLM
from orchestral.llm.base.response import Response

from spawn.engines.orchestral import make_orchestral_engine_factory
from spawn.record import AgentSpec
from spawn.runtime import Runtime


class _CountingFakeLLM(LLM):
    """Each instance records every set_tools call against ITSELF."""

    def __init__(self) -> None:
        # Initialize the counter BEFORE super().__init__ — the base
        # class calls self.set_tools([]) inside its init.
        self.set_tools_calls: list[list] = []
        self.tools: list = []
        super().__init__(tools=None)

    def set_tools(self, tools):
        self.tools = list(tools)
        self.set_tools_calls.append(list(tools))

    def get_response(self, context, **kwargs):
        return Response(
            model="fake",
            message=Message(role="assistant", text="ok", tool_calls=None),
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


def test_each_agent_gets_a_fresh_llm_via_factory():
    """With llm_factories, each spawned agent gets a brand-new client
    instance — verified by checking that no two agents share the same
    LLM object.
    """
    created: list[_CountingFakeLLM] = []

    def build():
        llm = _CountingFakeLLM()
        created.append(llm)
        return llm

    factory = make_orchestral_engine_factory(llm_factories={"default": build})
    rt = Runtime(engine_factory=factory)

    root = rt.root(AgentSpec(role_prompt="r", tools=["primitive"]))
    child_a = rt._spawn(parent=root, spec=AgentSpec(role_prompt="a", tools=["primitive"]))
    child_b = rt._spawn(parent=root, spec=AgentSpec(role_prompt="b", tools=["primitive"]))

    # Three distinct LLM instances created — one per agent.
    assert len(created) == 3
    assert len({id(x) for x in created}) == 3

    # Each LLM had set_tools called for its own Agent (with non-empty
    # tools). The base LLM __init__ also calls set_tools([]), so the
    # non-empty call is the meaningful one — and it should only ever
    # happen ONCE per LLM (no other agent reused this client).
    for llm in created:
        non_empty = [c for c in llm.set_tools_calls if c]
        assert len(non_empty) == 1, f"expected one non-empty set_tools call, got {len(non_empty)}"

    rt.shutdown()


def test_legacy_llms_arg_still_works_but_shares():
    """The ``llms`` (instance) keyword still works for single-threaded
    tests. It deliberately shares the client — only acceptable when
    you know agents won't run concurrently."""
    shared = _CountingFakeLLM()
    factory = make_orchestral_engine_factory(llms={"default": shared})
    rt = Runtime(engine_factory=factory)

    rt.root(AgentSpec(role_prompt="r", tools=["primitive"]))
    rt._spawn(
        parent=rt.root_addr,
        spec=AgentSpec(role_prompt="a", tools=["primitive"]),
    )

    # The shared client had set_tools called twice (once per agent).
    # Last call wins — that's the bug this whole change is fixing
    # for the factory path.
    assert len(shared.set_tools_calls) >= 2
    rt.shutdown()


def test_factory_must_be_provided():
    import pytest
    with pytest.raises(ValueError, match="llm_factories or llms"):
        make_orchestral_engine_factory()
