"""Rich-based rendering for the REPL.

The CLI builds one ``Console`` and one ``RichDisplayHookBuilder`` per
session. The hook is handed to the engine factory and invoked by
``orchestral.Agent`` after each context update. It tracks how many
messages it has already rendered so each call only prints the *new*
ones — preventing duplicates when the agent makes multiple tool calls
during a single ``step``.

Visual conventions:

- ``[bold cyan]you ›[/bold cyan]`` — the user prompt.
- ``[bold magenta]<label>[/bold magenta]`` — an agent's spoken text.
- ``[dim cyan]← tool_name(args)[/dim cyan]`` — outgoing tool call.
- ``[dim]→ result preview[/dim]`` — tool result.
- ``[dim]…[/dim]`` — internal status (spinner, etc.).
- ``[red]error: ...[/red]`` — errors surfaced from drivers.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from rich.console import Console
from rich.text import Text

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
        tool_calls = getattr(inner, "tool_calls", None) or []
        for tc in tool_calls:
            console.print(
                f"  [cyan dim]←[/] [cyan]{_tool_name(tc)}[/]"
                f"({_args_preview(_tool_args(tc))})"
            )
        text = getattr(inner, "text", None)
        if text:
            console.print(f"[bold magenta]{label}[/]  {text}")
        return

    role = getattr(msg, "role", None)
    text = getattr(msg, "text", "") or ""
    if role == "tool":
        failed = getattr(msg, "failed", False)
        preview = _truncate(text, _BODY_PREVIEW_LEN)
        if failed:
            console.print(f"  [red]→ tool error:[/] {preview}")
        else:
            console.print(f"  [dim]→ {preview}[/]")
        return
    if role == "user":
        # The REPL prints the user's input itself; skip the echo from context.
        return
    if role == "assistant" and text:
        console.print(f"[bold magenta]{label}[/]  {text}")


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
