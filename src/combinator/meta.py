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


# Status icon legend (mirrors combinator-main): filled circle; colour
# encodes the agent's lifecycle. Active states pulse via a 4-step
# brightness cycle, driven by a 250ms timer (smoother than the
# terminal's hard blink attribute).
_STATUS_ICON = "●"
_PULSE_STEPS = 4
_PULSE_INTERVAL = 0.25
_PULSE_STYLES = {
    "lazy":    ["bold green",  "green",  "dim green",  "green"],
    "running": ["bold yellow", "yellow", "dim yellow", "yellow"],
    "error":   ["bold red",    "red",    "dim red",    "red"],
}
_STATIC_STYLES = {
    "idle": "green",
    "terminated": "dim",
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
        scrollbar-size: 1 1;
        scrollbar-gutter: stable;
    }
    Tree {
        background: $surface;
        scrollbar-size: 1 1;
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
        # Cache of the last tree *structure* we rendered (addresses +
        # parent/child shape, status excluded). We only tear down and
        # rebuild the textual ``Tree`` when this signature changes —
        # status updates flow through ``_update_labels`` and don't
        # touch expand state. This is what keeps user-collapsed nodes
        # collapsed across the periodic refresh.
        self._tree_signature: tuple | None = None
        # Optional: agents the user explicitly collapsed. Applied when
        # a rebuild does happen (e.g. a new agent appears).
        self._collapsed: set[str] = set()
        # Smooth pulse for active agent dots — same scheme as the main
        # window. Phase advances on its own timer; refresh ticks reuse
        # the latest cached tree.
        self._pulse_phase: int = 0
        self._last_tree: dict[str, Any] | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal():
            with Vertical(id="left"):
                yield StatusTree("spawn tree", id="tree-pane")
                yield Static("(no costs yet)", id="cost-pane")
            yield Static(
                "(select an agent to preview its activity)",
                id="preview-pane",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_all()
        self.set_interval(1.0, self.refresh_all)
        self.set_interval(_PULSE_INTERVAL, self._tick_pulse)
        self.query_one("#tree-pane", StatusTree).focus()

    def _tick_pulse(self) -> None:
        self._pulse_phase = (self._pulse_phase + 1) % _PULSE_STEPS
        if self._last_tree is not None:
            self._update_labels(self._last_tree)

    # ---- data refresh ----

    def action_refresh(self) -> None:
        self.refresh_all()

    def refresh_all(self) -> None:
        self.refresh_tree()
        self.refresh_cost()
        if self.selected_addr is not None:
            self.refresh_preview(self.selected_addr)

    def refresh_tree(self) -> None:
        """Periodic refresh.

        On every tick we compare the current daemon-reported tree
        structure (addresses + parent/child relationships, *not*
        status) to the last one we rendered. If unchanged, we only
        update the labels of existing nodes — no clear/rebuild, no
        touching of expand state. Status icons update visually,
        user-collapsed nodes stay collapsed.

        Only when an agent is added or removed do we rebuild the
        tree from scratch, applying ``self._collapsed`` to honor any
        prior user collapses on still-existing nodes.
        """
        reply = self.client.call("tree")
        if not reply.get("ok"):
            return
        node = reply.get("tree")
        self._last_tree = node
        new_sig = _structure_signature(node)
        if new_sig == self._tree_signature:
            self._update_labels(node)
            return
        self._tree_signature = new_sig
        self._rebuild_tree(node)

    def _rebuild_tree(self, node: dict[str, Any] | None) -> None:
        tree = self.query_one("#tree-pane", StatusTree)
        tree.clear()
        if node is None:
            tree.root.set_label("(no agents)")
            tree.root.expand()
            return
        tree.root.set_label("agents")
        tree.root.expand()
        self._populate(tree.root, node)

    def _populate(self, parent: TreeNode, node: dict[str, Any]) -> None:
        addr_id = node.get("addr")
        # Honor a prior user collapse if the agent still exists;
        # otherwise default to expanded (new agents are visible).
        expand = not (isinstance(addr_id, str) and addr_id in self._collapsed)
        child = parent.add(
            _format_node_label(node, self._pulse_phase),
            data=addr_id,
            expand=expand,
        )
        for c in node.get("children", []):
            self._populate(child, c)

    def _update_labels(self, node: dict[str, Any] | None) -> None:
        """Walk the existing tree and update node labels in place for
        status changes. Does not touch expand/collapse state."""
        if node is None:
            return
        tree = self.query_one("#tree-pane", StatusTree)
        by_addr: dict[str, dict[str, Any]] = {}

        def collect(n: dict[str, Any]) -> None:
            addr = n.get("addr")
            if isinstance(addr, str):
                by_addr[addr] = n
            for c in n.get("children", []):
                collect(c)

        collect(node)

        def walk(tnode: TreeNode) -> None:
            if isinstance(tnode.data, str) and tnode.data in by_addr:
                new_label = _format_node_label(
                    by_addr[tnode.data], self._pulse_phase
                )
                # Only update if the label string changed — avoids
                # unnecessary repaints on quiet ticks.
                if str(tnode.label) != new_label:
                    tnode.set_label(new_label)
            for child in tnode.children:
                walk(child)

        walk(tree.root)

    @on(Tree.NodeCollapsed)
    def _on_node_collapsed(self, event: Tree.NodeCollapsed) -> None:
        addr = event.node.data
        if isinstance(addr, str):
            self._collapsed.add(addr)

    @on(Tree.NodeExpanded)
    def _on_node_expanded(self, event: Tree.NodeExpanded) -> None:
        addr = event.node.data
        if isinstance(addr, str):
            self._collapsed.discard(addr)

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
        try:
            reply = self.client.call("inbox", addr=addr_id)
        except Exception as exc:
            self.query_one("#preview-pane", Static).update(
                Text(f"control error: {exc}", style="red")
            )
            return
        preview = self.query_one("#preview-pane", Static)
        if not reply.get("ok"):
            preview.update(Text(reply.get("error", "?"), style="red"))
            return
        envs = reply.get("envelopes", [])
        if not envs:
            preview.update(
                Text(
                    f"(inbox empty for {self.selected_label or addr_id})",
                    style="dim",
                )
            )
            return
        # Build a Group of Text rows directly; no ANSI capture needed,
        # which removes a class of width / parsing bugs and avoids
        # Static's markup parser seeing escape codes.
        from rich.console import Group as _Group

        rows: list[Any] = [
            Text(f"inbox of {self.selected_label or addr_id}", style="bold"),
            Text(""),
        ]
        for env in envs[-12:]:
            sender = env.get("from_label") or env.get("from") or "?"
            body = env.get("body")
            body_repr = body if isinstance(body, str) else _short_repr(body, 200)
            row = Text()
            row.append(f"seq={env.get('seq')}  ", style="cyan")
            row.append(f"from={sender}  ", style="magenta")
            row.append(str(body_repr))
            rows.append(row)
        preview.update(_Group(*rows))

    # ---- selection actions ----

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        # Guard the whole handler: any exception here would propagate
        # into textual's event loop and crash the popup. Surface errors
        # in the preview pane instead so the user can keep navigating.
        try:
            addr_id = event.node.data
            if not isinstance(addr_id, str):
                return
            self.selected_addr = addr_id
            # ``node.label`` is a ``rich.Text``; ``.plain`` is the
            # rendered string without markup. Our labels look like
            # "✗ worker-3" — last token is the agent label.
            label_text = event.node.label.plain
            self.selected_label = label_text.split()[-1] if label_text else addr_id
            self.refresh_preview(addr_id)
        except Exception as exc:
            try:
                self.query_one("#preview-pane", Static).update(
                    Text(f"selection error: {exc}", style="red")
                )
            except Exception:
                pass

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


def _structure_signature(node: dict[str, Any] | None) -> tuple | None:
    """Return a stable hashable signature of the tree's structure
    (addresses + parent/child relations, *not* status). Used to detect
    when a rebuild is actually necessary."""
    if node is None:
        return None
    return (
        node.get("addr"),
        tuple(_structure_signature(c) for c in node.get("children", [])),
    )


def _format_node_label(node: dict[str, Any], pulse_phase: int = 0) -> str:
    status = node.get("status", "")
    if status in _PULSE_STYLES:
        style = _PULSE_STYLES[status][pulse_phase % _PULSE_STEPS]
    else:
        style = _STATIC_STYLES.get(status, "white")
    label_text = node.get("label") or node.get("addr") or "?"
    return f"[{style}]{_STATUS_ICON}[/] [bold]{label_text}[/]"


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
