"""Filesystem tools — ``Read``, ``Write``, ``Edit``, ``Bash``,
``Grep``, ``Glob``.

Every tool operates inside the calling agent's *sandbox directory*
and refuses to read or write anything outside it. The sandbox path
comes from:

  1. ``agent.spec.sandbox_dir`` if set explicitly, or
  2. ``{runtime.store_dir}/sandboxes/{agent_id}/`` auto-allocated on
     first use.

If neither is available (no store_dir AND no sandbox_dir on the
spec), filesystem tools refuse to run with ``code="no_sandbox"``.

A per-tool permission decision lives on ``agent.spec.permissions``
(e.g. ``{"Write": "deny", "Bash": "ask"}``). Defaults to ``"allow"``.
``"deny"`` is a hard refusal; ``"ask"`` is treated as deny when no UI
hook is wired (Phase 2 will surface an interactive prompt).

Tool naming and surface match Claude Code's filesystem tools so
prompts and conventions transfer cleanly between engines.
"""

from __future__ import annotations

import fnmatch
import shlex
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from combinator.address import Address
from combinator.tools._base import (
    RuntimeField,
    StateField,
    StatelessRuntimeTool,
    resolve_token,
)

if TYPE_CHECKING:
    from combinator.record import AgentRecord
    from combinator.runtime import Runtime


# ---------- shared helpers ----------


def _err(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "code": code, "error": message}


def _sandbox_for(
    record: "AgentRecord", runtime: "Runtime"
) -> Path | None:
    """Return the agent's sandbox path (create if missing). ``None``
    when neither the spec nor the runtime tell us where to put it."""
    explicit = record.spec.sandbox_dir
    if explicit:
        sb = Path(explicit).expanduser()
    else:
        if runtime.store_dir is None:
            return None
        sb = Path(runtime.store_dir).expanduser() / "sandboxes" / record.addr.id
    sb.mkdir(parents=True, exist_ok=True)
    return sb.resolve()


def _within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _check_permission(record: "AgentRecord", tool_name: str) -> dict[str, Any] | None:
    """Return an error dict if the per-agent permission denies this
    tool, else None."""
    decision = (record.spec.permissions or {}).get(tool_name, "allow")
    if decision == "deny":
        return _err("permission_denied", f"{tool_name} denied by agent permissions")
    if decision == "ask":
        # No interactive UI hook yet — treat as deny so the agent
        # gets a clear signal rather than silently hanging.
        return _err("permission_required", f"{tool_name} requires approval")
    return None


def _enter_sandbox(
    token: str, tool_name: str
) -> "tuple[Path, AgentRecord, Runtime] | dict[str, Any]":
    """Resolve token → (sandbox_path, record, runtime); enforce per-tool
    permission. Returns an error dict on any failure."""
    resolved = resolve_token(token)
    if resolved is None:
        return _err("no_runtime", "tool is not bound to a runtime")
    runtime, caller = resolved
    record = runtime.record_for(caller)
    perm_err = _check_permission(record, tool_name)
    if perm_err is not None:
        return perm_err
    sandbox = _sandbox_for(record, runtime)
    if sandbox is None:
        return _err("no_sandbox", "agent has no sandbox dir configured")
    return sandbox, record, runtime


# ---------- implementation functions ----------


def read_impl(*, token: str, path: str) -> dict[str, Any]:
    res = _enter_sandbox(token, "Read")
    if isinstance(res, dict):
        return res
    sandbox, _record, _runtime = res
    target = (sandbox / path).resolve()
    if not _within(target, sandbox):
        return _err("escape", "path escapes sandbox")
    if not target.exists():
        return _err("not_found", path)
    if target.is_dir():
        return _err("is_directory", path)
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return _err("not_text", "file is not UTF-8 text")
    except OSError as exc:
        return _err("io_error", str(exc))
    rel = str(target.relative_to(sandbox))
    return {"ok": True, "path": rel, "content": content}


def write_impl(*, token: str, path: str, content: str) -> dict[str, Any]:
    res = _enter_sandbox(token, "Write")
    if isinstance(res, dict):
        return res
    sandbox, _record, _runtime = res
    target = (sandbox / path).resolve()
    if not _within(target, sandbox):
        return _err("escape", "path escapes sandbox")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        return _err("io_error", str(exc))
    rel = str(target.relative_to(sandbox))
    return {"ok": True, "path": rel, "bytes": len(content.encode("utf-8"))}


def edit_impl(
    *, token: str, path: str, old_string: str, new_string: str
) -> dict[str, Any]:
    """Exact string replacement. ``old_string`` must appear exactly
    once in the file."""
    res = _enter_sandbox(token, "Edit")
    if isinstance(res, dict):
        return res
    sandbox, _record, _runtime = res
    target = (sandbox / path).resolve()
    if not _within(target, sandbox):
        return _err("escape", "path escapes sandbox")
    if not target.exists():
        return _err("not_found", path)
    try:
        current = target.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        return _err("read_failed", str(exc))
    occurrences = current.count(old_string)
    if occurrences == 0:
        return _err("not_found_in_file", "old_string not present")
    if occurrences > 1:
        return _err(
            "ambiguous",
            f"old_string occurs {occurrences} times; make it unique",
        )
    updated = current.replace(old_string, new_string, 1)
    try:
        target.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return _err("io_error", str(exc))
    return {"ok": True, "path": str(target.relative_to(sandbox))}


def bash_impl(
    *, token: str, command: str, timeout_s: float = 30.0
) -> dict[str, Any]:
    """Run a shell command with ``cwd=sandbox``. The agent has the
    shell's full surface inside the sandbox; permissions on the
    ``Bash`` tool name are the gate."""
    res = _enter_sandbox(token, "Bash")
    if isinstance(res, dict):
        return res
    sandbox, _record, _runtime = res
    try:
        completed = subprocess.run(
            command,
            shell=True,
            cwd=str(sandbox),
            capture_output=True,
            text=True,
            timeout=max(0.1, timeout_s),
        )
    except subprocess.TimeoutExpired:
        return _err("timeout", f"command exceeded {timeout_s}s")
    except OSError as exc:
        return _err("exec_failed", str(exc))
    return {
        "ok": True,
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def grep_impl(
    *, token: str, pattern: str, path: str = ".", max_matches: int = 50
) -> dict[str, Any]:
    """Plain-substring search across all UTF-8 files under ``path``
    (relative to sandbox). For regex use Bash + ``grep``."""
    res = _enter_sandbox(token, "Grep")
    if isinstance(res, dict):
        return res
    sandbox, _record, _runtime = res
    root = (sandbox / path).resolve()
    if not _within(root, sandbox):
        return _err("escape", "path escapes sandbox")
    if not root.exists():
        return _err("not_found", path)
    matches: list[dict[str, Any]] = []
    for f in _walk_files(root):
        if len(matches) >= max_matches:
            break
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern in line:
                matches.append(
                    {
                        "path": str(f.relative_to(sandbox)),
                        "line": lineno,
                        "text": line[:200],
                    }
                )
                if len(matches) >= max_matches:
                    break
    return {"ok": True, "matches": matches, "truncated": len(matches) >= max_matches}


def glob_impl(*, token: str, pattern: str) -> dict[str, Any]:
    """Match files in the sandbox by glob pattern (``**/*.py`` style).
    Patterns are evaluated relative to the sandbox root."""
    res = _enter_sandbox(token, "Glob")
    if isinstance(res, dict):
        return res
    sandbox, _record, _runtime = res
    paths: list[str] = []
    try:
        for p in sandbox.glob(pattern):
            if _within(p, sandbox):
                paths.append(str(p.relative_to(sandbox)))
    except (OSError, ValueError) as exc:
        return _err("glob_failed", str(exc))
    paths.sort()
    return {"ok": True, "paths": paths}


def _walk_files(root: Path):
    """Yield every regular file under ``root``. Skips hidden dirs."""
    if root.is_file():
        yield root
        return
    for p in root.rglob("*"):
        if any(part.startswith(".") for part in p.parts):
            continue
        if p.is_file():
            yield p


# ---------- orchestral tool classes ----------


class ReadTool(StatelessRuntimeTool):
    """Read a UTF-8 text file from the agent's sandbox."""

    path: str = RuntimeField(description="Path relative to the sandbox.")
    runtime_token: str = StateField(description="(internal) caller token.")

    def _run(self) -> dict[str, Any]:
        return read_impl(token=self.runtime_token, path=self.path)


class WriteTool(StatelessRuntimeTool):
    """Write a UTF-8 text file. Overwrites if it exists; creates
    parent directories as needed. Refuses paths outside the sandbox."""

    path: str = RuntimeField(description="Path relative to the sandbox.")
    content: str = RuntimeField(description="Full file content (UTF-8).")
    runtime_token: str = StateField(description="(internal) caller token.")

    def _run(self) -> dict[str, Any]:
        return write_impl(
            token=self.runtime_token, path=self.path, content=self.content
        )


class EditTool(StatelessRuntimeTool):
    """Replace a single exact-string occurrence in a sandbox file.
    ``old_string`` must appear exactly once."""

    path: str = RuntimeField(description="Path relative to the sandbox.")
    old_string: str = RuntimeField(description="Exact text to replace.")
    new_string: str = RuntimeField(description="Replacement text.")
    runtime_token: str = StateField(description="(internal) caller token.")

    def _run(self) -> dict[str, Any]:
        return edit_impl(
            token=self.runtime_token,
            path=self.path,
            old_string=self.old_string,
            new_string=self.new_string,
        )


class BashTool(StatelessRuntimeTool):
    """Run a shell command with ``cwd=sandbox``. Captures stdout
    and stderr. The agent has full shell surface inside the sandbox."""

    command: str = RuntimeField(description="Shell command to run.")
    timeout_s: float = RuntimeField(
        default=30.0, description="Hard timeout in seconds."
    )
    runtime_token: str = StateField(description="(internal) caller token.")

    def _run(self) -> dict[str, Any]:
        return bash_impl(
            token=self.runtime_token,
            command=self.command,
            timeout_s=float(self.timeout_s or 30.0),
        )


class GrepTool(StatelessRuntimeTool):
    """Plain-substring search across UTF-8 files in the sandbox."""

    pattern: str = RuntimeField(description="Substring to find.")
    path: str = RuntimeField(
        default=".", description="Subdirectory to search (relative)."
    )
    max_matches: int = RuntimeField(default=50, description="Cap on matches.")
    runtime_token: str = StateField(description="(internal) caller token.")

    def _run(self) -> dict[str, Any]:
        return grep_impl(
            token=self.runtime_token,
            pattern=self.pattern,
            path=self.path or ".",
            max_matches=int(self.max_matches or 50),
        )


class GlobTool(StatelessRuntimeTool):
    """Match files in the sandbox by glob pattern (``**/*.py`` style)."""

    pattern: str = RuntimeField(description="Glob pattern.")
    runtime_token: str = StateField(description="(internal) caller token.")

    def _run(self) -> dict[str, Any]:
        return glob_impl(token=self.runtime_token, pattern=self.pattern)


FILESYSTEM_TOOL_CLASSES: list[type] = [
    ReadTool,
    WriteTool,
    EditTool,
    BashTool,
    GrepTool,
    GlobTool,
]


def build_filesystem_tools(token: str) -> list[StatelessRuntimeTool]:
    """Instantiate the full filesystem-tool set bound to ``token``."""
    return [cls(runtime_token=token) for cls in FILESYSTEM_TOOL_CLASSES]
