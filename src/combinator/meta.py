"""``combinator-meta`` — lazygit-style overview popup.

Launched via ``tmux display-popup -E "combinator-meta --session <name>"``
(bound to ``Ctrl+B M`` by the daemon). One self-contained textual app
with multiple panels modeled on lazyclaude / lazygit:

    ┌─ combinator › combinator-abc ──────────────────────────────────┐
    │ ┌─ tree ──────┐ ┌─ activity: <selected agent> ─────────────────┐│
    │ │ iota   idle │ │ ╭─ iota ──╮                                  ││
    │ │ ├ wkr-1 ✗   │ │ │ 1² = 1  │                                  ││
    │ │ └ wkr-2 ✗   │ │ ╰─────────╯                                  ││
    │ └─────────────┘ │   ✓ msg_id=msg-1                              ││
    │ ┌─ cost ──────┐ │ ╭─ iota ──╮                                  ││
    │ │ iota $0.001 │ │ │ Sending result to user.                    ││
    │ │ ...         │ │ ╰────────╯                                   ││
    │ │ total $0.04 │ └────────────────────────────────────────────┘ ││
    │ └─────────────┘                                                  │
    │ Tab cycle  j/k navigate  Enter open chat  r refresh  q quit      │
    └──────────────────────────────────────────────────────────────────┘

Keybindings:

- ``q`` / ``Esc``         — close popup
- ``Tab`` / ``Shift+Tab`` — cycle focus between panels
- ``j`` / ``k`` / arrows  — navigate within focused panel
- ``Enter``               — switch tmux to the selected agent's window
- ``r``                   — refresh data immediately
- ``t``                   — terminate the selected agent (cascade)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path
from typing import Any, Sequence

from rich.console import Console
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Static, Tree
from textual.widgets.tree import TreeNode

from combinator._ui import render_event
from combinator.control import ControlClient
from combinator.daemon import list_session_names, socket_path_for


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="combinator-meta")
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
            "combinator-meta: could not locate a daemon socket "
            "(pass --socket or --session)",
            file=sys.stderr,
        )
        return 2
    if not socket_path.exists():
        print(f"combinator-meta: socket not found: {socket_path}", file=sys.stderr)
        return 2

    tmux_session = args.session or _current_tmux_session()
    MetaApp(socket_path=socket_path, tmux_session=tmux_session).run()
    return 0


def _resolve_socket(explicit: Path | None, session: str | None) -> Path | None:
    if explicit is not None:
        return explicit
    if session is not None:
        return socket_path_for(session)
    tmux_session = _current_tmux_session()
    if tmux_session and tmux_session.startswith("combinator-"):
        return socket_path_for(tmux_session)
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


def _current_tmux_session() -> str | None:
    if not os.environ.get("TMUX"):
        return None
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", "#S"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


_STATUS_ICON = {
    "lazy": "…",
    "running": "▶",
    "idle": "✓",
    "terminated": "✗",
}
_STATUS_STYLE = {
    "lazy": "yellow",
    "running": "green",
    "idle": "white",
    "terminated": "red dim",
}


class MetaApp(App):
    """Lazygit-style overview of the daemon's runtime state."""

    CSS = """
    Screen {
        background: $surface;
    }
    #left {
        width: 35%;
    }
    #tree-pane, #cost-pane {
        border: round $primary;
        background: $surface;
        padding: 0 1;
    }
    #tree-pane {
        height: 70%;
    }
    #cost-pane {
        height: 30%;
    }
    #preview-pane {
        border: round $primary;
        background: $surface;
        padding: 0 1;
        overflow-y: auto;
    }
    Tree {
        background: $surface;
    }
    Header {
        background: $primary;
    }
    Footer {
        background: $primary-darken-2;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("escape", "quit", "Close"),
        Binding("tab", "focus_next", "Next panel"),
        Binding("shift+tab", "focus_previous", "Prev panel"),
        Binding("r", "refresh", "Refresh"),
        Binding("enter", "select_agent", "Open chat", show=True),
        Binding("t", "terminate_agent", "Terminate"),
    ]

    def __init__(
        self,
        *,
        socket_path: Path,
        tmux_session: str | None,
    ) -> None:
        super().__init__()
        self.socket_path = socket_path
        self.tmux_session = tmux_session
        self.client = ControlClient(socket_path)
        self.title = "combinator"
        self.sub_title = (
            f"session: {socket_path.stem} "
            f"({'attached' if tmux_session else 'standalone'})"
        )
        self.selected_addr: str | None = None
        self.selected_label: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal():
            with Vertical(id="left"):
                yield Tree("spawn tree", id="tree-pane")
                yield Static("(no costs yet)", id="cost-pane")
            yield Static(
                "(select an agent to preview its activity)",
                id="preview-pane",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_all()
        self.set_interval(1.0, self.refresh_all)
        self.query_one("#tree-pane", Tree).focus()

    # ---- data refresh ----

    def action_refresh(self) -> None:
        self.refresh_all()

    def refresh_all(self) -> None:
        self.refresh_tree()
        self.refresh_cost()
        if self.selected_addr is not None:
            self.refresh_preview(self.selected_addr)

    def refresh_tree(self) -> None:
        reply = self.client.call("tree")
        tree = self.query_one("#tree-pane", Tree)
        tree.clear()
        node = reply.get("tree") if reply.get("ok") else None
        if node is None:
            tree.root.set_label("(no agents)")
            tree.root.expand()
            return
        tree.root.set_label("agents")
        tree.root.expand()
        self._populate(tree.root, node)

    def _populate(self, parent: TreeNode, node: dict[str, Any]) -> None:
        icon = _STATUS_ICON.get(node.get("status", ""), "?")
        style = _STATUS_STYLE.get(node.get("status", ""), "white")
        label_text = node.get("label") or node.get("addr") or "?"
        markup = f"[{style}]{icon}[/] [bold]{label_text}[/]"
        addr_id = node.get("addr")
        child = parent.add(markup, data=addr_id, expand=True)
        for c in node.get("children", []):
            self._populate(child, c)

    def refresh_cost(self) -> None:
        reply = self.client.call("cost")
        cost = self.query_one("#cost-pane", Static)
        if not reply.get("ok"):
            cost.update(f"[red]{reply.get('error', '?')}[/]")
            return
        lines: list[str] = ["[bold]cost[/]"]
        total = reply.get("total", 0.0)
        for row in reply.get("rows", []):
            usd = row.get("cost", 0.0)
            label = row.get("label") or row.get("addr") or "?"
            lines.append(f"  {label:<14}  {_format_usd(usd)}")
        lines.append("")
        lines.append(f"[bold]total[/]  {_format_usd(total)}")
        cost.update("\n".join(lines))

    def refresh_preview(self, addr_id: str) -> None:
        reply = self.client.call("inbox", addr=addr_id)
        preview = self.query_one("#preview-pane", Static)
        if not reply.get("ok"):
            preview.update(f"[red]{reply.get('error', '?')}[/]")
            return
        envs = reply.get("envelopes", [])
        if not envs:
            preview.update(
                f"[dim](inbox empty for[/] [bold]{self.selected_label or addr_id}[/][dim])[/]"
            )
            return
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, color_system="truecolor", width=120)
        console.print(f"[bold]inbox of {self.selected_label or addr_id}[/]")
        console.print()
        for env in envs[-12:]:
            sender = env.get("from_label") or env.get("from") or "?"
            body = env.get("body")
            body_repr = body if isinstance(body, str) else _short_repr(body, 200)
            console.print(
                f"[cyan]seq={env.get('seq')}[/]  "
                f"from=[magenta]{sender}[/]  {body_repr}"
            )
        preview.update(buf.getvalue().rstrip("\n"))

    # ---- selection actions ----

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        addr_id = event.node.data
        if isinstance(addr_id, str):
            self.selected_addr = addr_id
            label = str(event.node.label).strip()
            # Strip the icon + style markup we added in _populate.
            label_clean = label.split()[-1] if label else addr_id
            self.selected_label = label_clean
            self.refresh_preview(addr_id)

    def action_select_agent(self) -> None:
        """Switch tmux to the selected agent's window, then close popup."""
        if not self.selected_label:
            return
        if not self.tmux_session:
            return
        try:
            subprocess.run(
                ["tmux", "select-window", "-t",
                 f"{self.tmux_session}:{self.selected_label}"],
                check=False,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        self.exit()

    def action_terminate_agent(self) -> None:
        if not self.selected_addr:
            return
        try:
            self.client.call("terminate", addr=self.selected_addr)
        except Exception:
            pass
        self.refresh_all()


def _format_usd(usd: float) -> str:
    if usd <= 0:
        return "[dim]$0.0000[/]"
    if usd < 0.01:
        return f"[cyan]${usd:.6f}[/]"
    return f"[cyan]${usd:.4f}[/]"


def _short_repr(value: object, limit: int = 200) -> str:
    s = repr(value)
    return s if len(s) <= limit else s[: limit - 1] + "…"


if __name__ == "__main__":
    sys.exit(main())
