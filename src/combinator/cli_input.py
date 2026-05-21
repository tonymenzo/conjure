"""``combinator-input`` — the interactive prompt that lives in window 0
of a tmux-mode session.

Reads a line from the user, writes it as JSON to an input channel file
that the runtime daemon tails. Plain text becomes a user message to the
root agent; ``:quit`` requests daemon shutdown. Other control commands
are recognized and forwarded but produce no inline output here — for
the meta-view (spawn tree, inbox, etc.) use the popup (Ctrl+\\) once
that lands.

The input file is append-only JSONL so it survives concurrent writes
(only one writer at a time in practice, but the format is robust to
restarts and partial reads from the daemon side).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from rich.console import Console


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="combinator-input")
    parser.add_argument(
        "--input-path",
        type=Path,
        required=True,
        help="Append-only JSONL file the daemon tails for input.",
    )
    parser.add_argument(
        "--label",
        default="root",
        help="Display label for the agent receiving plain-text messages.",
    )
    args = parser.parse_args(argv)

    args.input_path.parent.mkdir(parents=True, exist_ok=True)
    args.input_path.touch()

    console = Console(highlight=False)
    console.print(
        f"[dim]combinator input →[/] [bold magenta]{args.label}[/]"
    )
    console.print(
        "[dim]plain text → root; [/][cyan]:quit[/][dim] → stop the daemon[/]"
    )
    console.print()

    while True:
        try:
            line = console.input("[bold cyan]you[/] [dim]›[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if not line:
            continue
        _append(args.input_path, {"line": line})
        if line in (":quit", ":q", ":exit"):
            console.print("[dim](quit sent — daemon will shut down)[/]")
            break
        console.print("[dim](sent)[/]")
    return 0


def _append(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        f.flush()


if __name__ == "__main__":
    sys.exit(main())
