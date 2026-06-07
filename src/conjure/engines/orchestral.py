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

from conjure.engines.registry import (
    DEFAULT_TOOL_GROUPS,
    ToolGroupRegistry,
    build_tools,
)

if TYPE_CHECKING:
    from conjure.record import AgentRecord
    from conjure.runtime import Runtime


DisplayHook = Callable[[Any], None]
EngineFactory = Callable[["AgentRecord", "Runtime"], "OrchestralEngine"]


_DEFAULT_FRAME = """You are an agent in the Spawn multi-agent framework.

Your identity:
- Address id:  {addr_id}
- Label:       {label}
- Depth:       {depth} (root is depth 0; max allowed is {max_depth})

Your role:
{role_prompt}

================ MESSAGING PROTOCOL — READ CAREFULLY ================

Every message you receive has a header
    ``[seq=N thread=... from=<sender-addr>]: <body>``
The way you reply depends on WHO the sender is.

CASE A — sender is ``@user`` (the human):
  Your final assistant text (a turn with no tool calls) is shown to
  the human through the UI. You do NOT need to ``send`` anything.
  Just answer.

CASE B — sender is ANY OTHER ADDRESS (another agent in the graph):
  Your final assistant text reaches NOBODY. There is no automatic
  forwarding between agents. You MUST explicitly call:
      send(to="<sender-addr>", body=<your reply>)
  using the address shown in the ``from`` field. Body can be a string
  or a JSON object. If you forget this, the sender will hang waiting
  for a reply that never comes.

CASE C — body contains an explicit ``reply_to`` field
  (e.g. ``{{"item": 2, "reply_to": "ag-abc..."}}``):
  ``send`` to the ``reply_to`` address instead of the sender. The
  caller has routed the result somewhere specific (often a collector
  agent).

================ DELEGATING TO A CHILD AGENT ================

If your task genuinely needs a child agent (be conservative — most
tasks don't), the pattern is THREE steps and all three are required:

  1. ``spawn(role_prompt="...", tools=[], label="...")`` — create
     the child. Pass ``tools=[]`` unless the child needs to spawn or
     send.
  2. ``send(to="<child-addr>", body="<task description>")`` — give
     the child its task. WITHOUT THIS SEND, THE CHILD SITS IDLE.
     spawn alone does nothing useful; the child has no idea what to do
     until you message it.
  3. Wait. The child will run, then ``send(to="<your-addr>", body=...)``
     back to YOU. Your driver will wake you with the reply in your
     inbox. Then synthesize your final answer for the original sender
     (per CASE A or CASE B above).

When NOT to spawn:
  - You can answer the question yourself with reasoning.
  - The sub-task is small and doesn't need parallelism or a different
    role.
  - You're tempted to "delegate" just to avoid thinking — that's a
    cascade waiting to happen.

The runtime enforces ``max_depth = {max_depth}``. Spawn beyond that
returns ``code=depth_exceeded`` — fall back to answering directly.

================ WHEN A CONVERSATION IS DONE ================

Once you've sent the reply that completes the task, STOP. Specifically:

  - Do NOT send acknowledgement messages ("got it", "thanks", "noted").
  - Do NOT send status pings ("are you still working?", "are you in
    standby?", "any updates?"). The driver wakes you when a real
    message arrives — there's no need to poll.
  - Do NOT message a child you spawned after you've received its
    reply, unless you have a NEW task for it. A reply to a reply
    starts an infinite politeness loop. The child is just sitting
    idle; that is the correct state.
  - Do NOT send a "done!" announcement to a parent that already got
    your task result. The result IS the announcement.

A turn that produces NO tool calls and NO final assistant text is a
valid way to end a conversation when there's nothing more to say.

================ SENTINELS ================

``@user`` is the human user. ``@system`` is the framework itself —
DO NOT send anywhere near ``@system``; it forwards nothing.
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
        prompt = system_prompt or self._build_system_prompt(record, runtime)
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

    def model_name(self) -> str | None:
        """Best-effort model identifier (e.g. ``claude-haiku-4-5``)."""
        llm = getattr(self._agent, "llm", None)
        if llm is None:
            return None
        for attr in ("model", "model_name", "model_id"):
            value = getattr(llm, attr, None)
            if isinstance(value, str) and value:
                return value
        return None

    def context_usage(self) -> tuple[int, int] | None:
        """Return ``(tokens_used, tokens_max)`` for this agent's
        context window, or None when we don't have token info.

        Estimated from message text lengths (~4 chars/token). Only
        invoked for the selected agent each UI tick (the control
        server gates that), so the O(messages) cost is bounded."""
        messages = getattr(getattr(self._agent, "context", None), "messages", None)
        if messages is None:
            return None
        total_chars = 0
        for msg in messages:
            text = getattr(msg, "text", None)
            if isinstance(text, str):
                total_chars += len(text)
                continue
            inner = getattr(msg, "message", None)
            if inner is not None:
                text = getattr(inner, "text", "") or ""
                total_chars += len(text)
        tokens_used = max(1, total_chars // 4)
        tokens_max = _model_context_window(self.model_name() or "")
        return (tokens_used, tokens_max)

    def cost(self) -> float:
        """Return the cumulative LLM cost (USD) seen by this engine."""
        try:
            return float(self._agent.get_total_cost())
        except Exception:
            return 0.0

    @staticmethod
    def _build_system_prompt(record: "AgentRecord", runtime: "Runtime") -> str:
        return _DEFAULT_FRAME.format(
            addr_id=record.addr.id,
            label=record.addr.label or "(none)",
            role_prompt=record.spec.role_prompt,
            depth=record.depth,
            max_depth=getattr(runtime, "max_depth", 3),
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
            from conjure.events import serialize_message

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


# ---------- model context-window lookup ----------

# Maximum input-token capacity per model. Used by the UI's context
# meter to render a progress bar (where roughly does the agent sit
# in its window, when would compaction kick in). Not authoritative —
# tune as Anthropic ships new variants.
_DEFAULT_CONTEXT_WINDOW = 200_000
_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-haiku-4-5": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-opus-4": 200_000,
    "claude-opus-4-7": 200_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-sonnet-20240620": 200_000,
    "claude-3-haiku": 200_000,
    "claude-3-opus": 200_000,
}


def _model_context_window(model: str) -> int:
    if not model:
        return _DEFAULT_CONTEXT_WINDOW
    if model in _CONTEXT_WINDOWS:
        return _CONTEXT_WINDOWS[model]
    # Best-effort prefix match (handles versioned variants).
    for known, window in _CONTEXT_WINDOWS.items():
        if model.startswith(known):
            return window
    return _DEFAULT_CONTEXT_WINDOW
