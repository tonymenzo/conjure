"""A replayed session shows what it cost and where its files are.

Replay has no engines, so before 0.2.7 every agent showed $0 and no
tokens. Cost comes from the journal (``agent_meta.cost_usd``); tokens
from the journal, or from the event log's ``usage`` events for older
journals; a gateway model with no price counts as unpriced. A session
pulled from another machine finds its agents' sandboxes in this copy
of the run directory.
"""

from __future__ import annotations

import json
from pathlib import Path

from conjure.web.state import SessionView, _log_tokens

REMOTE = "/remote/fleet/trial/run"


def _spawn(aid, label, parent, **spec):
    return {"kind": "spawn", "ts": 1.0, "payload": {
        "addr": {"id": aid, "label": label},
        "parent": {"id": parent, "label": "root"} if parent else None,
        "spec": {"role_prompt": "r", "label": label, "engine": "orchestral", **spec},
    }}


def _meta(aid, **meta):
    return {"kind": "agent_meta", "ts": 2.0, "payload": {"addr": aid, "meta": meta}}


def _usage_turns(*turns):
    out = []
    for turn in turns:
        out.append({"kind": "turn_start", "ts": 1.5})
        out += [{"kind": "usage", "tokens_in": i, "tokens_out": o, "ts": 1.6} for i, o in turn]
        out.append({"kind": "turn_end", "ts": 1.7})
    return out


def _pulled_run(tmp_path: Path, journal: list[dict], logs: dict[str, list[dict]]) -> Path:
    """A run copied from another machine: <run>/conjure-native/sessions/s1
    beside <run>/workspace, with the remote paths in its records."""
    run = tmp_path / "run"
    session = run / "conjure-native" / "sessions" / "s1"
    (session / "agents").mkdir(parents=True)
    (run / "workspace" / "work" / "w").mkdir(parents=True)
    (session / "journal.jsonl").write_text("".join(json.dumps(e) + "\n" for e in journal))
    for aid, events in logs.items():
        (session / "agents" / f"{aid}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    (session / "manifest.json").write_text(json.dumps({"config": {
        "runtime": {"store_dir": f"{REMOTE}/conjure-native"},
        "llms": {
            "gw": {"provider": "openai_compatible", "model": "mystery-model-1", "pricing": {}},
            "sonnet": {"provider": "openai_compatible", "model": "claude-sonnet-5-5", "pricing": {}},
        },
    }}))
    return session


def test_log_tokens_counts_each_turns_largest_usage():
    assert _log_tokens(_usage_turns([(100, 10), (300, 20)], [(50, 5)])) == 375
    assert _log_tokens([]) == 0


def test_replay_shows_journaled_cost_and_logged_tokens(tmp_path):
    session = _pulled_run(tmp_path, [
        _spawn("a-root", "root", None, llm="gw", model="mystery-model-1"),
        _spawn("a-w", "worker", "a-root", llm="sonnet", model="claude-sonnet-5-5"),
        _meta("a-w", model="claude-sonnet-5-5", cost_usd=1.5),
        _meta("a-root", model="mystery-model-1", cost_usd=0.0),
    ], {
        "a-root": _usage_turns([(1000, 100)]),
        "a-w": _usage_turns([(100, 10), (300, 20)], [(50, 5)]),
    })
    snap = SessionView.replay_session(session).snapshot()
    root, worker = snap["tree"], snap["tree"]["children"][0]
    assert (worker["cost"], worker["tokens"], worker["unpriced_tokens"]) == (1.5, 375, 0)
    # No configured or list price for the gateway's model: unpriced, not free.
    assert (root["cost"], root["tokens"], root["unpriced_tokens"]) == (0.0, 1100, 1100)
    assert snap["cost"] == {"total": 1.5, "subscription": False, "tokens": 1475, "unpriced_tokens": 1100}


def test_journaled_token_counts_win_over_the_log(tmp_path):
    session = _pulled_run(tmp_path, [
        _spawn("a-root", "root", None, llm="gw", model="mystery-model-1"),
        _meta("a-root", cost_usd=0.25, tokens=999, unpriced_tokens=0),
    ], {"a-root": _usage_turns([(1, 1)])})
    root = SessionView.replay_session(session).snapshot()["tree"]
    assert (root["cost"], root["tokens"], root["unpriced_tokens"]) == (0.25, 999, 0)


def test_replay_finds_sandboxes_in_the_pulled_run(tmp_path):
    session = _pulled_run(tmp_path, [
        _spawn("a-root", "root", None, llm="gw", sandbox_dir=f"{REMOTE}/workspace"),
        _spawn("a-w", "worker", "a-root", llm="gw", sandbox_dir=f"{REMOTE}/workspace/work/w"),
        _spawn("a-out", "outside", "a-root", llm="gw", sandbox_dir="/remote/fleet/elsewhere"),
        _spawn("a-gone", "gone", "a-root", llm="gw", sandbox_dir=f"{REMOTE}/workspace/never-pulled"),
    ], {})
    view = SessionView.replay_session(session)
    run = session.parent.parent.parent
    assert view.agent("a-root")["sandbox_dir"] == str(run / "workspace")
    assert view.agent("a-w")["sandbox_dir"] == str(run / "workspace" / "work" / "w")
    listing = view.files_list("a-root", None)
    assert listing["ok"], listing
    # Only paths inside the run directory, and only ones that were pulled.
    assert view.agent("a-out")["sandbox_dir"] == "/remote/fleet/elsewhere"
    assert view.agent("a-gone")["sandbox_dir"] == f"{REMOTE}/workspace/never-pulled"


def test_a_turn_journals_its_tokens_for_replay(tmp_path):
    import time

    from orchestral.context.message import Message
    from orchestral.llm.base.response import Response
    from orchestral.llm.base.usage import Usage

    from conjure.engines.orchestral import make_orchestral_engine_factory
    from conjure.record import AgentSpec
    from conjure.runtime import Runtime
    from tests.unit.test_spawn_listener import _FakeLLM

    class CountingLLM(_FakeLLM):
        def get_response(self, context, **kwargs):
            usage = Usage("fake", {"prompt_tokens": 40, "completion_tokens": 2}, 0.0)
            return Response(model="fake", message=Message(role="assistant", text="hi"), usage=usage)

    rt = Runtime(store_dir=tmp_path, engine_factory=make_orchestral_engine_factory(llms={"default": CountingLLM()}))
    root = rt.root(AgentSpec(role_prompt="r", label="root", tools=["primitive"]))
    try:
        rt.send_external(to=root, body="hi")
        deadline = time.time() + 10
        while time.time() < deadline and rt.record_for(root).meta.get("tokens") != 42:
            time.sleep(0.05)
    finally:
        rt.shutdown()
    replayed = SessionView.replay_session(Path(rt.session_dir)).snapshot()["tree"]
    assert replayed["tokens"] == 42
