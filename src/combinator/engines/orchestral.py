"""``OrchestralEngine`` — wraps ``orchestral.Agent`` for production use.

The engine is constructed once per spawned agent. It holds an
``orchestral.Agent`` configured with:

- An LLM client (resolved by name from the runtime's LLM registry).
- The tools requested by ``spec.tools`` (each instantiated with this
  agent's ``runtime_token`` so capability checks authenticate
  correctly).
- A system prompt synthesized from ``spec.role_prompt`` plus combinator
  identity context (the agent's own address, label, and a brief
  description of the available tools).

``step(prompt)`` delegates to ``orchestral.Agent.run`` and returns the
final assistant text. Streaming and richer display hooks are deferred
to a follow-on iteration.
"""

from __future__ import annotations

from typing import Any, Callable, TYPE_CHECKING

from orchestral import Agent as OrchestralAgent

from combinator.engines.registry import (
    DEFAULT_TOOL_GROUPS,
    ToolGroupRegistry,
    build_tools,
)

if TYPE_CHECKING:
    from combinator.record import AgentRecord
    from combinator.runtime import Runtime


_DEFAULT_FRAME = """You are an agent in the Combinator multi-agent framework.

Your identity:
- Address id:  {addr_id}
- Label:       {label}

Your role:
{role_prompt}

You can use your tools to spawn child agents, send and receive messages,
terminate descendants, and introduce capabilities between agents.

When you have a new message in your inbox, you will be prompted with its
contents and asked to act. Reply with the ``send`` tool to address other
agents, or take other actions as appropriate. Return final responses by
sending them to the address from which the request originated (commonly
included in the message as ``reply_to``).
"""


class OrchestralEngine:

    def __init__(
        self,
        *,
        record: "AgentRecord",
        runtime: "Runtime",
        llm: Any,
        tools: list[Any],
        system_prompt: str | None = None,
        max_tool_iterations: int = 8,
    ) -> None:
        self._record = record
        self._runtime = runtime
        prompt = system_prompt or self._build_system_prompt(record)
        self._agent = OrchestralAgent(
            llm=llm,
            tools=tools,
            system_prompt=prompt,
            max_tool_interations=max_tool_iterations,
            # Tools already carry their runtime_token; tool_config left empty
            # so orchestral's clone-and-inject step doesn't overwrite them.
            tool_config=None,
            debug=False,
        )

    def step(self, prompt: str) -> str:
        response = self._agent.run(prompt)
        return response.text if response.text is not None else ""

    @staticmethod
    def _build_system_prompt(record: "AgentRecord") -> str:
        return _DEFAULT_FRAME.format(
            addr_id=record.addr.id,
            label=record.addr.label or "(none)",
            role_prompt=record.spec.role_prompt,
        )


def make_orchestral_engine_factory(
    *,
    llms: dict[str, Any],
    tool_registry: ToolGroupRegistry | None = None,
    max_tool_iterations: int = 8,
) -> Callable[["AgentRecord", "Runtime"], "OrchestralEngine"]:
    """Build an ``engine_factory`` suitable for ``Runtime(engine_factory=...)``.

    ``llms`` maps LLM names (referenced by ``AgentSpec.llm``) to live
    LLM client instances (e.g., ``orchestral.llm.GPT(...)``).
    """
    registry = tool_registry if tool_registry is not None else DEFAULT_TOOL_GROUPS

    def factory(record: "AgentRecord", runtime: "Runtime") -> OrchestralEngine:
        llm_name = record.spec.llm or "default"
        if llm_name not in llms:
            raise KeyError(
                f"LLM {llm_name!r} not configured (known: {sorted(llms)})"
            )
        llm = llms[llm_name]
        tool_names = list(record.spec.tools) if record.spec.tools else ["primitive"]
        tools = build_tools(record.token, tool_names, registry)
        return OrchestralEngine(
            record=record,
            runtime=runtime,
            llm=llm,
            tools=tools,
            max_tool_iterations=max_tool_iterations,
        )

    return factory
