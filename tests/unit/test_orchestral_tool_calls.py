"""An orchestral agent's tool call round-trips: the name the model is
offered is the name the router dispatches. 0.2.1 offered ``catalog`` /
``listinbox`` while routing ``ToolCatalog`` / ``ListInbox``, so every
conjure tool call failed with "Tool '...' not found"."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from conjure.engines.registry import build_tools


def test_every_spec_is_offered_under_its_routed_name():
    for tool in build_tools("t", ["all", "toolkit", "models"]):
        assert type(tool).get_tool_spec().name == tool.get_name()


class ToolCallingGateway:
    """OpenAI-compatible endpoint whose model calls ``want`` (matched
    case-insensitively against the tools it was offered), then answers
    with what the tool returned."""

    def __init__(self, want: str) -> None:
        self.requests: list[dict] = []
        gw = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                gw.requests.append(body)
                offered = [t["function"]["name"] for t in body.get("tools") or []]
                tool_msgs = [m for m in body.get("messages", []) if m.get("role") == "tool"]
                if not tool_msgs:
                    name = next(n for n in offered if n.lower() == want.lower())
                    message = {"role": "assistant", "content": None, "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": name, "arguments": "{}"}}]}
                    finish = "tool_calls"
                else:
                    message = {"role": "assistant", "content": "tool said: " + str(tool_msgs[-1].get("content"))[:200]}
                    finish = "stop"
                out = json.dumps({"id": "c", "object": "chat.completion", "created": 0, "model": body.get("model"),
                                  "choices": [{"index": 0, "finish_reason": finish, "message": message}],
                                  "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def test_an_orchestral_agent_calls_a_conjure_tool_through_a_gateway(monkeypatch):
    from conjure.config import load_config_from_mapping
    from conjure.runner import build_runtime

    gw = ToolCallingGateway("ModelCatalog")
    monkeypatch.setenv("GW_KEY", "k")
    cfg = load_config_from_mapping({
        "llms": {"gw": {"provider": "openai_compatible", "base_url": gw.url, "api_key_env": "GW_KEY", "model": "m"}},
        "models": {"allow": ["gw/*"]},
        "root": {"role_prompt": "r", "engine": "orchestral", "llm": "gw", "tools": ["primitive"]},
    })
    rt, root = build_runtime(cfg)
    try:
        rt.send_external(to=root, body="which models can you use?")
        deadline = time.time() + 15
        while time.time() < deadline and len(gw.requests) < 2:
            time.sleep(0.05)
        assert len(gw.requests) >= 2, "the agent never answered after its tool call"
        offered = [t["function"]["name"] for t in gw.requests[0]["tools"]]
        assert "ModelCatalog" in offered and "Spawn" in offered
        result = [m for m in gw.requests[1]["messages"] if m.get("role") == "tool"][-1]["content"]
        assert "not found" not in str(result) and "gw/m" in str(result)
    finally:
        rt.shutdown()
        gw.server.shutdown()
