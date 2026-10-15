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
    from conjure.toolbase import ToolbaseMount


DisplayHook = Callable[[Any], None]
EngineFactory = Callable[["AgentRecord", "Runtime"], "OrchestralEngine"]




class OrchestralEngine:

    def __init__(
        self,
        *,
        record: "AgentRecord",
        runtime: "Runtime",
        llm: Any,
        tools: list[Any],
        system_prompt: str | None = None,
        max_tool_iterations: int = 50,
        display_hook: DisplayHook | None = None,
        stream_emit: Callable[[dict[str, Any]], None] | None = None,
        toolbase_mount: "ToolbaseMount | None" = None,
    ) -> None:
        """``toolbase_mount`` (optional) serves the agent's toolbase
        toolkits. Its servers start at the first step, not here, so a
        fan-out spawn doesn't start every worker's toolkits serially
        inside the spawn call; a live grant swaps the tool list at the
        next step, keeping the conversation.

        ``stream_emit`` (optional) — when provided, ``step`` streams
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
        self._llm = llm
        # Retries of a dropped or throttled LLM call (``llm_resilience``)
        # show in the chat, and stop once the agent is terminated.
        try:
            llm.conjure_on_retry = self._on_llm_retry
        except Exception:
            pass
        self._base_tools = list(tools)
        self._display_hook = display_hook
        self._toolbase = toolbase_mount
        prompt = system_prompt or self._build_system_prompt(record, runtime)
        self.system_prompt_text = prompt  # provenance
        self._agent = OrchestralAgent(
            llm=llm,
            tools=tools,
            system_prompt=prompt,
            max_tool_interations=max_tool_iterations,
            tool_config=None,
            display_hook=display_hook,
            debug=False,
        )

    def _sync_toolkits(self) -> None:
        if self._toolbase is None:
            return
        served = self._toolbase.sync()
        if served is None:
            return
        # Same LLM, same context (so the conversation carries over);
        # only the tool list changes.
        self._agent = OrchestralAgent(
            llm=self._llm,
            context=self._agent.context,
            tools=self._base_tools + served,
            max_tool_interations=self._max_tool_iterations,
            tool_config=None,
            display_hook=self._display_hook,
            debug=False,
        )

    def close_toolkits(self) -> None:
        if self._toolbase is not None:
            self._toolbase.close()

    def _on_llm_retry(self, attempt: int, max_retries: int, delay_s: float, exc: BaseException) -> bool:
        if self._record.status == "terminated":
            return False
        if self._stream_emit is not None:
            try:
                # Close any partial reply the dropped stream showed; the
                # retry streams a fresh one.
                self._stream_emit({"kind": "stream_end", "tool_calls": []})
                self._stream_emit({
                    "kind": "retry",
                    "attempt": attempt,
                    "max": max_retries,
                    "delay_s": round(delay_s, 1),
                    "error": f"{type(exc).__name__}: {' '.join(str(exc).split())[:300]}",
                })
            except Exception:
                pass
        return True

    def shutdown(self) -> None:
        self.close_toolkits()

    def step(self, prompt: str) -> str:
        self._sync_toolkits()
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
        else:
            if self._extract_tool_calls(self._agent.context.messages[-1]):
                # The turn ran out of tool rounds with calls still pending:
                # say so rather than ending in silence.
                self._emit_event({
                    "kind": "notice",
                    "text": (
                        f"turn ended after {self._max_tool_iterations} tool rounds "
                        "(runtime.max_tool_iterations); its last tool calls didn't run"
                    ),
                })

        return accumulated

    @staticmethod
    def _extract_tool_calls(msg: Any) -> list[Any]:
        inner = getattr(msg, "message", None)
        if inner is not None:
            return list(getattr(inner, "tool_calls", None) or [])
        return list(getattr(msg, "tool_calls", None) or [])

    def _emit_event(self, event: dict[str, Any]) -> None:
        if self._stream_emit is None:
            return
        try:
            self._stream_emit(event)
        except Exception:
            pass

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
        from conjure.prompts import build_system_prompt

        return build_system_prompt(record, runtime, "orchestral")


def make_orchestral_engine_factory(
    *,
    llm_factories: dict[str, Callable[[], Any]] | None = None,
    llms: dict[str, Any] | None = None,
    tool_registry: ToolGroupRegistry | None = None,
    max_tool_iterations: int = 50,
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
            factory_fn = llm_factories[llm_name]
            # A spec's model (resolved from the catalog: a gateway model,
            # say) overrides the LLM entry's configured one.
            # Only with a model catalog: without one, ``spec.model`` may be
            # a Claude alias meant for another engine.
            override = record.spec.model if getattr(runtime, "models", None) is not None else None
            try:
                llm = factory_fn(model=override) if override else factory_fn()
            except TypeError:
                llm = factory_fn()
        else:
            if llm_name not in llms:
                raise KeyError(
                    f"LLM {llm_name!r} not configured (known: {sorted(llms)})"
                )
            llm = llms[llm_name]
        tool_names = list(record.spec.tools) if record.spec.tools else ["primitive"]
        hub = getattr(runtime, "toolbase", None)
        if hub is not None and "toolkit" in registry and "toolkit" not in tool_names:
            # Toolkit curation (catalog, grant, request) rides along
            # whenever the runtime has toolbase configured.
            tool_names.append("toolkit")
        if getattr(runtime, "models", None) is not None and "models" in registry and "models" not in tool_names:
            tool_names.append("models")
        tools = build_tools(record.token, tool_names, registry)
        mount = None
        if hub is not None:
            # Every agent gets a mount, even with no toolkits yet: a
            # later GrantToolkits lands through it. An empty mount
            # starts nothing.
            from conjure.toolbase import ToolbaseMount
            from conjure.tools.filesystem import _sandbox_for

            # Orchestral has no native skills: no skills_dir, and the
            # agent reads granted skills through ``LoadSkill``.
            mount = ToolbaseMount(
                hub, record, lambda: _sandbox_for(record, runtime)
            )
            mount.prestart()

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
            toolbase_mount=mount,
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
