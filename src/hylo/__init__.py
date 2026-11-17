"""Hylo — agentic functional programming on top of orchestral.

The name is short for hylomorphism, the recursion scheme that unfolds a
structure and folds it back: a hylo run unfolds a tree of agents by
spawning and folds their results back up through the mailboxes.

A multi-agent harness built around three primitives — recursive
spawning, addressable mailboxes, and capability passing — with FP-style
combinators (``agent_map``, ``agent_fold``, ``agent_filter``,
``agent_fixed_point``) and higher-order patterns (``agent_race``,
``agent_ensemble``, ``agent_critic``, ``agent_supervisor``) layered on top.
"""

from __future__ import annotations

from hylo import env as _env

_env.adopt_legacy_variables()

from hylo.address import SYSTEM, USER, Address
from hylo.agent import Agent, Engine
from hylo.capability import CapabilitySet
from hylo.combinators import (
    agent_critic,
    agent_ensemble,
    agent_filter,
    agent_fixed_point,
    agent_fold,
    agent_map,
    agent_race,
    agent_supervisor,
)
from hylo.envelope import Envelope
from hylo.errors import (
    HyloError,
    NoSuchAddress,
    NotPermitted,
    Terminated,
    Timeout,
)
from hylo.record import AgentRecord, AgentSpec, AgentStatus
from hylo.runtime import Runtime
from hylo.scripted import BehaviorRegistry, ScriptedEngine
from hylo.tools.combinators import (
    COMBINATOR_TOOL_CLASSES,
    build_combinator_tools,
)
from hylo.tools.primitives import (
    PRIMITIVE_TOOL_CLASSES,
    build_primitive_tools,
)


__version__ = "0.3.0"

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
    "HyloError",
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
