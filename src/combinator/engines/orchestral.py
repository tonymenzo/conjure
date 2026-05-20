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


DisplayHook = Callable[[Any], None]
EngineFactory = Callable[["AgentRecord", "Runtime"], "OrchestralEngine"]


_DEFAULT_FRAME = """You are an agent in the Combinator multi-agent framework.

Your identity:
- Address id:  {addr_id}
- Label:       {label}

Your role:
{role_prompt}

You can use your tools to spawn child agents, send and receive messages
with them, terminate descendants you spawned, and introduce capabilities
between agents. The FP combinators (agent_map / agent_fold /
agent_filter / agent_fixed_point) are available when applicable.

Reply mechanics:

- Your **final assistant text** is shown directly to whoever sent you
  the original task. For natural-language answers to the user, you do
  NOT need to ``send`` anything — just respond, and your reply reaches
  them automatically.
- Use the ``send`` tool only when you need to deliver a STRUCTURED
  message to another agent (a child you spawned, a peer, or to
  ``@user`` / ``@system`` when a structured payload is appropriate).
- Both ``@user`` and ``@system`` are valid send targets if you want to
  deliver a structured payload back; they are always in your capability
  set.
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
        display_hook: DisplayHook | None = None,
    ) -> None:
        self._record = record
        self._runtime = runtime
        prompt = system_prompt or self._build_system_prompt(record)
        self._agent = OrchestralAgent(
            llm=llm,
            tools=tools,
            system_prompt=prompt,
            max_tool_interations=max_tool_iterations,
            tool_config=None,
            display_hook=display_hook,
            debug=False,
        )

    def step(self, prompt: str) -> str:
        response = self._agent.run(prompt)
        return response.text if response.text is not None else ""

    def cost(self) -> float:
        """Return the cumulative LLM cost (USD) seen by this engine."""
        try:
            return float(self._agent.get_total_cost())
        except Exception:
            return 0.0

    @staticmethod
    def _build_system_prompt(record: "AgentRecord") -> str:
        return _DEFAULT_FRAME.format(
            addr_id=record.addr.id,
            label=record.addr.label or "(none)",
            role_prompt=record.spec.role_prompt,
        )


def make_orchestral_engine_factory(
    *,
    llm_factories: dict[str, Callable[[], Any]] | None = None,
    llms: dict[str, Any] | None = None,
    tool_registry: ToolGroupRegistry | None = None,
    max_tool_iterations: int = 8,
    display_hook_builder: Callable[["AgentRecord"], DisplayHook] | None = None,
    event_log_router: Callable[["AgentRecord"], Any] | None = None,
) -> EngineFactory:
    """Build an ``engine_factory`` suitable for ``Runtime(engine_factory=...)``.

    ``llm_factories`` maps LLM names (referenced by ``AgentSpec.llm``)
    to zero-arg builders that produce a fresh LLM client each call.
    Each spawned agent gets its own client — orchestral mutates the
    client's tool router via ``set_tools``, so sharing the client
    across agents corrupts everyone's routing.

    ``llms`` (legacy) accepts pre-built client instances. Kept for
    tests that use a FakeLLM single-threadedly. Production code should
    pass ``llm_factories``.

    ``display_hook_builder`` (REPL path) — called once per spawned
    agent; returns a ``DisplayHook`` that renders to a console.

    ``event_log_router`` (tmux path) — called once per spawned agent;
    returns an ``EventLog`` (or None to fall back to
    ``display_hook_builder``). When present, the engine's display
    hook serializes orchestral context messages to event dicts and
    emits them to the log, which the per-agent renderer process tails.
    """
    if llm_factories is None and llms is None:
        raise ValueError("must provide one of llm_factories or llms")
    registry = tool_registry if tool_registry is not None else DEFAULT_TOOL_GROUPS

    def factory(record: "AgentRecord", runtime: "Runtime") -> OrchestralEngine:
        llm_name = record.spec.llm or "default"
        if llm_factories is not None:
            if llm_name not in llm_factories:
                raise KeyError(
                    f"LLM {llm_name!r} not configured (known: {sorted(llm_factories)})"
                )
            llm = llm_factories[llm_name]()
        else:
            if llm_name not in llms:
                raise KeyError(
                    f"LLM {llm_name!r} not configured (known: {sorted(llms)})"
                )
            llm = llms[llm_name]
        tool_names = list(record.spec.tools) if record.spec.tools else ["primitive"]
        tools = build_tools(record.token, tool_names, registry)
        hook = _build_hook(
            record=record,
            display_hook_builder=display_hook_builder,
            event_log_router=event_log_router,
        )
        return OrchestralEngine(
            record=record,
            runtime=runtime,
            llm=llm,
            tools=tools,
            max_tool_iterations=max_tool_iterations,
            display_hook=hook,
        )

    return factory


def _build_hook(
    *,
    record: "AgentRecord",
    display_hook_builder: Callable[["AgentRecord"], DisplayHook] | None,
    event_log_router: Callable[["AgentRecord"], Any] | None,
) -> DisplayHook | None:
    """Pick the right display-hook flavor for this agent.

    Preference order:
    1. event_log_router → returns an EventLog → use event-log hook
    2. display_hook_builder → returns a DisplayHook → use that
    3. neither → no hook (silent engine)
    """
    if event_log_router is not None:
        event_log = event_log_router(record)
        if event_log is not None:
            from combinator.events import serialize_message

            seen = {"n": 0}

            def hook(context: Any) -> None:
                messages = getattr(context, "messages", None) or []
                for msg in messages[seen["n"]:]:
                    event_log.emit(serialize_message(msg))
                seen["n"] = len(messages)

            return hook
    if display_hook_builder is not None:
        return display_hook_builder(record)
    return None
