"""Scripted test substrate.

``ScriptedEngine`` is an ``Engine`` implementation that delegates each
``step`` to a user-supplied callable. The callable observes the prompt
and any new inbox envelopes, may invoke primitive tool ``*_impl``
functions to perform actions, and returns a string result. No LLM is
involved — tests build deterministic agent loops by writing behavior
functions.

``BehaviorRegistry`` indexes behaviors by ``spec.role_prompt`` so a
single ``engine_factory`` produces appropriately-scripted engines for
agents with different roles in the same runtime.

This module is part of the public API: external users can write their
own offline tests against the framework using these primitives.
"""

from __future__ import annotations

from typing import Any, Callable, TYPE_CHECKING

from combinator.address import Address
from combinator.envelope import Envelope

if TYPE_CHECKING:
    from combinator.record import AgentRecord
    from combinator.runtime import Runtime


Behavior = Callable[["ScriptedEngine", str, list[Envelope]], Any]


class ScriptedEngine:

    def __init__(
        self,
        *,
        record: "AgentRecord",
        runtime: "Runtime",
        behavior: Behavior,
    ) -> None:
        self.record = record
        self.runtime = runtime
        self.behavior = behavior
        self._cursor = 0
        self.calls: int = 0
        # Per-agent state slot for event-driven behaviors that span
        # multiple ``step`` calls (e.g. spawn-and-await-reply patterns).
        self.state: dict[str, Any] = {}

    @property
    def addr(self) -> Address:
        return self.record.addr

    @property
    def token(self) -> str:
        return self.record.token

    def step(self, prompt: str) -> str:
        envelopes = self.record.inbox.read(
            since_seq=self._cursor, max_n=1024, timeout_s=0.0
        )
        if envelopes:
            self._cursor = envelopes[-1].seq
        self.calls += 1
        result = self.behavior(self, prompt, envelopes)
        return str(result) if result is not None else "ok"


class BehaviorRegistry:
    """Maps ``spec.role_prompt`` values to behavior callables.

    Use ``register(role_prompt, behavior)`` to register a behavior, then
    ``factory()`` to obtain an ``EngineFactory`` that picks the right
    behavior for each spawned agent. Agents whose role has no
    registered behavior get a no-op default (returns ``"noop"``).
    """

    def __init__(self) -> None:
        self._behaviors: dict[str, Behavior] = {}

    def register(self, role_prompt: str, behavior: Behavior) -> None:
        self._behaviors[role_prompt] = behavior

    def factory(self):
        def make(record: "AgentRecord", runtime: "Runtime") -> ScriptedEngine:
            behavior = self._behaviors.get(
                record.spec.role_prompt,
                _default_behavior,
            )
            return ScriptedEngine(
                record=record, runtime=runtime, behavior=behavior
            )
        return make


def _default_behavior(
    engine: ScriptedEngine,
    prompt: str,
    envelopes: list[Envelope],
) -> str:
    return "noop"
