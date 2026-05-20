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


AgentStatus = Literal["lazy", "running", "idle", "terminated"]


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
    tools: list[str] = Field(default_factory=list)
    llm: str = "default"
    capabilities: list[Address] = Field(default_factory=list)
    initial_message: str | None = None
    lazy: bool = False
    cost_ceiling: int | None = None


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
    cost_used: int = 0
    spawned_at: float = field(default_factory=time.time)

    # Driver-side state — populated when a driver thread is attached.
    # The wakeup event is created up front so messages arriving for a
    # lazy agent can be queued and signaled when the driver starts.
    wakeup: Any = None
    driver: Any = None
    agent: Any = None
