"""``combinator`` CLI entry point.

Subcommands:

- ``combinator run <config.yaml>`` — load config, spawn the root agent,
  drop into a REPL (or run a one-shot task if configured).

The REPL has a few control commands prefixed with ``:``:

- ``:tree`` — print the live spawn tree
- ``:inbox <addr_id>`` — print envelopes in any known agent's inbox
- ``:send <addr_id> <body>`` — send a message to any known agent
- ``:status`` — print each agent's status
- ``:quit`` — terminate the runtime and exit

Anything not prefixed with ``:`` is sent as a user message to the root
agent's inbox.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import TextIO

from combinator.address import Address
from combinator.config import load_config
from combinator.runner import build_runtime
from combinator.runtime import Runtime


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="combinator")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Run a configured agent session.")
    run_p.add_argument("config", type=Path, help="Path to a YAML config file.")

    args = parser.parse_args(argv)

    if args.cmd == "run":
        return _cmd_run(args.config)
    parser.print_help()
    return 2


def _cmd_run(config_path: Path) -> int:
    cfg = load_config(config_path)
    runtime, root = build_runtime(cfg)

    print(
        f"[combinator] runtime up. "
        f"root={root.id}({root.label or 'root'}) — "
        f"engine={cfg.root.engine}, llm={cfg.root.llm}",
        file=sys.stderr,
    )
    if cfg.mode == "one-shot":
        return _run_one_shot(runtime, root, cfg.initial_task or "")
    return _run_repl(runtime, root, out=sys.stdout)


def _run_one_shot(runtime: Runtime, root: Address, task: str) -> int:
    if not task:
        print("[combinator] mode is one-shot but no initial_task is set", file=sys.stderr)
        runtime.shutdown()
        return 2
    runtime.send_external(to=root, body={"task": task, "reply_to": "@user"})
    # Wait until the root agent finishes its first response cycle.
    # v1: poll the inbox for any envelope addressed back to "@user"; if
    # none arrives in 60 s, time out.
    deadline = time.monotonic() + 60.0
    cursor = 0
    while time.monotonic() < deadline:
        envelopes = runtime.read_inbox(root, since_seq=cursor)
        if envelopes:
            cursor = envelopes[-1].seq
        time.sleep(0.2)
        # Root's status returning to idle is the heuristic for "done".
        if runtime.record_for(root).status == "idle":
            break
    print("[combinator] root went idle; shutting down")
    runtime.shutdown()
    return 0


def _run_repl(runtime: Runtime, root: Address, *, out: TextIO) -> int:
    try:
        while True:
            try:
                line = input("> ")
            except EOFError:
                print()
                break
            line = line.strip()
            if not line:
                continue
            if line.startswith(":"):
                if _handle_command(line, runtime=runtime, root=root, out=out):
                    break
                continue
            runtime.send_external(to=root, body=line)
            # Give the agent a moment to start.
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\n[combinator] interrupt — shutting down")
    finally:
        runtime.shutdown()
    return 0


def _handle_command(
    line: str,
    *,
    runtime: Runtime,
    root: Address,
    out: TextIO,
) -> bool:
    parts = line.split(maxsplit=2)
    cmd = parts[0]
    if cmd in (":q", ":quit", ":exit"):
        print("[combinator] quit requested")
        return True
    if cmd == ":help":
        _print_help(out)
        return False
    if cmd == ":tree":
        _print_tree(runtime, root, out=out)
        return False
    if cmd == ":status":
        _print_status(runtime, out=out)
        return False
    if cmd == ":inbox":
        if len(parts) < 2:
            print("usage: :inbox <addr_id>", file=out)
            return False
        _print_inbox(runtime, parts[1], out=out)
        return False
    if cmd == ":send":
        if len(parts) < 3:
            print("usage: :send <addr_id> <body>", file=out)
            return False
        _send(runtime, parts[1], parts[2], out=out)
        return False
    print(f"[combinator] unknown command: {cmd}", file=out)
    return False


def _print_help(out: TextIO) -> None:
    print(
        "commands:\n"
        "  :help            show this help\n"
        "  :tree            show the spawn tree\n"
        "  :status          show each agent's status\n"
        "  :inbox <addr>    list envelopes in an agent's inbox\n"
        "  :send <addr> <body>  send a message to any known agent\n"
        "  :quit            terminate and exit\n"
        "Anything else is sent as a user message to the root.",
        file=out,
    )


def _walk(runtime: Runtime, addr: Address, depth: int, out: TextIO) -> None:
    rec = runtime.record_for(addr)
    print(
        f"{'  ' * depth}{addr.id}({addr.label or '-'}) [{rec.status}]",
        file=out,
    )
    for child in sorted(rec.children, key=lambda a: a.id):
        _walk(runtime, child, depth + 1, out)


def _print_tree(runtime: Runtime, root: Address, *, out: TextIO) -> None:
    _walk(runtime, root, 0, out)


def _print_status(runtime: Runtime, *, out: TextIO) -> None:
    # Internal access kept minimal — reading the registry directly is
    # the cleanest path for a CLI inspector.
    with runtime._lock:  # noqa: SLF001 - intentional internal access
        records = list(runtime._records.values())  # noqa: SLF001
    for r in records:
        print(
            f"  {r.addr.id}({r.addr.label or '-'}): {r.status} "
            f"caps={len(r.capabilities)} inbox={len(r.inbox)}",
            file=out,
        )


def _print_inbox(runtime: Runtime, addr_id: str, *, out: TextIO) -> None:
    addr = runtime.address_by_id(addr_id)
    if addr is None:
        print(f"[combinator] no agent with id {addr_id}", file=out)
        return
    for env in runtime.read_inbox(addr):
        print(
            f"  seq={env.seq} from={env.from_} body={_body_preview(env.body)}",
            file=out,
        )


def _send(runtime: Runtime, addr_id: str, body: str, *, out: TextIO) -> None:
    addr = runtime.address_by_id(addr_id)
    if addr is None:
        print(f"[combinator] no agent with id {addr_id}", file=out)
        return
    # Try JSON; fall back to raw string.
    try:
        parsed = json.loads(body)
        runtime.send_external(to=addr, body=parsed)
    except json.JSONDecodeError:
        runtime.send_external(to=addr, body=body)
    print(f"[combinator] sent to {addr.id}", file=out)


def _body_preview(body) -> str:
    s = json.dumps(body, default=str) if not isinstance(body, str) else body
    return s if len(s) <= 200 else s[:197] + "..."


if __name__ == "__main__":
    sys.exit(main())
