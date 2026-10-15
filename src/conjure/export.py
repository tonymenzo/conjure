"""``conjure export``: a session as a portable, read-only record.

A session directory is a working artifact: absolute paths from the
machine that ran it, and the sandboxes live elsewhere in the store.
An export is what you can hand to a collaborator, attach to a paper,
or keep as the audit trail of a benchmark run:

  manifest.json   the session manifest (frozen config, software and
                  toolkit versions, start time) plus an ``export`` block
                  saying what this bundle contains and how it was made
  summary.json    session and per-agent totals: turns, errored turns,
                  tool calls and failures, output tokens, active and
                  wall time, messages, cost
  agents.jsonl    one flat, schema-versioned row per agent (engine,
                  model, system prompt, toolkits, totals) — what a
                  results table consumes without knowing conjure
  journal.jsonl   the raw journal: spawns, messages, terminations,
                  grants, per-agent provenance
  agents/         each agent's event log: every reply, tool call and
                  result, turn and token count
  sandboxes/      each agent's working files (optional; large files
                  are listed in the manifest, not copied)

``conjure web --replay <export dir or .tar.gz>`` opens it in the web UI,
read-only, with the scrubber, graph, timeline and files.

Paths in the record files are scrubbed by default: the session and
store directories and your home become ``${SESSION}``, ``${STORE}`` and
``${HOME}``. Sandbox files are copied verbatim (they're the work
itself). Credentials never leave: Codex homes (which link your login)
are not part of an export.
"""

from __future__ import annotations

import json
import re
import shutil
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from conjure.provenance import SCHEMA_VERSION, read_manifest, summarize

_RECORD_SUFFIXES = {".json", ".jsonl", ".md", ".txt", ".yaml", ".yml"}
# Inside sandboxes: harness plumbing, not the agent's work.
_SKIP_IN_SANDBOX = {".claude", ".codex_home", "__pycache__", ".DS_Store"}


def export_session(
    session_dir: Path,
    out_dir: Path,
    *,
    store_dir: Path | None = None,
    sandboxes: bool = True,
    max_file_mb: float = 50.0,
    scrub: bool = True,
    archive: bool = False,
) -> dict[str, Any]:
    session_dir = Path(session_dir).resolve()
    if not (session_dir / "journal.jsonl").is_file():
        raise FileNotFoundError(f"no journal in {session_dir}")
    store = Path(store_dir).resolve() if store_dir else session_dir.parent.parent
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(f"{out_dir} exists and isn't empty")
    out_dir.mkdir(parents=True, exist_ok=True)
    clean = _scrubber(session_dir, store) if scrub else (lambda text: text)

    summary = summarize(session_dir)
    agent_ids = [a["id"] for a in summary["agents"]]

    _copy_text(session_dir / "journal.jsonl", out_dir / "journal.jsonl", clean)
    (out_dir / "agents").mkdir(exist_ok=True)
    for aid in agent_ids:
        src = session_dir / "agents" / f"{aid}.jsonl"
        if src.is_file():
            _copy_text(src, out_dir / "agents" / f"{aid}.jsonl", clean)

    copied: dict[str, int] = {}
    omitted: list[dict[str, Any]] = []
    if sandboxes:
        for a in summary["agents"]:
            src = _sandbox_of(a, session_dir, store)
            if src is None:
                continue
            n = _copy_sandbox(src, out_dir / "sandboxes" / a["id"], max_file_mb, omitted, a["id"])
            if n:
                copied[a["id"]] = n

    rows = [_agent_row(a, summary) for a in summary["agents"]]
    (out_dir / "agents.jsonl").write_text(
        "".join(clean(json.dumps(r, default=str)) + "\n" for r in rows), encoding="utf-8"
    )
    (out_dir / "summary.json").write_text(clean(json.dumps(summary, indent=2, default=str)) + "\n", encoding="utf-8")

    manifest = read_manifest(session_dir) or {"schema_version": SCHEMA_VERSION, "session": session_dir.name}
    from conjure import __version__

    now = time.time()
    manifest["export"] = {
        "schema_version": SCHEMA_VERSION,
        "exported_at": now,
        "exported_at_iso": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "conjure": __version__,
        "scrubbed": scrub,
        "contents": {
            "agents": len(agent_ids),
            "event_logs": sum(1 for _ in (out_dir / "agents").glob("*.jsonl")),
            "sandbox_files": copied,
            "omitted_files": omitted,
        },
        "view": "conjure web --replay <this directory or its .tar.gz>",
    }
    (out_dir / "manifest.json").write_text(clean(json.dumps(manifest, indent=2, default=str)) + "\n", encoding="utf-8")
    (out_dir / "README.md").write_text(_readme(manifest, summary), encoding="utf-8")

    report: dict[str, Any] = {
        "out": str(out_dir),
        "session": session_dir.name,
        "agents": len(agent_ids),
        "sandbox_files": sum(copied.values()),
        "omitted": len(omitted),
        "scrubbed": scrub,
    }
    if archive:
        tar_path = out_dir.with_name(out_dir.name + ".tar.gz")
        with tarfile.open(tar_path, "w:gz") as tar:
            tar.add(out_dir, arcname=out_dir.name)
        report["archive"] = str(tar_path)
        report["archive_bytes"] = tar_path.stat().st_size
    return report


def open_export(path: Path, workdir: Path) -> Path:
    """The session directory inside an export: the directory itself, or
    a ``.tar.gz`` unpacked (safely) under ``workdir``."""
    path = Path(path).expanduser()
    if path.is_dir():
        return path
    if path.is_file() and tarfile.is_tarfile(path):
        workdir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(path) as tar:
            # Refuse absolute paths, .., links out of the tree, devices.
            tar.extractall(workdir, filter="data")
        found = sorted(workdir.rglob("journal.jsonl"), key=lambda p: len(p.parts))
        if found:
            return found[0].parent
    raise FileNotFoundError(f"{path} isn't a conjure export")


# ----- helpers -----


def _scrubber(session_dir: Path, store: Path) -> Callable[[str], str]:
    subs: list[tuple[re.Pattern[str], str]] = []
    for path, label in ((session_dir, "${SESSION}"), (store, "${STORE}"), (Path.home(), "${HOME}")):
        for form in {str(path), str(path.resolve())}:
            if form and form != "/":
                subs.append((re.compile(re.escape(form)), label))
    subs.sort(key=lambda s: -len(s[0].pattern))  # longest (most specific) first

    def scrub(text: str) -> str:
        for pat, rep in subs:
            text = pat.sub(rep, text)
        return text

    return scrub


def _copy_text(src: Path, dst: Path, clean: Callable[[str], str]) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(clean(src.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")


def _sandbox_of(agent: dict[str, Any], session_dir: Path, store: Path) -> Path | None:
    for cand in (session_dir / "sandboxes" / agent["id"], store / "sandboxes" / agent["id"]):
        if cand.is_dir():
            return cand
    return None


def _copy_sandbox(src: Path, dst: Path, max_mb: float, omitted: list, aid: str) -> int:
    n = 0
    limit = max_mb * 1024 * 1024
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if any(part in _SKIP_IN_SANDBOX for part in rel.parts) or path.is_symlink():
            continue
        if not path.is_file():
            continue
        size = path.stat().st_size
        if size > limit:
            omitted.append({"agent": aid, "path": str(rel), "bytes": size})
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        n += 1
    return n


def _agent_row(a: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "session": summary["session"],
        "agent": a["id"],
        "label": a.get("label"),
        "parent": a.get("parent"),
        "engine": a.get("engine"),
        "model": a.get("model"),
        "model_source": a.get("model_source"),
        "role_prompt": a.get("role_prompt"),
        "system_prompt": a.get("system_prompt"),
        "toolkits": a.get("toolkits"),
        "spawned_at": a.get("spawned_at"),
        "terminated_at": a.get("terminated_at"),
        "turns": a.get("turns"),
        "errored_turns": a.get("errored_turns"),
        "tool_calls": a.get("tool_calls"),
        "tool_errors": a.get("tool_errors"),
        "tokens_out": a.get("tokens_out"),
        "active_s": a.get("active_s"),
        "messages_sent": a.get("messages_sent"),
        "messages_received": a.get("messages_received"),
        "cost_usd": a.get("cost_usd"),
    }


def _readme(manifest: dict[str, Any], summary: dict[str, Any]) -> str:
    t = summary["totals"]
    lines = [
        f"# conjure session {summary['session']}",
        "",
        f"Exported {manifest['export']['exported_at_iso']} by conjure {manifest['export']['conjure']}.",
        "",
        "View it (read-only): `conjure web --replay <this directory or its .tar.gz>`",
        "",
        f"- agents: {summary['agent_count']}, messages: {summary['messages']}",
        f"- wall time: {summary.get('wall_s')} s, agent active time: {t['active_s']} s",
        f"- turns: {t['turns']} ({t['errored_turns']} errored), tool calls: {t['tool_calls']} ({t['tool_errors']} failed)",
        f"- output tokens: {t['tokens_out']}, cost: ${t['cost_usd']}",
        "",
        "Files: manifest.json (config, versions), summary.json, agents.jsonl (one row per",
        "agent), journal.jsonl, agents/ (event logs), sandboxes/ (working files).",
        "",
    ]
    return "\n".join(lines)
