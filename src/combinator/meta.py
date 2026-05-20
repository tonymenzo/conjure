"""``combinator-meta`` — the textual popup that surfaces runtime state.

Launched as ``tmux display-popup -E "combinator meta"`` (or directly
via ``combinator meta --socket <path>``). Talks to the daemon over the
session's Unix socket. Pure read side for v1; mutations (send,
terminate) are deferred.

Layout (single screen, no resizing):

    ┌─ combinator ─ session: combinator-abc ─ total: $0.0042 ────┐
    │ ┌─ spawn tree ───┐ ┌─ inbox: <selected agent> ───────────┐ │
    │ │ iota      idle │ │ seq=1 from=@user body="hi"          │ │
    │ │ ├ worker-1 ✗   │ │ ...                                 │ │
    │ │ └ worker-2 ✗   │ │                                     │ │
    │ └────────────────┘ └─────────────────────────────────────┘ │
    │ [q]quit  [r]refresh  [j/k]navigate  [enter]select           │
    └─────────────────────────────────────────────────────────────┘

Auto-refreshes the tree every 1 s. Selecting a node in the tree
refreshes the inbox pane.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Static, Tree
from textual.widgets.tree import TreeNode

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

    MetaApp(socket_path).run()
    return 0


def _resolve_socket(explicit: Path | None, session: str | None) -> Path | None:
    """Pick the daemon socket to talk to.

    Resolution order:
    1. ``--socket``: explicit path.
    2. ``--session``: name lookup.
    3. ``TMUX`` env var: parse the current tmux session and use it.
    4. Newest live combinator-* daemon (PID file mtime).
    """
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
            key=lambda n: (socket_path_for(n).stat().st_mtime if socket_path_for(n).exists() else 0),
            reverse=True,
        )
        return socket_path_for(live[0])
    return None


def _current_tmux_session() -> str | None:
    """Read the current tmux session name from ``$TMUX`` / tmux display."""
    tmux = os.environ.get("TMUX")
    if not tmux:
        return None
    import subprocess

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


# ---- The textual app -----------------------------------------------------

_STATUS_ICON = {
    "lazy": "…",
    "running": "▶",
    "idle": "✓",
    "terminated": "✗",
}


class MetaApp(App):
    CSS = """
    Tree {
        width: 50%;
        background: $surface;
        border: solid $primary;
    }
    #inbox {
        width: 50%;
        padding: 1;
        background: $surface;
        border: solid $primary;
        overflow-y: auto;
    }
    #status {
        height: 3;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("escape", "quit", "Close"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self, socket_path: Path) -> None:
        super().__init__()
        self.socket_path = socket_path
        self.client = ControlClient(socket_path)
        self.selected_addr: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield Tree("spawn tree", id="tree")
            yield Static("(select an agent to view its inbox)", id="inbox")
        yield Static(id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_tree()
        self.refresh_cost()
        self.set_interval(1.0, self._tick)

    def _tick(self) -> None:
        self.refresh_tree()
        self.refresh_cost()
        if self.selected_addr is not None:
            self.refresh_inbox(self.selected_addr)

    def action_refresh(self) -> None:
        self._tick()

    # ---- data refresh ----

    def refresh_tree(self) -> None:
        reply = self.client.call("tree")
        tree_widget = self.query_one("#tree", Tree)
        tree_widget.clear()
        tree_widget.root.expand()
        node = reply.get("tree") if reply.get("ok") else None
        if node is None:
            tree_widget.root.set_label("(no agents)")
            return
        self._populate_tree(tree_widget.root, node)

    def _populate_tree(self, parent: TreeNode, node: dict) -> None:
        icon = _STATUS_ICON.get(node.get("status", ""), "?")
        label = f"{icon} {node.get('label')}"
        addr_id = node.get("addr")
        child_node = parent.add(label, data=addr_id, expand=True)
        for c in node.get("children", []):
            self._populate_tree(child_node, c)

    def refresh_cost(self) -> None:
        reply = self.client.call("cost")
        if not reply.get("ok"):
            return
        total = reply.get("total", 0.0)
        status = self.query_one("#status", Static)
        status.update(
            f"session: [cyan]{self.socket_path.stem}[/]   "
            f"total cost: [bold]${total:.4f}[/]"
        )

    def refresh_inbox(self, addr_id: str) -> None:
        reply = self.client.call("inbox", addr=addr_id)
        inbox = self.query_one("#inbox", Static)
        if not reply.get("ok"):
            inbox.update(f"[red]{reply.get('error', 'unknown error')}[/]")
            return
        envs = reply.get("envelopes", [])
        if not envs:
            inbox.update(f"[dim](inbox empty for {addr_id})[/]")
            return
        lines = [f"[bold]inbox for {addr_id}[/]\n"]
        for e in envs:
            sender = e.get("from", "?")
            body = e.get("body")
            preview = (
                body if isinstance(body, str) else _short_repr(body)
            )
            lines.append(
                f"[cyan]seq={e.get('seq')}[/]  "
                f"from=[magenta]{sender}[/]  {preview}"
            )
        inbox.update("\n".join(lines))

    # ---- tree selection ----

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        addr_id = event.node.data
        if isinstance(addr_id, str):
            self.selected_addr = addr_id
            self.refresh_inbox(addr_id)


def _short_repr(value: object, limit: int = 200) -> str:
    s = repr(value)
    return s if len(s) <= limit else s[: limit - 1] + "…"


if __name__ == "__main__":
    sys.exit(main())
