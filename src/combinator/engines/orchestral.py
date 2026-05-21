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

Replying to a message:

1. Inspect the incoming message in your prompt. Its header is
   ``[seq=N thread=... from=<sender-addr>]: <body>``.
2. If the body contains a ``reply_to`` field (e.g.
   ``{{"item": 2, "reply_to": "ag-abc..."}}``), reply by calling
   ``send(to="<reply_to>", body=...)`` — the caller has explicitly
   told you where the result goes.
3. Otherwise reply to the sender directly: ``send(to="<sender-addr>",
   body=...)`` using the address shown in the ``from`` field.

For natural-language answers to the *human* user, you do not need to
``send`` anything — your final assistant text (a turn with no tool
calls) is shown to whoever initiated the task and reaches them
automatically.

About the ``@user`` and ``@system`` sentinel addresses: ``@user`` is
the human user. ``@system`` is the framework itself — DO NOT send
result messages to ``@system``; it does not forward them anywhere.
Reply to the actual sender of the message you received, as described
above.
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
        stream_emit: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """``stream_emit`` (optional) — when provided, ``step`` streams
        text chunks to it as ``{"kind": "chunk", "text": ...}`` events
        and emits a ``{"kind": "stream_end", "tool_calls": [...]}``
        event when the LLM finishes a response. The display hook is
        expected to filter out ``response`` events in this mode (the
        engine has already emitted the text as chunks).
        """
        self._record = record
        self._runtime = runtime
        self._stream_emit = stream_emit
        self._max_tool_iterations = max_tool_iterations
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
        if self._stream_emit is None:
            response = self._agent.run(prompt)
            return response.text if response.text is not None else ""
        return self._step_streaming(prompt)

    def _step_streaming(self, prompt: str) -> str:
        """Drive orchestral's tool-call loop ourselves so we can stream
        each LLM response chunk as it arrives.

        Pattern:
        1. ``stream_text_message`` adds the user message and yields
           text chunks from the first LLM response. We emit each as a
           ``chunk`` event.
        2. When the stream ends, orchestral has added the full
           ``Response`` to context. We emit ``stream_end`` with the
           model's tool calls (if any).
        3. If there are tool calls, run them (the display hook emits
           ``tool`` events for the results), then stream the next LLM
           response. Repeat.
        4. Stop when an iteration produces no tool calls.
        """
        accumulated = ""
        # First iteration: includes adding the user message to context.
        for chunk in self._agent.stream_text_message(prompt):
            self._emit_chunk(chunk)
            accumulated += chunk
        self._emit_stream_end()

        for _ in range(self._max_tool_iterations):
            last = self._agent.context.messages[-1]
            tool_calls = self._extract_tool_calls(last)
            if not tool_calls:
                break
            self._agent._handle_tool_calls()  # noqa: SLF001
            # Stream the next response.
            for chunk in self._agent._stream_response():  # noqa: SLF001
                self._emit_chunk(chunk)
                accumulated += chunk
            self._emit_stream_end()

        return accumulated

    @staticmethod
    def _extract_tool_calls(msg: Any) -> list[Any]:
        inner = getattr(msg, "message", None)
        if inner is not None:
            return list(getattr(inner, "tool_calls", None) or [])
        return list(getattr(msg, "tool_calls", None) or [])

    def _emit_chunk(self, text: str) -> None:
        if self._stream_emit is None or not text:
            return
        try:
            self._stream_emit({"kind": "chunk", "text": text})
        except Exception:
            pass

    def _emit_stream_end(self) -> None:
        if self._stream_emit is None:
            return
        try:
            last = self._agent.context.messages[-1]
            tool_calls = self._extract_tool_calls(last)
            serialized = [
                {
                    "name": getattr(tc, "tool_name", None) or getattr(tc, "name", "?"),
                    "args": getattr(tc, "arguments", None) or {},
                }
                for tc in tool_calls
            ]
            self._stream_emit({"kind": "stream_end", "tool_calls": serialized})
        except Exception:
            pass

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
    stream: bool = False,
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

        # When streaming, the engine emits chunks directly into the
        # agent's event log and the display hook MUST filter out
        # ``response`` events (the engine already streamed them).
        stream_emit: Callable[[dict[str, Any]], None] | None = None
        if stream and event_log_router is not None:
            event_log = event_log_router(record)
            if event_log is not None:
                stream_emit = event_log.emit

        hook = _build_hook(
            record=record,
            display_hook_builder=display_hook_builder,
            event_log_router=event_log_router,
            streaming=stream and stream_emit is not None,
        )
        return OrchestralEngine(
            record=record,
            runtime=runtime,
            llm=llm,
            tools=tools,
            max_tool_iterations=max_tool_iterations,
            display_hook=hook,
            stream_emit=stream_emit,
        )

    return factory


def _build_hook(
    *,
    record: "AgentRecord",
    display_hook_builder: Callable[["AgentRecord"], DisplayHook] | None,
    event_log_router: Callable[["AgentRecord"], Any] | None,
    streaming: bool = False,
) -> DisplayHook | None:
    """Pick the right display-hook flavor for this agent.

    Preference order:
    1. event_log_router → returns an EventLog → use event-log hook
    2. display_hook_builder → returns a DisplayHook → use that
    3. neither → no hook (silent engine)

    When ``streaming`` is True, the event-log hook filters out
    ``response`` and ``assistant`` events — the engine has streamed
    that text as ``chunk`` events already. Tool results still flow
    through the hook normally.
    """
    if event_log_router is not None:
        event_log = event_log_router(record)
        if event_log is not None:
            from combinator.events import serialize_message

            seen = {"n": 0}

            def hook(context: Any) -> None:
                messages = getattr(context, "messages", None) or []
                for msg in messages[seen["n"]:]:
                    event = serialize_message(msg)
                    if streaming and event.get("kind") in ("response", "assistant"):
                        # Engine streamed this; skip the duplicate.
                        continue
                    event_log.emit(event)
                seen["n"] = len(messages)

            return hook
    if display_hook_builder is not None:
        return display_hook_builder(record)
    return None
