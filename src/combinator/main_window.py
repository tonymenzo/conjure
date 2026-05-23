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
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Header, Input, Static, Tree
from textual.widgets.tree import TreeNode

from combinator.chat import ChatView
from combinator.control import ControlClient
from combinator.daemon import list_session_names, socket_path_for
from combinator.status_tree import StatusTree
from combinator.tui_select import set_mouse_tracking


# Status icon = filled circle in every state; only the color
# communicates the agent's life-cycle. "active" states (lazy,
# running, error) pulse via a 4-step brightness cycle driven by a
# 250ms timer — smoother than the terminal's hard blink attribute
# and consistent across terminals. ``idle`` (done with current
# work) and ``terminated`` (finished) are solid.
_STATUS_ICON = "●"
_PULSE_STEPS = 4
_PULSE_INTERVAL = 0.25  # seconds per step → 1 Hz full cycle
_PULSE_STYLES = {
    # The 4-step pattern goes bold → normal → dim → normal so it
    # reads as a continuous up-down pulse rather than a sharp blink.
    "lazy":    ["bold green",  "green",  "dim green",  "green"],
    "running": ["bold yellow", "yellow", "dim yellow", "yellow"],
    "error":   ["bold red",    "red",    "dim red",    "red"],
    # Magenta pulse = "needs your attention" — a tool is blocked
    # waiting for a permission decision.
    "awaiting_permission": [
        "bold magenta", "magenta", "dim magenta", "magenta",
    ],
}
_STATIC_STYLES = {
    "idle": "green",
    "terminated": "dim",
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
        background: ansi_default;
        scrollbar-size: 1 1;
        scrollbar-background: ansi_default;
        scrollbar-background-hover: ansi_default;
        scrollbar-background-active: ansi_default;
        scrollbar-color: $primary-darken-2;
        scrollbar-color-hover: $primary;
        scrollbar-color-active: $accent;
    }
    #sidebar {
        width: 32%;
    }
    #sidebar.hidden {
        display: none;
    }
    #tree-pane, #activity-pane {
        border: round #00FF41;
        background: ansi_default;
        padding: 0 1;
    }
    /* Focused pane: white border + a translucent overlay so the
       active panel is unmistakable. The activity feed is
       informational only (not focusable, see below) so only the
       tree pane actually goes white in practice. */
    #tree-pane:focus {
        border: round white;
        background: $boost;
    }
    #tree-pane     { height: 45%; }
    #activity-pane {
        height: 55%;
        scrollbar-size: 1 1;
        scrollbar-gutter: stable;
    }
    #activity-content { height: auto; }
    Tree {
        background: ansi_default;
        scrollbar-size: 1 1;
    }
    #tree-pane > .tree--cursor {
        background: ansi_default;
        text-style: underline;
    }
    #tree-pane:focus > .tree--cursor {
        background: ansi_default;
        text-style: bold underline;
    }
    #tree-pane > .tree--highlight,
    #tree-pane > .tree--highlight-line {
        background: ansi_default;
    }
    ChatView {
        background: ansi_default;
        scrollbar-size: 1 1;
        border: none;
    }
    #context-bar {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $surface-darken-1;
        color: $foreground;
    }
    #perm-banner {
        dock: bottom;
        height: auto;
        padding: 0 1;
        background: ansi_default;
        color: $foreground;
        display: none;
        border: round magenta;
        text-style: bold;
    }
    #perm-banner.active {
        display: block;
    }
    #chat-input {
        dock: bottom;
        border: round #00FF41;
        background: ansi_default;
        margin: 0;
    }
    #chat-input:focus {
        border: round white;
    }
    Header {
        background: #1B4D3E;
    }
    """

    BINDINGS = [
        Binding("f2", "toggle_sidebar", "Toggle sidebar"),
        Binding("f3", "permission_allow", "Allow", show=False),
        Binding("f4", "permission_deny", "Deny", show=False),
        Binding("f5", "toggle_select_mode", "Select"),
        Binding("f6", "toggle_internal", "Show internal", show=False),
        Binding("ctrl+q", "quit", "Quit", show=False),
        Binding("escape", "focus_tree", "Focus tree"),
        Binding("o", "open_in_window", "Open in window"),
        Binding("r", "refresh", "Refresh", show=False),
        # Both F6 and Ctrl+G map to the same auto-mode toggle. Some
        # terminals / tmux configs intercept F6, so the Ctrl+G fallback
        # ensures the toggle is always reachable.
        Binding("f6", "toggle_auto_mode", "Auto", show=True),
        Binding("ctrl+g", "toggle_auto_mode", "Auto", show=False),
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
        # Per-agent model name + context-window usage, captured during
        # tree walk. Used by the context bar at the bottom of the
        # chat pane and by the header subtitle.
        self._addr_models: dict[str, str] = {}
        self._addr_context: dict[str, tuple[int, int]] = {}
        # Track which agents already have a dedicated tmux window so
        # ``o`` can create-on-demand instead of failing with a missing
        # target.
        self._windowed_addrs: set[str] = set()

        # Chat-pane tailing state. Each swap stops the previous tail,
        # spawns a fresh one targeting the new agent's log.
        self._tail_stop: threading.Event | None = None
        self._tail_thread: threading.Thread | None = None
        # Count of user echoes written locally that the tail hasn't yet
        # consumed. The tail decrements this for each ``user_input``
        # event it sees and skips rendering — otherwise live
        # submissions would show twice (local echo + log replay).
        self._pending_user_echoes: int = 0
        # Pulse animation state for active agent status dots. The
        # pulse timer increments this every ``_PULSE_INTERVAL`` and
        # triggers a label refresh for tree rows whose status pulses.
        self._pulse_phase: int = 0
        # Hide substrate-spawned plumbing agents (collectors, etc.)
        # from the tree by default. F6 toggles them on for debugging.
        self._show_internal: bool = False
        # Cache the most recent tree node dict so the pulse tick can
        # refresh labels without re-fetching from the daemon.
        self._last_tree: dict[str, Any] | None = None
        # Guards re-entrancy on ``refresh_all`` — if the previous
        # snapshot hasn't returned yet, the next tick is a no-op.
        # Keeps the UI thread responsive while the daemon is busy.
        self._snapshot_in_flight: bool = False
        # Diff-check fingerprints so we only call Static.update()
        # when the data actually changed. The chat input lives on
        # the same event loop as the panes, so a redundant update
        # every tick burns frame-budget that should be servicing
        # keystrokes.
        self._activity_signature: tuple | None = None
        self._permissions_signature: tuple | None = None
        # Latest cost values, written by ``_apply_cost`` and read by
        # ``_refresh_context_bar`` so the bottom status gutter shows
        # cost + model + context usage as a single line.
        self._cost_total: float | None = None
        self._cost_has_sub: bool = False
        self._context_signature: tuple | None = None
        # Currently-displayed permission request for the selected
        # agent (None when no pending request). F3/F4 resolve this.
        self._active_perm: dict[str, Any] | None = None

    # ----- compose -----

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal():
            with Vertical(id="sidebar"):
                yield StatusTree("spawn tree", id="tree-pane")
                # Activity-pane is a scrollable container so the feed
                # can hold more rows than fit on screen; the actual
                # content widget lives one level deeper. Set
                # ``can_focus = False`` so Tab/Shift+Tab skip it —
                # this pane is read-only ambient information, not
                # something the user navigates into.
                activity_pane = VerticalScroll(id="activity-pane")
                activity_pane.can_focus = False
                with activity_pane:
                    yield Static("(no activity yet)", id="activity-content")
            with Vertical(id="main"):
                yield ChatView(id="chat-history")
                yield Static("", id="perm-banner")
                yield Input(
                    placeholder="type a message — Enter to send to selected agent",
                    id="chat-input",
                )
        # Bottom status gutter — full width, below the sidebar/chat
        # split. Renders cost + selected agent's model + the context-
        # window meter on a single line.
        yield Static("", id="context-bar")

    def on_mount(self) -> None:
        tree = self.query_one("#tree-pane", StatusTree)
        # The synthetic "agents" root would render its own expand
        # arrow next to iota's; hide it so the user sees only the
        # real agents (one arrow per agent that has children).
        tree.show_root = False
        # ``guides`` are the vertical lines tying parent → child;
        # the bold variant is too loud against the surface tone.
        tree.guide_depth = 2
        self.refresh_all()
        # 1Hz tick: snapshot RPC + UI applies happen on the same
        # event loop as keystrokes, so we keep this conservative.
        # The 250ms pulse timer (separate) keeps status icons live.
        self.set_interval(1.0, self.refresh_all)
        # Smooth status-dot pulse for active agents.
        self.set_interval(_PULSE_INTERVAL, self._tick_pulse)
        # Pre-select iota in the tree (drives the chat pane) but land
        # the cursor in the input box so the user can just start typing.
        self._select_root_if_available()
        self.query_one("#chat-input", Input).focus()

    def on_unmount(self) -> None:
        self._stop_chat_tail()

    # ----- actions -----

    def action_toggle_select_mode(self) -> None:
        """Release / re-acquire textual's mouse capture so the user
        can click-drag-select text natively. Header subtitle gets a
        ``[select]`` tag while active."""
        self._select_mode = not getattr(self, "_select_mode", False)
        set_mouse_tracking(self, on=not self._select_mode)
        self._update_subtitle()

    def action_toggle_sidebar(self) -> None:
        sidebar = self.query_one("#sidebar")
        sidebar.set_class(not sidebar.has_class("hidden"), "hidden")

    def action_toggle_internal(self) -> None:
        self._show_internal = not self._show_internal
        self._rebuild_tree(self._last_tree)

    def action_focus_tree(self) -> None:
        self.query_one("#tree-pane", StatusTree).focus()

    def action_refresh(self) -> None:
        self.refresh_all()

    def action_toggle_auto_mode(self) -> None:
        """Flip the daemon's ``auto_mode`` flag. When on, ``ask``
        tool decisions are auto-allowed without surfacing the perm-
        banner — the "trust me, just go" mode for fast iteration.
        Status is reflected in the bottom gutter (``AUTO``) and a
        transient toast confirms the toggle so the user knows the
        keybinding actually fired (some tmux configs intercept F6;
        if the toast doesn't appear, try ``Ctrl+G``).
        """
        new_state = not bool(getattr(self, "_auto_mode", False))
        try:
            reply = self.client.call("set_auto_mode", on=new_state)
        except Exception as exc:
            self.notify(
                f"auto-mode toggle failed: {exc}",
                severity="error",
            )
            return
        if not reply.get("ok"):
            self.notify(
                f"daemon rejected set_auto_mode: {reply.get('error', '?')} "
                "— restart the daemon to pick up the new RPC.",
                severity="warning",
            )
            return
        self._auto_mode = bool(reply.get("on"))
        # Force the gutter to repaint with the new flag.
        self._context_signature = None
        self._refresh_context_bar()
        self.notify(
            f"auto-mode {'ON' if self._auto_mode else 'OFF'}",
            severity="information",
            timeout=2,
        )

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
        """Kick off a snapshot fetch in the background. Returns
        immediately — the textual event loop must NOT block on the
        socket round-trip or keystrokes feel laggy. When the reply
        lands, ``_apply_snapshot`` runs on the UI thread to update
        the panes."""
        if self._snapshot_in_flight:
            return
        self._snapshot_in_flight = True
        addr = self.selected_addr
        threading.Thread(
            target=self._fetch_snapshot,
            args=(addr,),
            daemon=True,
            name="snapshot-fetch",
        ).start()

    def _fetch_snapshot(self, addr: str | None) -> None:
        try:
            reply = self.client.call("snapshot", addr=addr)
        except Exception:
            self._snapshot_in_flight = False
            return
        try:
            self.call_from_thread(self._apply_snapshot, reply)
        except Exception:
            self._snapshot_in_flight = False

    def _apply_snapshot(self, reply: dict[str, Any]) -> None:
        try:
            if not reply.get("ok"):
                return
            self._apply_tree(reply.get("tree"))
            self._apply_cost(reply.get("cost") or {})
            self._auto_mode = bool(reply.get("auto_mode"))
            # Merge envelope activity + tool events on a single
            # time-sorted feed so the activity pane shows the full
            # picture (agent ↔ agent traffic + each agent's local
            # tool invocations) rather than just messages.
            merged = _merge_feed(
                reply.get("activity") or [],
                reply.get("log_events") or [],
            )
            self._apply_activity(merged)
            self._apply_permissions(reply.get("pending_permissions") or [])
            self._apply_context(reply.get("context"))
            self._refresh_context_bar()
            # Tree is populated now. If we still don't have a
            # selected agent (the on_mount auto-select ran before
            # the async snapshot returned), pick root.
            if self.selected_addr is None:
                self._select_root_if_available()
        finally:
            self._snapshot_in_flight = False

    def _apply_context(self, ctx: dict[str, Any] | None) -> None:
        if not self.selected_addr:
            return
        if not isinstance(ctx, dict):
            self._addr_context.pop(self.selected_addr, None)
            return
        used, total = ctx.get("used"), ctx.get("max")
        if isinstance(used, int) and isinstance(total, int) and total > 0:
            self._addr_context[self.selected_addr] = (used, total)

    def _apply_tree(self, node: dict[str, Any] | None) -> None:
        # Cache the latest tree so the pulse tick can refresh labels
        # without a fresh daemon round-trip.
        self._last_tree = node
        new_sig = _structure_signature(node)
        if new_sig == self._tree_signature:
            self._update_labels(node)
            return
        # Topology changed — auto-expand any branch where a new agent
        # appeared, so newly-spawned children pop into view even if
        # the user had collapsed the parent earlier.
        prev_addrs = set(self._addr_labels)
        for parent_addr in _parents_with_new_children(node, prev_addrs):
            self._collapsed.discard(parent_addr)
        self._tree_signature = new_sig
        self._rebuild_tree(node)

    def _tick_pulse(self) -> None:
        """Advance the pulse phase and refresh tree labels so active
        agents (lazy/running/error) animate smoothly. No daemon call —
        we use the cached tree from the last snapshot tick."""
        self._pulse_phase = (self._pulse_phase + 1) % _PULSE_STEPS
        if self._last_tree is not None:
            self._update_labels(self._last_tree)

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
        if node.get("internal") and not self._show_internal:
            return
        addr_id = node.get("addr")
        log_path = node.get("log_path")
        label = node.get("label")
        if isinstance(addr_id, str):
            if isinstance(log_path, str):
                self._log_paths[addr_id] = log_path
            if isinstance(label, str):
                self._addr_labels[addr_id] = label
            self._cache_extras(addr_id, node)
        expand = not (isinstance(addr_id, str) and addr_id in self._collapsed)
        child = parent.add(
            _format_node_label(node, self._pulse_phase),
            data=addr_id,
            expand=expand,
        )
        for c in node.get("children", []):
            self._populate(child, c)

    def _update_labels(self, node: dict[str, Any] | None) -> None:
        if node is None:
            return
        tree = self.query_one("#tree-pane", StatusTree)
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
                self._cache_extras(addr, n)
            for c in n.get("children", []):
                collect(c)

        collect(node)

        def walk(tnode: TreeNode) -> None:
            if isinstance(tnode.data, str) and tnode.data in by_addr:
                new_label = _format_node_label(
                    by_addr[tnode.data], self._pulse_phase
                )
                if str(tnode.label) != new_label:
                    tnode.set_label(new_label)
            for child in tnode.children:
                walk(child)

        walk(tree.root)

    def _update_subtitle(self) -> None:
        """Header subtitle reflects the selected agent + its model.
        When select mode is on, append a yellow indicator so the user
        knows clicks won't be intercepted."""
        base = f"session: {self.socket_path.stem}"
        if self.selected_addr:
            label = self.selected_label or self.selected_addr
            model = self._addr_models.get(self.selected_addr)
            if model:
                base = f"{base}  ·  {label}  ·  {model}"
            else:
                base = f"{base}  ·  {label}"
        self._base_sub_title = base
        if getattr(self, "_select_mode", False):
            self.sub_title = f"{base}  [bold yellow][select — F5 to exit][/]"
        else:
            self.sub_title = base

    def _cache_extras(self, addr_id: str, node: dict[str, Any]) -> None:
        """Pluck the per-agent ``model`` off a tree node. Context
        usage is fetched separately for the selected agent only (in
        ``refresh_all``) to keep the tick cost bounded."""
        model = node.get("model")
        if isinstance(model, str) and model:
            self._addr_models[addr_id] = model

    def _refresh_context_bar(self) -> None:
        """Render the unified status gutter at the bottom of the
        screen: cost + model (+ auto-mode flag) on the left, context-
        window meter pinned to the right via a two-column
        ``Table.grid``. Diff-checked so the gutter doesn't repaint
        every tick when nothing changed."""
        addr = self.selected_addr
        model = self._addr_models.get(addr) if addr else None
        ctx = self._addr_context.get(addr) if addr else None
        cost = getattr(self, "_cost_total", None)
        has_sub = bool(getattr(self, "_cost_has_sub", False))
        auto_mode = bool(getattr(self, "_auto_mode", False))
        sig = (
            addr, model, ctx,
            round(cost, 6) if cost is not None else None,
            has_sub, auto_mode,
        )
        if sig == self._context_signature:
            return
        self._context_signature = sig
        bar = self.query_one("#context-bar", Static)

        left = Text()
        if cost is not None:
            left.append_text(Text.from_markup(_format_usd(cost)))
            if has_sub:
                left.append(" *", style="bold magenta")
        if model:
            if len(left):
                left.append("   ·   ", style="dim")
            left.append(model, style="dim cyan")
        if auto_mode:
            if len(left):
                left.append("   ·   ", style="dim")
            left.append("AUTO", style="bold yellow")

        right = _render_token_bar(*ctx) if ctx is not None else Text("")

        # Two-column grid: left flexes, right hugs its content and
        # is right-justified so the token meter pins to the gutter's
        # right edge regardless of width.
        from rich.table import Table as _Table

        grid = _Table.grid(expand=True)
        grid.add_column(justify="left", no_wrap=True, overflow="ellipsis")
        grid.add_column(justify="right", no_wrap=True)
        grid.add_row(left, right)
        bar.update(grid)
        # Refresh subtitle too — the model may have just become known.
        self._update_subtitle()

    def _apply_permissions(self, pending: list[dict[str, Any]]) -> None:
        """Show the first pending permission for the selected agent
        as a banner above the input. Diff-checked."""
        if self.selected_addr is None:
            mine: list[dict[str, Any]] = []
        else:
            mine = [p for p in pending if p.get("addr") == self.selected_addr]
        first = mine[0] if mine else None
        sig = (
            first.get("req_id") if first else None,
            first.get("tool_name") if first else None,
        )
        if sig == self._permissions_signature:
            self._active_perm = first
            return
        self._permissions_signature = sig
        banner = self.query_one("#perm-banner", Static)
        if first is None:
            self._active_perm = None
            banner.set_class(False, "active")
            banner.update("")
            return
        self._active_perm = first
        args_preview = _args_preview(first.get("args") or {})
        body = Text()
        body.append("PERMISSION REQUEST  ", style="bold magenta")
        body.append(first.get("tool_name", "?"), style="bold cyan")
        body.append(f"({args_preview})", style="dim cyan")
        body.append("    ")
        body.append("[F3] allow", style="bold green")
        body.append("    ")
        body.append("[F4] deny", style="bold red")
        banner.update(body)
        banner.set_class(True, "active")

    def action_permission_allow(self) -> None:
        self._resolve_active_permission("allow")

    def action_permission_deny(self) -> None:
        self._resolve_active_permission("deny")

    def _resolve_active_permission(self, decision: str) -> None:
        req = self._active_perm
        if not req:
            return
        try:
            self.client.call(
                "resolve_permission",
                req_id=req.get("req_id"),
                decision=decision,
            )
        except Exception:
            pass
        # Hide the banner immediately — the next snapshot will confirm.
        self._active_perm = None
        banner = self.query_one("#perm-banner", Static)
        banner.set_class(False, "active")
        banner.update("")

    def _apply_activity(self, rows: list[dict[str, Any]]) -> None:
        """Unified activity feed. Each row is one of:

        - ``_kind == "envelope"`` — a message between agents:
          ``ts  src → dst  [kind]  body``
        - ``_kind == "tool_call"`` — an agent invoked a tool:
          ``ts  src    ● ToolName``
        - ``_kind == "tool_result"`` — a tool returned (success/fail):
          ``ts  src    ⎿ ✓`` / ``⎿ ✗ summary``

        All rows share the same ``ts`` / sender padding so the eye
        scans the same column for *who is doing what*. Sender labels
        keep their type colors (cyan @user / magenta agent / dim
        @system); body + metadata are dim; ``[error]`` and failed
        tool results are the only loud colors. Diff-checked."""
        sig = tuple(
            (r.get("ts"), r.get("from"), r.get("to"), r.get("_kind"),
             r.get("tool"), r.get("failed"), r.get("in_reply_to"))
            for r in rows
        )
        if sig == self._activity_signature:
            return
        self._activity_signature = sig
        content = self.query_one("#activity-content", Static)
        pane = self.query_one("#activity-pane", VerticalScroll)
        if not rows:
            content.update(Text("(no activity yet)", style="dim"))
            return
        import time as _time
        from rich.console import Group as _Group

        now = _time.time()
        max_src = min(
            12,
            max(
                len(r.get("from_label") or r.get("from") or "?")
                for r in rows
            ),
        )
        lines: list[Any] = []
        for r in rows:
            kind = r.get("_kind", "envelope")
            if kind == "envelope":
                lines.append(_render_envelope_row(r, max_src, now))
            elif kind == "tool_call":
                lines.append(_render_tool_call_row(r, max_src, now))
            elif kind == "tool_result":
                lines.append(_render_tool_result_row(r, max_src, now))
            # Unknown kinds are silently dropped — better than
            # rendering garbage into the feed.
        content.update(_Group(*lines))
        pane.call_after_refresh(pane.scroll_end, animate=False)

    def _apply_cost(self, cost: dict[str, Any]) -> None:
        """Stash the latest cost into instance state; the gutter
        (``_refresh_context_bar``) does the actual rendering."""
        self._cost_total = float(cost.get("total", 0.0) or 0.0)
        self._cost_has_sub = bool(cost.get("has_subscription_agent"))

    # ----- selection / chat pane swap -----

    def _select_root_if_available(self) -> None:
        """First tree population may not have happened yet at on_mount;
        try once, then retry on the first refresh tick if needed."""
        tree = self.query_one("#tree-pane", StatusTree)
        for top in tree.root.children:
            if isinstance(top.data, str):
                tree.select_node(top)
                return

    @on(Tree.NodeHighlighted)
    def _on_tree_highlighted(self, event: Tree.NodeHighlighted) -> None:
        """Arrow-navigation lands here. Swap the chat pane to the
        highlighted agent so navigating the tree is instantly visible
        in the chat. The selected-addr early-return in ``_swap_chat_to``
        makes repeated highlights of the same node a no-op. The next
        ``refresh_all`` tick picks up the new addr's inbox; we also
        trigger one immediately for snappier feedback."""
        try:
            addr_id = event.node.data
            if not isinstance(addr_id, str):
                return
            label = self._addr_labels.get(addr_id) or addr_id
            self._swap_chat_to(addr=addr_id, label=label)
            self.refresh_all()
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
            self.query_one(ChatView).write_error(f"selection error: {exc}")

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
        clear ChatView, replay the agent's recent backlog, then start
        a fresh tail.

        Snappy refresh: re-render the bottom gutter immediately so
        the model + cached context bar flip in the same frame as the
        swap. ``refresh_all`` is also triggered (by the caller) to
        pull a fresh context-window reading from the daemon."""
        if addr == self.selected_addr:
            return
        self.selected_addr = addr
        self.selected_label = label
        self._pending_user_echoes = 0
        self._update_subtitle()
        # Force the gutter to repaint for the new agent right away.
        # Reset the diff signature so any cached values for the new
        # addr land in the same frame as the tree highlight.
        self._context_signature = None
        self._refresh_context_bar()

        view = self.query_one(ChatView)
        view.reset()

        log_path_str = self._log_paths.get(addr)
        if not log_path_str:
            view.write_error("(no log path yet for this agent)")
            return
        log_path = Path(log_path_str)
        self._stop_chat_tail()
        self._render_backlog(log_path, label, view)
        self._start_chat_tail(log_path, label)

    def _render_backlog(self, log_path: Path, label: str, view: ChatView) -> None:
        """Read the recent N events from disk and let ChatView replay
        them (which handles chunk accumulation and mounts blocks)."""
        if not log_path.exists():
            return
        try:
            content = log_path.read_text(encoding="utf-8")
        except OSError:
            return
        events: list[dict[str, Any]] = []
        for line in content.splitlines()[-_INITIAL_BACKLOG:]:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        view.replay_events(label, events)

    def _start_chat_tail(self, log_path: Path, label: str) -> None:
        stop = threading.Event()
        self._tail_stop = stop
        initial_offset = log_path.stat().st_size if log_path.exists() else 0

        def reader() -> None:
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
                        self.call_from_thread(self._on_tail_event, label, event)
            except OSError:
                return

        t = threading.Thread(
            target=reader, daemon=True, name=f"chat-tail-{label}"
        )
        self._tail_thread = t
        t.start()

    def _on_tail_event(self, label: str, event: dict[str, Any]) -> None:
        # Only render if the user hasn't swapped to a different agent
        # while this tail was producing events.
        if label != self.selected_label:
            return
        if event.get("kind") == "user_input" and self._pending_user_echoes > 0:
            self._pending_user_echoes -= 1
            return
        # thinking / usage / chunk / stream_end all flow through
        # ChatView.apply_event — the in-flow TurnHeader block manages
        # its own lifecycle from these events.
        view = self.query_one(ChatView)
        try:
            view.apply_event(label, event)
        except Exception as exc:
            view.write_error(f"render error: {exc}")

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
        view = self.query_one(ChatView)
        view.echo_user(text)
        # The daemon will write a matching ``user_input`` event into
        # the agent's log; the tail must skip it so the local echo
        # doesn't duplicate.
        self._pending_user_echoes += 1
        try:
            reply = self.client.call("send", addr=self.selected_addr, body=text)
        except Exception as exc:
            self._pending_user_echoes = max(0, self._pending_user_echoes - 1)
            view.write_error(f"send failed: {exc}")
            return
        if not reply.get("ok"):
            self._pending_user_echoes = max(0, self._pending_user_echoes - 1)
            view.write_error(f"send rejected: {reply.get('error', '?')}")


# ---- helpers ----

def _parents_with_new_children(
    node: dict[str, Any] | None, known_addrs: set[str]
) -> set[str]:
    """Walk ``node`` and collect every addr whose tree slot contains a
    child that wasn't present in ``known_addrs``. Used to auto-expand
    branches when new agents spawn."""
    parents: set[str] = set()
    if node is None:
        return parents

    def walk(n: dict[str, Any], parent_addr: str | None) -> None:
        addr = n.get("addr")
        if isinstance(addr, str) and addr not in known_addrs and parent_addr:
            parents.add(parent_addr)
        carrier = addr if isinstance(addr, str) else parent_addr
        for child in n.get("children", []):
            walk(child, carrier)

    walk(node, None)
    return parents


def _structure_signature(node: dict[str, Any] | None) -> tuple | None:
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


def _truncate(s: str, n: int) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _render_token_bar(used: int, total: int) -> Text:
    """A 16-cell progress bar + token counts. Color shifts from green
    (under 70%) to yellow (70-90%) to red (over 90%) — 70% is where
    Claude Code's compaction typically kicks in, so the visual
    matches the same intuition."""
    if total <= 0:
        return Text("")
    pct = max(0.0, min(1.0, used / total))
    width = 16
    filled = int(round(pct * width))
    if pct < 0.7:
        color = "green"
    elif pct < 0.9:
        color = "yellow"
    else:
        color = "red"
    bar = Text()
    bar.append("[", style="dim")
    bar.append("█" * filled, style=color)
    bar.append("░" * (width - filled), style="dim")
    bar.append("] ", style="dim")
    bar.append(f"{_fmt_tokens(used)}/{_fmt_tokens(total)}", style="dim")
    bar.append(f" ({pct * 100:.0f}%)", style=f"dim {color}")
    return bar


def _fmt_tokens(n: int) -> str:
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _args_preview(args: dict[str, Any]) -> str:
    """One-line preview of tool args for the permission banner."""
    if not args:
        return ""
    parts = []
    for k, v in args.items():
        s = str(v) if isinstance(v, str) else repr(v)
        parts.append(f"{k}={_truncate(s, 60)}")
    return ", ".join(parts)


def _render_envelope_row(
    r: dict[str, Any], max_src: int, now: float
) -> Text:
    """``ts  src → dst  [kind]  body`` — one envelope between agents."""
    src_id = r.get("from")
    dst_id = r.get("to")
    src = r.get("from_label") or src_id or "?"
    dst = r.get("to_label") or dst_id or "?"
    tag, body_preview = _classify_activity(
        r.get("body"), r.get("headers"), src_id
    )
    line = Text(no_wrap=True, overflow="ellipsis")
    line.append(_relative_time(r.get("ts"), now).rjust(4), style="dim")
    line.append("  ")
    line.append(
        src[:max_src].rjust(max_src), style=_activity_label_style(src_id)
    )
    line.append(" → ", style="dim")
    line.append(dst, style=_activity_label_style(dst_id))
    if r.get("in_reply_to"):
        line.append(" ↩", style="dim")
    if tag != "msg":
        line.append("  ")
        line.append(f"[{tag}]", style=_KIND_STYLES.get(tag, "dim"))
    line.append("  ")
    line.append(body_preview, style="dim")
    return line


def _render_tool_call_row(
    r: dict[str, Any], max_src: int, now: float
) -> Text:
    """``ts  src       ● ToolName`` — an agent called a tool. The
    sender column is the same width as in envelope rows so tool
    invocations line up with messages from the same agent — you
    can scan a column straight down to see one agent's activity."""
    src_id = r.get("from")
    src = r.get("from_label") or src_id or "?"
    tool = r.get("tool") or "?"
    line = Text(no_wrap=True, overflow="ellipsis")
    line.append(_relative_time(r.get("ts"), now).rjust(4), style="dim")
    line.append("  ")
    line.append(
        src[:max_src].rjust(max_src), style=_activity_label_style(src_id)
    )
    # Empty arrow column — tools are local to the agent, no recipient.
    # Three spaces keep the post-sender content aligned with envelope
    # rows' ``" → "`` separator.
    line.append("   ")
    line.append("● ", style="cyan")
    line.append(str(tool), style="cyan")
    return line


def _render_tool_result_row(
    r: dict[str, Any], max_src: int, now: float
) -> Text:
    """``ts  src       ⎿ ✓`` or ``⎿ ✗ <summary>`` — tool returned.
    Success is silent (just the checkmark); failure shows a brief
    error summary so the user can spot trouble without opening the
    chat pane."""
    src_id = r.get("from")
    src = r.get("from_label") or src_id or "?"
    failed = bool(r.get("failed"))
    summary = (r.get("summary") or "").strip()
    line = Text(no_wrap=True, overflow="ellipsis")
    line.append(_relative_time(r.get("ts"), now).rjust(4), style="dim")
    line.append("  ")
    line.append(
        src[:max_src].rjust(max_src), style=_activity_label_style(src_id)
    )
    line.append("   ")
    if failed:
        line.append("⎿ ", style="dim red")
        line.append("✗ ", style="bold red")
        line.append(summary or "(failed)", style="red")
    else:
        line.append("⎿ ", style="dim green")
        line.append("✓", style="green")
    return line


def _merge_feed(
    envelopes: list[dict[str, Any]],
    log_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Combine envelope-activity and tool-event rows into one
    time-sorted feed. Each row is tagged with ``_kind``:

    - ``"envelope"`` — a message between agents (from the inbox
      tap on the daemon side)
    - ``"tool_call"`` / ``"tool_result"`` — a local tool invocation
      from an agent's event log

    The renderer dispatches on ``_kind``. ts is the float seconds
    we sort on; envelopes already carry it, log events too (the
    EventLog now injects ``ts`` on emit). The merged list is
    truncated to a sane cap so even a busy session doesn't grow
    unbounded."""
    rows: list[dict[str, Any]] = []
    for env in envelopes:
        rows.append({**env, "_kind": "envelope"})
    for ev in log_events:
        # log_events already carry "kind": "tool_call" / "tool_result"
        rows.append({**ev, "_kind": ev.get("kind") or "tool_call"})
    rows.sort(key=lambda r: float(r.get("ts") or 0.0))
    return rows[-120:]


def _activity_label_style(addr_id: str | None) -> str:
    """Color the sender/recipient label in the activity feed. Plain
    cyan / magenta (no bold) — saturated enough to parse the agent
    flow at a glance, restrained enough to not dominate the feed."""
    if addr_id == "@user":
        return "cyan"
    if addr_id == "@system":
        return "dim"
    return "magenta"


def _relative_time(ts: float | None, now: float) -> str:
    """``ts`` → ``"3s"`` / ``"1m"`` / ``"2h"`` / ``"1d"``. Returns
    ``""`` when ``ts`` is missing/invalid. Width is bounded at 4
    chars so the column lines up cleanly."""
    if ts is None:
        return ""
    try:
        delta = max(0.0, now - float(ts))
    except (TypeError, ValueError):
        return ""
    if delta < 60:
        return f"{int(delta)}s"
    if delta < 3600:
        return f"{int(delta // 60)}m"
    if delta < 86400:
        return f"{int(delta // 3600)}h"
    return f"{int(delta // 86400)}d"


# Body-shape classifier: maps an envelope body + ``headers["kind"]``
# to a short tag we can show as a colored badge so the user can scan
# the feed for results/events without reading bodies. Only ``error``
# is loud; everything else stays muted so it reads as ambient
# metadata rather than a Christmas tree.
_KIND_STYLES = {
    "msg":       "dim",
    "result":    "dim green",
    "error":     "bold red",
    "event":     "dim yellow",
    "system":    "dim cyan",
}


def _classify_activity(
    body: Any, headers: dict[str, str] | None, from_id: str | None
) -> tuple[str, str]:
    """Pick a (tag, body_preview) pair for one activity row.

    Priority:
    1. ``headers["kind"]`` from the sender's ``Send(kind=...)`` arg.
    2. ``body["kind"] == "child_event"`` — runtime supervision.
    3. ``body["ok"] is False`` — tool/RPC failure.
    4. ``body["ok"] is True`` and a ``result`` field — RPC reply.
    5. Fallback ``"msg"``.

    The preview is shaped to the chosen tag so common structured
    shapes don't render as ``{'kind': 'child_event', ...}``."""
    hdr_kind = (headers or {}).get("kind") if isinstance(headers, dict) else None
    if isinstance(body, dict):
        body_kind = body.get("kind") if isinstance(body.get("kind"), str) else None
        if body_kind == "child_event":
            event = body.get("event") or "?"
            child = body.get("child_label") or body.get("child_addr") or ""
            if event == "batch_terminated":
                count = body.get("count", "?")
                return ("event", f"batch_terminated × {count}")
            tail = f": {child}" if child else ""
            return ("event", f"{event}{tail}")
        if body.get("ok") is False:
            code = body.get("code") or "error"
            msg = body.get("error") or ""
            return ("error", f"{code}: {msg}" if msg else str(code))
        if body.get("ok") is True and "result" in body:
            return ("result", _short_repr(body.get("result"), 120))
    if hdr_kind and hdr_kind != "msg":
        # Caller tagged it (e.g. ``Send(kind="result")``) — believe them.
        preview = body if isinstance(body, str) else _short_repr(body, 120)
        return (hdr_kind, str(preview))
    if from_id == "@system":
        preview = body if isinstance(body, str) else _short_repr(body, 120)
        return ("system", str(preview))
    preview = body if isinstance(body, str) else _short_repr(body, 120)
    return ("msg", str(preview))


if __name__ == "__main__":
    sys.exit(main())
