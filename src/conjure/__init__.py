"""Conjure — agentic functional programming on top of orchestral.

A multi-agent harness built around three primitives — recursive
spawning, addressable mailboxes, and capability passing — with FP-style
combinators (``agent_map``, ``agent_fold``, ``agent_filter``,
``agent_fixed_point``) and higher-order patterns (``agent_race``,
``agent_ensemble``, ``agent_critic``, ``agent_supervisor``) layered on top. See
``DESIGN.md`` at the repo root for the design philosophy.
"""

from __future__ import annotations

from conjure.address import SYSTEM, USER, Address
from conjure.agent import Agent, Engine
from conjure.capability import CapabilitySet
from conjure.combinators import (
    agent_critic,
    agent_ensemble,
    agent_filter,
    agent_fixed_point,
    agent_fold,
    agent_map,
    agent_race,
    agent_supervisor,
)
from conjure.envelope import Envelope
from conjure.errors import (
    ConjureError,
    NoSuchAddress,
    NotPermitted,
    Terminated,
    Timeout,
)
from conjure.record import AgentRecord, AgentSpec, AgentStatus
from conjure.runtime import Runtime
from conjure.scripted import BehaviorRegistry, ScriptedEngine
from conjure.tools.combinators import (
    COMBINATOR_TOOL_CLASSES,
    build_combinator_tools,
)
from conjure.tools.primitives import (
    PRIMITIVE_TOOL_CLASSES,
    build_primitive_tools,
)


__version__ = "0.1.1"

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
    "ConjureError",
    "NoSuchAddress",
    "NotPermitted",
    "Terminated",
    "Timeout",
    # Combinators (Python)
    "agent_map",
    "agent_fold",
    "agent_filter",
    "agent_fixed_point",
    "agent_race",
    "agent_ensemble",
    "agent_critic",
    "agent_supervisor",
    # Tools
    "build_primitive_tools",
    "build_combinator_tools",
    "PRIMITIVE_TOOL_CLASSES",
    "COMBINATOR_TOOL_CLASSES",
    # Test substrate
    "ScriptedEngine",
    "BehaviorRegistry",
]
