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

import ast
import json as _json

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.geometry import Size
from textual.widgets import Header, Input, RichLog

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
        border: none;
        padding: 0 1;
        scrollbar-size: 1 1;
        scrollbar-gutter: stable;
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
        # Streaming state — accumulating chunks live until stream_end.
        # ``_stream_marker`` is the RichLog row count at the moment
        # the response started; truncating to it lets each chunk
        # rewrite the in-progress response so text grows top-down.
        self._stream_buffer: str = ""
        self._streaming: bool = False
        self._stream_marker: int = 0
        # See main_window for the same pattern: local echo + log tail
        # would otherwise double-render the user's own messages.
        self._pending_user_echoes: int = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            history = RichLog(
                id="history",
                wrap=True,
                markup=True,
                highlight=False,
                auto_scroll=True,
            )
            history.can_focus = False
            yield history
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
        for row in _user_rows(text):
            log.write(row)
        self._pending_user_echoes += 1
        try:
            reply = self.client.call("send", addr=self.addr, body=text)
        except Exception as exc:
            self._pending_user_echoes = max(0, self._pending_user_echoes - 1)
            log.write(Text(f"send failed: {exc}", style="red"))
            return
        if not reply.get("ok"):
            self._pending_user_echoes = max(0, self._pending_user_echoes - 1)
            log.write(Text(f"send rejected: {reply.get('error', '?')}", style="red"))

    # ---- log tailing ----

    def _start_tail(self) -> None:
        """Spawn the background reader that tails the agent's event log
        and dispatches each event to the textual event loop for
        rendering. No rich Console captures — events are formatted
        into ``rich.Text`` rows directly by ``_format_event``."""

        def reader() -> None:
            for event in tail(self.log_path, poll_interval=0.05, stop_event=self._tail_stop):
                self.call_from_thread(self._render_event_into_log, event)

        self._tail_thread = threading.Thread(
            target=reader, daemon=True, name="chat-tail"
        )
        self._tail_thread.start()

    def _render_event_into_log(self, event: dict[str, Any]) -> None:
        """Format one event and route it to either the streaming pane
        (chunks accumulating live) or the main history (everything
        else)."""
        kind = event.get("kind")
        if kind == "chunk":
            self._on_chunk(event.get("text", "") or "")
            return
        if kind == "stream_end":
            self._on_stream_end(event.get("tool_calls", []) or [])
            return
        if kind == "user_input" and self._pending_user_echoes > 0:
            self._pending_user_echoes -= 1
            return
        log = self.query_one("#history", RichLog)
        try:
            rows = list(_format_event(self.agent_label, event))
        except Exception as exc:
            log.write(Text(f"render error: {exc}", style="red"))
            return
        for row in rows:
            log.write(row)

    def _on_chunk(self, text: str) -> None:
        if not text:
            return
        log = self.query_one("#history", RichLog)
        if not self._streaming:
            self._streaming = True
            self._stream_buffer = ""
            self._stream_marker = len(log.lines)
        self._stream_buffer += text
        _rewrite_stream(
            log, self.agent_label, self._stream_buffer, [], self._stream_marker
        )

    def _on_stream_end(self, tool_calls: list[dict[str, Any]]) -> None:
        log = self.query_one("#history", RichLog)
        if self._stream_buffer or tool_calls:
            _rewrite_stream(
                log,
                self.agent_label,
                self._stream_buffer,
                tool_calls,
                self._stream_marker,
            )
        self._stream_buffer = ""
        self._streaming = False
        self._stream_marker = 0


# Background colors used to differentiate user vs agent message
# blocks. User rows get a noticeable dark tint so they read as
# quoted input; agent rows get a subtler highlight so the response
# feels like the "main flow". Hex values are picked to read on both
# default light and dark Textual themes — applied as rich background
# styles directly to the Text rows since the rows live inside a
# RichLog (textual CSS doesn't reach row-level styles).
_USER_BG = "on #14202c"
_AGENT_BG = "on #1c1c1c"


def _rewrite_stream(
    history: RichLog,
    label: str,
    text: str,
    tool_calls: list[dict[str, Any]],
    marker: int,
) -> None:
    """Truncate ``history`` back to ``marker`` and re-write the
    in-progress response. Each streaming chunk calls this with the
    full accumulated text so the visible content grows top-down inside
    the chat history (new lines appear below the previous, the header
    stays where it landed when the response started)."""
    marker = max(0, min(marker, len(history.lines)))
    history.lines = history.lines[:marker]
    history.virtual_size = Size(history.virtual_size.width, len(history.lines))
    for row in _format_response_rows(label, text, tool_calls):
        history.write(row)


def _format_response_rows(
    label: str,
    text: str,
    tool_calls: list[dict[str, Any]],
) -> list[Text]:
    """Build the rows that a completed response writes to the chat
    history. Agent blocks use a subtle background tint to stand apart
    from user messages, with the agent label as a bold-magenta header
    and tool calls indented underneath.
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
        row.append("  ")
        row.append("← ", style="cyan")
        row.append(name, style="bold cyan")
        row.append(f"({args})", style="cyan")
        row.stylize(_AGENT_BG)
        rows.append(row)
    rows.append(Text(""))
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
    rows.append(Text(""))
    return rows


def _format_event(label: str, event: dict[str, Any]) -> list[Text]:
    """Convert one event dict into the rows to write to a RichLog.

    User and agent blocks are visually distinguished by background
    color (no bar prefix); tool calls / results render as compact
    indented one-liners.
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
        row.append("  ")
        if failed:
            row.append(f"✗ {summary}", style="red")
        else:
            row.append(f"✓ {summary}", style="dim cyan")
        rows.append(row)
        return rows

    if kind == "assistant":
        return _format_response_rows(label, event.get("text", "") or "", [])

    if kind == "spawned":
        spawned_label = event.get("label") or event.get("addr") or "?"
        parent = event.get("parent")
        suffix = f" under {parent}" if parent else " (root)"
        row = Text()
        row.append("  + spawned ", style="dim")
        row.append(spawned_label, style="bold")
        row.append(suffix, style="dim")
        rows.append(row)
        return rows

    if kind == "terminated":
        addr = event.get("addr", "?")
        row = Text()
        row.append("  × terminated ", style="red dim")
        row.append(addr, style="red")
        rows.append(row)
        return rows

    if kind == "user_input":
        return _user_rows(event.get("text", "") or "")

    # ``user`` is the noisy driver-wrapped prompt; skip silently.
    # ``unknown`` is anything we can't classify.
    return rows


# ---- helpers (duplicated from _ui to avoid pulling rich.Panel deps) ----

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
