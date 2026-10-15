"""Gateway-friendly LLM calls: transient-error retries (including a
stream that breaks midway), stated reset times, the shared concurrency
cap, and an orchestral agent riding out 429s from a gateway."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from conjure import llm_resilience as lr


class APIConnectionError(Exception):
    pass


class RateLimitError(Exception):
    status_code = 429

    def __init__(self, msg, headers=None):
        super().__init__(msg)
        self.response = type("R", (), {"headers": headers or {}})()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(lr, "_sleep", lambda s: slept.append(s))
    return slept


def test_transient_classification_follows_the_cause_chain():
    assert lr.is_transient(APIConnectionError("Connection error."))
    assert lr.is_transient(RateLimitError("429"))
    try:
        try:
            raise ConnectionResetError("peer closed")
        except ConnectionResetError as inner:
            raise ValueError("wrapped") from inner
    except ValueError as outer:
        assert lr.is_transient(outer)
    assert not lr.is_transient(ValueError("bad request"))


def test_a_429_waits_for_the_stated_reset():
    reset = (datetime.now(timezone.utc) + timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
    exc = RateLimitError(f"Rate limit exceeded ... Limit resets at: {reset} UTC")
    assert 25 <= lr.retry_delay(exc, 1) <= 32
    assert lr.retry_delay(RateLimitError("x", headers={"retry-after": "7"}), 1) == 7.5
    assert 1.0 <= lr.retry_delay(APIConnectionError("x"), 1) <= 2.0  # backoff with jitter


class FakeLLM:
    def __init__(self, failures):
        self.failures = list(failures)  # exceptions (or None) per attempt
        self.calls = 0

    def get_response(self, context, **kw):
        self.calls += 1
        exc = self.failures.pop(0) if self.failures else None
        if exc:
            raise exc
        return "response"

    def stream_response(self, context, **kw):
        self.calls += 1
        exc = self.failures.pop(0) if self.failures else None
        yield "par"
        if exc:
            raise exc
        yield "tial"
        return "final-response"


def _drain(gen):
    chunks = []
    while True:
        try:
            chunks.append(next(gen))
        except StopIteration as stop:
            return chunks, stop.value


def test_a_stream_that_breaks_midway_is_retried_and_the_engine_told():
    llm = lr.make_resilient(FakeLLM([APIConnectionError("peer closed")]), key="t1", max_retries=3)
    heard = []
    llm.conjure_on_retry = lambda a, m, d, e: heard.append((a, m, type(e).__name__)) or True
    chunks, value = _drain(llm.stream_response(None))
    assert value == "final-response" and chunks == ["par", "par", "tial"]
    assert heard == [(1, 3, "APIConnectionError")]


def test_non_transient_errors_and_exhausted_retries_raise(no_sleep):
    llm = lr.make_resilient(FakeLLM([ValueError("400 bad request")]), key="t2", max_retries=3)
    with pytest.raises(ValueError):
        llm.get_response(None)
    assert llm.calls == 1
    llm = lr.make_resilient(FakeLLM([RateLimitError("429")] * 5), key="t3", max_retries=2)
    with pytest.raises(RateLimitError):
        llm.get_response(None)
    assert llm.calls == 3 and len(no_sleep) == 2


def test_the_engine_can_stop_retries_for_a_terminated_agent():
    llm = lr.make_resilient(FakeLLM([RateLimitError("429")] * 3), key="t4", max_retries=5)
    llm.conjure_on_retry = lambda *a: False
    with pytest.raises(RateLimitError):
        llm.get_response(None)
    assert llm.calls == 1


def test_clients_of_one_entry_share_the_concurrency_cap(monkeypatch):
    active, peak, lock = [0], [0], threading.Lock()

    class Slow:
        def get_response(self, context, **kw):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with lock:
                active[0] -= 1
            return "ok"

        def stream_response(self, context, **kw):
            yield "x"
            return "ok"

    clients = [lr.make_resilient(Slow(), key="gateway-cap", max_concurrency=2) for _ in range(6)]
    threads = [threading.Thread(target=c.get_response, args=(None,)) for c in clients]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 2


def test_an_orchestral_agent_rides_out_gateway_429s(monkeypatch):
    """End to end on a loopback gateway that refuses the first two chat
    requests with 429 max_parallel_requests."""
    from tests.unit.test_models import Gateway

    from conjure.config import load_config_from_mapping
    from conjure.runner import build_runtime
    from conjure.tools.primitives import spawn_impl

    gw = Gateway()
    monkeypatch.setenv("MAG_API_KEY", gw.key)
    refusals = [2]
    real_post = gw.server.RequestHandlerClass.do_POST

    def do_post(handler):
        if handler.path.endswith("/chat/completions") and refusals[0] > 0:
            refusals[0] -= 1
            n = int(handler.headers.get("Content-Length") or 0)
            handler.rfile.read(n)
            body = b'{"error": {"message": "Rate limit exceeded. Limit type: max_parallel_requests. Current limit: 20", "code": "429"}}'
            handler.send_response(429)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Retry-After", "0")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            return
        return real_post(handler)

    gw.server.RequestHandlerClass.do_POST = do_post
    cfg = load_config_from_mapping({
        "llms": {"mag": {"provider": "openai_compatible", "base_url": gw.url, "api_key_env": "MAG_API_KEY",
                         "model": "mistral-large", "max_concurrency": 4}},
        "root": {"role_prompt": "r", "engine": "orchestral", "llm": "mag", "tools": ["primitive"]},
    })
    rt, root = build_runtime(cfg)
    try:
        out = spawn_impl(token=rt.record_for(root).token, role_prompt="say hi", engine="orchestral", llm="mag", initial_message="hi")
        child = rt.record_for(rt.address_by_id(out["address"]))
        ok = lambda: [r for r in gw.requests if r["path"].endswith("/chat/completions") and "body" in r]  # noqa: E731
        deadline = time.time() + 15
        while time.time() < deadline and not (ok() and child.status == "idle"):
            time.sleep(0.05)
        assert refusals[0] == 0, "both 429s were served"
        assert ok(), "the retried request reached the gateway"
        assert child.status == "idle"  # finished its turn, not "error"
    finally:
        rt.shutdown()
        gw.close()
