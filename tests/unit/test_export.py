"""Provenance, summaries, export, and viewing an export read-only.

A two-agent session runs on fake engines that write real event logs
(replies, tool calls, a failed tool, usage) and a sandbox file; the
driver journals each agent's provenance. Then: summary totals, a
scrubbed export, an archive, and a replay of the archive with model and
files intact.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from conjure.event_log import EventLog
from conjure.export import export_session, open_export
from conjure.provenance import read_manifest, summarize, write_manifest
from conjure.record import AgentSpec
from conjure.runtime import Runtime
from conjure.tools.primitives import send_impl, spawn_impl


class FakeEngine:
    system_prompt_text = "You are a careful physicist."

    def __init__(self, record, runtime):
        self.r, self.rt = record, runtime

    def model_info(self):
        return ("gpt-test-1" if self.r.parent else "claude-test-9"), "observed"

    def cost(self):
        return 0.25

    def step(self, prompt):
        log = self.r.event_log
        log.emit({"kind": "usage", "tokens_out": 40})
        log.emit({"kind": "usage", "tokens_out": 100})  # cumulative within the turn
        log.emit({"kind": "chunk", "text": f"working in {self.rt.store_dir}"})
        log.emit({"kind": "stream_end", "tool_calls": [{"name": "Read", "args": {}}, {"name": "Bash", "args": {}}]})
        log.emit({"kind": "tool", "text": "ok", "failed": False})
        log.emit({"kind": "tool", "text": "boom", "failed": True})
        sandbox = Path(self.rt.store_dir) / "sandboxes" / self.r.addr.id
        sandbox.mkdir(parents=True, exist_ok=True)
        (sandbox / "result.txt").write_text(f"answer from {self.r.addr.label}\n")
        (sandbox / "big.bin").write_bytes(b"x" * 3 * 1024 * 1024)
        if self.r.parent is not None:
            send_impl(token=self.r.token, to=self.r.parent.id, body="done")
        return "ok"


def _session(tmp_path: Path):
    store = tmp_path / "store"
    rt = Runtime(store_dir=store, session_id="s1", engine_factory=FakeEngine)
    agents = rt.session_dir / "agents"

    def listener(rec):
        rec.event_log = EventLog(agents / f"{rec.addr.id}.jsonl")

    rt._spawn_listener = listener
    root = rt.root(AgentSpec(role_prompt="lead", label="root"))
    write_manifest(rt.session_dir, name="s1", config={"root": {"role_prompt": "lead"}}, runtime=rt)
    root_rec = rt.record_for(root)
    spawn_impl(token=root_rec.token, role_prompt="compute", label="worker", initial_message="go")
    deadline = time.time() + 10
    while time.time() < deadline:
        s = summarize(rt.session_dir)
        if s["totals"]["turns"] >= 2:
            break
        time.sleep(0.05)
    time.sleep(0.2)
    return rt, store


def test_provenance_and_summary(tmp_path):
    rt, store = _session(tmp_path)
    try:
        m = read_manifest(rt.session_dir)
        assert m["schema_version"] and m["session"] == "s1" and m["environment"]["python"]
        s = summarize(rt.session_dir)
        by = {a["label"]: a for a in s["agents"]}
        w = by["worker"]
        assert (w["model"], w["model_source"]) == ("gpt-test-1", "observed")
        assert by["root"]["model"] == "claude-test-9"
        assert w["system_prompt"] == "You are a careful physicist."
        assert (w["turns"], w["tool_calls"], w["tool_errors"], w["tokens_out"]) == (1, 2, 1, 100)
        assert w["cost_usd"] == 0.25 and w["messages_sent"] == 1
        assert s["totals"]["turns"] >= 2 and s["messages"] >= 2
    finally:
        rt.shutdown()


def test_export_is_scrubbed_complete_and_viewable(tmp_path):
    rt, store = _session(tmp_path)
    try:
        out = tmp_path / "export"
        rep = export_session(rt.session_dir, out, store_dir=store, max_file_mb=1, archive=True)
    finally:
        rt.shutdown()
    for name in ("manifest.json", "summary.json", "agents.jsonl", "journal.jsonl", "README.md"):
        assert (out / name).is_file(), name
    rows = [json.loads(line) for line in (out / "agents.jsonl").read_text().splitlines()]
    assert {r["label"] for r in rows} == {"root", "worker"}
    assert all(r["schema_version"] for r in rows)
    # Scrubbed: no absolute store path anywhere in the record files.
    for f in out.rglob("*.json*"):
        assert str(store.resolve()) not in f.read_text(), f
    assert "${STORE}" in "".join(f.read_text() for f in (out / "agents").glob("*.jsonl"))
    # Sandboxes: work copied, big file listed not copied.
    worker = next(r for r in rows if r["label"] == "worker")
    assert (out / "sandboxes" / worker["agent"] / "result.txt").read_text() == "answer from worker\n"
    assert not (out / "sandboxes" / worker["agent"] / "big.bin").exists()
    manifest = json.loads((out / "manifest.json").read_text())
    assert any(o["path"] == "big.bin" for o in manifest["export"]["contents"]["omitted_files"])
    assert rep["archive"] and Path(rep["archive"]).is_file()

    # View the archive, read-only, as `conjure web --replay x.tar.gz` does.
    from conjure.web.state import SessionView

    sdir = open_export(Path(rep["archive"]), tmp_path / "unpacked")
    view = SessionView.replay_session(sdir)
    assert view.live is False
    tree = view.snapshot()["tree"]
    child = tree["children"][0]
    assert child["model"] == "gpt-test-1"
    assert view.agent(child["addr"])["system_prompt"] == "You are a careful physicist."
    files = view.files_list(child["addr"], None)
    assert files["ok"] and any(e["name"] == "result.txt" for e in files.get("entries", []))
    record = view.run_record()
    assert record["manifest"]["export"]["scrubbed"] is True
    assert record["summary"]["agent_count"] == 2


def test_export_refuses_to_overwrite_and_never_ships_codex_homes(tmp_path):
    rt, store = _session(tmp_path)
    try:
        (store / "codex_homes" / "ag-x").mkdir(parents=True)
        (store / "codex_homes" / "ag-x" / "auth.json").write_text("secret")
        out = tmp_path / "e"
        export_session(rt.session_dir, out, store_dir=store)
        assert "secret" not in "".join(p.read_text(errors="ignore") for p in out.rglob("*") if p.is_file())
        try:
            export_session(rt.session_dir, out, store_dir=store)
            raise AssertionError("expected FileExistsError")
        except FileExistsError:
            pass
    finally:
        rt.shutdown()
