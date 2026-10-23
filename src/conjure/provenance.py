"""Session provenance: what ran, on what, and how it went.

A session directory holds the raw record: ``journal.jsonl`` (spawns,
messages, terminations, grants, and ``agent_meta``: which engine and
model each agent ran on, its system prompt, its cost) and one event log
per agent under ``agents/`` (every reply, tool call, tool result, turn
and token count). This module adds:

- ``manifest.json``, written when the session starts: the frozen
  config, the software that ran it (conjure, toolbase, SDK and CLI
  versions), the installed toolkit versions, and the start time;
- ``summarize()``: per-agent and session totals computed from the
  record (turns, errored turns, tool calls and failures, output tokens,
  active and wall time, messages, cost), used by ``conjure export`` and
  the web UI's run record.

Everything here is best-effort and read-only toward the runtime: a
probe that fails leaves its field empty, never stops a session.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
MANIFEST = "manifest.json"
_PACKAGES = ("conjure-agents", "toolbase", "orchestral-ai", "claude-agent-sdk", "mcp", "openai")
_CLIS = {"claude": ["claude", "--version"], "codex": ["codex", "--version"]}


def environment() -> dict[str, Any]:
    packages = {}
    for name in _PACKAGES:
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    clis = {}
    for name, cmd in _CLIS.items():
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            text = (out.stdout or out.stderr).strip().splitlines()
            if out.returncode == 0 and text:
                clis[name] = text[0]
        except (OSError, subprocess.SubprocessError):
            continue
    return {
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "packages": packages,
        "clis": clis,
    }


def write_manifest(session_dir: Path, *, name: str, config: Any, runtime: Any = None) -> Path:
    """Write ``manifest.json`` for a session that is starting."""
    now = time.time()
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "conjure-session",
        "session": name,
        "started_at": now,
        "started_at_iso": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "config": _dump(config),
        "environment": environment(),
    }
    hub = getattr(runtime, "toolbase", None)
    if hub is not None:
        tb: dict[str, Any] = {
            "project_root": str(hub.project_root) if hub.project_root else None,
            "authority": hub.authority_mode,
        }
        try:
            tb["toolkits"] = {
                n: info.version for n, info in sorted(hub.catalog().toolkits.items())
            }
        except Exception as exc:
            tb["catalog_error"] = str(exc)
        manifest["toolbase"] = tb
    path = Path(session_dir) / MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def read_manifest(session_dir: Path) -> dict[str, Any]:
    try:
        return json.loads((Path(session_dir) / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def token_usage(tokens: dict[str, Any] | None) -> dict[str, int]:
    """One response's token counts as ``{"input", "output", "cache_read",
    "cache_creation"}``, the same for every provider.

    ``input`` is every input token the model processed, cached ones
    included; ``cache_read`` and ``cache_creation`` are the parts of it
    read from and written to the prompt cache. Anthropic-style counts
    (``input_tokens`` beside ``cache_read_input_tokens`` and
    ``cache_creation_input_tokens``) exclude the cache from the input, so
    it is added back; OpenAI-style counts (``prompt_tokens`` with
    ``cached_prompt_tokens`` or ``cached_input_tokens``) already include
    it.
    """
    t = tokens or {}

    def n(*keys: str) -> int:
        for key in keys:
            v = t.get(key)
            if v:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return 0
        return 0

    read_separate = n("cache_read_input_tokens")
    creation = n("cache_creation_input_tokens")
    read_included = n("cached_prompt_tokens", "cached_input_tokens")
    base = n("prompt_tokens", "input_tokens")
    return {
        "input": base + read_separate + creation,
        "output": n("completion_tokens", "output_tokens"),
        "cache_read": read_separate or read_included,
        "cache_creation": creation,
    }


def add_usage(total: dict[str, int], part: dict[str, int]) -> dict[str, int]:
    """``total`` with ``part`` (both :func:`token_usage` dicts) added in place."""
    for key in ("input", "output", "cache_read", "cache_creation"):
        total[key] = int(total.get(key, 0)) + int(part.get(key, 0))
    return total


def _dump(config: Any) -> Any:
    if config is None:
        return None
    dump = getattr(config, "model_dump", None)
    return dump(mode="json") if callable(dump) else config


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def summarize(session_dir: Path) -> dict[str, Any]:
    """Per-agent and session totals from the journal and event logs."""
    session_dir = Path(session_dir)
    journal = _read_jsonl(session_dir / "journal.jsonl")
    agents: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    sent: dict[str, int] = {}
    received: dict[str, int] = {}
    terminated: dict[str, float] = {}
    first_ts = last_ts = None
    for entry in journal:
        kind, payload, ts = entry.get("kind"), entry.get("payload") or {}, entry.get("ts")
        if isinstance(ts, (int, float)):
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)
        if kind == "spawn":
            addr = payload.get("addr") or {}
            spec = payload.get("spec") or {}
            aid = addr.get("id")
            if not aid:
                continue
            order.append(aid)
            agents[aid] = {
                "id": aid,
                "label": addr.get("label") or aid,
                "parent": (payload.get("parent") or {}).get("id"),
                "spawned_at": ts,
                "role_prompt": spec.get("role_prompt"),
                "engine": spec.get("engine"),
                "model": spec.get("model"),
                "toolkits": spec.get("toolkits"),
                "internal": bool(spec.get("internal")),
            }
        elif kind == "agent_meta":
            a = agents.get(payload.get("addr"))
            if a is not None:
                meta = dict(payload.get("meta") or {})
                if "system_prompt" in meta:
                    a["system_prompt"] = meta.pop("system_prompt")
                a.update({k: v for k, v in meta.items() if v is not None})
        elif kind == "toolkits":
            a = agents.get((payload.get("addr") or {}).get("id"))
            if a is not None and payload.get("toolkits") is not None:
                a["toolkits"] = payload.get("toolkits")
        elif kind == "send":
            env = payload.get("envelope") or {}
            src = (env.get("from") or {}).get("id")
            dst = (env.get("to") or {}).get("id")
            if src:
                sent[src] = sent.get(src, 0) + 1
            if dst:
                received[dst] = received.get(dst, 0) + 1
        elif kind == "terminate":
            aid = (payload.get("addr") or {}).get("id")
            if aid and isinstance(ts, (int, float)):
                terminated.setdefault(aid, ts)

    totals = {"turns": 0, "errored_turns": 0, "tool_calls": 0, "tool_errors": 0,
              "tokens_out": 0, "active_s": 0.0, "cost_usd": 0.0}
    for aid in order:
        a = agents[aid]
        stats = _agent_stats(session_dir / "agents" / f"{aid}.jsonl")
        for k in ("first_event", "last_event"):
            v = stats.get(k)
            if isinstance(v, (int, float)):
                first_ts = v if first_ts is None else min(first_ts, v)
                last_ts = v if last_ts is None else max(last_ts, v)
        a.update(stats)
        a["messages_sent"] = sent.get(aid, 0)
        a["messages_received"] = received.get(aid, 0)
        a["terminated_at"] = terminated.get(aid)
        for k in totals:
            v = a.get(k)
            if isinstance(v, (int, float)):
                totals[k] += v
    totals["active_s"] = round(totals["active_s"], 3)
    totals["cost_usd"] = round(totals["cost_usd"], 6)
    return {
        "schema_version": SCHEMA_VERSION,
        "session": session_dir.name,
        "started_at": first_ts,
        "ended_at": last_ts,
        "wall_s": round(last_ts - first_ts, 3) if first_ts is not None and last_ts is not None else None,
        "agents": [agents[a] for a in order],
        "agent_count": len(order),
        "messages": sum(sent.values()),
        "totals": totals,
    }


def _agent_stats(log: Path) -> dict[str, Any]:
    turns = errored = calls = tool_errors = tokens = 0
    active = 0.0
    turn_start: float | None = None
    turn_tokens = 0
    first = last = None
    for ev in _read_jsonl(log):
        kind, ts = ev.get("kind"), ev.get("ts")
        if isinstance(ts, (int, float)):
            first = ts if first is None else first
            last = ts
        if kind == "turn_start":
            turns += 1
            turn_start = ts if isinstance(ts, (int, float)) else None
            turn_tokens = 0
        elif kind == "turn_end":
            if ev.get("errored"):
                errored += 1
            if turn_start is not None and isinstance(ts, (int, float)):
                active += ts - turn_start
            tokens += turn_tokens
            turn_start, turn_tokens = None, 0
        elif kind == "usage":
            # Cumulative within a turn for some engines: keep the max.
            turn_tokens = max(turn_tokens, int(ev.get("tokens_out") or 0))
        elif kind == "stream_end":
            calls += len(ev.get("tool_calls") or [])
        elif kind == "tool" and ev.get("failed"):
            tool_errors += 1
    tokens += turn_tokens  # a turn still open when the log ends
    return {
        "turns": turns,
        "errored_turns": errored,
        "tool_calls": calls,
        "tool_errors": tool_errors,
        "tokens_out": tokens,
        "active_s": round(active, 3),
        "first_event": first,
        "last_event": last,
    }
