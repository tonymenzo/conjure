"""Read-only file browsing for the web UI's files view.

Every agent works in a sandbox directory. Children usually share their
parent's (so one workspace holds a whole team's files), and an agent
given ``sandbox_dir`` works in a folder inside its parent's. This
module maps agents to those workspaces, nests them, and lists, searches
and reads files inside one, refusing any path that resolves (through
``..`` or a symlink) outside it. Nothing here creates or writes a file.
"""

from __future__ import annotations

import heapq
import os
from collections import deque
from pathlib import Path
from typing import Any, Iterator

# Bytes of a text file sent for preview; the rest is a download.
TEXT_LIMIT = 512 * 1024
# Bytes served raw (images, PDFs, downloads).
RAW_LIMIT = 64 * 1024 * 1024
# Directory entries walked by a search or the recent-files list.
SCAN_LIMIT = 100_000
# Entries returned for one directory.
LIST_LIMIT = 5_000
# Not searched or listed as recent (still browsable).
SKIP_DIRS = frozenset({"__pycache__", "node_modules"})

# Served with their own type, for <img> and the PDF viewer. Everything
# else goes out as application/octet-stream, so no file an agent wrote
# is ever handed to the browser as a page.
RAW_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".avif": "image/avif", ".bmp": "image/bmp", ".ico": "image/x-icon",
    ".svg": "image/svg+xml", ".pdf": "application/pdf",
}
_PREVIEW_KINDS = {**{ext: "image" for ext, t in RAW_TYPES.items() if t.startswith("image/")}, ".pdf": "pdf"}


class FileAccessError(ValueError):
    """A path outside the workspace, or one that isn't there."""


def sandbox_dir(record: Any, runtime: Any) -> Path | None:
    """Where ``record``'s agent works, without creating it: its spec's
    ``sandbox_dir``, else the runtime's default for it."""
    raw = record.spec.sandbox_dir
    try:
        path = Path(raw).expanduser() if raw else runtime.sandbox_of(record.addr)
    except Exception:
        return None
    if path is None:
        return None
    try:
        return path.resolve()
    except OSError:
        return path


def workspaces(records: dict[Any, Any], runtime: Any) -> list[dict[str, Any]]:
    """The run's workspaces, outermost first, each with the agents that
    work in it. A workspace inside another names it as ``parent`` and
    gives its path relative to it as ``rel``. Each is identified by the
    address of its first agent, which the other calls take as ``addr``."""
    groups: dict[Path, list[Any]] = {}
    for rec in sorted(records.values(), key=lambda r: (r.spawned_at or 0, r.addr.id)):
        path = sandbox_dir(rec, runtime)
        if path is not None:
            groups.setdefault(path, []).append(rec)
    paths = sorted(groups, key=lambda p: (len(p.parts), str(p)))
    ids = {p: groups[p][0].addr.id for p in paths}
    out = []
    for path in paths:
        parent = next((q for q in reversed(paths) if q != path and q in path.parents), None)
        recs = groups[path]
        out.append({
            "id": ids[path],
            "path": str(path),
            # The default layout names a sandbox after its agent's id.
            "name": "workspace" if path.name == recs[0].addr.id else (path.name or str(path)),
            "parent": ids[parent] if parent is not None else None,
            "rel": str(path.relative_to(parent)) if parent is not None else "",
            "exists": _is_dir(path),
            "agents": [
                {"addr": r.addr.id, "label": r.addr.label or r.addr.id, "status": r.status}
                for r in recs
            ],
        })
    # Depth-first, so a nested workspace follows the one it is in.
    children: dict[str | None, list[dict[str, Any]]] = {}
    for ws in out:
        children.setdefault(ws["parent"], []).append(ws)
    ordered: list[dict[str, Any]] = []

    def walk(key: str | None, depth: int) -> None:
        for ws in children.get(key, []):
            ordered.append({**ws, "depth": depth})
            walk(ws["id"], depth + 1)

    walk(None, 0)
    return ordered


def resolve(root: Path, rel: str | None) -> Path:
    """``rel`` inside ``root``, refusing a path that leaves it."""
    rel = (rel or "").replace("\\", "/").strip().lstrip("/")
    if "\x00" in rel:
        raise FileAccessError("bad path")
    base = root.resolve()
    target = (base / rel).resolve() if rel else base
    if target != base and base not in target.parents:
        raise FileAccessError("path escapes the workspace")
    return target


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root.resolve()).as_posix()


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _stat(entry: os.DirEntry) -> tuple[bool, int, float]:
    """``(is_dir, size, mtime)``; a broken symlink is a 0-byte file."""
    try:
        is_dir = entry.is_dir()
        st = entry.stat()
        return is_dir, (0 if is_dir else st.st_size), st.st_mtime
    except OSError:
        return False, 0, 0.0


def listing(root: Path, rel: str | None, *, hidden: bool = False) -> dict[str, Any]:
    """One directory: folders first, then files, by name."""
    target = resolve(root, rel)
    if not target.exists():
        raise FileAccessError("not found")
    if not target.is_dir():
        raise FileAccessError("not a directory")
    here = _rel(target, root) if target != root.resolve() else ""
    entries = []
    truncated = False
    with os.scandir(target) as it:
        for entry in it:
            if not hidden and entry.name.startswith("."):
                continue
            if len(entries) >= LIST_LIMIT:
                truncated = True
                break
            is_dir, size, mtime = _stat(entry)
            entries.append({
                "name": entry.name,
                "path": f"{here}/{entry.name}" if here else entry.name,
                "dir": is_dir,
                "size": size,
                "mtime": mtime,
                "link": entry.is_symlink(),
            })
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return {"ok": True, "path": here, "entries": entries, "truncated": truncated}


def _walk(root: Path, *, hidden: bool, stats: dict[str, bool]) -> Iterator[tuple[str, os.DirEntry, bool]]:
    """``(relative path, entry, is_dir)`` for everything under ``root``,
    breadth-first, symlinked folders not followed. Stops after
    ``SCAN_LIMIT`` entries, setting ``stats["truncated"]``."""
    stats["truncated"] = False
    base = root.resolve()
    queue = deque([(base, "")])
    seen = 0
    while queue:
        folder, prefix = queue.popleft()
        try:
            with os.scandir(folder) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in sorted(entries, key=lambda e: e.name):
            seen += 1
            if seen > SCAN_LIMIT:
                stats["truncated"] = True
                return
            if not hidden and entry.name.startswith("."):
                continue
            rel = f"{prefix}{entry.name}"
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            yield rel, entry, is_dir
            if is_dir and entry.name not in SKIP_DIRS:
                queue.append((Path(entry.path), rel + "/"))


def find(root: Path, query: str, *, limit: int = 200, hidden: bool = False) -> dict[str, Any]:
    """Files and folders whose path contains every word of ``query``
    (case-insensitive); name matches first, then shorter paths."""
    terms = [t for t in query.lower().split() if t]
    if not terms:
        return {"ok": True, "query": query, "entries": [], "truncated": False}
    resolve(root, "")
    hits = []
    stats: dict[str, bool] = {}
    for rel, entry, is_dir in _walk(root, hidden=hidden, stats=stats):
        low = rel.lower()
        if all(t in low for t in terms):
            name = entry.name.lower()
            in_name = sum(t in name for t in terms)
            _, size, mtime = _stat(entry)
            hits.append(((-in_name, len(rel), rel), {"name": entry.name, "path": rel, "dir": is_dir, "size": size, "mtime": mtime}))
    hits.sort(key=lambda h: h[0])
    return {
        "ok": True, "query": query,
        "entries": [e for _, e in hits[:limit]],
        "truncated": len(hits) > limit or stats["truncated"],
    }


def recent(root: Path, *, limit: int = 50, hidden: bool = False) -> dict[str, Any]:
    """The ``limit`` most recently modified files, newest first."""
    resolve(root, "")
    if not _is_dir(root):
        return {"ok": True, "entries": []}
    files = []
    stats: dict[str, bool] = {}
    for rel, entry, is_dir in _walk(root, hidden=hidden, stats=stats):
        if is_dir:
            continue
        _, size, mtime = _stat(entry)
        files.append((mtime, rel, entry.name, size))
    newest = heapq.nlargest(limit, files)
    return {
        "ok": True, "truncated": stats["truncated"],
        "entries": [{"name": n, "path": p, "dir": False, "size": s, "mtime": m} for m, p, n, s in newest],
    }


def preview_kind(name: str) -> str:
    """``image`` | ``pdf`` | ``text`` (text unless the bytes say binary)."""
    return _PREVIEW_KINDS.get(Path(name).suffix.lower(), "text")


def read(root: Path, rel: str | None) -> dict[str, Any]:
    """A file's metadata, and its text when it is text (up to
    ``TEXT_LIMIT`` bytes). Images and PDFs come from :func:`raw`."""
    target = resolve(root, rel)
    if not target.exists():
        raise FileAccessError("not found")
    if target.is_dir():
        raise FileAccessError("is a directory")
    st = target.stat()
    out: dict[str, Any] = {
        "ok": True, "path": _rel(target, root), "name": target.name,
        "size": st.st_size, "mtime": st.st_mtime, "kind": preview_kind(target.name),
        "raw_limit": RAW_LIMIT,
    }
    if out["kind"] != "text":
        return out
    with target.open("rb") as fh:
        data = fh.read(TEXT_LIMIT)
    text = _decode(data)
    if text is None:
        out["kind"] = "binary"
        return out
    out["content"] = text
    out["truncated"] = st.st_size > TEXT_LIMIT
    return out


_TEXT_CONTROLS = frozenset(b"\t\n\r\f\b\x1b")


def _decode(data: bytes) -> str | None:
    """``data`` as text, or ``None`` when it is binary: a NUL, or many
    control characters. UTF-8 (a few bad bytes, or a character cut at
    the preview's end, replaced), else Latin-1."""
    head = data[:8192]
    if b"\x00" in head:
        return None
    if sum(b < 32 and b not in _TEXT_CONTROLS for b in head) > len(head) // 20:
        return None
    text = data.decode("utf-8", errors="replace")
    if text.count("\N{REPLACEMENT CHARACTER}") <= max(1, len(text) // 200):
        return text
    return data.decode("latin-1")


def raw(root: Path, rel: str | None) -> tuple[bytes, str]:
    """``(bytes, content type)`` of a file of at most ``RAW_LIMIT`` bytes."""
    target = resolve(root, rel)
    if not target.is_file():
        raise FileAccessError("not found")
    if target.stat().st_size > RAW_LIMIT:
        raise FileAccessError(f"larger than {RAW_LIMIT // (1024 * 1024)} MB")
    ctype = RAW_TYPES.get(target.suffix.lower(), "application/octet-stream")
    return target.read_bytes(), ctype


__all__ = [
    "FileAccessError", "find", "listing", "preview_kind", "raw", "read",
    "recent", "resolve", "sandbox_dir", "workspaces",
]
