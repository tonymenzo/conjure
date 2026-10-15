"""Web UI backend: the session view (live + replay) and the HTTP server's
routing, auth and read-only rules. Scripted engines, no LLM."""

from __future__ import annotations

import http.client
import json
import os
import stat
import time
from pathlib import Path

import pytest

from conjure import AgentSpec, BehaviorRegistry, Runtime
from conjure.control import ControlServer
from conjure.event_log import EventLog
from conjure.events import make_system_prompt_event
from conjure.tools.primitives import introduce_impl, send_impl, spawn_impl
from conjure.web import ReplayCache, SessionView, WebServer


def _wait(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def session(tmp_path):
    """A live runtime with per-agent event logs (as ``start_session``
    wires them): root spawns two workers and introduces them; the
    ``echo`` worker streams a reply with a tool call."""
    reg = BehaviorRegistry()
    reg.register("idle", lambda *a, **kw: "idle")

    def echo(engine, prompt, envelopes):
        rec = engine_record[engine.token]
        log = rec.event_log
        log.emit({"kind": "thinking_start"})
        log.emit({"kind": "chunk", "text": "hel"})
        log.emit({"kind": "chunk", "text": "lo"})
        log.emit({"kind": "stream_end", "tool_calls": [{"name": "Send", "args": {"to": "parent"}}]})
        log.emit({"kind": "tool", "text": '{"ok": true, "msg_id": "m1"}', "failed": False})
        log.emit({"kind": "thinking_end"})
        for env in envelopes:
            if env.from_.id != "@user":
                send_impl(token=engine.token, to=env.from_.id, body="pong")
        return "ok"

    reg.register("echo", echo)
    engine_record: dict = {}
    base_factory = reg.factory()

    def factory(record, runtime):
        engine_record[record.token] = record
        return base_factory(record, runtime)

    rt = Runtime(store_dir=tmp_path, session_id="s1", engine_factory=factory)
    agents = rt.session_dir / "agents"

    def listener(rec):
        rec.event_log = EventLog(agents / f"{rec.addr.id}.jsonl")
        if rec.parent is not None:
            rec.event_log.emit(make_system_prompt_event(text=rec.spec.role_prompt, label=rec.spec.label))

    rt._spawn_listener = listener
    root = rt.root(AgentSpec(role_prompt="idle", label="root"))
    tok = rt.record_for(root).token
    a = spawn_impl(token=tok, role_prompt="echo", label="a")
    b = spawn_impl(token=tok, role_prompt="idle", label="b", budget=1.0)
    introduce_impl(token=tok, child=a["address"], capability=b["address"])
    send_impl(token=tok, to="a", body="ping")
    a_addr = rt.address_by_id(a["address"])
    assert _wait(lambda: any(e.from_ == a_addr for e in rt.read_inbox(root)))
    assert _wait(lambda: rt.record_for(a_addr).status == "idle")
    control = ControlServer(runtime=rt, socket_path=tmp_path / "unused.sock")
    view = SessionView.live_session(rt, control, name="s1")
    yield {"rt": rt, "view": view, "root": root, "a": a_addr, "b": rt.address_by_id(b["address"]), "dir": rt.session_dir}
    for rec in list(rt._records.values()):
        if getattr(rec, "event_log", None) is not None:
            rec.event_log.close()
    rt.shutdown()


# ---------- SessionView ----------


def test_snapshot_tree_carries_ui_fields(session):
    snap = session["view"].snapshot()
    tree = snap["tree"]
    assert tree["label"] == "root"
    labels = [c["label"] for c in tree["children"]]
    assert labels == ["a", "b"]  # spawn order
    b = tree["children"][1]
    assert b["budget"] == 1.0 and b["parent"] == tree["addr"] and b["depth"] == 1
    assert {"subtree_cost", "toolkits", "awaiting_permission", "inbox_size"} <= set(b)
    bodies = [a["body"] for a in snap["activity"]]
    assert "ping" in bodies and "pong" in bodies


def test_events_tail_and_paging(session):
    view, a = session["view"], session["a"].id
    full = view.events(a)
    kinds = [e["kind"] for e in full["events"]]
    assert kinds[0] == "system_prompt"
    assert "chunk" in kinds and "turn_start" in kinds and "turn_end" in kinds
    assert full["offset"] == full["size"]
    # Nothing new since the end.
    assert view.events(a, since=full["offset"])["events"] == []
    # A small window pages back on line boundaries.
    tail = view.events(a, window=200)
    assert 0 < tail["start"] < full["size"]
    older = view.events(a, before=tail["start"], window=10_000)
    assert older["start"] == 0
    assert len(older["events"]) + len(tail["events"]) == len(full["events"])


def test_graph_edges(session):
    g = session["view"].graph()
    a, b, root = session["a"].id, session["b"].id, session["root"].id
    assert {"from": root, "to": a} in g["spawn"]
    # The introduction shows up as a capability edge a → b.
    assert {"from": a, "to": b} in g["capability"]
    pairs = {(m["from"], m["to"]): m["count"] for m in g["message"]}
    assert pairs[(root, a)] == 1 and pairs[(a, root)] == 1


def test_timeline_uses_driver_turns(session):
    tl = session["view"].timeline()
    lane = next(l for l in tl["lanes"] if l["addr"] == session["a"].id)
    assert len(lane["turns"]) == 1 and lane["turns"][0]["end"] is not None
    assert [t["name"] for t in lane["tools"]] == ["Send"]
    assert lane["tools"][0]["done"] is not None
    assert lane["messages"][0]["from"] == session["root"].id


def test_agent_detail(session):
    d = session["view"].agent(session["a"].id)
    assert d["role_prompt"] == "echo"
    assert session["b"].id in {c["addr"] for c in d["capabilities"]}
    assert d["inbox"][0]["body"] == "ping" and "ts" in d["inbox"][0]
    assert session["view"].agent("nope")["ok"] is False


def test_send_records_user_input(session):
    view, b = session["view"], session["b"].id
    assert view.send(b, "hello b")["ok"]
    assert view.events(b)["events"][-1]["kind"] in ("user_input", "turn_start", "turn_end")
    assert any(e["kind"] == "user_input" and e["text"] == "hello b" for e in view.events(b)["events"])


def test_replay_matches_and_scrubs(session):
    rt = session["rt"]
    rt._journal.flush()
    view = SessionView.replay_session(session["dir"])
    tree = view.snapshot()["tree"]
    assert [c["label"] for c in tree["children"]] == ["a", "b"]
    assert all(c["status"] == "idle" for c in tree["children"])
    # Event logs resolve from the session dir.
    assert view.events(session["a"].id)["events"]
    # Before b was spawned, the replayed tree has only a.
    b_spawned = rt.record_for(session["b"]).spawned_at
    early = SessionView.replay_session(session["dir"], until=b_spawned - 1e-4)
    assert [c["label"] for c in early.snapshot()["tree"]["children"]] == ["a"]
    with pytest.raises(Exception):
        view.send(session["a"].id, "x")


def test_replay_cache_reuses_views(session):
    session["rt"]._journal.flush()
    cache = ReplayCache(session["dir"], size=2)
    assert cache.at(None) is cache.at(None)
    first = cache.at(1.0)
    cache.at(2.0)
    cache.at(3.0)
    assert cache.at(1.0) is not first  # evicted


# ---------- HTTP server ----------


def _req(server, method, path, *, token=True, body=None, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    headers = {"Host": host or f"127.0.0.1:{server.port}"}
    if token:
        headers["X-Conjure-Token"] = server.token if token is True else token
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=headers)
    res = conn.getresponse()
    raw = res.read()
    conn.close()
    try:
        return res.status, json.loads(raw)
    except ValueError:
        return res.status, raw


@pytest.fixture
def server(session):
    web = WebServer(lambda _at: session["view"], port=0).start()
    yield web
    web.stop()


def test_page_is_public_api_is_not(server):
    status, body = _req(server, "GET", "/", token=False)
    assert status == 200 and b"conjure" in body
    assert _req(server, "GET", "/static/app.js", token=False)[0] == 200
    assert _req(server, "GET", "/api/snapshot", token=False)[0] == 401
    assert _req(server, "GET", "/api/snapshot", token="wrong")[0] == 401
    status, snap = _req(server, "GET", "/api/snapshot")
    assert status == 200 and snap["tree"]["label"] == "root"


def test_host_header_must_name_the_server(server):
    assert _req(server, "GET", "/api/meta", host="evil.example:80")[0] == 421
    assert _req(server, "POST", "/api/send", host="evil.example", body={})[0] == 421
    assert _req(server, "GET", "/api/meta", host=f"localhost:{server.port}")[0] == 200


def test_static_cannot_escape(server):
    assert _req(server, "GET", "/static/../server.py", token=False)[0] == 404
    assert _req(server, "GET", "/static/%2e%2e/server.py", token=False)[0] == 404


def test_post_routes(server, session):
    a = session["a"].id
    status, out = _req(server, "POST", "/api/send", body={"addr": a, "body": "hi"})
    assert status == 200 and out["ok"]
    assert _req(server, "POST", "/api/send", body={"addr": a, "body": ""})[0] == 400
    assert _req(server, "POST", "/api/send", body=b"not json")[0] == 400
    assert _req(server, "POST", "/api/send", token=False, body={"addr": a, "body": "x"})[0] == 401
    status, out = _req(server, "POST", "/api/auto_mode", body={"on": True})
    assert out["on"] is True and session["rt"].auto_mode is True
    status, out = _req(server, "POST", "/api/terminate", body={"addr": session["b"].id})
    assert out["ok"] and session["b"].id in out["terminated"]


def test_permission_round_trip(server, session):
    rt = session["rt"]
    req = rt.submit_permission_request(addr=session["a"], tool_name="Bash", args={"command": "ls"})
    status, snap = _req(server, "GET", "/api/snapshot")
    [pending] = snap["permissions"]
    assert pending["tool_name"] == "Bash"
    awaiting = next(c for c in snap["tree"]["children"] if c["addr"] == session["a"].id)
    assert awaiting["awaiting_permission"]
    _req(server, "POST", "/api/permission", body={"req_id": pending["req_id"], "decision": "allow", "scope": "session"})
    assert req.wait(1.0) == "allow"
    assert rt.session_allow_contains(session["a"], "Bash")


def test_replay_server_is_read_only(session):
    session["rt"]._journal.flush()
    cache = ReplayCache(session["dir"])
    web = WebServer(cache.at, port=0, live=False).start()
    try:
        status, meta = _req(web, "GET", "/api/meta")
        assert meta["live"] is False and meta["started"] is not None
        early = meta["started"] + 1e-6
        status, snap = _req(web, "GET", f"/api/snapshot?at={early}")
        assert status == 200 and snap["tree"]["children"] == []
        assert _req(web, "POST", "/api/send", body={"addr": "x", "body": "y"})[0] == 403
        assert _req(web, "GET", "/api/snapshot?at=notanumber")[0] == 400
    finally:
        web.stop()


def test_port_fallback(session):
    first = WebServer(lambda _a: session["view"], port=0)
    try:
        second = WebServer(lambda _a: session["view"], port=first.port)
        assert second.port != first.port
        second.stop()
    finally:
        first.stop()


# ---------- launch helpers ----------


def test_url_file_is_owner_only(tmp_path, monkeypatch):
    from conjure.web import launch

    monkeypatch.setattr(launch, "session_dir", lambda: tmp_path)
    launch._write_url("conjure-abc", "http://127.0.0.1:1/?token=t")
    mode = stat.S_IMODE(os.stat(tmp_path / "conjure-abc.url").st_mode)
    assert mode == 0o600


def test_resolve_session_dir(session, tmp_path):
    from conjure.web.launch import resolve_session_dir

    session["rt"]._journal.flush()
    store = session["dir"].parent.parent
    assert resolve_session_dir("s1", store) == session["dir"]
    assert resolve_session_dir(str(session["dir"]), Path("/nowhere")) == session["dir"]
    assert resolve_session_dir("missing", store) is None


def test_cli_parses_web_flags(monkeypatch, tmp_path):
    from conjure import cli
    from conjure.web import launch

    seen = {}
    monkeypatch.setattr(launch, "cmd_web_replay", lambda target, **kw: seen.update(target=target, **kw) or 0)
    assert cli.main(["web", "--replay", "s1", "--store", str(tmp_path), "--port", "9999", "--no-browser"]) == 0
    assert seen == {"target": "s1", "store_dir": tmp_path, "port": 9999, "host": "127.0.0.1", "open_browser": False}
