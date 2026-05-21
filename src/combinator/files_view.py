"""``combinator-files`` — popup showing every agent's sandbox tree.

Launched via ``tmux display-popup -E "combinator-files --session
<name>"`` (bound to ``Ctrl+B F`` by the daemon).

Layout:

    ┌─ combinator › files ───────────────────────────────────────────┐
    │ ┌─ agents ─────┐ ┌─ sandbox tree ─────────────────────────────┐│
    │ │ ● root       │ │ src/                                       ││
    │ │   worker-1   │ │   main.py                                  ││
    │ │   worker-2   │ │ tests/                                     ││
    │ │              │ │   test_main.py                             ││
    │ │              │ │ README.md                                  ││
    │ └──────────────┘ └────────────────────────────────────────────┘│
    │ ┌─ preview ────────────────────────────────────────────────────┐│
    │ │ # README                                                     ││
    │ │ ...                                                          ││
    │ └──────────────────────────────────────────────────────────────┘│
    │ Tab cycle  Enter open  q close                                  │
    └──────────────────────────────────────────────────────────────────┘

Agents on the left, sandbox tree in the upper-right, file preview
below. Navigate with arrow keys / Tab; Enter on a file loads its
preview.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Static, Tree
from textual.widgets.tree import TreeNode

from combinator.control import ControlClient
from combinator.daemon import list_session_names, socket_path_for
from combinator.status_tree import StatusTree


_STATUS_DOT = {
    "lazy": "[bold green]●[/]",
    "running": "[bold yellow]●[/]",
    "idle": "[green]●[/]",
    "terminated": "[dim]●[/]",
    "error": "[bold red]●[/]",
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="combinator-files")
    parser.add_argument("--socket", type=Path, default=None)
    parser.add_argument("--session", default=None)
    args = parser.parse_args(argv)
    socket_path = _resolve_socket(args.socket, args.session)
    if socket_path is None or not socket_path.exists():
        print(
            "combinator-files: could not locate a daemon socket "
            "(pass --socket or --session)",
            file=sys.stderr,
        )
        return 2
    FilesApp(socket_path=socket_path).run()
    return 0


def _resolve_socket(explicit: Path | None, session: str | None) -> Path | None:
    if explicit is not None:
        return explicit
    if session is not None:
        return socket_path_for(session)
    live = list_session_names()
    if not live:
        return None
    live.sort(
        key=lambda n: (
            socket_path_for(n).stat().st_mtime
            if socket_path_for(n).exists()
            else 0
        ),
        reverse=True,
    )
    return socket_path_for(live[0])


class FilesApp(App):
    """File browser for any agent's sandbox."""

    CSS = """
    Screen { background: $surface; }
    #agents-pane, #files-pane, #preview-pane {
        border: round $primary;
        background: $surface;
        padding: 0 1;
    }
    #agents-pane  { width: 30%; height: 50%; }
    #files-pane   { height: 50%; }
    #preview-pane { height: 50%; }
    Tree { background: $surface; }
    #agents-pane > .tree--cursor,
    #files-pane > .tree--cursor {
        background: $surface;
        text-style: bold underline;
    }
    Header { background: $primary; }
    Footer { background: $primary-darken-2; }
    """

    BINDINGS = [
        Binding("q", "quit", "Close"),
        Binding("escape", "quit", "Close"),
        Binding("tab", "focus_next", "Next panel"),
        Binding("shift+tab", "focus_previous", "Prev panel"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self, *, socket_path: Path) -> None:
        super().__init__()
        self.socket_path = socket_path
        self.client = ControlClient(socket_path)
        self.title = "combinator › files"
        self.sub_title = f"session: {socket_path.stem}"

        self._selected_addr: str | None = None
        self._known_addrs: set[str] = set()
        self._known_file_paths: set[tuple[str, str]] = set()  # (addr, path)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal():
            with Vertical(id="left"):
                yield StatusTree("agents", id="agents-pane")
            with Vertical(id="right"):
                yield StatusTree("sandbox", id="files-pane")
                yield Static("(select a file)", id="preview-pane")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_data()
        self.set_interval(1.0, self.refresh_data)
        self.query_one("#agents-pane", StatusTree).focus()

    def action_refresh(self) -> None:
        self.refresh_data()

    # ---- data refresh ----

    def refresh_data(self) -> None:
        try:
            tree_reply = self.client.call("tree")
        except Exception as exc:
            self.query_one("#preview-pane", Static).update(
                Text(f"control error: {exc}", style="red")
            )
            return
        if not tree_reply.get("ok"):
            return
        self._apply_agents_tree(tree_reply.get("tree"))
        if self._selected_addr is None:
            self._select_first_agent()
        if self._selected_addr is not None:
            self._refresh_files(self._selected_addr)

    def _apply_agents_tree(self, node: dict[str, Any] | None) -> None:
        tree = self.query_one("#agents-pane", StatusTree)
        # Flatten to a list (depth-first); we don't need the nested
        # structure for this view — just the agents themselves.
        agents: list[dict[str, Any]] = []

        def walk(n: dict[str, Any]) -> None:
            agents.append(n)
            for c in n.get("children", []):
                walk(c)

        if node is not None:
            walk(node)
        addrs_now = {a.get("addr") for a in agents if a.get("addr")}
        if addrs_now == self._known_addrs:
            # Just update labels in place.
            self._update_agent_labels(agents)
            return
        self._known_addrs = addrs_now
        tree.clear()
        tree.show_root = False
        tree.root.expand()
        for a in agents:
            label = _agent_label(a)
            tree.root.add(label, data=a.get("addr"), allow_expand=False)

    def _update_agent_labels(self, agents: list[dict[str, Any]]) -> None:
        tree = self.query_one("#agents-pane", StatusTree)
        by_addr = {a.get("addr"): a for a in agents}
        for node in tree.root.children:
            if isinstance(node.data, str) and node.data in by_addr:
                new_label = _agent_label(by_addr[node.data])
                if str(node.label) != new_label:
                    node.set_label(new_label)

    def _select_first_agent(self) -> None:
        tree = self.query_one("#agents-pane", StatusTree)
        for child in tree.root.children:
            if isinstance(child.data, str):
                self._selected_addr = child.data
                tree.select_node(child)
                return

    def _refresh_files(self, addr: str, sub_path: str = "") -> None:
        try:
            reply = self.client.call("sandbox", addr=addr, path=sub_path)
        except Exception as exc:
            self.query_one("#preview-pane", Static).update(
                Text(f"sandbox error: {exc}", style="red")
            )
            return
        if not reply.get("ok"):
            self.query_one("#files-pane", StatusTree).clear()
            self.query_one("#preview-pane", Static).update(
                Text(reply.get("error", "?"), style="red")
            )
            return
        if reply.get("kind") == "dir":
            self._populate_files(reply.get("entries") or [], addr=addr)
        else:
            self._preview_file(reply)

    def _populate_files(
        self, entries: list[dict[str, Any]], *, addr: str
    ) -> None:
        tree = self.query_one("#files-pane", StatusTree)
        tree.clear()
        tree.show_root = False
        tree.root.expand()
        if not entries:
            tree.root.add("(empty sandbox)", allow_expand=False)
            return
        for entry in entries:
            icon = "📁 " if entry.get("is_dir") else "📄 "
            label = f"{icon}{entry.get('name', '?')}"
            tree.root.add(label, data=entry.get("path"), allow_expand=False)

    def _preview_file(self, payload: dict[str, Any]) -> None:
        pane = self.query_one("#preview-pane", Static)
        path = payload.get("path", "")
        content = payload.get("content") or ""
        truncated = payload.get("truncated")
        rendered = Text()
        rendered.append(f"{path}\n", style="bold cyan")
        rendered.append(content)
        if truncated:
            rendered.append("\n[…truncated]", style="dim")
        pane.update(rendered)

    # ---- selection handlers ----

    @on(Tree.NodeSelected, "#agents-pane")
    def _on_agent_selected(self, event: Tree.NodeSelected) -> None:
        addr = event.node.data
        if isinstance(addr, str):
            self._selected_addr = addr
            self._refresh_files(addr)

    @on(Tree.NodeHighlighted, "#agents-pane")
    def _on_agent_highlighted(self, event: Tree.NodeHighlighted) -> None:
        addr = event.node.data
        if isinstance(addr, str) and addr != self._selected_addr:
            self._selected_addr = addr
            self._refresh_files(addr)

    @on(Tree.NodeSelected, "#files-pane")
    def _on_file_selected(self, event: Tree.NodeSelected) -> None:
        path = event.node.data
        if isinstance(path, str) and self._selected_addr:
            self._refresh_files(self._selected_addr, path)


def _agent_label(agent: dict[str, Any]) -> str:
    dot = _STATUS_DOT.get(agent.get("status", ""), "[dim]●[/]")
    label = agent.get("label") or agent.get("addr") or "?"
    return f"{dot} [bold]{label}[/]"


if __name__ == "__main__":
    sys.exit(main())
