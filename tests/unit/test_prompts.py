"""One system prompt document for every engine, with engine slots."""

from __future__ import annotations

import pytest

from conjure.engines.registry import build_tools
from conjure.prompts import build_system_prompt
from conjure.record import AgentSpec
from conjure.runtime import Runtime


@pytest.mark.parametrize("engine,title,naming", [
    ("claude_agent", "(Claude Code engine)", "mcp__conjure__<Name>"),
    ("codex", "(Codex engine)", "`conjure` MCP server"),
    ("orchestral", "(orchestral engine)", "named as in this prompt"),
])
def test_every_engine_builds_from_the_same_document(engine, title, naming):
    rt = Runtime()
    rec = rt.record_for(rt.root(AgentSpec(role_prompt="study Z decays", label="root")))
    p = build_system_prompt(rec, rt, engine)
    assert title in p and naming in p and "study Z decays" in p
    # Shared sections, and none of the drift the old per-engine prompts had.
    for section in ("## How messaging works", "## Tools available to you", "## When to stop", "## Child lifecycle events"):
        assert section in p
    assert "Spawn multi-agent framework" not in p and "default to the **haiku**" not in p
    assert "$" + "native_tools" not in p and "$" + "tool_naming" not in p
    rt.shutdown()


def test_orchestral_says_whether_it_has_file_tools():
    rt = Runtime()
    root = rt.root(AgentSpec(role_prompt="r"))
    bare = rt.record_for(rt._spawn(parent=root, spec=AgentSpec(role_prompt="c", tools=["primitive"])))
    fs = rt.record_for(rt._spawn(parent=root, spec=AgentSpec(role_prompt="c", tools=["primitive", "filesystem"])))
    assert "no shell or file tools" in build_system_prompt(bare, rt, "orchestral")
    assert "`Bash`" in build_system_prompt(fs, rt, "orchestral")
    rt.shutdown()


def test_tool_names_match_the_prompt_on_orchestral():
    names = [t.get_name() for t in build_tools("t", ["all"])]
    for n in ("Spawn", "WaitFor", "Recv", "AgentMap", "ToolCatalog", "GrantToolkits", "ModelCatalog", "Bash"):
        assert n in names
