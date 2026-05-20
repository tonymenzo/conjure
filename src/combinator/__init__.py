"""Combinator — agentic functional programming on top of orchestral.

A multi-agent harness built around three primitives — recursive
spawning, addressable mailboxes, and capability passing — with FP-style
combinators (``agent_map``, ``agent_fold``, ``agent_filter``,
``agent_fixed_point``) layered on top. See ``DESIGN.md`` at the repo
root for the design philosophy.
"""

from __future__ import annotations

from combinator.address import SYSTEM, USER, Address
from combinator.agent import Agent, Engine
from combinator.capability import CapabilitySet
from combinator.combinators import (
    agent_filter,
    agent_fixed_point,
    agent_fold,
    agent_map,
)
from combinator.envelope import Envelope
from combinator.errors import (
    CombinatorError,
    NoSuchAddress,
    NotPermitted,
    Terminated,
    Timeout,
)
from combinator.record import AgentRecord, AgentSpec, AgentStatus
from combinator.runtime import Runtime
from combinator.scripted import BehaviorRegistry, ScriptedEngine
from combinator.tools.combinators import (
    COMBINATOR_TOOL_CLASSES,
    build_combinator_tools,
)
from combinator.tools.primitives import (
    PRIMITIVE_TOOL_CLASSES,
    build_primitive_tools,
)


__version__ = "0.1.0"

__all__ = [
    "__version__",
    # Value types
    "Address",
    "Envelope",
    "CapabilitySet",
    "USER",
    "SYSTEM",
    # Runtime
    "Runtime",
    "Agent",
    "Engine",
    "AgentSpec",
    "AgentRecord",
    "AgentStatus",
    # Errors
    "CombinatorError",
    "NoSuchAddress",
    "NotPermitted",
    "Terminated",
    "Timeout",
    # Combinators (Python)
    "agent_map",
    "agent_fold",
    "agent_filter",
    "agent_fixed_point",
    # Tools
    "build_primitive_tools",
    "build_combinator_tools",
    "PRIMITIVE_TOOL_CLASSES",
    "COMBINATOR_TOOL_CLASSES",
    # Test substrate
    "ScriptedEngine",
    "BehaviorRegistry",
]
