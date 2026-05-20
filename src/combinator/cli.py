"""``combinator`` CLI entry point.

Subcommands:

- ``combinator run <config.yaml>`` — load config, spawn the root agent,
  drop into a rich-rendered REPL (or run a one-shot task).
- ``combinator check <config.yaml>`` — validate config + report API key
  availability.
- ``combinator config list|set|unset`` — manage the user-global .env.

REPL control commands:

- ``:tree`` — print the live spawn tree
- ``:status`` — print each agent's status
- ``:inbox <addr_id>`` — print envelopes in any known agent's inbox
- ``:send <addr_id> <body>`` — send a message to any known agent
- ``:help`` — show commands
- ``:quit`` — terminate the runtime and exit
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from rich.rule import Rule

from combinator import _ui
from combinator.address import USER, Address
from combinator.config import load_config
from combinator.runner import build_runtime
from combinator.runtime import Runtime


_AGENT_RESPONSE_TIMEOUT_S = 180.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="combinator")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Run a configured agent session.")
    run_p.add_argument("config", type=Path, help="Path to a YAML config file.")

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
        return _cmd_run(args.config)
    if args.cmd == "check":
        return _cmd_check(args.config)
    if args.cmd == "config":
        return _cmd_config(args)
    parser.print_help()
    return 2


def _cmd_run(config_path: Path) -> int:
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
    """Print any structured messages agents have sent to ``@user`` since
    ``since_seq`` and return the new cursor."""
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
    """Block until ``addr`` has consumed up to ``target_seq`` and gone
    idle. Shows a spinner while waiting."""
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
    status_color = {
        "lazy": "yellow",
        "running": "green",
        "idle": "white",
        "terminated": "red",
    }.get(rec.status, "white")
    label = addr.label or "—"
    console.print(
        f"{prefix}[bold magenta]{label}[/] [dim]({addr.id})[/] "
        f"[{status_color}]{rec.status}[/]"
    )
    children = sorted(rec.children, key=lambda a: a.id)
    for i, child in enumerate(children):
        is_last = i == len(children) - 1
        branch = "└── " if is_last else "├── "
        next_prefix = prefix + ("    " if is_last else "│   ")
        _walk(runtime, child, prefix=prefix + branch, console=console)


def _print_status(runtime: Runtime, *, console) -> None:
    with runtime._lock:  # noqa: SLF001 - intentional internal access for inspector
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
