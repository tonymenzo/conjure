"""``combinator-inbox`` — popup that shows every agent's inbox.

Launched via ``tmux display-popup -E "combinator-inbox --session
<name>"`` (bound to ``Ctrl+B I`` by the daemon). One self-contained
textual app:

    ┌─ combinator › inboxes ───────────────────────────────────────┐
    │ ● root                                          pending: 1   │
    │     seq=3 from=@user      compute 7!                         │
    │                                                              │
    │ ● worker-1                                      pending: 0   │
    │     (empty)                                                  │
    │                                                              │
    │ q close  r refresh                                           │
    └──────────────────────────────────────────────────────────────┘

The list refreshes every 500ms via the control socket. Agents are
sorted by tree depth then label so the root sits at the top with
its children below.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from rich.console import Group, RenderableType
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Static

from combinator.control import ControlClient
from combinator.daemon import list_session_names, socket_path_for


_STATUS_DOT = {
    "lazy": "[bold green]●[/]",
    "running": "[bold yellow]●[/]",
    "idle": "[green]●[/]",
    "terminated": "[dim]●[/]",
    "error": "[bold red]●[/]",
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="combinator-inbox")
    parser.add_argument(
        "--socket",
        type=Path,
        default=None,
        help="Daemon control socket path (auto-discovered if omitted).",
    )
    parser.add_argument(
        "--session",
        default=None,
        help="Daemon session name (resolved to socket via the standard path).",
    )
    args = parser.parse_args(argv)

    socket_path = _resolve_socket(args.socket, args.session)
    if socket_path is None:
        print(
            "combinator-inbox: could not locate a daemon socket "
            "(pass --socket or --session)",
            file=sys.stderr,
        )
        return 2
    if not socket_path.exists():
        print(f"combinator-inbox: socket not found: {socket_path}", file=sys.stderr)
        return 2

    InboxApp(socket_path=socket_path).run()
    return 0


def _resolve_socket(explicit: Path | None, session: str | None) -> Path | None:
    if explicit is not None:
        return explicit
    if session is not None:
        return socket_path_for(session)
    live = list_session_names()
    if live:
        live.sort(
            key=lambda n: (
                socket_path_for(n).stat().st_mtime
                if socket_path_for(n).exists()
                else 0
            ),
            reverse=True,
        )
        return socket_path_for(live[0])
    return None


class InboxApp(App):
    """List of every agent's inbox, refreshed live."""

    CSS = """
    Screen {
        background: $surface;
    }
    #inbox-list {
        padding: 0 1;
    }
    Header {
        background: $primary;
    }
    Footer {
        background: $primary-darken-2;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Close"),
        Binding("escape", "quit", "Close"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self, *, socket_path: Path) -> None:
        super().__init__()
        self.socket_path = socket_path
        self.client = ControlClient(socket_path)
        self.title = "combinator › inboxes"
        self.sub_title = f"session: {socket_path.stem}"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with VerticalScroll():
            yield Static("(loading…)", id="inbox-list")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_data()
        self.set_interval(0.5, self.refresh_data)

    def action_refresh(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        try:
            reply = self.client.call("inboxes", limit=15)
        except Exception as exc:
            self.query_one("#inbox-list", Static).update(
                Text(f"control error: {exc}", style="red")
            )
            return
        if not reply.get("ok"):
            self.query_one("#inbox-list", Static).update(
                Text(reply.get("error", "?"), style="red")
            )
            return
        agents = reply.get("agents", []) or []
        self.query_one("#inbox-list", Static).update(_render_inboxes(agents))


def _render_inboxes(agents: list[dict[str, Any]]) -> RenderableType:
    if not agents:
        return Text("(no agents)", style="dim")
    rows: list[RenderableType] = []
    for agent in agents:
        rows.append(_render_agent(agent))
        rows.append(Text(""))  # spacer between agents
    return Group(*rows)


def _render_agent(agent: dict[str, Any]) -> RenderableType:
    label = agent.get("label") or agent.get("addr") or "?"
    status = agent.get("status", "")
    dot = _STATUS_DOT.get(status, "[dim]●[/]")
    envs = agent.get("envelopes", []) or []
    peers = agent.get("peers", []) or []
    header = Text.from_markup(dot)
    header.append(" ")
    header.append(label, style="bold magenta")
    header.append(f"   pending: {len(envs)}", style="dim")
    rows: list[RenderableType] = [header]
    if not envs:
        rows.append(Text("    (empty)", style="dim"))
    else:
        for e in envs[-5:]:
            sender = e.get("from_label") or e.get("from") or "?"
            body = e.get("body")
            body_repr = body if isinstance(body, str) else repr(body)
            line = Text()
            line.append(f"    seq={e.get('seq')}  ", style="cyan")
            line.append(f"from={sender}  ", style="magenta")
            line.append(_truncate(str(body_repr), 100))
            rows.append(line)
    if peers:
        now = time.time()
        rows.append(Text(""))
        rows.append(Text("    conversations", style="dim"))
        for p in peers[:5]:
            rows.append(_render_peer(p, now))
    return Group(*rows)


def _render_peer(peer: dict[str, Any], now: float) -> RenderableType:
    """One conversation row: arrow (last direction), peer name, age,
    and an awaiting-reply badge if the local agent is waiting on the
    peer."""
    last_in = peer.get("last_in_ts", 0) or 0
    last_out = peer.get("last_out_ts", 0) or 0
    last_ts = peer.get("last_ts", 0) or 0
    direction_in = last_in >= last_out
    arrow = "←" if direction_in else "→"
    arrow_style = "cyan" if direction_in else "magenta"
    age_s = max(0, int(now - last_ts))
    line = Text()
    line.append("      ")
    line.append(f"{arrow}  ", style=arrow_style)
    line.append(peer.get("peer_label") or peer.get("peer") or "?", style="bold")
    line.append(f"   {age_s}s ago", style="dim")
    if peer.get("awaiting_reply"):
        line.append("   awaiting reply", style="yellow")
    return line


def _truncate(s: str, n: int) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


if __name__ == "__main__":
    sys.exit(main())
