"""Event dict schema + serializer.

One agent's event log is a stream of JSONL dicts. Each event has a
``kind`` discriminator plus kind-specific fields. The renderer reads
these and produces the same rich output the REPL produces today.

Event kinds (and how the renderer interprets each):

- ``response``    — assistant text plus tool calls in one block
                    (emitted only in *non-streaming* mode)
- ``chunk``       — partial assistant text from a streaming response
                    (emitted only in *streaming* mode, one per text
                    delta from the LLM)
- ``stream_end``  — marks the end of a streaming response and carries
                    any tool calls the model emitted along with it
- ``tool``        — a tool result (success or failure)
- ``user``        — user/peer message arrived in this agent's inbox
                    (skipped in agent-pane render; the user already sees
                    what they typed)
- ``assistant``   — bare assistant text (no Response wrapper)
- ``spawned``     — a meta event written by the orchestrator when this
                    agent is created (and once per direct child it
                    spawns)
- ``system_prompt`` — the role/system prompt this agent was spawned
                    with. Emitted once, immediately after the agent's
                    event log is opened, so the chat pane can show
                    initialization context as the first message.
- ``terminated``  — meta event written when this agent goes away

The orchestral-message serializer (``serialize_message``) is a 1:1
dispatch on context entries. Streaming chunks bypass the serializer —
the engine writes them to the log directly as it receives them.
Lifecycle events are emitted by the runtime / CLI orchestrator.
"""

from __future__ import annotations

from typing import Any


def serialize_message(msg: Any) -> dict[str, Any]:
    """Convert one orchestral context message to a JSON-friendly dict.

    Handles ``Response`` (with optional ``tool_calls``), ``Message``
    with each of the three roles (``tool``, ``user``, ``assistant``),
    and falls back to ``{"kind": "unknown", "text": ...}`` for
    anything else so the log never silently drops information.
    """
    inner = getattr(msg, "message", None)
    if inner is not None:
        text = getattr(inner, "text", "") or ""
        tool_calls = getattr(inner, "tool_calls", None) or []
        return {
            "kind": "response",
            "text": text,
            "tool_calls": [
                {
                    "name": getattr(tc, "tool_name", None) or getattr(tc, "name", "?"),
                    "args": getattr(tc, "arguments", None) or {},
                }
                for tc in tool_calls
            ],
        }
    role = getattr(msg, "role", None)
    text = getattr(msg, "text", "") or ""
    if role == "tool":
        return {
            "kind": "tool",
            "text": text,
            "failed": bool(getattr(msg, "failed", False)),
        }
    if role == "user":
        return {"kind": "user", "text": text}
    if role == "assistant":
        return {"kind": "assistant", "text": text}
    return {"kind": "unknown", "text": text}


def make_spawned_event(*, addr: str, label: str, parent: str | None) -> dict[str, Any]:
    return {
        "kind": "spawned",
        "addr": addr,
        "label": label,
        "parent": parent,
    }


def make_terminated_event(*, addr: str) -> dict[str, Any]:
    return {"kind": "terminated", "addr": addr}


def make_system_prompt_event(*, text: str, label: str = "") -> dict[str, Any]:
    return {"kind": "system_prompt", "text": text or "", "label": label or ""}
