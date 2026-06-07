"""Agent addresses — opaque, frozen, hashable identifiers for routing.

Implemented as a frozen slotted dataclass rather than a pydantic model:
addresses are constructed on every spawn and live in capability sets,
record maps, and envelope routing — the per-instance overhead of
pydantic adds up under fan-out. The slotted ``__hash__``/``__eq__`` pair
is roughly an order of magnitude cheaper than the pydantic equivalent
for set/dict membership, which is the dominant access pattern.

``to_dict`` / ``from_dict`` give us pydantic-compatible JSON shapes for
the journal and the recv/wait_for return values — callers that used to
do ``addr.model_dump()`` can swap to ``addr.to_dict()`` with the same
on-the-wire bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Address:
    """An agent's address.

    ``id`` is opaque — never parse it, never derive routing or authority
    from its shape. ``label`` is a human-readable hint set at spawn time
    (e.g. ``"researcher-1"``); it is informational only and never used
    for delivery.

    Frozen so addresses can live in sets and dict keys.
    """

    id: str
    label: str = ""

    def __str__(self) -> str:
        return self.id if not self.label else f"{self.id}({self.label})"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Address":
        return cls(id=str(data["id"]), label=str(data.get("label", "")))

    # ------------------------------------------------------------------
    # Pydantic interop: external callers (config validation, tests that
    # build records with ``AgentSpec``) still expect ``model_dump`` and
    # ``model_validate``. Forward them to the dict helpers so the wire
    # shape is identical to the old pydantic-backed Address.
    def model_dump(self, **_: Any) -> dict[str, Any]:
        return self.to_dict()

    @classmethod
    def model_validate(cls, data: Any) -> "Address":
        if isinstance(data, cls):
            return data
        return cls.from_dict(data)


# Reserved sentinel addresses for non-agent participants. The ``@`` prefix
# distinguishes them from minted agent IDs (which start with ``ag-``).
USER = Address(id="@user", label="user")
SYSTEM = Address(id="@system", label="system")
