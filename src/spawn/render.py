"""``spawn-render`` — per-agent renderer process.

Each spawned agent gets a tmux window running this script. The window
tails the agent's JSONL event log and renders rich panels exactly as
the REPL would. The process keeps running after the agent terminates
so the user can scroll through history; close it explicitly with
``Ctrl+B &`` or by killing the tmux window.

Usage:

.. code-block:: bash

    spawn-render --log <path> --label <label>

The renderer never writes to the event log — it's pure read side.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path
from typing import Sequence

from rich.console import Console
from rich.rule import Rule

from spawn._ui import render_event
from spawn.event_log import tail


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="spawn-render")
    parser.add_argument("--log", type=Path, required=True, help="Event log to tail.")
    parser.add_argument(
        "--label",
        default="agent",
        help="Display label for this agent.",
    )
    parser.add_argument(
        "--poll",
        type=float,
        default=0.05,
        help="Polling interval (seconds) when caught up.",
    )
    args = parser.parse_args(argv)

    console = Console(highlight=False)
    console.print(
        f"[dim]spawn-render › [/][bold magenta]{args.label}[/] "
        f"[dim]› log {args.log}[/]"
    )
    console.print(Rule(style="dim"))

    stop = threading.Event()

    def _handle_sigterm(_signum, _frame):
        stop.set()

    # signal.signal only works in the main thread of the main interpreter.
    # Tests invoke main() from a worker thread; skip signal setup there.
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _handle_sigterm)
        signal.signal(signal.SIGINT, _handle_sigterm)

    try:
        for event in tail(args.log, poll_interval=args.poll, stop_event=stop):
            try:
                render_event(console, args.label, event)
            except Exception as exc:  # rendering should never kill the loop
                console.print(f"[red]render error:[/] {exc}")
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
