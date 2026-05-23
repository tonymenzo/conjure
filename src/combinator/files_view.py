"""``combinator-files`` — popup showing every agent's sandbox tree.

Launched via ``tmux display-popup -E "combinator-files --session
<name>"`` (bound to ``Ctrl+B F`` by the daemon).

Layout:

    ┌─ combinator › files ─────────────────────────────────────────────┐
    │ ┌─ agents ────┐ ┌─ <agent>/<path>  (syntax-highlighted) ───────┐│
    │ │ ● root      │ │  1  # README                                  ││
    │ │   worker-1  │ │  2                                            ││
    │ └─────────────┘ │  3  …                                          ││
    │ ┌─ sandbox ───┐ │                                                ││
    │ │ src/        │ │                                                ││
    │ │ tests/      │ │                                                ││
    │ │ README.md   │ │                                                ││
    │ └─────────────┘ │                                                ││
    │ ┌─ recent ────┐ │                                                ││
    │ │ src/main.py │ │                                                ││
    │ │ README.md   │ │                                                ││
    │ └─────────────┘ └────────────────────────────────────────────────┘│
    │ /pattern  (search input — only visible when active)               │
    │ Tab cycle  /  search  n/N next/prev  r refresh  q close            │
    └────────────────────────────────────────────────────────────────────┘

Left column (28%): agents, sandbox tree, recent-files list. Right
column (72%): syntax-highlighted preview with in-file search.

Keybindings:

- ``q`` / ``Esc``         — close popup (Esc also exits search)
- ``Tab`` / ``Shift+Tab`` — cycle focus between left panels
- ``/``                   — open the search input (focuses an input
                            at the bottom of the popup; type to filter
                            preview matches, Enter scrolls to first)
- ``n`` / ``N``           — next / previous match in preview
- ``r``                   — refresh
- ``Enter`` on a file     — load its content into the preview
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from rich.console import Group
from rich.syntax import Syntax
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, Input, Static, Tree

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
    Screen { background: ansi_default; }
    #left { width: 28%; }
    #agents-pane, #files-pane, #recent-pane, #preview-pane {
        border: round ansi_default;
        background: ansi_default;
        padding: 0 1;
    }
    /* Focused pane brightens to white so the active panel is
       unmistakable. VerticalScroll has no built-in cursor; without
       this rule, focus on the preview pane is invisible. */
    #agents-pane:focus, #files-pane:focus,
    #recent-pane:focus, #preview-pane:focus {
        border: round white;
    }
    /* Three stacked panels on the left; preview fills the right. */
    #agents-pane  { height: 18%; }
    #files-pane   { height: 50%; }
    #recent-pane  { height: 32%; }
    #preview-pane {
        height: 1fr;
        overflow-y: auto;
        scrollbar-size: 1 1;
        scrollbar-gutter: stable;
        scrollbar-background: ansi_default;
        scrollbar-color: $primary-darken-2;
        scrollbar-color-hover: $primary;
        scrollbar-color-active: $accent;
    }
    Tree { background: ansi_default; }
    #agents-pane > .tree--cursor,
    #files-pane > .tree--cursor,
    #recent-pane > .tree--cursor {
        background: ansi_default;
        text-style: bold underline;
    }
    /* Search input docked at the bottom, hidden until ``/`` opens it. */
    #search-input {
        dock: bottom;
        border: round #00FF41;
        background: ansi_default;
        margin: 0;
        display: none;
    }
    #search-input:focus {
        border: round white;
    }
    #search-input.active { display: block; }
    Header { background: #00FF41; }
    Footer { background: #00FF41; }
    """

    BINDINGS = [
        Binding("q", "quit", "Close"),
        Binding("escape", "escape", "Close / exit search"),
        Binding("tab", "focus_next", "Next panel"),
        Binding("shift+tab", "focus_previous", "Prev panel"),
        Binding("r", "refresh", "Refresh"),
        Binding("slash", "open_search", "Search"),
        Binding("n", "next_match", "Next match", show=True),
        Binding("N", "prev_match", "Prev match", show=True),
    ]

    # ``200_000`` is the daemon's read cap, but for a popup we'll trim
    # the on-screen render further so very large files don't lag.
    _PREVIEW_RENDER_CAP = 80_000

    def __init__(self, *, socket_path: Path) -> None:
        super().__init__()
        self.socket_path = socket_path
        self.client = ControlClient(socket_path)
        self.title = "combinator › files"
        self.sub_title = f"session: {socket_path.stem}"

        self._selected_addr: str | None = None
        self._known_addrs: set[str] = set()
        # Last-loaded preview state — kept so the search machinery can
        # re-render highlights / re-scroll without another sandbox RPC.
        self._preview_path: str = ""
        self._preview_content: str = ""
        self._preview_truncated: bool = False
        # Search state.
        self._search_query: str = ""
        self._search_matches: list[int] = []  # line numbers (0-indexed)
        self._search_cursor: int = -1

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal():
            with Vertical(id="left"):
                yield StatusTree("agents", id="agents-pane")
                yield StatusTree("sandbox", id="files-pane")
                yield StatusTree("recent", id="recent-pane")
            yield VerticalScroll(
                Static("(select a file)", id="preview-content"),
                id="preview-pane",
            )
        yield Input(placeholder="search in preview…", id="search-input")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_data()
        self.set_interval(1.5, self.refresh_data)
        self.query_one("#agents-pane", StatusTree).focus()

    def action_refresh(self) -> None:
        self.refresh_data()

    # ---- data refresh ----

    def refresh_data(self) -> None:
        try:
            tree_reply = self.client.call("tree")
        except Exception as exc:
            self._set_preview_error(f"control error: {exc}")
            return
        if not tree_reply.get("ok"):
            return
        self._apply_agents_tree(tree_reply.get("tree"))
        if self._selected_addr is None:
            self._select_first_agent()
        if self._selected_addr is not None:
            self._refresh_files(self._selected_addr)
            self._refresh_recent(self._selected_addr)

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
            self._set_preview_error(f"sandbox error: {exc}")
            return
        if not reply.get("ok"):
            self.query_one("#files-pane", StatusTree).clear()
            self._set_preview_error(reply.get("error", "?"))
            return
        if reply.get("kind") == "dir":
            self._populate_files(reply.get("entries") or [], addr=addr)
        else:
            self._load_preview(reply)

    def _refresh_recent(self, addr: str) -> None:
        try:
            reply = self.client.call("sandbox_recent", addr=addr, limit=20)
        except Exception:
            return
        if not reply.get("ok"):
            return
        self._populate_recent(reply.get("entries") or [])

    def _populate_files(
        self, entries: list[dict[str, Any]], *, addr: str
    ) -> None:
        del addr  # kept for future use (multi-agent expansion)
        tree = self.query_one("#files-pane", StatusTree)
        tree.clear()
        tree.show_root = False
        tree.root.expand()
        if not entries:
            tree.root.add("(empty sandbox)", allow_expand=False)
            return
        for entry in entries:
            is_dir = bool(entry.get("is_dir"))
            icon = "📁 " if is_dir else "📄 "
            label = f"{icon}{entry.get('name', '?')}"
            # Store ``(path, is_dir)`` so highlight handlers can tell
            # them apart without round-tripping to the daemon. The
            # tuple is opaque to the Tree widget — it just hands it
            # back via event.node.data.
            tree.root.add(
                label,
                data=(entry.get("path"), is_dir),
                allow_expand=False,
            )

    def _populate_recent(self, entries: list[dict[str, Any]]) -> None:
        tree = self.query_one("#recent-pane", StatusTree)
        tree.clear()
        tree.show_root = False
        tree.root.expand()
        if not entries:
            tree.root.add("(no files yet)", allow_expand=False)
            return
        now = time.time()
        for entry in entries:
            path = entry.get("path", "?")
            mtime = entry.get("mtime")
            rel = _format_relative(mtime, now) if mtime else ""
            label = f"{path}  [dim]{rel}[/]" if rel else path
            tree.root.add(label, data=path, allow_expand=False)

    # ---- preview rendering ----

    def _load_preview(self, payload: dict[str, Any]) -> None:
        self._preview_path = payload.get("path", "")
        content = payload.get("content") or ""
        if len(content) > self._PREVIEW_RENDER_CAP:
            content = content[: self._PREVIEW_RENDER_CAP]
            self._preview_truncated = True
        else:
            self._preview_truncated = bool(payload.get("truncated"))
        self._preview_content = content
        # New file → drop search state so we start fresh.
        self._search_matches = []
        self._search_cursor = -1
        self._rerender_preview()

    def _set_preview_error(self, msg: str) -> None:
        self._preview_path = ""
        self._preview_content = ""
        self._preview_truncated = False
        self._search_matches = []
        self._search_cursor = -1
        self.query_one("#preview-content", Static).update(
            Text(msg, style="red")
        )

    def _rerender_preview(self) -> None:
        """Re-render the preview pane using the current file content
        + the current search query. Called on file load, on search
        query change, and on match navigation."""
        pane = self.query_one("#preview-content", Static)
        if not self._preview_path:
            pane.update(Text("(select a file)", style="dim"))
            return
        # Recompute matches against the current query.
        self._recompute_matches()
        # Render: Syntax for the body (pygments lexer by filename),
        # plus a small header line with the path + match counter.
        header = Text()
        header.append(self._preview_path, style="bold cyan")
        if self._preview_truncated:
            header.append("  [truncated]", style="dim yellow")
        if self._search_query:
            count = len(self._search_matches)
            if count == 0:
                header.append(
                    f"   /{self._search_query}/  no matches", style="dim red"
                )
            else:
                idx = self._search_cursor + 1 if self._search_cursor >= 0 else 0
                header.append(
                    f"   /{self._search_query}/  {idx}/{count}", style="dim cyan"
                )
        body = _build_preview_body(
            content=self._preview_content,
            path=self._preview_path,
            query=self._search_query,
            cursor_line=(
                self._search_matches[self._search_cursor]
                if 0 <= self._search_cursor < len(self._search_matches)
                else None
            ),
        )
        pane.update(Group(header, Text(""), body))
        # If we have a cursor, scroll to it.
        if 0 <= self._search_cursor < len(self._search_matches):
            self._scroll_preview_to_line(
                self._search_matches[self._search_cursor]
            )

    def _scroll_preview_to_line(self, line_no: int) -> None:
        """Best-effort: scroll the preview so the target line is at
        ~25% from the top of the viewport. ``line_no`` is 0-indexed."""
        scroll = self.query_one("#preview-pane", VerticalScroll)
        # Header is 2 lines (path + blank); content starts at row 2.
        # Syntax(line_numbers=True) wraps each source line on one row
        # unless the line is longer than the pane width. For long
        # lines this estimate undershoots; that's fine for "near the
        # match" UX.
        target_y = max(0, line_no + 2 - int(scroll.size.height * 0.25))
        scroll.scroll_to(y=target_y, animate=False)

    def _recompute_matches(self) -> None:
        """Refresh ``_search_matches`` (line numbers) from the current
        content + query. Anchors ``_search_cursor`` to 0 when the
        query just became non-empty, else clamps it into range."""
        query = self._search_query
        prev_cursor = self._search_cursor
        prev_matches = list(self._search_matches)
        if not query:
            self._search_matches = []
            self._search_cursor = -1
            return
        try:
            pattern = re.compile(re.escape(query), re.IGNORECASE)
        except re.error:
            self._search_matches = []
            self._search_cursor = -1
            return
        matches: list[int] = []
        for i, line in enumerate(self._preview_content.splitlines()):
            if pattern.search(line):
                matches.append(i)
        self._search_matches = matches
        if not matches:
            self._search_cursor = -1
            return
        # Preserve the cursor across edits when possible (so typing
        # one more letter doesn't bounce the view).
        if 0 <= prev_cursor < len(prev_matches):
            anchor = prev_matches[prev_cursor]
            # Snap to the closest match at-or-after the previous anchor.
            for i, ln in enumerate(matches):
                if ln >= anchor:
                    self._search_cursor = i
                    return
        self._search_cursor = 0

    # ---- selection handlers ----

    @on(Tree.NodeSelected, "#agents-pane")
    def _on_agent_selected(self, event: Tree.NodeSelected) -> None:
        addr = event.node.data
        if isinstance(addr, str):
            self._selected_addr = addr
            self._refresh_files(addr)
            self._refresh_recent(addr)

    @on(Tree.NodeHighlighted, "#agents-pane")
    def _on_agent_highlighted(self, event: Tree.NodeHighlighted) -> None:
        addr = event.node.data
        if isinstance(addr, str) and addr != self._selected_addr:
            self._selected_addr = addr
            self._refresh_files(addr)
            self._refresh_recent(addr)

    @on(Tree.NodeSelected, "#files-pane")
    def _on_file_selected(self, event: Tree.NodeSelected) -> None:
        """Enter on a files-pane node. Files: load into preview.
        Directories: descend (replace the tree with the subdir's
        listing). The split between Selected (Enter) and Highlighted
        (arrows/click) below keeps directories from descending on
        every cursor move."""
        data = event.node.data
        path, _ = _split_file_data(data)
        if not (path and self._selected_addr):
            return
        self._refresh_files(self._selected_addr, path)

    @on(Tree.NodeHighlighted, "#files-pane")
    def _on_file_highlighted(self, event: Tree.NodeHighlighted) -> None:
        """Arrow / click on a files-pane node: live-preview if it's a
        file, no-op if it's a directory (waiting for Enter to descend
        so navigation past a directory doesn't reshape the tree)."""
        data = event.node.data
        path, is_dir = _split_file_data(data)
        if not (path and self._selected_addr) or is_dir:
            return
        self._refresh_files(self._selected_addr, path)

    @on(Tree.NodeSelected, "#recent-pane")
    def _on_recent_selected(self, event: Tree.NodeSelected) -> None:
        path = event.node.data
        if isinstance(path, str) and self._selected_addr:
            self._refresh_files(self._selected_addr, path)

    @on(Tree.NodeHighlighted, "#recent-pane")
    def _on_recent_highlighted(self, event: Tree.NodeHighlighted) -> None:
        """Recent-pane only ever contains files (the daemon's recent
        RPC filters dirs), so arrow / click always previews."""
        path = event.node.data
        if isinstance(path, str) and self._selected_addr:
            self._refresh_files(self._selected_addr, path)

    # ---- search actions ----

    def action_open_search(self) -> None:
        inp = self.query_one("#search-input", Input)
        inp.set_class(True, "active")
        inp.value = self._search_query
        inp.focus()

    def action_escape(self) -> None:
        inp = self.query_one("#search-input", Input)
        if inp.has_class("active"):
            inp.set_class(False, "active")
            self._search_query = ""
            self._search_matches = []
            self._search_cursor = -1
            self._rerender_preview()
            self.query_one("#preview-pane", VerticalScroll).focus()
            return
        self.exit()

    def action_next_match(self) -> None:
        if not self._search_matches:
            return
        self._search_cursor = (
            self._search_cursor + 1
        ) % len(self._search_matches)
        self._rerender_preview()

    def action_prev_match(self) -> None:
        if not self._search_matches:
            return
        self._search_cursor = (
            self._search_cursor - 1
        ) % len(self._search_matches)
        self._rerender_preview()

    @on(Input.Changed, "#search-input")
    def _on_search_changed(self, event: Input.Changed) -> None:
        self._search_query = event.value
        self._rerender_preview()

    @on(Input.Submitted, "#search-input")
    def _on_search_submitted(self, event: Input.Submitted) -> None:
        # Enter jumps to the first / next match and returns focus to
        # the preview so subsequent ``n`` / ``N`` work without the
        # input swallowing the keys.
        del event
        if self._search_matches:
            self._rerender_preview()
        self.query_one("#preview-pane", VerticalScroll).focus()


def _split_file_data(data: Any) -> tuple[str | None, bool]:
    """Files-pane node data was historically a bare path string; now
    it's a ``(path, is_dir)`` tuple. Tolerate both so a stale node
    (e.g. mid-refresh) doesn't crash the handler."""
    if isinstance(data, tuple) and len(data) == 2:
        path, is_dir = data
        return (path if isinstance(path, str) else None, bool(is_dir))
    if isinstance(data, str):
        return (data, False)
    return (None, False)


def _agent_label(agent: dict[str, Any]) -> str:
    dot = _STATUS_DOT.get(agent.get("status", ""), "[dim]●[/]")
    label = agent.get("label") or agent.get("addr") or "?"
    return f"{dot} [bold]{label}[/]"


def _format_relative(ts: float, now: float) -> str:
    """``ts`` → ``"3s"`` / ``"5m"`` / ``"2h"`` / ``"4d"``."""
    delta = max(0.0, now - ts)
    if delta < 60:
        return f"{int(delta)}s"
    if delta < 3600:
        return f"{int(delta // 60)}m"
    if delta < 86400:
        return f"{int(delta // 3600)}h"
    return f"{int(delta // 86400)}d"


def _build_preview_body(
    *,
    content: str,
    path: str,
    query: str,
    cursor_line: int | None,
) -> Any:
    """Render the file body. When no query is active, use rich's
    ``Syntax`` for pygments-driven highlighting. When the user is
    searching, fall back to a ``Text`` so we can paint highlights
    over the matched substrings (Syntax's renderable is opaque to
    span overlays). The active match's line gets an additional bold
    underline so the cursor position is unmistakable."""
    if not query:
        try:
            # ``ansi_dark`` is a transparent rich theme that uses the
            # terminal's ANSI palette without painting its own
            # background — so syntax colors layer on the existing
            # ``$surface`` instead of carving out a monokai-colored
            # box inside the preview pane.
            return Syntax(
                content,
                _guess_lexer(path),
                theme="ansi_dark",
                line_numbers=True,
                word_wrap=False,
            )
        except Exception:
            return Text(content)
    return _highlighted_text(content, query, cursor_line)


def _guess_lexer(path: str) -> str:
    """Map a file extension to a pygments lexer name. Falls back to
    ``"text"`` for anything we don't recognize so ``Syntax`` won't
    raise."""
    suffix = Path(path).suffix.lower()
    by_ext = {
        ".py": "python",
        ".pyi": "python",
        ".js": "javascript",
        ".mjs": "javascript",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".jsx": "jsx",
        ".rs": "rust",
        ".go": "go",
        ".c": "c",
        ".h": "c",
        ".cpp": "cpp",
        ".hpp": "cpp",
        ".cc": "cpp",
        ".java": "java",
        ".kt": "kotlin",
        ".rb": "ruby",
        ".sh": "bash",
        ".bash": "bash",
        ".zsh": "bash",
        ".fish": "fish",
        ".sql": "sql",
        ".md": "markdown",
        ".markdown": "markdown",
        ".yml": "yaml",
        ".yaml": "yaml",
        ".toml": "toml",
        ".json": "json",
        ".xml": "xml",
        ".html": "html",
        ".css": "css",
        ".jsonl": "json",
        ".ini": "ini",
        ".cfg": "ini",
        ".dockerfile": "docker",
        ".lock": "text",
    }
    if Path(path).name.lower() in ("dockerfile", "makefile"):
        return Path(path).name.lower()
    return by_ext.get(suffix, "text")


def _highlighted_text(content: str, query: str, cursor_line: int | None) -> Text:
    """Render ``content`` with ``query`` matches highlighted. Uses
    case-insensitive substring matching (re.escape the query so the
    user can search for literals containing regex metacharacters).
    The active match's line gets a bold underline so it's
    distinguishable from other hits."""
    out = Text(no_wrap=False)
    if not query:
        out.append(content)
        return out
    try:
        pattern = re.compile(re.escape(query), re.IGNORECASE)
    except re.error:
        out.append(content)
        return out
    width = max(3, len(str(content.count("\n") + 1)))
    for i, line in enumerate(content.splitlines()):
        # Line gutter (matches Syntax's number column visually).
        out.append(f"{i + 1:>{width}}  ", style="dim")
        is_cursor_line = cursor_line is not None and i == cursor_line
        cursor = 0
        for m in pattern.finditer(line):
            if m.start() > cursor:
                out.append(line[cursor:m.start()])
            out.append(
                line[m.start():m.end()],
                style="bold yellow on grey23",
            )
            cursor = m.end()
        if cursor < len(line):
            out.append(line[cursor:])
        if is_cursor_line:
            # Underline the whole line so the active match stands out
            # even when it shares the line with other matches.
            out.stylize(
                "underline",
                start=len(out) - len(line),
                end=len(out),
            )
        out.append("\n")
    return out


if __name__ == "__main__":
    sys.exit(main())
