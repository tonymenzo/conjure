"""The web UI's files view backend (conjure.web.files): workspaces,
listing, search, recent files and reads, all confined to a workspace."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from conjure.web import files
from conjure.web.files import FileAccessError


def _rec(id_, label, sandbox, spawned):
    return SimpleNamespace(
        addr=SimpleNamespace(id=id_, label=label),
        spec=SimpleNamespace(sandbox_dir=str(sandbox) if sandbox else None),
        status="idle", spawned_at=spawned,
    )


class _Runtime:
    """``sandbox_of`` as the runtime has it: ``<store>/sandboxes/<id>``."""

    def __init__(self, store):
        self.store = store

    def sandbox_of(self, addr):
        return (self.store / "sandboxes" / addr.id).resolve()


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    for rel, text in {
        "README.md": "# hi\n",
        "analysis.py": "print('fit')\n",
        "results/fit.json": '{"p1": 9.87}',
        "results/figures/mjj_fit.png": "",
        "work/syst/NOTES.md": "notes\n",
        ".conjure/tool-output/call_9.txt": "full output\n",
        "__pycache__/analysis.cpython-312.pyc": "",
    }.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    (root / "results/figures/mjj_fit.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(64))
    return root


def test_workspaces_group_agents_and_nest(tmp_path, ws):
    rt = _Runtime(tmp_path / "store")
    records = {
        "r": _rec("a-root", "root", ws, 1),
        "f": _rec("a-fit", "fitter", ws, 2),                   # shares the root's
        "s": _rec("a-syst", "syst", ws / "work/syst", 3),     # nested in it
        "d": _rec("a-solo", "solo", None, 4),                  # the default, not created
    }
    out = files.workspaces(records, rt)
    by_id = {w["id"]: w for w in out}
    assert [a["label"] for a in by_id["a-root"]["agents"]] == ["root", "fitter"]
    assert by_id["a-syst"]["parent"] == "a-root" and by_id["a-syst"]["rel"] == "work/syst"
    assert by_id["a-syst"]["depth"] == 1
    # Depth-first: a nested workspace follows its parent.
    ids = [w["id"] for w in out]
    assert ids.index("a-syst") == ids.index("a-root") + 1
    # The default layout's directory is named after the agent: shown as "workspace".
    assert by_id["a-solo"]["name"] == "workspace" and by_id["a-solo"]["exists"] is False
    # Listing workspaces never creates one.
    assert not (tmp_path / "store").exists()


def test_listing_folders_first_dotfiles_hidden(ws):
    out = files.listing(ws, "")
    names = [e["name"] for e in out["entries"]]
    assert names == ["__pycache__", "results", "work", "analysis.py", "README.md"]
    assert ".conjure" in [e["name"] for e in files.listing(ws, "", hidden=True)["entries"]]
    sub = files.listing(ws, "results")
    assert [e["path"] for e in sub["entries"]] == ["results/figures", "results/fit.json"]
    assert sub["path"] == "results"


@pytest.mark.parametrize("rel", ["..", "../x", "results/../../x", "/etc/passwd", "a\x00b"])
def test_paths_cannot_leave_the_workspace(ws, rel):
    if rel == "/etc/passwd":
        # A leading slash is relative to the workspace, not the disk.
        with pytest.raises(FileAccessError, match="not found"):
            files.read(ws, rel)
        return
    with pytest.raises(FileAccessError):
        files.read(ws, rel)


def test_symlink_out_of_the_workspace_is_refused(ws, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("token")
    os.symlink(secret, ws / "link.txt")
    os.symlink(tmp_path, ws / "up")
    with pytest.raises(FileAccessError, match="escapes"):
        files.read(ws, "link.txt")
    with pytest.raises(FileAccessError, match="escapes"):
        files.raw(ws, "up/secret.txt")
    # Listed (so the agent's layout shows), never followed by search.
    assert any(e["name"] == "link.txt" and e["link"] for e in files.listing(ws, "")["entries"])
    assert not any("secret" in e["path"] for e in files.find(ws, "secret")["entries"])


def test_read_text_binary_image_and_truncation(ws, monkeypatch):
    t = files.read(ws, "analysis.py")
    assert t["kind"] == "text" and t["content"] == "print('fit')\n" and not t["truncated"]
    assert files.read(ws, "results/figures/mjj_fit.png")["kind"] == "image"
    (ws / "data.bin").write_bytes(bytes(range(256)) * 4)
    assert files.read(ws, "data.bin")["kind"] == "binary"
    (ws / "latin.txt").write_bytes("café résumé naïve".encode("latin-1"))
    assert files.read(ws, "latin.txt")["content"] == "café résumé naïve"
    (ws / "ansi.log").write_bytes(b"\x1b[31mred\x1b[0m\n")
    assert files.read(ws, "ansi.log")["kind"] == "text"
    monkeypatch.setattr(files, "TEXT_LIMIT", 8)
    (ws / "long.log").write_text("é" * 20)          # a cut through a 2-byte character
    out = files.read(ws, "long.log")
    assert out["kind"] == "text" and out["truncated"] and out["size"] == 40
    with pytest.raises(FileAccessError, match="directory"):
        files.read(ws, "results")


def test_raw_types_never_serve_a_page(ws):
    (ws / "page.html").write_text("<script>alert(1)</script>")
    (ws / "plot.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (ws / "paper.pdf").write_bytes(b"%PDF-1.4\n")
    assert files.raw(ws, "page.html")[1] == "application/octet-stream"
    assert files.raw(ws, "plot.svg")[1] == "image/svg+xml"
    assert files.raw(ws, "paper.pdf") == (b"%PDF-1.4\n", "application/pdf")
    assert files.raw(ws, "results/figures/mjj_fit.png")[1] == "image/png"


def test_raw_refuses_large_files(ws, monkeypatch):
    monkeypatch.setattr(files, "RAW_LIMIT", 4)
    with pytest.raises(FileAccessError, match="larger"):
        files.raw(ws, "analysis.py")


def test_find_matches_every_word_names_first(ws):
    out = files.find(ws, "fit")
    paths = [e["path"] for e in out["entries"]]
    assert paths[:2] == ["results/fit.json", "results/figures/mjj_fit.png"]
    assert [e["path"] for e in files.find(ws, "results png")["entries"]] == ["results/figures/mjj_fit.png"]
    # Dot-folders and caches are skipped unless asked for.
    assert files.find(ws, "call_9")["entries"] == []
    assert files.find(ws, "call_9", hidden=True)["entries"][0]["path"] == ".conjure/tool-output/call_9.txt"
    assert files.find(ws, "cpython")["entries"] == []
    assert files.find(ws, "   ")["entries"] == []


def test_recent_is_newest_first(ws):
    now = time.time()
    for path in ws.rglob("*"):
        os.utime(path, (now - 99_999, now - 99_999))
    for rel, age in [("results/fit.json", 0), ("README.md", 3600), ("analysis.py", 7200)]:
        os.utime(ws / rel, (now - age, now - age))
    paths = [e["path"] for e in files.recent(ws, limit=3)["entries"]]
    assert paths == ["results/fit.json", "README.md", "analysis.py"]


def test_walk_stops_at_the_scan_limit(ws, monkeypatch):
    monkeypatch.setattr(files, "SCAN_LIMIT", 3)
    assert files.find(ws, "e")["truncated"] is True
    assert files.recent(ws)["truncated"] is True
    monkeypatch.setattr(files, "SCAN_LIMIT", 10_000)
    assert files.find(ws, "e")["truncated"] is False


def test_sandbox_dir_does_not_create(tmp_path):
    rt = _Runtime(tmp_path / "store")
    assert files.sandbox_dir(_rec("a-x", "x", None, 0), rt) == (tmp_path / "store/sandboxes/a-x").resolve()
    assert not (tmp_path / "store").exists()
    explicit = tmp_path / "elsewhere"
    assert files.sandbox_dir(_rec("a-y", "y", explicit, 0), rt) == explicit.resolve()
    assert not explicit.exists()


def test_paths_are_posix_relative(ws):
    entry = files.listing(ws, "work/syst")["entries"][0]
    assert entry["path"] == "work/syst/NOTES.md"
    assert Path(entry["path"]).as_posix() == entry["path"]
