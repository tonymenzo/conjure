"""Smoke tests for combinator.tools.combinators — LLM-callable wrappers.

Heavy combinator behavior is covered in ``test_combinators.py``; here
we just verify the tools construct correctly and delegate.
"""

from __future__ import annotations

from combinator.record import AgentSpec
from combinator.runtime import Runtime
from combinator.scripted import BehaviorRegistry
from combinator.tools.combinators import (
    AgentFilterTool,
    AgentFixedPointTool,
    AgentFoldTool,
    AgentMapTool,
    COMBINATOR_TOOL_CLASSES,
    build_combinator_tools,
)
from combinator.tools.primitives import send_impl


def _reply_with(transform):
    def behavior(engine, prompt, envelopes):
        for env in envelopes:
            send_impl(
                token=engine.token,
                to=env.body["reply_to"],
                body=transform(env.body),
            )
        return "ok"
    return behavior


def _runtime_with(role: str, behavior):
    reg = BehaviorRegistry()
    reg.register("root", lambda *_args, **_kw: "idle")
    reg.register(role, behavior)
    return Runtime(engine_factory=reg.factory())


def test_build_combinator_tools_returns_one_per_class():
    rt = Runtime()
    addr = rt.root(AgentSpec(role_prompt="root"))
    token = rt.record_for(addr).token
    tools = build_combinator_tools(token)
    assert len(tools) == len(COMBINATOR_TOOL_CLASSES)
    rt.shutdown()


def test_map_tool_squares_items():
    rt = _runtime_with("sq", _reply_with(lambda b: b["item"] ** 2))
    addr = rt.root(AgentSpec(role_prompt="root"))
    token = rt.record_for(addr).token
    tool = AgentMapTool(runtime_token=token)
    out = tool._run.__func__(  # bypass execute's reset to set fields explicitly
        _ToolStub(token, spec={"role_prompt": "sq"}, items=[1, 2, 3, 4], timeout_s=5.0)
    ) if False else None
    # The simpler way: just set fields and call _run().
    tool.spec = {"role_prompt": "sq"}
    tool.items = [1, 2, 3, 4]
    tool.timeout_s = 5.0
    result = tool._run()
    assert result["ok"] is True
    assert result["result"] == [1, 4, 9, 16]
    rt.shutdown()


def test_fold_tool_sums_items():
    def adder(engine, prompt, envelopes):
        for env in envelopes:
            send_impl(
                token=engine.token,
                to=env.body["reply_to"],
                body=env.body["acc"] + env.body["item"],
            )
        return "ok"
    rt = _runtime_with("add", adder)
    addr = rt.root(AgentSpec(role_prompt="root"))
    token = rt.record_for(addr).token
    tool = AgentFoldTool(runtime_token=token)
    tool.spec = {"role_prompt": "add"}
    tool.items = [1, 2, 3, 4, 5]
    tool.init = 0
    tool.timeout_s = 5.0
    result = tool._run()
    assert result["ok"] is True
    assert result["result"] == 15
    rt.shutdown()


def test_filter_tool_keeps_even():
    rt = _runtime_with("even", _reply_with(lambda b: b["item"] % 2 == 0))
    addr = rt.root(AgentSpec(role_prompt="root"))
    token = rt.record_for(addr).token
    tool = AgentFilterTool(runtime_token=token)
    tool.spec = {"role_prompt": "even"}
    tool.items = [1, 2, 3, 4, 5, 6]
    tool.timeout_s = 5.0
    result = tool._run()
    assert result["ok"] is True
    assert result["result"] == [2, 4, 6]
    rt.shutdown()


def test_fixed_point_tool_converges():
    rt = _runtime_with("half", _reply_with(lambda b: b["value"] // 2))
    addr = rt.root(AgentSpec(role_prompt="root"))
    token = rt.record_for(addr).token
    tool = AgentFixedPointTool(runtime_token=token)
    tool.spec = {"role_prompt": "half"}
    tool.seed = 8
    tool.max_iters = 10
    tool.timeout_s = 5.0
    result = tool._run()
    assert result["ok"] is True
    assert result["result"] == 0
    assert result["converged"] is True
    rt.shutdown()


def test_tool_with_unknown_token_returns_no_runtime():
    tool = AgentMapTool(runtime_token="fake")
    tool.spec = {"role_prompt": "x"}
    tool.items = [1]
    tool.timeout_s = 1.0
    result = tool._run()
    assert result == {"ok": False, "code": "no_runtime", "error": "tool is not bound to a runtime"}


# Helper unused — kept for reference but the simpler set-attr-then-call
# path above works fine.
class _ToolStub:
    def __init__(self, token, **kw):
        self.runtime_token = token
        for k, v in kw.items():
            setattr(self, k, v)
