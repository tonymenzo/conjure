"""``combinator-chat`` — per-agent chat TUI.

Each tmux window in a combinator session runs one ``combinator-chat``
instance for one agent. The window is a self-contained chat:

- **Top pane** — scrollable history of this agent's activity. Sourced
  from the agent's JSONL event log, which the daemon writes to. Each
  event is rendered as a rich panel (responses) or compact line (tool
  results, lifecycle).
- **Bottom pane** — input prompt. Pressing Enter sends the text to
  this agent's inbox via the daemon's control socket.

The chat does not own the agent's runtime — that lives in the daemon.
It's a pure UI: tail-log + send-on-Enter.

Keybindings:

- ``Enter``         — send the current input
- ``Tab``           — toggle focus between history (scroll) and input
- ``PgUp / PgDn``   — scroll history
- ``Ctrl+L``        — clear the input field
- ``Esc``           — focus history (for scrollback)
- ``Ctrl+Q``        — close this window only (daemon untouched)
- ``Ctrl+\\``       — open the meta-view popup (handled by tmux binding)
"""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path
from typing import Any, Sequence

from rich.console import Console
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, Header, Input, RichLog

from combinator._ui import render_event
from combinator.control import ControlClient
from combinator.daemon import socket_path_for
from combinator.event_log import tail


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="combinator-chat")
    parser.add_argument(
        "--log",
        type=Path,
        required=True,
        help="Event log file to tail.",
    )
    parser.add_argument(
        "--addr",
        required=True,
        help="Agent address id (sent to the daemon when this window submits a message).",
    )
    parser.add_argument(
        "--label",
        default="agent",
        help="Display label for this agent in the chat header.",
    )
    parser.add_argument(
        "--socket",
        type=Path,
        default=None,
        help="Daemon control socket (default: derived from --session).",
    )
    parser.add_argument(
        "--session",
        default=None,
        help="Daemon session name; resolved to a socket if --socket is omitted.",
    )
    args = parser.parse_args(argv)

    socket_path = args.socket
    if socket_path is None and args.session:
        socket_path = socket_path_for(args.session)
    if socket_path is None:
        print(
            "combinator-chat: need --socket or --session",
            file=sys.stderr,
        )
        return 2

    ChatApp(
        log_path=args.log,
        addr=args.addr,
        label=args.label,
        socket_path=socket_path,
    ).run()
    return 0


class ChatApp(App):
    """One agent, one chat window."""

    CSS = """
    Screen {
        background: $surface;
    }
    RichLog {
        background: $surface;
        border: round $primary;
        padding: 0 1;
    }
    Input {
        dock: bottom;
        border: round $accent;
        margin: 0;
    }
    Header {
        background: $primary;
        color: $text;
    }
    Footer {
        background: $primary-darken-2;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit_window", "Close window"),
        Binding("escape", "focus_history", "Scroll mode"),
        Binding("tab", "toggle_focus", "Toggle focus"),
        Binding("ctrl+l", "clear_input", "Clear input"),
        Binding("pageup", "scroll_up", "Page up", show=False),
        Binding("pagedown", "scroll_down", "Page down", show=False),
    ]

    def __init__(
        self,
        *,
        log_path: Path,
        addr: str,
        label: str,
        socket_path: Path,
    ) -> None:
        super().__init__()
        self.log_path = log_path
        self.addr = addr
        self.agent_label = label
        self.socket_path = socket_path
        self.client = ControlClient(socket_path)
        self.title = f"combinator › {label}"
        self.sub_title = f"({addr})"
        self._tail_stop = threading.Event()
        self._tail_thread: threading.Thread | None = None
        self._render_console: Console | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield RichLog(
                id="history",
                wrap=True,
                markup=True,
                highlight=False,
                auto_scroll=True,
            )
        yield Input(placeholder="type a message — Enter to send", id="input")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(Input).focus()
        self._start_tail()
        self._refresh_status()
        self.set_interval(2.0, self._refresh_status)

    def on_unmount(self) -> None:
        self._tail_stop.set()

    # ---- status indicator ----

    def _refresh_status(self) -> None:
        """Poll the daemon for this agent's status and update the
        window's subtitle so the user can see at a glance whether the
        agent is alive."""
        try:
            reply = self.client.call("status")
        except Exception:
            return
        if not reply.get("ok"):
            return
        my_status: str | None = None
        for agent in reply.get("agents", []):
            if agent.get("addr") == self.addr:
                my_status = agent.get("status")
                break
        if my_status is None:
            return
        # Use a status icon similar to the meta-view for consistency.
        icon = {"lazy": "…", "running": "▶", "idle": "✓", "terminated": "✗"}.get(
            my_status, "?"
        )
        self.sub_title = f"({self.addr})  {icon} {my_status}"

    # ---- key actions ----

    def action_quit_window(self) -> None:
        self.exit()

    def action_focus_history(self) -> None:
        self.query_one("#history", RichLog).focus()

    def action_toggle_focus(self) -> None:
        if isinstance(self.focused, Input):
            self.query_one("#history", RichLog).focus()
        else:
            self.query_one(Input).focus()

    def action_clear_input(self) -> None:
        inp = self.query_one(Input)
        inp.value = ""

    def action_scroll_up(self) -> None:
        log = self.query_one("#history", RichLog)
        log.scroll_page_up()

    def action_scroll_down(self) -> None:
        log = self.query_one("#history", RichLog)
        log.scroll_page_down()

    # ---- input submission ----

    @on(Input.Submitted)
    def on_submit(self, event: Input.Submitted) -> None:
        text = (event.value or "").strip()
        event.input.value = ""
        if not text:
            return
        # Echo the user's message into the local history right away so
        # the user sees what they sent. The daemon will also emit a
        # ``user`` event into the log; the renderer skips that to avoid
        # a duplicate.
        log = self.query_one("#history", RichLog)
        log.write(f"[bold cyan]you[/] [dim]›[/] {text}")
        try:
            reply = self.client.call("send", addr=self.addr, body=text)
        except Exception as exc:
            log.write(f"[red]send failed:[/] {exc}")
            return
        if not reply.get("ok"):
            log.write(f"[red]send rejected:[/] {reply.get('error', '?')}")

    # ---- log tailing ----

    def _start_tail(self) -> None:
        log = self.query_one("#history", RichLog)
        # Use a Console that renders to a string, then write the
        # rendered output into the RichLog. RichLog accepts renderables
        # directly, which is cleaner — but render_event prints to a
        # console. We use RichLog.write with a renderable per event.
        from rich.console import Console
        from io import StringIO

        capture_console = Console(
            file=StringIO(),
            force_terminal=True,
            color_system="truecolor",
            width=max(self.size.width - 4, 40),
        )
        self._render_console = capture_console

        def reader() -> None:
            for event in tail(self.log_path, poll_interval=0.05, stop_event=self._tail_stop):
                self.call_from_thread(self._render_event_into_log, event)

        self._tail_thread = threading.Thread(
            target=reader, daemon=True, name="chat-tail"
        )
        self._tail_thread.start()

    def _render_event_into_log(self, event: dict[str, Any]) -> None:
        log = self.query_one("#history", RichLog)
        # Render through a transient Console targeting an in-memory
        # file, then write its output into the RichLog.
        from io import StringIO
        from rich.console import Console

        buf = StringIO()
        console = Console(
            file=buf,
            force_terminal=True,
            color_system="truecolor",
            width=max(self.size.width - 4, 40),
            highlight=False,
        )
        try:
            render_event(console, self.agent_label, event)
        except Exception as exc:
            log.write(f"[red]render error:[/] {exc}")
            return
        text = buf.getvalue().rstrip("\n")
        if text:
            log.write(text)


if __name__ == "__main__":
    sys.exit(main())
