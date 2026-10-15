"""Codex engine: each agent is a Codex CLI thread (``codex exec --json``).

Runs under the user's logged-in ChatGPT subscription (``codex login``);
``OPENAI_API_KEY`` is withheld from the CLI so a stray key never flips
it to per-token billing. Each turn is one ``codex exec`` process:

- the first turn starts a thread (``-C <sandbox>``) and records its
  ``thread_id``; later turns run ``codex exec resume <thread_id>``, so
  the conversation carries over;
- conjure's tools, and the agent's toolbase toolkits, come from the
  runtime's loopback MCP server (``conjure.mcp_http``), authenticated
  with this agent's token through an env var;
- ``CODEX_HOME`` is per agent (under the store): ``auth.json`` is
  symlinked in, and nothing else of the user's ``~/.codex`` comes
  along: no ``AGENTS.md``, personal MCP servers, profiles, or ChatGPT
  connectors (``features.apps=false``). Granted toolkit skills are
  surfaced into ``$CODEX_HOME/skills``, where Codex looks for them;
- Codex's own shell and file edits run in its sandbox
  (``workspace-write`` by default: writes confined to the agent's
  sandbox directory). ``codex exec`` can't ask for approval, so there
  is no permission panel for Codex agents yet.

Events are translated into conjure's event-log kinds (``chunk``,
``stream_end`` with tool calls, ``tool`` results, ``usage``,
``thinking_*``), so the web UI, tmux chat and timeline show Codex
agents like any other.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from conjure.record import AgentRecord
    from conjure.runtime import Runtime
    from conjure.toolbase import ToolbaseMount


DEFAULT_SANDBOX = "workspace-write"
_TOKEN_ENV = "CONJURE_MCP_TOKEN"
_MCP_SERVER = "conjure"


def codex_available() -> bool:
    return shutil.which("codex") is not None


class CodexEngine:
    """See the module docstring."""

    def __init__(
        self,
        *,
        record: "AgentRecord",
        runtime: "Runtime",
        sandbox_dir: Path,
        codex_home: Path,
        stream_emit: Callable[[dict[str, Any]], None] | None = None,
        model: str | None = None,
        sandbox_mode: str = DEFAULT_SANDBOX,
        reasoning_effort: str | None = None,
        toolbase_mount: "ToolbaseMount | None" = None,
        codex_bin: str | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self._record = record
        self._runtime = runtime
        # Absolute: codex runs with cwd = the sandbox, so a relative store
        # path would resolve somewhere else.
        self._sandbox = Path(sandbox_dir).expanduser().resolve()
        self._home = Path(codex_home).expanduser().resolve()
        self._emit_fn = stream_emit
        self._model = model
        self._sandbox_mode = sandbox_mode or DEFAULT_SANDBOX
        self._effort = reasoning_effort
        self._toolbase = toolbase_mount
        self._bin = codex_bin or shutil.which("codex") or "codex"
        self._system_prompt = system_prompt
        self.thread_id: str | None = None
        self.system_prompt_text: str | None = None
        self._grant_note: str | None = None
        self._observed_model: str | None = None
        self._context: tuple[int, int] | None = None
        self._proc: subprocess.Popen[str] | None = None
        self._proc_lock = threading.Lock()
        self._interrupted = False
        from conjure.mcp_http import mcp_http_for

        self._mcp = mcp_http_for(runtime)
        self._mcp.register_mount(record.token, toolbase_mount)

    # ----- Engine interface -----

    def step(self, prompt: str) -> str:
        self._sync_toolkits()
        if self._grant_note:
            prompt = f"{self._grant_note}\n\n{prompt}"
            self._grant_note = None
        if self.thread_id is None:
            system = self._system_prompt or build_system_prompt(self._record, self._runtime)
            self.system_prompt_text = system  # provenance
            prompt = f"{system}\n\n---\n\n{prompt}"
        cmd = self._command(prompt)
        env = dict(os.environ)
        env.pop("OPENAI_API_KEY", None)
        env["CODEX_HOME"] = str(self._prepare_home())
        env[_TOKEN_ENV] = self._record.token

        self._emit({"kind": "thinking_start"})
        self._interrupted = False
        with self._proc_lock:
            self._proc = subprocess.Popen(
                cmd,
                cwd=str(self._sandbox),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            proc = self._proc
        stderr: list[str] = []
        drain = threading.Thread(
            target=lambda: stderr.extend(proc.stderr or []), daemon=True
        )
        drain.start()
        final = ""
        streamed = False  # a reply segment is open (chunks since the last stream_end)
        try:
            for raw in proc.stdout or []:
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                kind = ev.get("type")
                item = ev.get("item") or {}
                if kind == "thread.started" and ev.get("thread_id"):
                    self.thread_id = ev["thread_id"]
                if self._observed_model is None and self.thread_id:
                    # The session file names the model from the turn's
                    # start; don't make the UI wait for the turn to end.
                    self._read_session_meta()
                elif kind == "item.completed" and item.get("type") == "agent_message":
                    text = item.get("text") or ""
                    if not text:
                        continue
                    if streamed:  # a new message: close the previous one
                        self._emit({"kind": "stream_end", "tool_calls": []})
                    self._emit({"kind": "chunk", "text": text})
                    streamed, final = True, text
                elif kind == "item.completed":
                    call = _tool_call(item)
                    if call is None:
                        continue
                    # The call closes the reply segment before it (or
                    # stands alone), then its result follows.
                    call.pop("id", None)
                    self._emit({"kind": "stream_end", "tool_calls": [call]})
                    streamed = False
                    text, failed = _tool_result(item)
                    self._emit({"kind": "tool", "text": text, "failed": failed})
                elif kind == "turn.completed":
                    usage = ev.get("usage") or {}
                    out = int(usage.get("output_tokens") or 0)
                    if out:
                        self._emit({"kind": "usage", "tokens_in": 0, "tokens_out": out})
                    inp = int(usage.get("input_tokens") or 0)
                    if inp:
                        window = self._context[1] if self._context else 0
                        self._context = (inp, window)
                elif kind in ("turn.failed", "error"):
                    err = ev.get("error")
                    msg = err.get("message") if isinstance(err, dict) else ev.get("message")
                    if msg:
                        self._emit({"kind": "error", "text": str(msg)})
            proc.wait()
        finally:
            drain.join(timeout=5)
            with self._proc_lock:
                self._proc = None
            if streamed:
                self._emit({"kind": "stream_end", "tool_calls": []})
            self._emit({"kind": "thinking_end"})
        self._read_session_meta()
        if proc.returncode != 0 and not self._interrupted:
            detail = "".join(stderr).strip()[-1500:]
            raise RuntimeError(f"codex exited {proc.returncode}: {detail or 'no output'}")
        return final

    def cost(self) -> float:
        return 0.0  # ChatGPT subscription: flat rate

    def uses_subscription(self) -> bool:
        return True

    def model_name(self) -> str | None:
        return self.model_info()[0]

    def model_info(self) -> tuple[str | None, str]:
        """``observed`` once Codex's session file names the model (from
        the first turn's start); before that the configured ``-m``, or,
        unpinned, the default Codex would pick (``resolved``)."""
        if self._observed_model:
            return self._observed_model, "observed"
        if self._model:
            return self._model, "configured"
        default = codex_default_model(self._home)
        if default:
            return default, "resolved"
        return None, "default"

    def context_usage(self) -> tuple[int, int] | None:
        if self._context and self._context[1]:
            return self._context
        return None

    def interrupt(self) -> None:
        with self._proc_lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        self._interrupted = True
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

    def shutdown(self) -> None:
        self.interrupt()
        self.close_toolkits()

    def close_toolkits(self) -> None:
        self._mcp.register_mount(self._record.token, None)
        if self._toolbase is not None:
            self._toolbase.close()

    # ----- internals -----

    def _sync_toolkits(self) -> None:
        if self._toolbase is None:
            return
        served = self._toolbase.sync()
        if served is None:
            return
        if self.thread_id is not None and served:
            # A live grant mid-conversation: the first prompt listed the
            # original set only.
            from conjure.engines.claude_agent import grant_note

            self._grant_note = grant_note(served, "mcp__conjure__", self._toolbase.skill_names())
        # Nothing to rewire: each turn's codex process opens a fresh MCP
        # session, which lists the mount's current tools.
        self._emit(
            {
                "kind": "toolkits",
                "tools": [t.get_name() for t in served],
                "skills": self._toolbase.skill_names(),
            }
        )

    def _command(self, prompt: str) -> list[str]:
        if self.thread_id:
            cmd = [self._bin, "exec", "resume", self.thread_id]
        else:
            cmd = [self._bin, "exec", "-C", str(self._sandbox), "-s", self._sandbox_mode]
        cmd += ["--json", "--skip-git-repo-check", "--ignore-user-config"]
        if self.thread_id:
            # ``resume`` takes no -s; carry the sandbox policy as config.
            cmd += ["-c", f"sandbox_mode={json.dumps(self._sandbox_mode)}"]
        cmd += ["-c", "features.apps=false"]
        if self._model:
            cmd += ["-m", self._model]
        if self._effort:
            cmd += ["-c", f"model_reasoning_effort={json.dumps(self._effort)}"]
        url = self._mcp.url
        server = f"mcp_servers.{_MCP_SERVER}"
        cmd += [
            "-c", f"{server}.url={json.dumps(url)}",
            "-c", f"{server}.bearer_token_env_var={json.dumps(_TOKEN_ENV)}",
            # ``codex exec`` can't prompt: without this, MCP calls end as
            # "user cancelled". The grant (or spawn) is the authorization.
            "-c", f"{server}.default_tools_approval_mode={json.dumps('approve')}",
        ]
        cmd.append(prompt)
        return cmd

    def _prepare_home(self) -> Path:
        home = self._home
        home.mkdir(parents=True, exist_ok=True)
        real = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        src, link = real / "auth.json", home / "auth.json"
        if src.exists() and not (link.exists() or link.is_symlink()):
            try:
                link.symlink_to(src)  # a token refresh writes through
            except OSError:
                shutil.copy2(src, link)
        return home

    def _read_session_meta(self) -> None:
        """The model and its context window aren't in the JSON stream;
        Codex records them in the thread's session file."""
        if not self.thread_id:
            return
        try:
            files = sorted((self._home / "sessions").rglob(f"*{self.thread_id}*.jsonl"))
        except OSError:
            return
        if not files:
            return
        model = window = None
        try:
            with open(files[-1], encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    payload = entry.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    # Only Codex's own records: a tool call's arguments can
                    # carry a "model" too (a child's, say).
                    if entry.get("type") == "turn_context" and isinstance(payload.get("model"), str):
                        model = payload["model"]
                    elif entry.get("type") == "event_msg" and isinstance(payload.get("model_context_window"), int):
                        window = payload["model_context_window"]
        except OSError:
            return
        if model:
            self._observed_model = model
        if window:
            used = self._context[0] if self._context else 0
            self._context = (used, window)

    def _emit(self, event: dict[str, Any]) -> None:
        if self._emit_fn is None:
            return
        try:
            self._emit_fn(event)
        except Exception:
            pass


def codex_default_model(home: Path | None = None) -> str | None:
    """The model Codex picks when none is set: the listed model with the
    lowest ``priority`` in its models cache (the agent's own home first,
    then the user's)."""
    homes = [home] if home is not None else []
    homes.append(Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"))
    for h in homes:
        try:
            data = json.loads((Path(h) / "models_cache.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        models = data.get("models") if isinstance(data, dict) else None
        listed = [
            m for m in models or []
            if isinstance(m, dict) and m.get("slug") and m.get("visibility", "list") == "list"
        ]
        if listed:
            return min(listed, key=lambda m: m.get("priority", 1 << 30))["slug"]
    return None


def _tool_call(item: dict[str, Any]) -> dict[str, Any] | None:
    itype = item.get("type")
    iid = item.get("id")
    if itype == "command_execution":
        return {"id": iid, "name": "shell", "args": {"command": _strip_shell(item.get("command") or "")}}
    if itype == "mcp_tool_call":
        name = item.get("tool") or item.get("name") or "mcp_tool"
        return {"id": iid, "name": name, "args": item.get("arguments") or {}}
    if itype == "file_change":
        changes = item.get("changes") or []
        return {"id": iid, "name": "edit", "args": {"changes": changes}}
    if itype == "web_search":
        return {"id": iid, "name": "web_search", "args": {"query": item.get("query") or ""}}
    return None


def _tool_result(item: dict[str, Any]) -> tuple[str, bool]:
    itype = item.get("type")
    if itype == "command_execution":
        code = item.get("exit_code")
        out = str(item.get("aggregated_output") or "")
        return (out if code in (0, None) else f"exit {code}\n{out}"), code not in (0, None)
    if itype == "mcp_tool_call":
        err = item.get("error")
        if err:
            return str(err.get("message") if isinstance(err, dict) else err), True
        result = item.get("result")
        if isinstance(result, dict):
            parts = [c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict)]
            text = "\n".join(p for p in parts if p) or json.dumps(result, default=str)
            return text, bool(result.get("is_error") or result.get("isError"))
        return str(result or ""), False
    if itype == "file_change":
        failed = item.get("status") in ("failed", "error", "declined")
        paths = [c.get("path", "?") for c in item.get("changes") or [] if isinstance(c, dict)]
        return ("edited " + ", ".join(paths)) if paths else str(item.get("status") or ""), failed
    return str(item.get("output") or item.get("status") or ""), False


def _strip_shell(command: str) -> str:
    # ``/bin/zsh -lc 'echo hi'`` → ``echo hi``
    m = re.match(r"^\S*sh -l?c (['\"])(.*)\1$", command, re.S)
    if m:
        return m.group(2)
    m = re.match(r"^\S*sh -l?c (\S.*)$", command, re.S)  # unquoted: /bin/zsh -lc pwd
    return m.group(1) if m else command


def build_system_prompt(record: "AgentRecord", runtime: "Runtime") -> str:
    """The shared agent prompt (``conjure.prompts``), for Codex."""
    from conjure.prompts import build_system_prompt as build

    return build(record, runtime, "codex")
