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

import threading

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
        # orchestral checks this between tool calls and stream chunks;
        # ``interrupt()`` sets it so terminating the agent stops its turn.
        self._interrupt = threading.Event()
        self._agent.interrupt_flag = self._interrupt
        try:
            # The record's status flips to ``terminated`` before any
            # interrupt arrives, so this can't race the runtime's teardown.
            llm.conjure_should_stop = (
                lambda: self._interrupt.is_set() or self._record.status == "terminated"
            )
            llm.conjure_guard = loop_guard
        except Exception:
            pass

    def interrupt(self) -> None:
        """Stop the in-flight turn: no further tool calls or LLM rounds.
        (The runtime separately stops the agent's shell commands.)"""
        self._interrupt.set()

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
            tools=check_and_return(self._base_tools + served),
            max_tool_interations=self._max_tool_iterations,
            tool_config=None,
            display_hook=self._display_hook,
            debug=False,
        )
        self._agent.interrupt_flag = self._interrupt

    def close_toolkits(self) -> None:
        if self._toolbase is not None:
            self._toolbase.close()

    def _on_llm_retry(self, attempt: int, max_retries: int, delay_s: float, exc: BaseException) -> bool:
        if self._record.status == "terminated" or self._interrupt.is_set():
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
        from conjure.llm_resilience import LLMCallStopped

        self._sync_toolkits()
        self._turn_start_usage = self.usage()
        try:
            if self._stream_emit is None:
                response = self._agent.run(prompt)
                return response.text if response.text is not None else ""
            return self._step_streaming(prompt)
        except LLMCallStopped as stopped:
            if str(stopped):  # the loop guard: say why the turn ended
                self._emit_event({"kind": "notice", "text": str(stopped)})
            return ""

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
        3. If there are tool calls, run them and call the display hook,
           which emits ``tool`` events for the results; then stream the
           next LLM response. Repeat.
        4. Stop when an iteration produces no tool calls.
        """
        accumulated = ""
        # First iteration: includes adding the user message to context.
        for chunk in self._agent.stream_text_message(prompt):
            self._emit_chunk(chunk)
            accumulated += chunk
        self._emit_stream_end()
        self._emit_turn_usage(self._turn_start_usage)

        for _ in range(self._max_tool_iterations):
            if self._interrupt.is_set():
                break
            last = self._agent.context.messages[-1]
            tool_calls = self._extract_tool_calls(last)
            if not tool_calls:
                break
            self._agent._handle_tool_calls()  # noqa: SLF001
            # Log the round's results. Agent.run() calls the display hook
            # here; orchestral's streaming methods don't, so without this
            # call no tool result reached the event log.
            if self._display_hook is not None:
                self._display_hook(self._agent.context)
            # Stream the next response.
            for chunk in self._agent._stream_response():  # noqa: SLF001
                self._emit_chunk(chunk)
                accumulated += chunk
            self._emit_stream_end()
            self._emit_turn_usage(self._turn_start_usage)
        else:
            if not self._interrupt.is_set() and self._extract_tool_calls(self._agent.context.messages[-1]):
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

    def usage(self) -> dict[str, Any]:
        """Token counts and cost over this agent's responses.

        ``cost`` covers the priced responses only; ``unpriced_tokens``
        counts the rest (a gateway model with no known price), so a UI
        can say "unpriced" instead of a misleading $0."""
        book = getattr(self._llm, "conjure_pricebook", None)
        tokens_in = tokens_out = unpriced = 0
        cost = 0.0
        sources: set[str] = set()
        try:
            messages = list(self._agent.context.messages)
        except Exception:
            messages = []
        for msg in messages:
            usage = getattr(msg, "usage", None)
            if usage is None:
                continue
            toks = getattr(usage, "tokens", None) or {}
            n_in = int(toks.get("prompt_tokens") or toks.get("input_tokens") or 0)
            n_out = int(toks.get("completion_tokens") or toks.get("output_tokens") or 0)
            tokens_in += n_in
            tokens_out += n_out
            if book is not None:
                price = book.price(getattr(usage, "model_name", None))
                if price is None:
                    unpriced += n_in + n_out
                    continue
                cost += price.cost(toks)
                sources.add(price.source)
            else:
                try:
                    cost += float(usage.get_cost())
                except Exception:
                    pass
        return {
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost": round(cost, 6),
            "unpriced_tokens": unpriced,
            "price_sources": sorted(sources),
        }

    def cost(self) -> float:
        """Cumulative cost (USD) of this agent's priced responses."""
        try:
            return float(self.usage()["cost"])
        except Exception:
            return 0.0

    def _emit_turn_usage(self, before: dict[str, Any]) -> None:
        """A ``usage`` event with this turn's tokens so far (cumulative
        within the turn, like the other engines'), for the turn header,
        timeline and run record."""
        if self._stream_emit is None:
            return
        now = self.usage()
        self._emit_event({
            "kind": "usage",
            "tokens_in": now["tokens_in"] - before["tokens_in"],
            "tokens_out": now["tokens_out"] - before["tokens_out"],
        })

    @staticmethod
    def _build_system_prompt(record: "AgentRecord", runtime: "Runtime") -> str:
        from conjure.prompts import build_system_prompt

        return build_system_prompt(record, runtime, "orchestral")


# Loop guard: identical failing tool results in a row end the turn.
LOOP_UNKNOWN_TOOL = 3   # calls to a tool that doesn't exist
LOOP_SAME_ERROR = 4     # the same error from tool calls, back to back


def _role(msg: Any) -> str | None:
    inner = getattr(msg, "message", None)
    return getattr(msg, "role", None) or getattr(inner, "role", None)


def _is_error(text: str) -> bool:
    t = text.lstrip()
    return t.startswith("Error") or "'ok': False" in t or '"ok": false' in t


def loop_guard(context: Any) -> str | None:
    """Why this turn should stop instead of calling the model again, or
    None. Looks at the current turn's tool results (after the last user
    message): a model calling a tool that doesn't exist, or hitting the
    same error over and over, won't get unstuck by another round."""
    import re

    results: list[str] = []
    for msg in reversed(list(getattr(context, "messages", None) or [])):
        role = _role(msg)
        if role == "user":
            break
        if role == "tool":
            results.append(str(getattr(msg, "text", "") or ""))
    results.reverse()
    unknown = [r for r in results if "not found! Available tools" in r]
    if len(unknown) >= LOOP_UNKNOWN_TOOL:
        return (
            f"turn stopped: {len(unknown)} calls to tools that don't exist "
            f"(last: {unknown[-1][:160]})"
        )
    tail = results[-LOOP_SAME_ERROR:]
    if len(tail) == LOOP_SAME_ERROR and all(_is_error(r) for r in tail):
        if len({re.sub(r"\d+", "#", r)[:500] for r in tail}) == 1:
            return (
                f"turn stopped: the same tool error {LOOP_SAME_ERROR} times in a row "
                f"({tail[-1][:160]})"
            )
    return None


def check_and_return(tools: list[Any]) -> list[Any]:
    check_tool_names(tools)
    return tools


def check_tool_names(tools: list[Any]) -> None:
    """Refuse to start an agent whose tools would be offered to the model
    under one name and dispatched under another (0.2.1 shipped exactly
    that), or under a duplicate name."""
    offered = [t.get_tool_spec().name for t in tools]
    routed = [t.get_name() for t in tools]
    mismatched = [(o, r) for o, r in zip(offered, routed) if o != r]
    if mismatched:
        raise RuntimeError(f"tool names offered to the model don't match the dispatcher: {mismatched}")
    dupes = sorted({n for n in routed if routed.count(n) > 1})
    if dupes:
        raise RuntimeError(f"duplicate tool names: {dupes}")


# Tool groups an orchestral agent gets when its spec doesn't list any.
DEFAULT_TOOL_GROUPS_FOR_AGENTS = ("primitive", "combinator", "filesystem")


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
        # Like a coding harness, an agent gets the core tools (orchestration,
        # combinators, files and shell) unless its spec names its own set.
        tool_names = list(record.spec.tools) if record.spec.tools else list(DEFAULT_TOOL_GROUPS_FOR_AGENTS)
        hub = getattr(runtime, "toolbase", None)
        if hub is not None and "toolkit" in registry and "toolkit" not in tool_names:
            # Toolkit curation (catalog, grant, request) rides along
            # whenever the runtime has toolbase configured.
            tool_names.append("toolkit")
        if getattr(runtime, "models", None) is not None and "models" in registry and "models" not in tool_names:
            tool_names.append("models")
        tools = build_tools(record.token, tool_names, registry)
        check_tool_names(tools)
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
