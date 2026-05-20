"""Envelopes — immutable messages flowing between agents.

An envelope captures the full state of one message in transit. `seq` is
monotonic *per receiving inbox*, not globally; `msg_id` is globally
unique. `thread_id` defaults to the opening message's `msg_id` and is
carried through subsequent replies via `in_reply_to`.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from combinator.address import Address


class Envelope(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    seq: int
    msg_id: str
    from_: Address = Field(alias="from")
    to: Address
    thread_id: str
    in_reply_to: str | None = None
    body: Any
    headers: dict[str, str] = Field(default_factory=dict)
    ts: float = Field(default_factory=time.time)
