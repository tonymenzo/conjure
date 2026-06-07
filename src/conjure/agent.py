"""Agent wrapper and the ``Engine`` protocol.

``Engine`` is the abstract reasoning surface the driver invokes. In
production it is satisfied by a small adapter over ``orchestral.Agent``;
in tests it is satisfied by ``conjure.scripted.ScriptedEngine``. The
driver and runtime never reference orchestral directly — that
dependency lives entirely in the chosen Engine implementation.

``Agent`` is the lightweight pair (record, engine) the driver works with.
It carries no logic of its own — its ``step`` simply forwards to the
engine. Identity and capability live on the ``AgentRecord``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from conjure.address import Address
from conjure.record import AgentRecord


@runtime_checkable
class Engine(Protocol):
    """LLM-backed reasoning loop.

    ``step`` consumes a prompt and returns the final assistant text after
    running tool calls to completion. Implementations are not required to
    be thread-safe — the driver ensures one ``step`` runs at a time per
    agent.
    """

    def step(self, prompt: str) -> str: ...


class Agent:

    def __init__(self, *, record: AgentRecord, engine: Engine) -> None:
        self.record = record
        self._engine = engine

    def step(self, prompt: str) -> str:
        return self._engine.step(prompt)

    @property
    def addr(self) -> Address:
        return self.record.addr

    @property
    def engine(self) -> Engine:
        return self._engine
