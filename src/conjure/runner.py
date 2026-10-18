"""Compose a runtime from a ``Config``.

``build_runtime(config) -> (Runtime, root_addr)`` is the bridge between
declarative configuration (``conjure.config``) and the live runtime.

Engine dispatch: the per-agent ``spec.engine`` field selects which
engine builds that agent. ``orchestral`` (default) uses an LLM
client + spawn tool surface. ``claude_agent`` uses the
``claude-agent-sdk`` with Claude Code's tool surface, configured
against the agent's sandbox dir.
"""

from __future__ import annotations

from pathlib import Path

from typing import Callable, Any

from conjure.address import Address
from conjure.config import Config
from conjure.engines import resolve_engine_name
from conjure.engines.orchestral import make_orchestral_engine_factory
from conjure.llm import build_llm
from conjure.record import AgentRecord, AgentSpec
from conjure.runtime import Runtime
from conjure.toolbase import build_hub_from_config, parse_toolkits


def build_runtime(
    config: Config,
    *,
    session_id: str | None = None,
    display_hook_builder: Callable[[AgentRecord], Callable[[Any], None]] | None = None,
    event_log_router: Callable[[AgentRecord], Any] | None = None,
    spawn_listener: Callable[[AgentRecord], None] | None = None,
    stream: bool = False,
    control_socket: "Path | None" = None,
) -> tuple[Runtime, Address]:
    """Build a ``Runtime`` and spawn the root agent per ``config``.

    Each spawned agent gets its own freshly-constructed engine. Engine
    selection is per-agent via ``spec.engine``.

    ``display_hook_builder`` (REPL path) — returns a console-rendering
    display hook per agent.

    ``event_log_router`` (tmux path) — returns an EventLog per agent;
    when present, engines emit events to the log instead of rendering
    directly.

    ``spawn_listener`` — called synchronously after every spawn (root
    or child); used by the tmux orchestrator to set up per-agent state.
    """
    # Capture each LLM config as a default arg so the lambda closes
    # over the right value (avoid the classic late-binding bug).
    from conjure.llm_resilience import make_resilient

    # Every client of one ``llms`` entry shares its concurrency cap, so
    # all agents on a gateway together stay under its limit.
    llm_factories = {
        name: (
            lambda model=None, c=cfg.model_dump(), n=name: _priced(
                make_resilient(
                    build_llm({**c, "model": model} if model else c),
                    key=n,
                    max_concurrency=c.get("max_concurrency"),
                    max_retries=int(c.get("max_retries") or 0),
                ),
                c,
            )
        )
        for name, cfg in config.llms.items()
    }

    orchestral_factory = make_orchestral_engine_factory(
        llm_factories=llm_factories,
        max_tool_iterations=config.runtime.max_tool_iterations,
        display_hook_builder=display_hook_builder,
        event_log_router=event_log_router,
        stream=stream,
    )

    def dispatch(record: AgentRecord, runtime: Runtime):
        engine_name = _engine_for(record, runtime)
        if engine_name == "codex":
            return _build_codex_engine(
                record=record,
                runtime=runtime,
                event_log_router=event_log_router,
                codex_config=config.codex,
            )
        if engine_name == "orchestral":
            return orchestral_factory(record, runtime)
        if engine_name == "claude_agent":
            return _build_claude_agent_engine(
                record=record,
                runtime=runtime,
                event_log_router=event_log_router,
                control_socket=control_socket,
            )
        raise ValueError(
            f"unknown engine {engine_name!r} on agent {record.addr.id}; "
            f"supported: 'orchestral', 'claude_agent', 'codex', 'auto'"
        )

    def validate_spec(spec, parent=None):
        # Spawn-time check, before the child is recorded or journaled: route
        # to the engine this spec will run on (a stand-in record is enough
        # for effective_engine). Only the orchestral engine reads ``tools``
        # as tool groups; the others interpret it themselves.
        from types import SimpleNamespace

        stub = SimpleNamespace(spec=spec, parent=parent)
        if _engine_for(stub, runtime) == "orchestral":
            orchestral_factory.validate_spec(spec, parent)

    dispatch.validate_spec = validate_spec  # type: ignore[attr-defined]

    store_dir = Path(config.runtime.store_dir) if config.runtime.store_dir else None
    hub = build_hub_from_config(config.toolbase)
    from conjure.models import build_model_hub

    runtime = Runtime(
        store_dir=store_dir,
        session_id=session_id,
        engine_factory=dispatch,
        max_workers=config.runtime.max_workers,
        max_depth=config.runtime.max_depth,
        spawn_listener=spawn_listener,
        toolbase=hub,
        models=build_model_hub(config),
        deadline=_deadline(config),
        max_wait_s=config.runtime.max_wait_s,
        tool_output_max_chars=config.runtime.tool_output_max_chars,
    )
    runtime.auto_mode = bool(config.runtime.auto_approve)
    # Every configured provider's key variable, kept out of agents' shells
    # (``conjure.tools.filesystem._agent_env``) whatever its name.
    runtime.secret_env_names = _secret_env_names(config)

    root_spec = AgentSpec(
        role_prompt=config.root.role_prompt,
        engine=config.root.engine,
        llm=config.root.llm,
        tools=config.root.tools,
        label=config.root.label,
        sandbox_dir=config.root.sandbox_dir,
        permissions=config.root.permissions,
        model=config.root.model,
        toolkits=parse_toolkits(config.root.toolkits) or None,
        toolbase_loadout=config.root.toolbase_loadout,
    )
    root_addr = runtime.root(root_spec)
    return runtime, root_addr


def _secret_env_names(config: Config) -> set[str]:
    from conjure.llm import key_env_for

    names = set()
    for cfg in config.llms.values():
        try:
            name = cfg.api_key_env or key_env_for(cfg.provider)
        except Exception:
            name = cfg.api_key_env
        if name:
            names.add(name)
    return names


def _deadline(config: Config) -> float | None:
    """The run's absolute deadline: ``runtime.deadline``, or
    ``runtime.wall_clock_s`` from now, whichever is earlier."""
    import time

    candidates = []
    if config.runtime.deadline is not None:
        candidates.append(float(config.runtime.deadline))
    if config.runtime.wall_clock_s is not None:
        candidates.append(time.time() + float(config.runtime.wall_clock_s))
    return min(candidates) if candidates else None


def _priced(llm: Any, cfg: dict[str, Any]) -> Any:
    """Attach the entry's prices (``conjure.pricing``). An
    ``openai_compatible`` client needs them: orchestral prices its calls
    at $0. Native providers keep orchestral's prices unless the entry
    sets ``pricing``."""
    from conjure.pricing import PriceBook

    gateway = str(cfg.get("provider", "")).lower() == "openai_compatible"
    if gateway or cfg.get("pricing"):
        try:
            llm.conjure_pricebook = PriceBook(cfg.get("pricing"), use_list_prices=gateway)
        except Exception:
            pass
    return llm


def _engine_for(record: AgentRecord, runtime: Runtime) -> str:
    from conjure.engines import effective_engine

    return effective_engine(record, runtime)


def _build_codex_engine(
    *,
    record: AgentRecord,
    runtime: Runtime,
    event_log_router: Callable[[AgentRecord], Any] | None,
    codex_config: Any,
):
    """A ``CodexEngine`` for ``record``: its sandbox is Codex's working
    root, and its ``CODEX_HOME`` (auth link, sessions, surfaced skills)
    lives beside the sandboxes in the store."""
    import tempfile

    from conjure.engines.codex import CodexEngine
    from conjure.tools.filesystem import _sandbox_for

    stream_emit = None
    if event_log_router is not None:
        event_log = event_log_router(record)
        if event_log is not None:
            stream_emit = event_log.emit
    sandbox = _sandbox_for(record, runtime)
    if sandbox is None:
        sandbox = Path(tempfile.mkdtemp(prefix=f"conjure-{record.addr.id}-"))
    if runtime.store_dir is not None:
        home = Path(runtime.store_dir).expanduser() / "codex_homes" / record.addr.id
    else:
        home = sandbox.parent / f".codex_home-{record.addr.id}"
    model = record.spec.model or (codex_config.child_model if record.parent is not None else None)
    mount = None
    if runtime.toolbase is not None:
        from conjure.toolbase import ToolbaseMount

        mount = ToolbaseMount(runtime.toolbase, record, sandbox, skills_dir=home / "skills")
        mount.prestart()
    return CodexEngine(
        record=record,
        runtime=runtime,
        sandbox_dir=sandbox,
        codex_home=home,
        stream_emit=stream_emit,
        model=model,
        sandbox_mode=codex_config.sandbox,
        reasoning_effort=codex_config.reasoning_effort,
        toolbase_mount=mount,
        codex_bin=codex_config.bin,
    )


def _build_claude_agent_engine(
    *,
    record: AgentRecord,
    runtime: Runtime,
    event_log_router: Callable[[AgentRecord], Any] | None,
    control_socket: "Path | None",
):
    """Construct a ``ClaudeAgentEngine`` for this record. Resolves
    the agent's sandbox (same logic the FS tools use) and routes
    streaming chunks to the agent's event log if one is configured.
    Also passes the daemon's control socket so the engine can
    register the conjure-mcp bridge — giving the SDK access to
    spawn/send/recv/agent_map/... via MCP."""
    from conjure.engines.claude_agent import ClaudeAgentEngine
    from conjure.tools.filesystem import _sandbox_for

    stream_emit = None
    if event_log_router is not None:
        event_log = event_log_router(record)
        if event_log is not None:
            stream_emit = event_log.emit
    sandbox = _sandbox_for(record, runtime)
    model = _resolve_model(record)
    mount = None
    if runtime.toolbase is not None:
        from conjure.toolbase import ToolbaseMount

        # Claude Code reads skills from ``<cwd>/.claude/skills``; the
        # cwd is the sandbox.
        mount = ToolbaseMount(
            runtime.toolbase,
            record,
            sandbox,
            skills_dir=(sandbox / ".claude" / "skills") if sandbox is not None else None,
        )
        mount.prestart()
    return ClaudeAgentEngine(
        record=record,
        runtime=runtime,
        sandbox_dir=sandbox,
        allowed_tools=record.spec.tools,
        stream_emit=stream_emit,
        mcp_socket=control_socket,
        model=model,
        toolbase_mount=mount,
    )


def _resolve_model(record: AgentRecord) -> str | None:
    """Pick the model for this agent's claude_agent session.

    Resolution order:
    1. ``spec.model`` if the caller (user config or ``Spawn`` tool
       call) set it explicitly.
    2. ``"haiku"`` for any non-root agent — children default to a
       cheap model so the substrate doesn't burn Opus on every
       helper. Override with an explicit ``model=`` on ``Spawn``
       when the child genuinely needs more capability.
    3. ``None`` for the root, letting the ``claude`` CLI use its
       configured default (typically what the user is paying for).
    """
    if record.spec.model:
        return record.spec.model
    if record.parent is not None:
        return "haiku"
    return None
