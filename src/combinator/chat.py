"""``combinator-chat`` — per-agent chat TUI.

Each tmux window in a combinator session runs one ``combinator-chat``
instance for one agent. The window is:

- **Top pane** — a ``ChatView`` (scrollable container of per-event
  blocks) showing this agent's activity. Sourced from the agent's
  JSONL event log.
- **Bottom pane** — input prompt. Pressing Enter sends the text to
  this agent's inbox via the daemon's control socket.

The chat does not own the agent's runtime — that lives in the daemon.
It's a pure UI: tail-log + send-on-Enter.

Streaming model: each response is one mounted ``Static`` widget. As
``chunk`` events arrive the widget is updated in place (textual's
native API, no internal-state hacking). On ``stream_end`` the widget
is finalized (tool calls appended) and a new block can mount on top.

Keybindings:

- ``Enter``         — send the current input
- ``Tab``           — focus the input (when scrolling the history)
- ``PgUp / PgDn``   — scroll history
- ``Ctrl+L``        — clear the input field
- ``Esc``           — focus history (for scrollback)
- ``Ctrl+Q``        — close this window only (daemon untouched)
"""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path
from typing import Any, Sequence

import ast
import json as _json

from rich.console import Group, RenderableType
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Header, Input, Static

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


# Background colors used to differentiate user vs agent message
# blocks. User rows get a noticeable dark tint so they read as
# quoted input; agent rows get a subtler highlight so the response
# feels like the "main flow".
_USER_BG = "on #14202c"
_AGENT_BG = "on #1c1c1c"


class ChatView(VerticalScroll):
    """Scrollable container of per-event chat blocks.

    Each event mounts a ``Static`` child. The currently-streaming
    response (if any) is tracked so subsequent ``chunk`` events update
    the same widget in place via the public ``Static.update`` API —
    no private-state manipulation, no flicker, no stale strip cache.
    """

    DEFAULT_CSS = """
    ChatView {
        background: $surface;
        padding: 0 1;
        scrollbar-size: 1 1;
        scrollbar-gutter: stable;
    }
    ChatView > Static {
        height: auto;
        margin-bottom: 1;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._streaming_block: Static | None = None
        self._stream_buffer: str = ""
        self.can_focus = False

    def reset(self) -> None:
        """Drop every block and reset stream state. Use on agent swap."""
        for child in list(self.children):
            child.remove()
        self._streaming_block = None
        self._stream_buffer = ""

    def apply_event(self, label: str, event: dict[str, Any]) -> None:
        """Route one live event (from the log tail) to the right path."""
        kind = event.get("kind")
        if kind == "chunk":
            self._stream_chunk(label, event.get("text", "") or "")
            return
        if kind == "stream_end":
            self._end_stream(label, event.get("tool_calls", []) or [])
            return
        # Any non-streaming event finalizes a hanging stream first so
        # the response block lands before the new block goes below it.
        if self._streaming_block is not None:
            self._end_stream(label, [])
        self._append_event(label, event)

    def replay_events(self, label: str, events: list[dict[str, Any]]) -> None:
        """Replay a backlog: accumulate chunk events into complete
        responses, mount everything in order, scroll to the bottom."""
        chunk_buffer = ""
        for event in events:
            kind = event.get("kind")
            if kind == "chunk":
                chunk_buffer += event.get("text", "") or ""
                continue
            if kind == "stream_end":
                self._mount_response(
                    label, chunk_buffer, event.get("tool_calls", []) or []
                )
                chunk_buffer = ""
                continue
            self._append_event(label, event)
        # Trailing chunks with no stream_end (mid-response when we
        # opened the log) get flushed as text without tool calls.
        if chunk_buffer:
            self._mount_response(label, chunk_buffer, [])
        self._scroll_to_end()

    def echo_user(self, text: str) -> None:
        """Mount a user block directly (used by the local-input echo)."""
        rows = _user_rows(text)
        if rows:
            self._mount_rows(rows)
            self._scroll_to_end()

    def write_error(self, text: str) -> None:
        """Mount a simple error/status line."""
        self._mount_rows([Text(text, style="red")])
        self._scroll_to_end()

    # ----- internals -----

    def _append_event(self, label: str, event: dict[str, Any]) -> None:
        rows = _format_event(label, event)
        if rows:
            self._mount_rows(rows)
            self._scroll_to_end()

    def _stream_chunk(self, label: str, text: str) -> None:
        if not text:
            return
        self._stream_buffer += text
        renderable = _streaming_renderable(label, self._stream_buffer, [])
        if self._streaming_block is None:
            self._streaming_block = Static(renderable)
            self.mount(self._streaming_block)
        else:
            self._streaming_block.update(renderable)
        self._scroll_to_end()

    def _end_stream(self, label: str, tool_calls: list[dict[str, Any]]) -> None:
        has_content = bool(self._stream_buffer) or bool(tool_calls)
        if self._streaming_block is None and not has_content:
            return
        renderable = _streaming_renderable(label, self._stream_buffer, tool_calls)
        if self._streaming_block is None:
            self._streaming_block = Static(renderable)
            self.mount(self._streaming_block)
        else:
            self._streaming_block.update(renderable)
        self._streaming_block = None
        self._stream_buffer = ""
        self._scroll_to_end()

    def _mount_response(
        self, label: str, text: str, tool_calls: list[dict[str, Any]]
    ) -> None:
        rows = _format_response_rows(label, text, tool_calls)
        if rows:
            self._mount_rows(rows)

    def _mount_rows(self, rows: list[Text]) -> None:
        renderable = _rows_to_renderable(rows)
        self.mount(Static(renderable))

    def _scroll_to_end(self) -> None:
        # call_after_refresh waits for the new mount/update to be in the
        # layout before scrolling, so the bottom is the actual bottom.
        self.call_after_refresh(self.scroll_end, animate=False)


class ChatApp(App):
    """One agent, one chat window."""

    CSS = """
    Screen {
        background: $surface;
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
    """

    BINDINGS = [
        Binding("ctrl+q", "quit_window", "Close window"),
        Binding("escape", "focus_input", "Focus input"),
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
        # Count of locally-echoed user messages waiting for the
        # matching ``user_input`` to arrive from the daemon. Each
        # arrival decrements; ones with the counter at zero render.
        self._pending_user_echoes: int = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield ChatView(id="history")
        yield Input(placeholder="type a message — Enter to send", id="input")

    def on_mount(self) -> None:
        self.query_one(Input).focus()
        self._start_tail()
        self._refresh_status()
        self.set_interval(2.0, self._refresh_status)

    def on_unmount(self) -> None:
        self._tail_stop.set()

    # ---- status indicator ----

    def _refresh_status(self) -> None:
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
        icon = {"lazy": "…", "running": "▶", "idle": "✓", "terminated": "✗"}.get(
            my_status, "?"
        )
        self.sub_title = f"({self.addr})  {icon} {my_status}"

    # ---- key actions ----

    def action_quit_window(self) -> None:
        self.exit()

    def action_focus_input(self) -> None:
        self.query_one(Input).focus()

    def action_clear_input(self) -> None:
        self.query_one(Input).value = ""

    def action_scroll_up(self) -> None:
        self.query_one(ChatView).scroll_page_up()

    def action_scroll_down(self) -> None:
        self.query_one(ChatView).scroll_page_down()

    # ---- input submission ----

    @on(Input.Submitted)
    def on_submit(self, event: Input.Submitted) -> None:
        text = (event.value or "").strip()
        event.input.value = ""
        if not text:
            return
        view = self.query_one(ChatView)
        view.echo_user(text)
        self._pending_user_echoes += 1
        try:
            reply = self.client.call("send", addr=self.addr, body=text)
        except Exception as exc:
            self._pending_user_echoes = max(0, self._pending_user_echoes - 1)
            view.write_error(f"send failed: {exc}")
            return
        if not reply.get("ok"):
            self._pending_user_echoes = max(0, self._pending_user_echoes - 1)
            view.write_error(f"send rejected: {reply.get('error', '?')}")

    # ---- log tailing ----

    def _start_tail(self) -> None:
        """Background reader: tails the JSONL log and pushes each event
        into the textual event loop for rendering."""

        def reader() -> None:
            for event in tail(self.log_path, poll_interval=0.05, stop_event=self._tail_stop):
                self.call_from_thread(self._on_event, event)

        self._tail_thread = threading.Thread(
            target=reader, daemon=True, name="chat-tail"
        )
        self._tail_thread.start()

    def _on_event(self, event: dict[str, Any]) -> None:
        if event.get("kind") == "user_input" and self._pending_user_echoes > 0:
            self._pending_user_echoes -= 1
            return
        view = self.query_one(ChatView)
        try:
            view.apply_event(self.agent_label, event)
        except Exception as exc:
            view.write_error(f"render error: {exc}")


# ---------------------------------------------------------------- helpers


def _streaming_renderable(
    label: str,
    text: str,
    tool_calls: list[dict[str, Any]],
) -> RenderableType:
    """Build the renderable shown while a response is streaming. Same
    shape as ``_format_response_rows`` produces, just packaged for a
    Static widget."""
    rows = _format_response_rows(label, text, tool_calls)
    return _rows_to_renderable(rows) if rows else Text("")


def _rows_to_renderable(rows: list[Text]) -> RenderableType:
    """Pack ``Text`` rows into a single ``Group`` (drops trailing empty
    spacers — CSS margin already gives blocks breathing room)."""
    filtered = [r for r in rows if r.plain != ""]
    return Group(*filtered) if filtered else Text("")


def _format_response_rows(
    label: str,
    text: str,
    tool_calls: list[dict[str, Any]],
) -> list[Text]:
    """Build the rows that a completed (or in-progress) response writes.

    Agent blocks use a subtle background tint to stand apart from user
    messages, with the agent label as a bold-magenta header and tool
    calls indented underneath.
    """
    rows: list[Text] = []
    if not text and not tool_calls:
        return rows
    rows.append(_agent_header(label))
    if text:
        for line in text.split("\n"):
            row = Text(no_wrap=False)
            row.append("  ")
            row.append(line)
            row.stylize(_AGENT_BG)
            rows.append(row)
    for tc in tool_calls:
        name = tc.get("name") or tc.get("tool_name") or "?"
        args = _args_preview(tc.get("args") or tc.get("arguments") or {})
        row = Text(no_wrap=False)
        row.append("  ● ", style="bold cyan")
        row.append(name, style="bold cyan")
        if args:
            row.append(f"({args})", style="dim cyan")
        else:
            row.append("()", style="dim cyan")
        row.stylize(_AGENT_BG)
        rows.append(row)
    return rows


def _agent_header(label: str) -> Text:
    row = Text(no_wrap=False)
    row.append(f" {label} ", style="bold magenta")
    row.stylize(_AGENT_BG)
    return row


def _user_rows(text: str) -> list[Text]:
    rows: list[Text] = []
    if not text:
        return rows
    lines = text.split("\n")
    header = Text(no_wrap=False)
    header.append(" you ", style="bold cyan")
    header.append("  ")
    header.append(lines[0])
    header.stylize(_USER_BG)
    rows.append(header)
    for line in lines[1:]:
        row = Text(no_wrap=False)
        row.append("      ")  # align past " you  "
        row.append(line)
        row.stylize(_USER_BG)
        rows.append(row)
    return rows


def _format_event(label: str, event: dict[str, Any]) -> list[Text]:
    """Convert one event dict into the rows for a single chat block.

    Blocks have no internal spacers — the ``ChatView`` CSS adds
    ``margin-bottom: 1`` between Static children.
    """
    rows: list[Text] = []
    kind = event.get("kind")

    if kind == "response":
        return _format_response_rows(
            label,
            event.get("text", "") or "",
            event.get("tool_calls", []) or [],
        )

    if kind == "tool":
        failed = bool(event.get("failed"))
        summary = _summarize_tool_result(event.get("text", "") or "")
        row = Text()
        row.append("  ⎿ ", style="dim")
        if failed:
            row.append(summary, style="red")
        else:
            row.append(summary, style="dim")
        rows.append(row)
        return rows

    if kind == "assistant":
        return _format_response_rows(label, event.get("text", "") or "", [])

    if kind == "spawned":
        spawned_label = event.get("label") or event.get("addr") or "?"
        parent = event.get("parent")
        suffix = f" under {parent}" if parent else " (root)"
        row = Text()
        row.append("+ spawned ", style="dim")
        row.append(spawned_label, style="bold")
        row.append(suffix, style="dim")
        rows.append(row)
        return rows

    if kind == "terminated":
        addr = event.get("addr", "?")
        row = Text()
        row.append("× terminated ", style="red dim")
        row.append(addr, style="red")
        rows.append(row)
        return rows

    if kind == "user_input":
        return _user_rows(event.get("text", "") or "")

    # ``user`` is the noisy driver-wrapped prompt; skip silently.
    # ``unknown`` is anything we can't classify.
    return rows


# ---- small parsing helpers ----

_ARG_PREVIEW_LEN = 60
_BODY_PREVIEW_LEN = 200


def _args_preview(args: Any) -> str:
    if not isinstance(args, dict):
        return _truncate(str(args), _ARG_PREVIEW_LEN)
    parts: list[str] = []
    for k, v in args.items():
        s = _json.dumps(v, default=str) if not isinstance(v, str) else repr(v)
        parts.append(f"{k}={_truncate(s, _ARG_PREVIEW_LEN)}")
    return ", ".join(parts)


def _summarize_tool_result(text: str) -> str:
    parsed = _parse_dict(text)
    if not isinstance(parsed, dict):
        return _truncate(text, _BODY_PREVIEW_LEN)
    ok = parsed.get("ok")
    if ok is False:
        code = parsed.get("code") or "error"
        msg = parsed.get("error") or ""
        if msg:
            return f"{code}: {_truncate(str(msg), 80)}"
        return str(code)
    for key in ("address", "result", "msg_id", "terminated", "envelopes", "next_seq"):
        if key in parsed:
            v = parsed[key]
            if isinstance(v, list):
                return f"{key}=[{len(v)} item(s)]"
            return f"{key}={_truncate(repr(v), 80)}"
    return "ok"


def _parse_dict(text: str) -> Any:
    if not text or not text.strip().startswith("{"):
        return None
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        try:
            return _json.loads(text)
        except (ValueError, TypeError):
            return None


def _truncate(s: str, n: int) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


if __name__ == "__main__":
    sys.exit(main())
