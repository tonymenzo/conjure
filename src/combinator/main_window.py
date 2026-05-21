"""``combinator-main`` — the main TUI window.

The default tmux window (window 0) of every combinator session. A
lazyclaude/lazygit-style split: a sidebar with the spawn tree, the
selected agent's inbox, and a cost pane on the left, plus an
interactive chat pane (history + input) on the right.

Navigating the tree (arrow keys or click) swaps the chat pane in
place — same window, same sidebar, just a different agent. The
sidebar is collapsible (``F2``) for a wider chat view. Press ``o``
to open a dedicated fullscreen chat window for the selected agent
(created on-demand the first time, reused thereafter).

Keybindings:

- ``F2``                   — toggle sidebar visibility
- ``Tab`` / ``Shift+Tab``  — cycle focus between panes
- ``j`` / ``k`` / arrows   — navigate within the focused pane;
                              tree navigation swaps the chat pane
- ``o`` on tree node       — open / switch to the dedicated
                              fullscreen chat for that agent
- ``Esc`` in input         — focus the chat history (scroll mode)
- ``Enter`` in input       — send the line to the selected agent
- ``Ctrl+L``               — clear the input
- ``Ctrl+Q``               — quit (closes the window, daemon untouched)
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Sequence

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Header, Input, RichLog, Static, Tree
from textual.widgets.tree import TreeNode

from combinator.chat import (  # shared event-row renderers
    _format_event,
    _format_response_rows,
    _rewrite_stream,
    _user_rows,
)
from combinator.control import ControlClient
from combinator.daemon import list_session_names, socket_path_for
from combinator.event_log import tail


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

# Initial backlog of events to load when the chat pane swaps to a new
# agent. Keeps the swap fast even if the log is large.
_INITIAL_BACKLOG = 60


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="combinator-main")
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
            "combinator-main: could not locate a daemon socket "
            "(pass --socket or --session)",
            file=sys.stderr,
        )
        return 2
    if not socket_path.exists():
        print(f"combinator-main: socket not found: {socket_path}", file=sys.stderr)
        return 2

    tmux_session = args.session or _current_tmux_session()
    MainApp(socket_path=socket_path, tmux_session=tmux_session).run()
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


class MainApp(App):

    CSS = """
    Screen {
        background: $surface;
    }
    #sidebar {
        width: 32%;
    }
    #sidebar.hidden {
        display: none;
    }
    #tree-pane, #inbox-pane, #cost-pane {
        border: round $primary;
        background: $surface;
        padding: 0 1;
    }
    #tree-pane    { height: 50%; }
    #inbox-pane   { height: 30%; }
    #cost-pane    { height: 20%; }
    Tree {
        background: $surface;
        scrollbar-size: 1 1;
    }
    #tree-pane > .tree--cursor {
        background: $boost;
        color: $foreground;
        text-style: bold;
    }
    #tree-pane:focus > .tree--cursor {
        background: $primary 30%;
        color: $foreground;
        text-style: bold;
    }
    #tree-pane > .tree--highlight,
    #tree-pane > .tree--highlight-line {
        background: $surface;
    }
    #chat-history {
        background: $surface;
        padding: 0 1;
        scrollbar-size: 1 1;
        border: none;
    }
    #chat-history:focus {
        border: none;
    }
    #chat-input {
        dock: bottom;
        border: round $accent;
        margin: 0;
    }
    Header {
        background: $primary;
    }
    """

    BINDINGS = [
        Binding("f2", "toggle_sidebar", "Toggle sidebar"),
        Binding("ctrl+q", "quit", "Quit", show=False),
        Binding("escape", "focus_tree", "Focus tree"),
        Binding("o", "open_in_window", "Open in window"),
        Binding("r", "refresh", "Refresh", show=False),
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
        self.sub_title = f"session: {socket_path.stem}"

        # Selection state.
        self.selected_addr: str | None = None
        self.selected_label: str | None = None

        # Tree refresh-gating + collapse-preserve state, same approach
        # as the meta popup.
        self._tree_signature: tuple | None = None
        self._collapsed: set[str] = set()
        self._log_paths: dict[str, str] = {}
        self._addr_labels: dict[str, str] = {}
        # Track which agents already have a dedicated tmux window so
        # ``o`` can create-on-demand instead of failing with a missing
        # target.
        self._windowed_addrs: set[str] = set()

        # Chat-pane tailing state. Each swap stops the previous tail,
        # spawns a fresh one targeting the new agent's log.
        self._tail_stop: threading.Event | None = None
        self._tail_thread: threading.Thread | None = None
        # Track the last seq we rendered so resumed tails don't replay
        # the backlog.
        self._chat_last_seen_path: Path | None = None
        # Streaming state for the chat pane — chunks accumulate until
        # the engine emits ``stream_end``. ``_stream_marker`` is the
        # row count of the chat history at the moment the response
        # started; on each chunk we truncate back to it and rewrite
        # the in-progress response so the text grows top-down inside
        # the history (no separate streaming pane).
        self._stream_buffer: str = ""
        self._streaming: bool = False
        self._stream_marker: int = 0
        # Count of user echoes written locally that the tail hasn't yet
        # consumed. The tail decrements this for each ``user`` event it
        # sees and skips rendering — otherwise live submissions show up
        # twice (local echo + log replay). Backlog replay (where the
        # counter is 0) shows ``user`` events from the log directly.
        self._pending_user_echoes: int = 0

    # ----- compose -----

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Tree("spawn tree", id="tree-pane")
                yield Static("(select an agent to see its inbox)", id="inbox-pane")
                yield Static("(no costs yet)", id="cost-pane")
            with Vertical(id="main"):
                history = RichLog(
                    id="chat-history",
                    wrap=True,
                    markup=True,
                    highlight=False,
                    auto_scroll=True,
                )
                history.can_focus = False
                yield history
                yield Input(
                    placeholder="type a message — Enter to send to selected agent",
                    id="chat-input",
                )

    def on_mount(self) -> None:
        tree = self.query_one("#tree-pane", Tree)
        # The synthetic "agents" root would render its own expand
        # arrow next to iota's; hide it so the user sees only the
        # real agents (one arrow per agent that has children).
        tree.show_root = False
        # ``guides`` are the vertical lines tying parent → child;
        # the bold variant is too loud against the surface tone.
        tree.guide_depth = 2
        self.refresh_all()
        # 500ms tick: light enough that interaction feels live, heavy
        # enough that we're not spamming the control socket.
        self.set_interval(0.5, self.refresh_all)
        tree.focus()
        # Default selection: try to land on the root (iota) so the
        # chat pane shows something useful immediately.
        self._select_root_if_available()

    def on_unmount(self) -> None:
        self._stop_chat_tail()

    # ----- actions -----

    def action_toggle_sidebar(self) -> None:
        sidebar = self.query_one("#sidebar")
        sidebar.set_class(not sidebar.has_class("hidden"), "hidden")

    def action_focus_tree(self) -> None:
        self.query_one("#tree-pane", Tree).focus()

    def action_refresh(self) -> None:
        self.refresh_all()

    def action_open_in_window(self) -> None:
        """Open the selected agent in a dedicated fullscreen chat window.

        Per-agent windows are NOT auto-created on spawn — the main
        window's swappable chat pane is the primary interface. ``o``
        creates the window on first access (running ``combinator-chat``
        against the agent's log), then ``tmux select-window``s to it.
        Subsequent presses just select the existing window."""
        if not self.selected_addr or not self.selected_label or not self.tmux_session:
            return
        target = f"{self.tmux_session}:{self.selected_label}"
        if self.selected_addr not in self._windowed_addrs:
            if not self._spawn_chat_window(self.selected_addr, self.selected_label):
                return
            self._windowed_addrs.add(self.selected_addr)
        try:
            subprocess.run(
                ["tmux", "select-window", "-t", target],
                check=False,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _spawn_chat_window(self, addr: str, label: str) -> bool:
        """Spawn a ``combinator-chat`` window for ``addr`` in this tmux
        session. Returns True on success."""
        log_path_str = self._log_paths.get(addr)
        if not log_path_str:
            return False
        import shutil as _shutil
        chat_bin = _shutil.which("combinator-chat") or "combinator-chat"
        cmd = " ".join(
            shlex.quote(p) for p in [
                chat_bin,
                "--log", log_path_str,
                "--addr", addr,
                "--label", label,
                "--session", self.tmux_session or "",
            ]
        )
        try:
            subprocess.run(
                [
                    "tmux", "new-window",
                    "-t", self.tmux_session or "",
                    "-n", label,
                    "-d",  # don't auto-select; the caller does that
                    cmd,
                ],
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return True

    # ----- refresh -----

    def refresh_all(self) -> None:
        self.refresh_tree()
        self.refresh_cost()
        if self.selected_addr is not None:
            self.refresh_inbox(self.selected_addr)

    def refresh_tree(self) -> None:
        try:
            reply = self.client.call("tree")
        except Exception:
            return
        if not reply.get("ok"):
            return
        node = reply.get("tree")
        new_sig = _structure_signature(node)
        if new_sig == self._tree_signature:
            self._update_labels(node)
            return
        self._tree_signature = new_sig
        self._rebuild_tree(node)

    def _rebuild_tree(self, node: dict[str, Any] | None) -> None:
        tree = self.query_one("#tree-pane", Tree)
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
        log_path = node.get("log_path")
        label = node.get("label")
        if isinstance(addr_id, str):
            if isinstance(log_path, str):
                self._log_paths[addr_id] = log_path
            if isinstance(label, str):
                self._addr_labels[addr_id] = label
        expand = not (isinstance(addr_id, str) and addr_id in self._collapsed)
        child = parent.add(_format_node_label(node), data=addr_id, expand=expand)
        for c in node.get("children", []):
            self._populate(child, c)

    def _update_labels(self, node: dict[str, Any] | None) -> None:
        if node is None:
            return
        tree = self.query_one("#tree-pane", Tree)
        by_addr: dict[str, dict[str, Any]] = {}

        def collect(n: dict[str, Any]) -> None:
            addr = n.get("addr")
            if isinstance(addr, str):
                by_addr[addr] = n
                log_path = n.get("log_path")
                if isinstance(log_path, str):
                    self._log_paths[addr] = log_path
                label = n.get("label")
                if isinstance(label, str):
                    self._addr_labels[addr] = label
            for c in n.get("children", []):
                collect(c)

        collect(node)

        def walk(tnode: TreeNode) -> None:
            if isinstance(tnode.data, str) and tnode.data in by_addr:
                new_label = _format_node_label(by_addr[tnode.data])
                if str(tnode.label) != new_label:
                    tnode.set_label(new_label)
            for child in tnode.children:
                walk(child)

        walk(tree.root)

    def refresh_inbox(self, addr_id: str) -> None:
        try:
            reply = self.client.call("inbox", addr=addr_id)
        except Exception as exc:
            self.query_one("#inbox-pane", Static).update(
                Text(f"control error: {exc}", style="red")
            )
            return
        inbox_pane = self.query_one("#inbox-pane", Static)
        if not reply.get("ok"):
            inbox_pane.update(Text(reply.get("error", "?"), style="red"))
            return
        envs = reply.get("envelopes", [])
        from rich.console import Group as _Group

        rows: list[Any] = [
            Text(f"inbox of {self.selected_label or addr_id}", style="bold"),
            Text(""),
        ]
        if not envs:
            rows.append(Text("(empty)", style="dim"))
        else:
            for env in envs[-8:]:
                sender = env.get("from_label") or env.get("from") or "?"
                body = env.get("body")
                body_repr = body if isinstance(body, str) else _short_repr(body, 140)
                row = Text()
                row.append(f"seq={env.get('seq')}  ", style="cyan")
                row.append(f"from={sender}  ", style="magenta")
                row.append(str(body_repr))
                rows.append(row)
        inbox_pane.update(_Group(*rows))

    def refresh_cost(self) -> None:
        try:
            reply = self.client.call("cost")
        except Exception:
            return
        cost_pane = self.query_one("#cost-pane", Static)
        if not reply.get("ok"):
            cost_pane.update(Text(reply.get("error", "?"), style="red"))
            return
        total = reply.get("total", 0.0)
        rows: list[Any] = [Text("cost", style="bold")]
        for row in reply.get("rows", []):
            label = row.get("label") or row.get("addr") or "?"
            usd = row.get("cost", 0.0)
            rows.append(Text(f"  {label:<14}  {_format_usd(usd)}"))
        rows.append(Text(""))
        rows.append(Text(f"total {_format_usd(total)}", style="bold"))
        from rich.console import Group as _Group

        cost_pane.update(_Group(*rows))

    # ----- selection / chat pane swap -----

    def _select_root_if_available(self) -> None:
        """First tree population may not have happened yet at on_mount;
        try once, then retry on the first refresh tick if needed."""
        tree = self.query_one("#tree-pane", Tree)
        for top in tree.root.children:
            if isinstance(top.data, str):
                tree.select_node(top)
                return

    @on(Tree.NodeHighlighted)
    def _on_tree_highlighted(self, event: Tree.NodeHighlighted) -> None:
        """Arrow-navigation lands here. Swap the chat pane to the
        highlighted agent so navigating the tree is instantly visible
        in the chat. The selected-addr early-return in ``_swap_chat_to``
        makes repeated highlights of the same node a no-op."""
        try:
            addr_id = event.node.data
            if not isinstance(addr_id, str):
                return
            label = self._addr_labels.get(addr_id) or addr_id
            self.refresh_inbox(addr_id)
            self._swap_chat_to(addr=addr_id, label=label)
        except Exception:
            pass

    @on(Tree.NodeSelected)
    def _on_tree_selected(self, event: Tree.NodeSelected) -> None:
        """Enter (or click) on a tree node — same swap as highlight."""
        try:
            addr_id = event.node.data
            if not isinstance(addr_id, str):
                return
            label = self._addr_labels.get(addr_id) or addr_id
            self._swap_chat_to(addr=addr_id, label=label)
        except Exception as exc:
            self.query_one("#chat-history", RichLog).write(
                Text(f"selection error: {exc}", style="red")
            )

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

    def _swap_chat_to(self, *, addr: str, label: str) -> None:
        """Bind the chat pane to a new agent: stop the old tail,
        clear the history, replay the agent's recent backlog, then
        start a fresh tail."""
        if addr == self.selected_addr:
            return
        self.selected_addr = addr
        self.selected_label = label

        history = self.query_one("#chat-history", RichLog)
        history.clear()
        # Drop any in-flight streaming state from the previous agent so
        # incoming chunks for that agent (which we'll ignore via the
        # ``label`` check in ``_render_into_chat``) don't leak into the
        # new agent's view.
        self._stream_buffer = ""
        self._streaming = False
        self._stream_marker = 0
        self._pending_user_echoes = 0

        log_path_str = self._log_paths.get(addr)
        if not log_path_str:
            history.write(Text("(no log path yet for this agent)", style="dim"))
            return
        log_path = Path(log_path_str)
        self._stop_chat_tail()
        self._render_backlog(log_path, label, history)
        self._start_chat_tail(log_path, label)

    def _render_backlog(self, log_path: Path, label: str, history: RichLog) -> None:
        """Replay the agent's recent events into the chat history.

        Streaming responses arrive as a sequence of ``chunk`` events
        followed by ``stream_end``; ``_format_event`` doesn't know how
        to render those on its own. Accumulate them here and emit a
        single response row at ``stream_end`` so the chat-history
        replay shows what the user saw live."""
        if not log_path.exists():
            return
        try:
            content = log_path.read_text(encoding="utf-8")
        except OSError:
            return
        lines = content.splitlines()[-_INITIAL_BACKLOG:]
        chunk_buffer = ""
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("kind")
            if kind == "chunk":
                chunk_buffer += event.get("text", "") or ""
                continue
            if kind == "stream_end":
                for row in _format_response_rows(
                    label, chunk_buffer, event.get("tool_calls", []) or []
                ):
                    history.write(row)
                chunk_buffer = ""
                continue
            for row in _format_event(label, event):
                history.write(row)
        # A trailing chunk with no stream_end (mid-stream snapshot)
        # would otherwise be dropped silently — flush it as text.
        if chunk_buffer:
            for row in _format_response_rows(label, chunk_buffer, []):
                history.write(row)

    def _start_chat_tail(self, log_path: Path, label: str) -> None:
        stop = threading.Event()
        self._tail_stop = stop
        self._chat_last_seen_path = log_path
        initial_offset = log_path.stat().st_size if log_path.exists() else 0

        def reader() -> None:
            # Open and seek past the already-rendered backlog before
            # entering ``tail`` so we don't double-render.
            try:
                with log_path.open("r", encoding="utf-8") as fh:
                    fh.seek(initial_offset)
                    while not stop.is_set():
                        line = fh.readline()
                        if not line:
                            if stop.is_set():
                                return
                            stop.wait(0.05)
                            continue
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        self.call_from_thread(self._render_into_chat, label, event)
            except OSError:
                return

        t = threading.Thread(
            target=reader, daemon=True, name=f"chat-tail-{label}"
        )
        self._tail_thread = t
        t.start()

    def _render_into_chat(self, label: str, event: dict[str, Any]) -> None:
        # Only render if the user hasn't swapped to a different agent
        # while this tail was producing events.
        if label != self.selected_label:
            return
        history = self.query_one("#chat-history", RichLog)
        kind = event.get("kind")
        if kind == "chunk":
            self._on_chunk(label, event.get("text", "") or "")
            return
        if kind == "stream_end":
            self._on_stream_end(label, event.get("tool_calls", []) or [])
            return
        if kind == "user_input" and self._pending_user_echoes > 0:
            # We already wrote a local echo for this submission; don't
            # render it a second time from the log tail.
            self._pending_user_echoes -= 1
            return
        try:
            for row in _format_event(label, event):
                history.write(row)
        except Exception as exc:
            history.write(Text(f"render error: {exc}", style="red"))

    def _on_chunk(self, label: str, text: str) -> None:
        if not text:
            return
        history = self.query_one("#chat-history", RichLog)
        if not self._streaming:
            self._streaming = True
            self._stream_buffer = ""
            self._stream_marker = len(history.lines)
        self._stream_buffer += text
        _rewrite_stream(history, label, self._stream_buffer, [], self._stream_marker)

    def _on_stream_end(self, label: str, tool_calls: list[dict[str, Any]]) -> None:
        history = self.query_one("#chat-history", RichLog)
        if self._stream_buffer or tool_calls:
            _rewrite_stream(
                history, label, self._stream_buffer, tool_calls, self._stream_marker
            )
        self._stream_buffer = ""
        self._streaming = False
        self._stream_marker = 0

    def _stop_chat_tail(self) -> None:
        if self._tail_stop is not None:
            self._tail_stop.set()
        self._tail_stop = None
        self._tail_thread = None

    # ----- chat input -----

    @on(Input.Submitted)
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        text = (event.value or "").strip()
        event.input.value = ""
        if not text or not self.selected_addr:
            return
        history = self.query_one("#chat-history", RichLog)
        for row in _user_rows(text):
            history.write(row)
        # The daemon will write a matching ``user_input`` event into
        # the agent's log; the tail must skip it so the local echo
        # doesn't duplicate.
        self._pending_user_echoes += 1
        try:
            reply = self.client.call("send", addr=self.selected_addr, body=text)
        except Exception as exc:
            self._pending_user_echoes = max(0, self._pending_user_echoes - 1)
            history.write(Text(f"send failed: {exc}", style="red"))
            return
        if not reply.get("ok"):
            self._pending_user_echoes = max(0, self._pending_user_echoes - 1)
            history.write(
                Text(f"send rejected: {reply.get('error', '?')}", style="red")
            )


# ---- helpers ----

def _structure_signature(node: dict[str, Any] | None) -> tuple | None:
    if node is None:
        return None
    return (
        node.get("addr"),
        tuple(_structure_signature(c) for c in node.get("children", [])),
    )


def _format_node_label(node: dict[str, Any]) -> str:
    icon = _STATUS_ICON.get(node.get("status", ""), "?")
    style = _STATUS_STYLE.get(node.get("status", ""), "white")
    label_text = node.get("label") or node.get("addr") or "?"
    return f"[{style}]{icon}[/] [bold]{label_text}[/]"


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
