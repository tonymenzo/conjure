"""``combinator`` CLI entry point.

Subcommands:

- ``combinator run <config>``   — tmux-native mode: each agent gets a
                                  tmux window, the user attaches to the
                                  session. The runtime keeps running
                                  inside the parent process; detaching
                                  shuts the runtime down cleanly.
- ``combinator run --attach``   — attach to the most-recent
                                  ``combinator-*`` tmux session.
- ``combinator run --attach <name>`` — attach to a specific session.
- ``combinator repl <config>``  — single-pane REPL mode (no tmux);
                                  useful for scripting and tests.
- ``combinator check <config>`` — validate config + report key
                                  availability.
- ``combinator config list|set|unset`` — manage the user .env.

REPL control commands (``combinator repl`` only):

- ``:tree``, ``:status``, ``:cost``, ``:inbox [addr]``,
  ``:send <addr> <body>``, ``:help``, ``:quit``.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from rich.rule import Rule

from combinator import _ui
from combinator.address import USER, Address
from combinator.config import load_config
from combinator.control import ControlServer
from combinator.daemon import (
    daemonize,
    is_daemon_running,
    list_session_names,
    log_path_for,
    pid_path_for,
    socket_path_for,
    stop_daemon,
)
from combinator.event_log import EventLog
from combinator.events import make_system_prompt_event
from combinator.profiling import profile_session
from combinator.record import AgentRecord
from combinator.runner import build_runtime
from combinator.runtime import Runtime
from combinator.tmux_session import TmuxSession, tmux_available


_AGENT_RESPONSE_TIMEOUT_S = 180.0


# ----- CLI entry point -------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="combinator")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser(
        "run",
        help="Tmux-native session: one window per agent; daemonized.",
    )
    run_p.add_argument("config", type=Path, nargs="?", help="Path to a YAML config file.")
    run_p.add_argument(
        "--attach",
        nargs="?",
        const="__most_recent__",
        default=None,
        metavar="SESSION",
        help="Attach to an existing combinator session (newest if name omitted).",
    )

    quit_p = sub.add_parser(
        "quit",
        help="Stop a running combinator daemon (newest if name omitted).",
    )
    quit_p.add_argument(
        "session",
        nargs="?",
        default=None,
        help="Session name (default: newest combinator-* daemon).",
    )

    repl_p = sub.add_parser(
        "repl",
        help="Single-pane REPL mode (no tmux).",
    )
    repl_p.add_argument("config", type=Path, help="Path to a YAML config file.")

    check_p = sub.add_parser(
        "check", help="Validate a config without spawning agents or calling LLMs."
    )
    check_p.add_argument("config", type=Path, help="Path to a YAML config file.")

    config_p = sub.add_parser(
        "config", help="Manage the user-global .env file used for API keys."
    )
    config_sub = config_p.add_subparsers(dest="subcmd", required=True)
    config_sub.add_parser("list", help="List values from the user .env (redacted).")
    set_p = config_sub.add_parser("set", help="Set a value in the user .env.")
    set_p.add_argument("key")
    set_p.add_argument("value")
    unset_p = config_sub.add_parser("unset", help="Remove a value from the user .env.")
    unset_p.add_argument("key")

    args = parser.parse_args(argv)

    if args.cmd == "run":
        if args.attach is not None:
            return _cmd_run_attach(args.attach)
        if args.config is None:
            parser.error("config path required (or pass --attach)")
        return _cmd_run_tmux(args.config)
    if args.cmd == "quit":
        return _cmd_quit(args.session)
    if args.cmd == "repl":
        return _cmd_repl(args.config)
    if args.cmd == "check":
        return _cmd_check(args.config)
    if args.cmd == "config":
        return _cmd_config(args)
    parser.print_help()
    return 2


# ----- combinator run (tmux orchestrator) ------------------------------------

def _cmd_run_tmux(config_path: Path) -> int:
    """Start a daemonized combinator runtime, set up the tmux session
    with one window per agent, then attach the user's terminal.

    Lifecycle:

    - Parent (the original ``combinator run`` invocation) picks a
      session name, daemonizes off a child, waits for the daemon to
      create the tmux session, and ``execvp`` to ``tmux attach``.
    - Daemon (the detached child) owns the runtime + tmux session. It
      blocks on SIGTERM and tears everything down cleanly on exit.

    Detaching with ``Ctrl+B d`` exits the parent (which is now the
    tmux client) but leaves the daemon running. Re-attach with
    ``combinator run --attach``. Stop the daemon with ``combinator quit``.
    """
    from combinator.env import load_env_files

    console = _ui.make_console()
    if not tmux_available():
        _ui.print_error(
            console,
            "tmux not found on PATH. Install tmux >=3.4 or use `combinator repl`.",
        )
        return 2

    load_env_files()
    try:
        cfg = load_config(config_path)
    except Exception as exc:
        _ui.print_error(console, f"config invalid: {exc}")
        return 2

    session_name = f"combinator-{uuid.uuid4().hex[:6]}"
    pid_path = pid_path_for(session_name)
    log_path = log_path_for(session_name)

    daemon_pid = daemonize(log_path=log_path, pid_path=pid_path)
    if daemon_pid > 0:
        # Parent — wait for daemon to bring up the tmux session, then
        # attach. If the daemon dies before then, surface the error
        # from the daemon log.
        if not _wait_for_tmux_session(session_name, timeout_s=10.0):
            _ui.print_error(
                console,
                f"daemon did not create tmux session within 10s "
                f"(check {log_path})",
            )
            return 1
        _ui.print_system(
            console,
            f"session [cyan]{session_name}[/] up; attaching "
            f"([dim]Ctrl+B d to detach[/], "
            f"[dim]`combinator quit` to stop the daemon[/])",
        )
        time.sleep(0.1)
        os.execvp("tmux", ["tmux", "attach-session", "-t", session_name])
        return 1  # only reached if exec fails

    # Daemon path — _run_daemon never returns (blocks until SIGTERM).
    return _run_daemon(cfg=cfg, session_name=session_name, pid_path=pid_path)


def _run_daemon(*, cfg, session_name: str, pid_path: Path) -> int:
    """Daemon body: build the runtime, set up tmux, block on signal.

    Tmux layout is a single window (``combinator-main``) hosting the
    sidebar + swappable chat pane. Per-agent dedicated chat windows
    are created on-demand by the main window (``o`` keypress) rather
    than auto-created on spawn — that keeps the window list focused
    on agents the user has actually drilled into.

    When ``COMBINATOR_PROFILE`` is set the whole session is wrapped in
    cProfile and the stats are written out on shutdown — see
    ``combinator.profiling`` for the env var contract.
    """
    with profile_session(f"daemon-{session_name}"):
        return _run_daemon_inner(cfg=cfg, session_name=session_name, pid_path=pid_path)


def _run_daemon_inner(*, cfg, session_name: str, pid_path: Path) -> int:
    shutdown_event = threading.Event()

    def _signal_handler(_signum, _frame):
        shutdown_event.set()

    # Signal handlers must be installed on the main thread before any
    # driver threads start (which build_runtime triggers).
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # Per-session scoping: every daemon invocation gets its own
    # ``store_dir/sessions/{session_name}/`` so two runs from the same
    # CWD never concatenate into one ambiguous journal. The tmux
    # session_name doubles as the on-disk session id so a user looking
    # at the tmux pane and a user looking at disk see the same name.
    store_dir = Path(cfg.runtime.store_dir or "./.combinator/store")
    session_dir = store_dir / "sessions" / session_name
    agents_dir = session_dir / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    # Resolve absolute path to combinator-main so tmux's shell finds it
    # regardless of the user's login-shell PATH (conda envs are rarely
    # on the login PATH).
    import shutil as _shutil
    main_bin = _shutil.which("combinator-main") or "combinator-main"

    def _main_command() -> str:
        parts = [main_bin, "--session", session_name]
        return " ".join(shlex.quote(p) for p in parts)

    def spawn_listener(record: AgentRecord) -> None:
        """Attach an event log to every spawned agent so the main
        window can tail it. We deliberately do NOT create a tmux
        window per agent — the main window's sidebar + swappable
        chat pane is the primary interface; the ``o`` keypress in
        the main window creates a dedicated window on-demand.

        For *child* agents, the first event written is their
        ``system_prompt`` — the role prompt they were spawned with —
        so the chat pane opens with that initialization context as
        the first visible block. The root agent is skipped: its
        role prompt is the user's own configuration, not something
        the user needs to see echoed back inside its own chat."""
        log_path = agents_dir / f"{record.addr.id}.jsonl"
        record.event_log = EventLog(log_path)
        if record.parent is not None:
            record.event_log.emit(
                make_system_prompt_event(
                    text=record.spec.role_prompt or "",
                    label=record.spec.label or record.addr.label,
                )
            )

    def event_log_router(record: AgentRecord) -> EventLog | None:
        return record.event_log

    control_path = socket_path_for(session_name)
    try:
        runtime, root = build_runtime(
            cfg,
            session_id=session_name,
            event_log_router=event_log_router,
            spawn_listener=spawn_listener,
            stream=True,  # tmux mode: agent text streams into chat panes
            control_socket=control_path,
        )
    except Exception as exc:
        print(f"daemon: failed to build runtime: {exc}", file=sys.stderr)
        _remove_pid(pid_path)
        return 2

    # Single tmux window (combinator-main) hosts everything. Dedicated
    # per-agent windows are spawned on-demand by the main window.
    try:
        tmux = TmuxSession.attach_or_create(
            session_name,
            initial_window_name="main",
            initial_command=_main_command(),
        )
    except Exception as exc:
        print(f"daemon: failed to create tmux session: {exc}", file=sys.stderr)
        _remove_pid(pid_path)
        return 2

    # ``remain-on-exit on`` keeps windows around after their command
    # exits so a crashing window surfaces its traceback instead of
    # vanishing. Close with ``Ctrl+B &`` once diagnosed.
    subprocess.run(
        ["tmux", "set-option", "-t", session_name, "remain-on-exit", "on"],
        check=False,
        timeout=3,
    )

    # Start the JSON-RPC control server so chat windows can send
    # messages and the meta-view popup can query state. ``control_path``
    # was computed earlier so claude_agent engines could embed it in
    # their MCP subprocess env.
    control_server = ControlServer(runtime=runtime, socket_path=control_path)
    try:
        control_server.start()
    except Exception as exc:
        print(f"daemon: control server failed to start: {exc}", file=sys.stderr)

    # Bind ``Ctrl+B F`` to the files popup. tmux key bindings are
    # server-global; the most recent daemon wins if there are
    # multiple, which is fine — the popup auto-discovers the live
    # socket if asked. The old ``M`` / ``I`` bindings (meta + inbox
    # popups) were retired once the main window's sidebar grew to
    # cover the same ground.
    _bind_files_popup(session_name)

    if cfg.mode == "one-shot" and cfg.initial_task:
        runtime.send_external(to=root, body=cfg.initial_task)

    # Block until SIGTERM / SIGINT.
    shutdown_event.wait()

    try:
        control_server.stop()
    except Exception:
        pass
    _shutdown(runtime, tmux)
    _remove_pid(pid_path)
    return 0


def _start_input_reader(
    *,
    input_path: Path,
    runtime: Runtime,
    root: Address,
    shutdown_event: threading.Event,
) -> None:
    """Tail ``input_path`` and dispatch each line.

    - ``:quit`` / ``:q`` / ``:exit`` → set ``shutdown_event``.
    - Anything else → ``runtime.send_external(to=root, body=line)``.
    Future control commands (``:tree`` etc.) will be added when the
    meta-view popup needs them; for now they're forwarded as plain
    text (they won't match anything meaningful agent-side, which is
    fine — the meta-view is the right place to put them).
    """

    def reader() -> None:
        # Read from start: the daemon's setup truncated this file (and
        # tests give us a fresh tmp_path) so there's nothing stale to
        # replay. Reading from start avoids a seek/append race that
        # would skip messages appended just before the thread starts.
        with input_path.open("r", encoding="utf-8") as fh:
            while not shutdown_event.is_set():
                raw = fh.readline()
                if not raw:
                    time.sleep(0.05)
                    continue
                try:
                    data = json.loads(raw.strip())
                except Exception:
                    continue
                line = (data.get("line") or "").strip()
                if not line:
                    continue
                if line in (":quit", ":q", ":exit"):
                    shutdown_event.set()
                    return
                try:
                    runtime.send_external(to=root, body=line)
                except Exception:
                    # Don't kill the reader on a single bad dispatch.
                    pass

    threading.Thread(target=reader, daemon=True, name="input-reader").start()


def _shutdown(runtime: Runtime, tmux: TmuxSession) -> None:
    """Tear down the runtime + tmux session cleanly."""
    try:
        for _addr, record in list(runtime._records.items()):  # noqa: SLF001
            log = getattr(record, "event_log", None)
            if log is not None:
                try:
                    log.close()
                except Exception:
                    pass
    except Exception:
        pass
    try:
        runtime.shutdown()
    except Exception:
        pass
    try:
        tmux.kill()
    except Exception:
        pass


def _remove_pid(pid_path: Path) -> None:
    try:
        pid_path.unlink()
    except OSError:
        pass


def _wait_for_tmux_session(session_name: str, *, timeout_s: float) -> bool:
    """Poll until ``session_name`` exists in tmux, or timeout."""
    import libtmux

    deadline = time.monotonic() + timeout_s
    server = libtmux.Server()
    while time.monotonic() < deadline:
        try:
            if server.has_session(session_name):
                return True
        except Exception:
            pass
        time.sleep(0.05)
    return False


def _bind_files_popup(session_name: str) -> None:
    """Bind ``prefix + F`` to a popup running the file browser
    against this daemon's sandboxes."""
    import shutil
    import subprocess

    files_path = shutil.which("combinator-files") or "combinator-files"
    popup_cmd = f"{files_path} --session {session_name}"
    try:
        subprocess.run(
            [
                "tmux",
                "bind-key",
                "F",
                "display-popup",
                "-E",
                "-w", "85%",
                "-h", "85%",
                "-T", f" files › {session_name} ",
                popup_cmd,
            ],
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _cmd_run_attach(session: str) -> int:
    """Attach to an existing combinator tmux session."""
    if not tmux_available():
        print("tmux not found on PATH.", file=sys.stderr)
        return 2
    if session == "__most_recent__":
        target = _find_most_recent_combinator_session()
        if target is None:
            print("no combinator-* tmux session found", file=sys.stderr)
            return 1
        session = target
    os.execvp("tmux", ["tmux", "attach-session", "-t", session])
    return 1  # only reached if exec fails


def _find_most_recent_combinator_session() -> str | None:
    """Return the name of the newest live combinator daemon session."""
    sessions = list_session_names()
    if sessions:
        # PID file mtime as proxy for "newest" — most recently spawned daemon.
        sessions.sort(key=lambda n: pid_path_for(n).stat().st_mtime, reverse=True)
        return sessions[0]
    # Fall back to tmux session listing for the case where the daemon
    # isn't tracked (older sessions or manual setups).
    import libtmux

    server = libtmux.Server()
    matching = [
        s for s in server.sessions
        if (s.session_name or "").startswith("combinator-")
    ]
    if not matching:
        return None
    matching.sort(
        key=lambda s: int(s.get("session_created") or 0),
        reverse=True,
    )
    return matching[0].session_name


def _cmd_quit(session: str | None) -> int:
    console = _ui.make_console()
    if session is None:
        session = _find_most_recent_combinator_session()
        if session is None:
            _ui.print_error(console, "no combinator daemon found")
            return 1
    pid_path = pid_path_for(session)
    if not pid_path.exists():
        _ui.print_error(console, f"no PID file for session {session}")
        return 1
    if stop_daemon(pid_path, timeout_s=10.0):
        _ui.print_system(console, f"stopped [cyan]{session}[/]")
        return 0
    _ui.print_error(console, f"daemon for {session} did not exit in time")
    return 1


# ----- combinator repl (single-pane REPL) ------------------------------------

def _cmd_repl(config_path: Path) -> int:
    from combinator.env import load_env_files

    console = _ui.make_console()
    load_env_files()
    try:
        cfg = load_config(config_path)
    except Exception as exc:
        _ui.print_error(console, f"config invalid: {exc}")
        return 2

    hook_builder = _ui.make_display_hook_builder(console)
    try:
        runtime, root = build_runtime(cfg, display_hook_builder=hook_builder)
    except Exception as exc:
        _ui.print_error(console, f"failed to build runtime: {exc}")
        return 2

    _ui.print_banner(
        console,
        label=root.label or "root",
        addr_id=root.id,
        engine=cfg.root.engine,
        llm=cfg.root.llm,
    )

    if cfg.mode == "one-shot":
        return _run_one_shot(console, runtime, root, cfg.initial_task or "")
    return _run_repl(console, runtime, root)


def _run_one_shot(console, runtime: Runtime, root: Address, task: str) -> int:
    if not task:
        _ui.print_error(console, "mode is one-shot but no initial_task is set")
        runtime.shutdown()
        return 2
    runtime.send_external(to=root, body=task)
    target_seq = runtime.record_for(root).inbox.latest_seq()
    _wait_for_idle(console, runtime, root, target_seq, label=root.label or "root")
    _ui.print_system(console, "root went idle; shutting down")
    runtime.shutdown()
    return 0


def _run_repl(console, runtime: Runtime, root: Address) -> int:
    label = root.label or "root"
    user_cursor = 0
    first = True
    try:
        while True:
            if not first:
                console.print(Rule(style="dim"))
            first = False
            try:
                line = console.input("[bold cyan]you[/] [dim]›[/] ").strip()
            except EOFError:
                console.print()
                break
            if not line:
                continue
            if line.startswith(":"):
                if _handle_command(line, console=console, runtime=runtime, root=root):
                    break
                continue
            runtime.send_external(to=root, body=line)
            target_seq = runtime.record_for(root).inbox.latest_seq()
            _wait_for_idle(console, runtime, root, target_seq, label=label)
            user_cursor = _flush_user_inbox(console, runtime, since_seq=user_cursor)
    except KeyboardInterrupt:
        console.print()
        _ui.print_system(console, "interrupt — shutting down")
    finally:
        runtime.shutdown()
    return 0


def _flush_user_inbox(console, runtime: Runtime, *, since_seq: int) -> int:
    envelopes = runtime.read_inbox(USER, since_seq=since_seq)
    if not envelopes:
        return since_seq
    for env in envelopes:
        sender = env.from_.label or env.from_.id
        body = env.body
        if isinstance(body, str):
            preview = body
        else:
            try:
                preview = json.dumps(body, ensure_ascii=False, default=str)
            except Exception:
                preview = repr(body)
        if len(preview) > 1000:
            preview = preview[:997] + "…"
        console.print(
            f"  [bold magenta]{sender}[/] [dim]→ you[/]  {preview}"
        )
    return envelopes[-1].seq


def _wait_for_idle(
    console, runtime: Runtime, addr: Address, target_seq: int, *, label: str
) -> None:
    with console.status(f"[dim]{label} is thinking…[/]", spinner="dots"):
        ok = runtime.wait_for_idle(addr, target_seq, timeout_s=_AGENT_RESPONSE_TIMEOUT_S)
    if not ok:
        _ui.print_error(console, f"{label} did not return within {int(_AGENT_RESPONSE_TIMEOUT_S)}s")


def _handle_command(line: str, *, console, runtime: Runtime, root: Address) -> bool:
    parts = line.split(maxsplit=2)
    cmd = parts[0]
    if cmd in (":q", ":quit", ":exit"):
        _ui.print_system(console, "quit requested")
        return True
    if cmd == ":help":
        _print_help(console)
        return False
    if cmd == ":tree":
        _print_tree(runtime, root, console=console)
        return False
    if cmd == ":status":
        _print_status(runtime, console=console)
        return False
    if cmd == ":cost":
        _print_cost(runtime, console=console)
        return False
    if cmd == ":inbox":
        target = parts[1] if len(parts) >= 2 else "@user"
        _print_inbox(runtime, target, console=console)
        return False
    if cmd == ":send":
        if len(parts) < 3:
            console.print("[dim]usage:[/] :send <addr_id> <body>")
            return False
        _send(runtime, parts[1], parts[2], console=console)
        return False
    _ui.print_error(console, f"unknown command: {cmd}")
    return False


def _print_help(console) -> None:
    console.print(
        "[bold]commands[/bold]:\n"
        "  [cyan]:help[/]                 show this help\n"
        "  [cyan]:tree[/]                 show the spawn tree\n"
        "  [cyan]:status[/]               show each agent's status\n"
        "  [cyan]:cost[/]                 show LLM spend (per agent + total)\n"
        "  [cyan]:inbox [addr][/]         list envelopes (default: @user — your inbox)\n"
        "  [cyan]:send <addr> <body>[/]   send a message to any known agent\n"
        "  [cyan]:quit[/]                 terminate and exit\n\n"
        "[dim]Anything else is sent as a user message to the root.[/]",
    )


def _print_cost(runtime: Runtime, *, console) -> None:
    rows = runtime.costs_by_agent()
    if not rows:
        console.print("[dim](no agents)[/]")
        return
    total = 0.0
    for addr, cost in rows:
        total += cost
        rec = runtime.record_for(addr)
        label = addr.label or "—"
        cost_str = _format_cost(cost)
        console.print(
            f"  [bold magenta]{label}[/] [dim]({addr.id})[/] "
            f"[{_status_color(rec.status)}]{rec.status}[/]  {cost_str}"
        )
    console.print(
        f"[dim]──────────────[/]\n"
        f"[bold]total[/]  {_format_cost(total)}"
    )


def _format_cost(usd: float) -> str:
    if usd <= 0:
        return "[dim]$0.0000[/]"
    if usd < 0.01:
        return f"[cyan]${usd:.6f}[/]"
    return f"[cyan]${usd:.4f}[/]"


def _status_color(status: str) -> str:
    return {
        "lazy": "yellow",
        "running": "green",
        "idle": "white",
        "terminated": "red",
    }.get(status, "white")


def _print_tree(runtime: Runtime, root: Address, *, console) -> None:
    _walk(runtime, root, prefix="", console=console)


def _walk(runtime: Runtime, addr: Address, *, prefix: str, console) -> None:
    rec = runtime.record_for(addr)
    label = addr.label or "—"
    console.print(
        f"{prefix}[bold magenta]{label}[/] [dim]({addr.id})[/] "
        f"[{_status_color(rec.status)}]{rec.status}[/]"
    )
    children = sorted(rec.children, key=lambda a: a.id)
    for i, child in enumerate(children):
        is_last = i == len(children) - 1
        branch = "└── " if is_last else "├── "
        _walk(runtime, child, prefix=prefix + branch, console=console)


def _print_status(runtime: Runtime, *, console) -> None:
    with runtime._lock:  # noqa: SLF001
        records = list(runtime._records.values())  # noqa: SLF001
    for r in records:
        console.print(
            f"  [bold magenta]{r.addr.label or '-'}[/] [dim]({r.addr.id})[/]: "
            f"{r.status} caps={len(r.capabilities)} inbox={len(r.inbox)}",
        )


def _print_inbox(runtime: Runtime, addr_id: str, *, console) -> None:
    addr = runtime.address_by_id(addr_id)
    if addr is None:
        _ui.print_error(console, f"no agent with id {addr_id}")
        return
    envelopes = runtime.read_inbox(addr)
    if not envelopes:
        console.print("[dim](inbox empty)[/]")
        return
    for env in envelopes:
        console.print(
            f"  seq=[cyan]{env.seq}[/] from=[magenta]{env.from_}[/] "
            f"body={_body_preview(env.body)}",
        )


def _send(runtime: Runtime, addr_id: str, body: str, *, console) -> None:
    addr = runtime.address_by_id(addr_id)
    if addr is None:
        _ui.print_error(console, f"no agent with id {addr_id}")
        return
    try:
        parsed = json.loads(body)
        runtime.send_external(to=addr, body=parsed)
    except json.JSONDecodeError:
        runtime.send_external(to=addr, body=body)
    _ui.print_system(console, f"sent to {addr.id}")


def _body_preview(body) -> str:
    s = json.dumps(body, default=str) if not isinstance(body, str) else body
    return s if len(s) <= 200 else s[:197] + "..."


# ----- combinator check ------------------------------------------------------

def _cmd_check(config_path: Path) -> int:
    from combinator.env import load_env_files
    from combinator.llm import api_key_present, key_env_for

    console = _ui.make_console()
    load_env_files()
    try:
        cfg = load_config(config_path)
    except Exception as e:
        _ui.print_error(console, f"config invalid: {e}")
        return 2

    console.print(f"[dim][combinator][/dim] config: [cyan]{config_path}[/]")
    console.print(f"  mode:        {cfg.mode}")
    console.print(f"  root.engine: {cfg.root.engine}")
    console.print(f"  root.llm:    {cfg.root.llm}")
    console.print(f"  root.tools:  {cfg.root.tools}")
    console.print()
    console.print("[dim][combinator][/dim] LLMs:")
    missing = []
    for name, llm_cfg in cfg.llms.items():
        env_var = llm_cfg.api_key_env or key_env_for(llm_cfg.provider)
        present = api_key_present(llm_cfg.provider) or not env_var
        status_tag = "[green]ok[/]" if present else "[red]MISSING[/]"
        env_display = env_var or "(no key needed)"
        console.print(
            f"  [bold magenta]{name}[/]: provider={llm_cfg.provider} "
            f"env={env_display} {status_tag}"
        )
        if not present:
            missing.append(env_var)
    if missing:
        console.print()
        _ui.print_error(
            console,
            f"{len(missing)} required env var(s) missing: {', '.join(missing)}",
        )
        console.print(
            "  [dim]Set them in your shell, in ~/.config/combinator/.env via "
            "`combinator config set`, or in a project-local ./.env[/]"
        )
        return 1
    return 0


# ----- combinator config -----------------------------------------------------

def _cmd_config(args) -> int:
    from combinator.env import (
        USER_ENV_PATH,
        list_user_env,
        redact,
        set_user_env,
        unset_user_env,
    )

    console = _ui.make_console()
    if args.subcmd == "list":
        try:
            values = list_user_env()
        except ImportError:
            _ui.print_error(console, "python-dotenv is not installed")
            return 2
        console.print(f"[dim]# {USER_ENV_PATH}[/]")
        for k, v in sorted(values.items()):
            console.print(f"{k}={redact(k, v)}")
        return 0
    if args.subcmd == "set":
        try:
            path = set_user_env(args.key, args.value)
        except ImportError:
            _ui.print_error(
                console, "python-dotenv required: pip install python-dotenv"
            )
            return 2
        _ui.print_system(console, f"set [cyan]{args.key}[/] in {path}")
        return 0
    if args.subcmd == "unset":
        try:
            path = unset_user_env(args.key)
        except ImportError:
            _ui.print_error(
                console, "python-dotenv required: pip install python-dotenv"
            )
            return 2
        _ui.print_system(console, f"unset [cyan]{args.key}[/] in {path}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
