"""Pricing gateway models: list prices, configured prices, and unpriced
tokens shown as such rather than as $0."""

from __future__ import annotations

import json
import time

import pytest

from conjure.pricing import Price, PriceBook, list_price


def test_list_prices_match_by_id_or_prefix_at_a_separator():
    assert list_price("claude-sonnet-5-5").source == "list:anthropic"
    assert list_price("claude-sonnet-5-5").input == list_price("claude-sonnet-5").input
    assert list_price("anthropic/claude-sonnet-5-5") is not None
    assert list_price("gpt-50") is None  # not gpt-5
    assert list_price("some-unknown-model") is None


def test_configured_prices_win_and_cover_unknown_models():
    book = PriceBook({"claude-haiku-5-5": {"input": 1, "output": 5}, "default": {"input": 9, "output": 9}})
    assert book.price("claude-haiku-5-5") == Price(1.0, 5.0, None, "config")
    assert book.price("anything-else").input == 9.0
    assert PriceBook({}).price("gpt-6-sol") is None


def test_price_cost_counts_cached_input_at_its_rate():
    p = Price(input=3.0, output=15.0, cached_input=0.3)
    cost = p.cost({"prompt_tokens": 1_000_000, "cached_prompt_tokens": 500_000, "completion_tokens": 100_000})
    assert cost == pytest.approx(0.5 * 3.0 + 0.5 * 0.3 + 0.1 * 15.0)


def _gateway_session(tmp_path, monkeypatch, model, pricing=None):
    from tests.unit.test_orchestral_tool_calls import ToolCallingGateway

    from conjure.config import load_config_from_mapping
    from conjure.runner import build_runtime

    gw = ToolCallingGateway("ModelCatalog")
    real = gw.server.RequestHandlerClass.do_POST

    def with_usage(handler):  # answer with text and a large usage block
        body = json.loads(handler.rfile.read(int(handler.headers.get("Content-Length") or 0)))
        gw.requests.append(body)
        out = json.dumps({"id": "c", "object": "chat.completion", "created": 0, "model": model,
                          "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                          "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 100_000, "total_tokens": 1_100_000}}).encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(out)))
        handler.end_headers()
        handler.wfile.write(out)

    gw.server.RequestHandlerClass.do_POST = with_usage
    monkeypatch.setenv("GW_KEY", "k")
    llm = {"provider": "openai_compatible", "base_url": gw.url, "api_key_env": "GW_KEY", "model": model}
    if pricing:
        llm["pricing"] = pricing
    cfg = load_config_from_mapping({
        "runtime": {"store_dir": str(tmp_path)},
        "llms": {"gw": llm},
        "root": {"role_prompt": "r", "engine": "orchestral", "llm": "gw", "tools": ["primitive"]},
    })
    rt, root = build_runtime(cfg)
    rt.send_external(to=root, body="hi")
    rec = rt.record_for(root)
    deadline = time.time() + 15
    while time.time() < deadline and not (gw.requests and rec.status == "idle"):
        time.sleep(0.05)
    return rt, rec, gw


def test_a_gateway_model_with_a_list_price_is_charged(tmp_path, monkeypatch):
    rt, rec, gw = _gateway_session(tmp_path, monkeypatch, "claude-sonnet-5-5")
    try:
        u = rec.agent.engine.usage()
        assert (u["tokens_in"], u["tokens_out"], u["unpriced_tokens"]) == (1_000_000, 100_000, 0)
        assert u["cost"] == pytest.approx(3.0 + 1.5)  # $3/M in, $15/M out
        assert rec.agent.engine.cost() == pytest.approx(4.5)
    finally:
        rt.shutdown()
        gw.server.shutdown()


def test_an_unknown_gateway_model_shows_unpriced_tokens_not_zero(tmp_path, monkeypatch):
    from conjure.web.state import SessionView

    rt, rec, gw = _gateway_session(tmp_path, monkeypatch, "gpt-6-sol")
    try:
        u = rec.agent.engine.usage()
        assert u["cost"] == 0 and u["unpriced_tokens"] == 1_100_000
        from conjure.control import ControlServer
        from pathlib import Path

        view = SessionView.live_session(rt, ControlServer(runtime=rt, socket_path=Path("/nonexistent/x.sock")), name="t")
        snap = view.snapshot()
        assert snap["cost"]["unpriced_tokens"] == 1_100_000 and snap["cost"]["tokens"] == 1_100_000
        assert snap["tree"]["unpriced_tokens"] == 1_100_000
    finally:
        rt.shutdown()
        gw.server.shutdown()


def test_a_configured_price_covers_a_model_without_a_list_price(tmp_path, monkeypatch):
    rt, rec, gw = _gateway_session(tmp_path, monkeypatch, "gpt-6-sol", {"gpt-6": {"input": 2, "output": 8}})
    try:
        assert rec.agent.engine.cost() == pytest.approx(2.0 + 0.8)
    finally:
        rt.shutdown()
        gw.server.shutdown()


def test_model_catalog_shows_prices():
    from conjure.models import ModelHub

    hub = ModelHub(llm_configs={"gw": {"provider": "openai_compatible", "base_url": "http://x/v1", "model": "claude-opus-5-5"}},
                   claude=False, codex=False)
    row = hub.describe()["models"][0]
    assert row["price_per_mtok"]["input"] == 5.0 and row["price_per_mtok"]["source"] == "list:anthropic"
