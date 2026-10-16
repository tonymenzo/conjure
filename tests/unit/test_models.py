"""Model catalog and resolution: API LLMs, a discovered OpenAI-compatible
gateway (a stand-in model access gateway on loopback), subscription
sources, the allow policy, and Spawn resolving ids to engines.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conjure.errors import ModelDenied
from conjure.models import ModelHub, ModelsPolicy
from conjure.record import AgentSpec
from conjure.runtime import Runtime
from conjure.tools.models import model_catalog_impl
from conjure.tools.primitives import spawn_impl


class Gateway:
    """OpenAI-compatible endpoint: /models and /chat/completions, Bearer
    key required. Records what each request carried."""

    def __init__(self, key: str = "gw-secret") -> None:
        self.key = key
        self.requests: list[dict] = []
        gw = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _auth(self) -> bool:
                gw.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
                if self.headers.get("Authorization") != f"Bearer {gw.key}":
                    self.send_response(401)
                    self.end_headers()
                    return False
                return True

            def _json(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if not self._auth():
                    return
                if self.path.endswith("/models"):
                    self._json({"object": "list", "data": [{"id": "llama-4-405b"}, {"id": "mistral-large"}]})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(n) or b"{}")
                if not self._auth():
                    return
                gw.requests[-1]["body"] = payload
                self._json({
                    "id": "c1", "object": "chat.completion", "created": 0, "model": payload.get("model"),
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": f"hello from {payload.get('model')}"}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                })

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def gateway(monkeypatch):
    gw = Gateway()
    monkeypatch.setenv("MAG_API_KEY", gw.key)
    yield gw
    gw.close()


def _hub(gw: Gateway | None = None, **policy) -> ModelHub:
    llms = {"default": {"provider": "anthropic", "model": "claude-sonnet-4-5"}}
    if gw is not None:
        llms["mag"] = {
            "provider": "openai_compatible", "base_url": gw.url, "api_key_env": "MAG_API_KEY",
            "headers": {"X-Agency": "${MAG_AGENCY}"}, "discover": True, "note": "government gateway",
        }
    return ModelHub(llm_configs=llms, policy=ModelsPolicy(**policy), claude=True, codex=False)


def test_catalog_lists_api_gateway_and_subscription_models(gateway, monkeypatch):
    monkeypatch.setenv("MAG_AGENCY", "doe")
    ids = [e.id for e in _hub(gateway).entries()]
    assert "default/claude-sonnet-4-5" in ids
    assert {"mag/llama-4-405b", "mag/mistral-large"} <= set(ids)
    assert {"claude/opus", "claude/haiku"} <= set(ids)
    listing = gateway.requests[0]
    assert listing["path"] == "/v1/models"
    assert listing["headers"]["authorization"] == "Bearer gw-secret"
    assert listing["headers"]["x-agency"] == "doe"  # ${VAR} expanded from the env


def test_unreachable_gateway_is_reported_not_fatal():
    hub = ModelHub(
        llm_configs={"mag": {"provider": "openai_compatible", "base_url": "http://127.0.0.1:9/v1", "discover": True}},
        claude=False, codex=False, discover_timeout_s=1,
    )
    assert hub.entries() == []
    assert "mag" in hub.describe()["unavailable_sources"]


def test_resolution_picks_engine_llm_and_model(gateway):
    hub = _hub(gateway)
    s = hub.resolve_spec(AgentSpec(role_prompt="x", model="mag/mistral-large"), parent_engine="claude_agent")
    assert (s.engine, s.llm, s.model) == ("orchestral", "mag", "mistral-large")
    s = hub.resolve_spec(AgentSpec(role_prompt="x", model="haiku"), parent_engine="claude_agent")
    assert (s.engine, s.model) == ("claude_agent", "haiku")
    s = hub.resolve_spec(AgentSpec(role_prompt="x", model="llama-4-405b"), parent_engine=None)
    assert (s.engine, s.llm) == ("orchestral", "mag")  # bare name, unique
    with pytest.raises(ModelDenied, match="unknown model 'gpt-9'"):
        hub.resolve_spec(AgentSpec(role_prompt="x", model="gpt-9"), parent_engine=None)
    with pytest.raises(ModelDenied, match="runs on engine orchestral"):
        hub.resolve_spec(AgentSpec(role_prompt="x", engine="claude_agent", model="mag/mistral-large"), parent_engine=None)


def test_policy_restricts_and_defaults(gateway):
    hub = _hub(gateway, allow=["mag/*"], default="mag/llama-4-405b", notes={"mag/llama-4-405b": "strongest"})
    assert [e.id for e in hub.allowed_entries()] == ["mag/llama-4-405b", "mag/mistral-large"]
    with pytest.raises(ModelDenied, match="isn't allowed.*mag/llama-4-405b"):
        hub.resolve_spec(AgentSpec(role_prompt="x", model="claude/opus"), parent_engine=None)
    # No model named: the policy default.
    s = hub.resolve_spec(AgentSpec(role_prompt="x"), parent_engine="orchestral")
    assert (s.engine, s.llm, s.model) == ("orchestral", "mag", "llama-4-405b")
    rows = {r["id"]: r for r in hub.describe()["models"]}
    assert rows["mag/llama-4-405b"]["note"] == "strongest"
    assert rows["mag/mistral-large"]["note"] == "government gateway"


def test_without_a_default_an_implied_model_outside_policy_is_refused(gateway, monkeypatch):
    monkeypatch.setattr("conjure.engines._claude_agent_available", lambda: True)
    hub = _hub(gateway, allow=["mag/*"])
    with pytest.raises(ModelDenied, match="implied one \\(claude/haiku\\)"):
        hub.resolve_spec(AgentSpec(role_prompt="x"), parent_engine="claude_agent")


def test_spawn_tool_resolves_and_refuses_with_codes(gateway):
    rt = Runtime(models=_hub(gateway, allow=["mag/*", "claude/haiku"]))
    root = rt.record_for(rt.root(AgentSpec(role_prompt="r", engine="orchestral", model="mag/mistral-large")))
    assert (root.spec.engine, root.spec.llm, root.spec.model) == ("orchestral", "mag", "mistral-large")

    ok = spawn_impl(token=root.token, role_prompt="c", model="mag/llama-4-405b")
    child = rt.record_for(rt.address_by_id(ok["address"]))
    assert (child.spec.engine, child.spec.llm, child.spec.model) == ("orchestral", "mag", "llama-4-405b")

    denied = spawn_impl(token=root.token, role_prompt="c", model="claude/opus")
    assert denied["ok"] is False and denied["code"] == "model_denied"
    assert "claude/haiku" in denied["error"]

    cat = model_catalog_impl(token=root.token)
    assert [r["id"] for r in cat["models"]] == ["mag/llama-4-405b", "mag/mistral-large", "claude/haiku"]
    rt.shutdown()


def test_an_orchestral_agent_really_calls_the_gateway_model(gateway):
    """End to end: config → runtime → spawn on a discovered gateway model
    → its turn goes to the gateway with that model and the key."""
    from conjure.config import load_config_from_mapping
    from conjure.runner import build_runtime

    cfg = load_config_from_mapping({
        "llms": {"mag": {"provider": "openai_compatible", "base_url": gateway.url,
                         "api_key_env": "MAG_API_KEY", "discover": True, "model": "mistral-large"}},
        "models": {"allow": ["mag/*"]},
        "root": {"role_prompt": "r", "engine": "orchestral", "llm": "mag", "tools": ["primitive"]},
    })
    rt, root = build_runtime(cfg)
    try:
        out = spawn_impl(token=rt.record_for(root).token, role_prompt="say hi", model="mag/llama-4-405b",
                         initial_message="hi")
        assert out["ok"], out
        child = rt.address_by_id(out["address"])
        import time

        deadline = time.time() + 15
        while time.time() < deadline and not any(r.get("body", {}).get("model") == "llama-4-405b" for r in gateway.requests):
            time.sleep(0.05)
        calls = [r for r in gateway.requests if r["path"].endswith("/chat/completions")]
        assert any(r["body"]["model"] == "llama-4-405b" for r in calls), [r["body"].get("model") for r in calls]
        assert all(r["headers"]["authorization"] == "Bearer gw-secret" for r in calls)
        assert rt.record_for(child).spec.llm == "mag"
    finally:
        rt.shutdown()


def test_an_exact_model_name_shared_by_two_aliases_resolves(monkeypatch):
    from conjure.engines import claude_agent as ca

    monkeypatch.setattr(ca, "_MODEL_MAP", {"default": "claude-opus-5-5", "opus": "claude-opus-5-5", "haiku": "claude-haiku-5-5"})
    hub = ModelHub(llm_configs={}, claude=True, codex=False)
    s = hub.resolve_spec(AgentSpec(role_prompt="x", model="claude-opus-5-5"), parent_engine=None)
    assert (s.engine, s.model) == ("claude_agent", "opus")



def test_the_policy_default_is_for_children_not_the_root(gateway):
    """A root without ``model`` runs on its llms entry's model, even when
    ``models.default`` names a cheaper one for children."""
    hub = _hub(gateway, allow=["mag/*"], default="mag/mistral-large")
    root = hub.resolve_spec(AgentSpec(role_prompt="r", engine="orchestral", llm="mag"), parent_engine=None)
    assert root.model is None and root.llm == "mag"  # the entry's own model
    child = hub.resolve_spec(AgentSpec(role_prompt="c"), parent_engine="orchestral")
    assert (child.llm, child.model) == ("mag", "mistral-large")


def test_the_root_runs_on_its_entrys_model_end_to_end(gateway):
    from conjure.config import load_config_from_mapping
    from conjure.runner import build_runtime

    cfg = load_config_from_mapping({
        "llms": {"mag": {"provider": "openai_compatible", "base_url": gateway.url,
                         "api_key_env": "MAG_API_KEY", "discover": True, "model": "llama-4-405b"}},
        "models": {"allow": ["mag/*"], "default": "mag/mistral-large"},
        "root": {"role_prompt": "r", "engine": "orchestral", "llm": "mag"},
    })
    rt, root = build_runtime(cfg)
    try:
        rec = rt.record_for(root)
        assert rec.spec.model is None
        assert rec.agent.engine.model_name() == "llama-4-405b"
    finally:
        rt.shutdown()
