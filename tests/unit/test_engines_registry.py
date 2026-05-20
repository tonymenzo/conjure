"""Tests for combinator.engines.registry — tool group resolution."""

from __future__ import annotations

import pytest

from combinator.engines.registry import (
    DEFAULT_TOOL_GROUPS,
    build_tools,
)
from combinator.tools.combinators import COMBINATOR_TOOL_CLASSES
from combinator.tools.primitives import PRIMITIVE_TOOL_CLASSES


def test_primitive_group_resolves_to_primitive_tool_set():
    tools = build_tools("token-abc", ["primitive"])
    assert len(tools) == len(PRIMITIVE_TOOL_CLASSES)
    for t in tools:
        assert t.runtime_token == "token-abc"


def test_combinator_group_resolves_to_combinator_tool_set():
    tools = build_tools("token-abc", ["combinator"])
    assert len(tools) == len(COMBINATOR_TOOL_CLASSES)


def test_all_group_combines_both():
    tools = build_tools("token-abc", ["all"])
    assert len(tools) == len(PRIMITIVE_TOOL_CLASSES) + len(COMBINATOR_TOOL_CLASSES)


def test_request_both_groups_dedups():
    tools = build_tools("token-abc", ["primitive", "combinator", "primitive"])
    assert len(tools) == len(PRIMITIVE_TOOL_CLASSES) + len(COMBINATOR_TOOL_CLASSES)


def test_unknown_group_raises():
    with pytest.raises(ValueError, match="unknown tool group"):
        build_tools("t", ["nonexistent"])


def test_custom_registry():
    custom = {"custom": (PRIMITIVE_TOOL_CLASSES[0],)}
    tools = build_tools("t", ["custom"], registry=custom)
    assert len(tools) == 1
    assert isinstance(tools[0], PRIMITIVE_TOOL_CLASSES[0])
