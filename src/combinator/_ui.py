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
- ``[bold magenta]<label>[/bold magenta]`` — an agent's spoken text.
- ``[cyan]← tool_name(args)[/cyan]`` — outgoing tool call.
- ``[dim cyan]✓ summary[/dim cyan]`` — tool result (success).
- ``[red]✗ code: reason[/red]`` — tool result (failure).
- ``[red]error: ...[/red]`` — runtime errors surfaced from drivers.
"""

from __future__ import annotations

import ast
import json
from typing import Any, Callable

from rich.console import Console

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
                _render_message(console, label, msg)
            state["seen"] = len(messages)

        return hook

    return build


def _render_message(console: Console, label: str, msg: Any) -> None:
    """Format and print one orchestral context entry."""
    inner = getattr(msg, "message", None)

    if inner is not None:
        # Response object: assistant text + optional tool_calls.
        # Render text BEFORE tool calls so the narrative reads top-to-
        # bottom (the model usually says "I'll do X" then calls X).
        text = getattr(inner, "text", None)
        if text:
            console.print(f"[bold magenta]{label}[/]  {text}")
        tool_calls = getattr(inner, "tool_calls", None) or []
        for tc in tool_calls:
            console.print(
                f"  [cyan]←[/] [cyan]{_tool_name(tc)}[/]"
                f"({_args_preview(_tool_args(tc))})"
            )
        return

    role = getattr(msg, "role", None)
    text = getattr(msg, "text", "") or ""
    if role == "tool":
        failed = getattr(msg, "failed", False)
        summary = _summarize_tool_result(text)
        if failed:
            console.print(f"  [red]✗ {summary}[/]")
        else:
            console.print(f"  [dim cyan]✓ {summary}[/]")
        return
    if role == "user":
        # The REPL prints the user's input itself; skip the echo from context.
        return
    if role == "assistant" and text:
        console.print(f"[bold magenta]{label}[/]  {text}")


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


def _tool_name(tc: Any) -> str:
    return getattr(tc, "tool_name", None) or getattr(tc, "name", "?")


def _tool_args(tc: Any) -> Any:
    return getattr(tc, "arguments", None) or {}


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
