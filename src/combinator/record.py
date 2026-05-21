"""Per-agent state held by the runtime.

`AgentSpec` is the user-supplied description of what to spawn.
`AgentRecord` is the runtime's bookkeeping: spec, inbox, capabilities,
parent/children, status, and the auth token used by tool calls to
identify their calling agent.

The driver thread, wakeup event, and orchestral.Agent instance are added
to the record in Step 5; v0.1 of this module keeps the Step-4 shape
explicit so the runtime is testable without an LLM or any threading
machinery beyond the mailbox.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from combinator.address import Address
from combinator.capability import CapabilitySet
from combinator.mailbox import Mailbox


AgentStatus = Literal[
    "lazy",
    "running",
    "awaiting_permission",
    "idle",
    "error",
    "terminated",
]


class AgentSpec(BaseModel):
    """User-supplied spec for spawning an agent.

    `capabilities` holds Addresses handed in at spawn time. They become
    part of the spawned agent's CapabilitySet alongside its own address
    and (if present) its parent's. The spawning caller must hold all of
    these as capabilities itself — capability passing is enforced by
    the spawn tool, not by this model.
    """

    model_config = ConfigDict(populate_by_name=True)

    role_prompt: str
    label: str = ""
    # ``"orchestral"`` (in-process LLM client), ``"claude_agent"``
    # (``claude-agent-sdk`` session), or ``"auto"`` — see
    # ``combinator.engines.resolve_engine_name``.
    engine: str = "auto"
    tools: list[str] = Field(default_factory=list)
    llm: str = "default"
    # Model alias ("haiku" / "sonnet" / "opus") or full name
    # ("claude-sonnet-4-6"). Only consulted by the ``claude_agent``
    # engine. None means: use the CLI's default for the root, default
    # spawned children down to "haiku" so the substrate doesn't burn
    # Opus on every helper agent.
    model: str | None = None
    capabilities: list[Address] = Field(default_factory=list)
    initial_message: str | None = None
    lazy: bool = False
    # On-disk sandbox for filesystem tools. ``None`` falls back to
    # ``{runtime.store_dir}/sandboxes/{agent_id}/`` at first FS-tool
    # use. Filesystem tools refuse to read or write outside the
    # resolved sandbox.
    sandbox_dir: str | None = None
    # Per-tool permission decisions, e.g. ``{"Write": "deny",
    # "Bash": "ask"}``. Tools default to ``"allow"`` when absent.
    # The orchestral engine routes ``"ask"`` decisions through its
    # ``permission_hook`` (typically the UI); ``"deny"`` is hard.
    permissions: dict[str, str] = Field(default_factory=dict)


@dataclass
class AgentRecord:
    """Runtime bookkeeping for one agent."""

    addr: Address
    spec: AgentSpec
    inbox: Mailbox
    capabilities: CapabilitySet
    token: str
    parent: Address | None = None
    children: set[Address] = field(default_factory=set)
    status: AgentStatus = "lazy"
    spawned_at: float = field(default_factory=time.time)
    # Tree depth: 0 for the root agent, parent.depth + 1 for any spawn.
    # Used by the runtime to enforce the configured ``max_depth`` ceiling.
    depth: int = 0

    # Driver-side state — populated when a driver thread is attached.
    # The wakeup event is created up front so messages arriving for a
    # lazy agent can be queued and signaled when the driver starts.
    wakeup: Any = None
    driver: Any = None
    agent: Any = None

    # Optional per-agent EventLog populated by the tmux orchestrator's
    # spawn_listener; engine factory wires it into the display hook.
    # None for non-tmux modes (REPL renders directly to stdout).
    event_log: Any = None
