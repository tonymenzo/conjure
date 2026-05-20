"""Rich-based rendering for the REPL.

The CLI builds one ``Console`` and one ``RichDisplayHookBuilder`` per
session. The hook is handed to the engine factory and invoked by
``orchestral.Agent`` after each context update. It tracks how many
messages it has already rendered so each call only prints the *new*
ones — preventing duplicates when the agent makes multiple tool calls
during a single ``step``.

Render order matches the narrative the LLM produces: its prose first,
then the tool calls that operationalize that prose, then the result of
those calls (compacted to a one-line summary). This avoids the
text-after-call inversion that makes the output read like a stream of
unrelated events.

Visual conventions:

- ``[bold cyan]you ›[/bold cyan]`` — the user prompt.
- Each agent's prose lives in a labeled, rounded panel; outgoing tool
  calls appear inside that panel (they are the agent's actions).
- Tool results appear *between* panels as one-line summaries, since
  they are framework events rather than an agent's speech.
- ``[red]error: ...[/red]`` — runtime errors surfaced from drivers.
"""

from __future__ import annotations

import ast
import json
from typing import Any, Callable

from rich.box import ROUNDED
from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

from combinator.events import serialize_message
from combinator.record import AgentRecord


_ARG_PREVIEW_LEN = 60
_BODY_PREVIEW_LEN = 200


def make_console() -> Console:
    """Console used everywhere in the CLI. Single instance per session."""
    return Console(highlight=False)


def make_display_hook_builder(
    console: Console,
) -> Callable[[AgentRecord], Callable[[Any], None]]:
    """Return a builder that produces per-agent display hooks.

    Each hook closes over the agent's label and a counter; orchestral
    invokes it after every context mutation, and we render only newly
    appended messages.
    """

    def build(record: AgentRecord) -> Callable[[Any], None]:
        label = record.addr.label or record.addr.id
        state = {"seen": 0}

        def hook(context: Any) -> None:
            messages = getattr(context, "messages", None) or []
            for msg in messages[state["seen"]:]:
                event = serialize_message(msg)
                render_event(console, label, event)
            state["seen"] = len(messages)

        return hook

    return build


def render_event(console: Console, label: str, event: dict[str, Any]) -> None:
    """Render one event dict.

    The single rendering entrypoint used by both the in-process REPL
    display hook AND the per-agent renderer that powers tmux windows.
    Dispatches on ``event["kind"]``; unknown kinds are silently skipped
    rather than dumped raw, because the schema may add new kinds the
    renderer doesn't yet know about.
    """
    kind = event.get("kind")
    if kind == "response":
        text = event.get("text", "") or ""
        tool_calls = event.get("tool_calls", []) or []
        if text or tool_calls:
            _print_agent_panel(console, label, text, tool_calls)
        return
    if kind == "tool":
        summary = _summarize_tool_result(event.get("text", ""))
        if event.get("failed"):
            console.print(f"  [red]✗ {summary}[/]")
        else:
            console.print(f"  [dim cyan]✓ {summary}[/]")
        return
    if kind == "assistant":
        text = event.get("text", "") or ""
        if text:
            _print_agent_panel(console, label, text, [])
        return
    if kind == "spawned":
        addr = event.get("addr", "")
        spawned_label = event.get("label", "") or addr
        parent = event.get("parent")
        if parent:
            console.print(f"  [dim]+ spawned {spawned_label} ({addr}) under {parent}[/]")
        else:
            console.print(f"  [dim]+ root {spawned_label} ({addr})[/]")
        return
    if kind == "terminated":
        addr = event.get("addr", "")
        console.print(f"  [red dim]× terminated {addr}[/]")
        return
    # user / unknown — skip silently.


def _print_agent_panel(
    console: Console,
    label: str,
    text: str,
    tool_calls: list[Any],
) -> None:
    """Render one agent's response (prose + tool calls) inside a
    labeled rounded panel. ``tool_calls`` is a list of plain dicts
    ``{"name": ..., "args": ...}`` matching the event schema."""
    pieces: list[Any] = []
    if text:
        pieces.append(Text(text, no_wrap=False))
    if tool_calls:
        if text:
            pieces.append(Text(""))  # blank line between prose and calls
        for tc in tool_calls:
            line = Text("  ← ", style="cyan")
            line.append(_tool_name_from_dict(tc), style="bold cyan")
            line.append(f"({_args_preview(_tool_args_from_dict(tc))})", style="cyan")
            pieces.append(line)
    body = Group(*pieces) if pieces else Text("")
    panel = Panel(
        body,
        title=f"[bold magenta]{label}[/]",
        title_align="left",
        border_style="magenta",
        box=ROUNDED,
        padding=(0, 1),
        expand=True,
    )
    console.print(panel)


def _tool_name_from_dict(tc: dict[str, Any]) -> str:
    return tc.get("name") or tc.get("tool_name") or "?"


def _tool_args_from_dict(tc: dict[str, Any]) -> Any:
    return tc.get("args") or tc.get("arguments") or {}


def _summarize_tool_result(text: str) -> str:
    """Compact a tool's return value (a stringified dict) to one line.

    Tools return ``{"ok": True, ...}`` or
    ``{"ok": False, "code": ..., "error": ...}``. The summary surfaces
    the success/failure and the most-meaningful one field. Falls back
    to a truncated raw preview when the body isn't a parseable dict.
    """
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

    # Success — pick the most informative field to surface.
    for key in ("address", "result", "msg_id", "terminated", "envelopes", "next_seq"):
        if key in parsed:
            value = parsed[key]
            if isinstance(value, list):
                return f"{key}=[{len(value)} item(s)]"
            return f"{key}={_truncate(repr(value), 80)}"
    return "ok"


def _parse_dict(text: str) -> Any:
    if not text or not text.strip().startswith("{"):
        return None
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return None


def _args_preview(args: Any) -> str:
    if not isinstance(args, dict):
        s = str(args)
        return _truncate(s, _ARG_PREVIEW_LEN)
    parts = []
    for k, v in args.items():
        s = json.dumps(v, default=str) if not isinstance(v, str) else repr(v)
        s = _truncate(s, _ARG_PREVIEW_LEN)
        parts.append(f"{k}={s}")
    return ", ".join(parts)


def _truncate(s: str, n: int) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def print_banner(console: Console, *, label: str, addr_id: str, engine: str, llm: str) -> None:
    console.print(
        f"[dim][combinator][/dim] runtime up: "
        f"[bold magenta]{label}[/bold magenta] [dim]({addr_id})[/dim] — "
        f"engine={engine}, llm={llm}",
        markup=True,
    )
    console.print(
        "[dim]type :help for commands, :quit to exit[/dim]",
        markup=True,
    )


def print_system(console: Console, message: str) -> None:
    console.print(f"[dim][combinator][/dim] {message}")


def print_error(console: Console, message: str) -> None:
    console.print(f"[red]error:[/] {message}")
