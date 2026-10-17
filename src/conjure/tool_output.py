"""Cap the size of a tool result before a model sees it.

One tool call can return more text than a model's whole context window
(a script that prints 16M characters). Handed back verbatim, the next
model call fails ("prompt is too long") and the agent dies with it.

``cap_tool_output`` keeps a long result's head and tail, puts a
``[... N chars truncated; full output saved to <path>]`` notice between
them, and saves the full text inside the agent's sandbox under
``.conjure/tool-output/`` so the agent can grep or page through it.
``ToolOutputCap`` applies it to every tool result an orchestral agent
receives (conjure's own tools, toolbase toolkits, MCP tools alike) as an
orchestral ``after_call`` tool hook, and records each cut in the agent's
event log as a ``tool_output_truncated`` event.

The cap is ``runtime.tool_output_max_chars`` (config
``runtime.tool_output_max_chars``; 0 or null disables it).
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from orchestral.tools.hooks.base import ToolHook, ToolHookResult

if TYPE_CHECKING:
    from conjure.record import AgentRecord
    from conjure.runtime import Runtime


TOOL_OUTPUT_DIR = Path(".conjure") / "tool-output"

# A result shorter than this is never cut, whatever the configured cap
# (a cap of 50 would leave nothing but the notice).
_MIN_CAP = 1_000


def _safe(text: str, limit: int = 40) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text or "")[:limit].strip("_") or "x"


def save_full_output(
    sandbox: Path, text: str, *, tool_name: str, call_id: str = ""
) -> Path:
    """Write ``text`` to a fresh file under ``<sandbox>/.conjure/tool-output``
    and return its path."""
    folder = sandbox / TOOL_OUTPUT_DIR
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    stem = f"{stamp}-{_safe(tool_name)}-{_safe(call_id, 24)}"
    path = folder / f"{stem}.txt"
    n = 1
    while path.exists():
        n += 1
        path = folder / f"{stem}-{n}.txt"
    path.write_text(text, encoding="utf-8", errors="replace")
    return path


def cap_tool_output(
    text: str,
    *,
    max_chars: int | None,
    sandbox: Path | None = None,
    tool_name: str = "",
    call_id: str = "",
) -> tuple[str, dict[str, Any] | None]:
    """Return ``(text_for_the_model, info)``. ``info`` is ``None`` when
    ``text`` fits; otherwise it describes the cut (``chars``, ``kept``,
    ``truncated``, ``path`` of the saved full output or ``None``)."""
    if not max_chars or max_chars <= 0 or len(text) <= max(max_chars, _MIN_CAP):
        return text, None
    max_chars = max(max_chars, _MIN_CAP)
    total = len(text)
    head_n = max_chars // 2
    tail_n = max_chars - head_n
    omitted = total - head_n - tail_n
    saved: Path | None = None
    where: str
    if tool_name == "Read":
        # The file is already on disk: page through it instead of
        # saving another copy.
        where = "the file is on disk: page through it with Read(offset=..., limit=...) or grep it with Bash"
    else:
        if sandbox is not None:
            try:
                saved = save_full_output(sandbox, text, tool_name=tool_name, call_id=call_id)
            except OSError:
                saved = None
        if saved is not None:
            try:
                shown = str(saved.relative_to(sandbox))  # type: ignore[arg-type]
            except ValueError:
                shown = str(saved)
            where = (
                f"full output ({total} chars) saved to {shown}; search it with "
                f"Bash (grep -n PATTERN {shown}) or page it with Read(offset=..., limit=...)"
            )
        else:
            where = "full output was not saved (no sandbox); rerun with less output"
    notice = (
        f"\n\n[... {omitted} chars truncated (tool output over "
        f"runtime.tool_output_max_chars={max_chars}); {where} ...]\n\n"
    )
    info = {
        "tool": tool_name,
        "chars": total,
        "kept": head_n + tail_n,
        "truncated": omitted,
        "path": str(saved) if saved is not None else None,
    }
    return text[:head_n] + notice + text[total - tail_n:], info


class ToolOutputCap(ToolHook):
    """orchestral ``after_call`` hook applying ``cap_tool_output`` to an
    agent's tool results. Reads the cap from the runtime at each call,
    so an embedding harness can change it on a live runtime."""

    def __init__(
        self,
        record: "AgentRecord",
        runtime: "Runtime",
        *,
        emit: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._record = record
        self._runtime = runtime
        self._emit = emit

    def _sandbox(self) -> Path | None:
        try:
            from conjure.tools.filesystem import _sandbox_for

            return _sandbox_for(self._record, self._runtime)
        except Exception:
            return None

    def after_call(self, tool_name: str, result: Any) -> ToolHookResult:
        text = getattr(result, "text", None)
        max_chars = getattr(self._runtime, "tool_output_max_chars", None)
        if not isinstance(text, str) or not max_chars or len(text) <= max(max_chars, _MIN_CAP):
            return ToolHookResult()
        call_id = getattr(result, "tool_call_id", "") or ""
        capped, info = cap_tool_output(
            text,
            max_chars=max_chars,
            sandbox=self._sandbox() if tool_name != "Read" else None,
            tool_name=tool_name,
            call_id=call_id,
        )
        if info is None:
            return ToolHookResult()
        from orchestral.context.message import Message

        replaced = Message(
            role=getattr(result, "role", "tool") or "tool",
            text=capped,
            tool_call_id=call_id or None,
            failed=bool(getattr(result, "failed", False)),
        )
        replaced.metadata = {**(getattr(result, "metadata", None) or {}), "conjure_truncated": info}
        self._log({"kind": "tool_output_truncated", "tool_call_id": call_id, **info})
        return ToolHookResult(modified_result=replaced)

    def _log(self, event: dict[str, Any]) -> None:
        emit = self._emit
        if emit is None:
            log = getattr(self._record, "event_log", None)
            emit = getattr(log, "emit", None)
        if emit is None:
            return
        try:
            emit(event)
        except Exception:
            pass
