"""Agent addresses — opaque, frozen, hashable identifiers for routing."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Address(BaseModel):
    """An agent's address.

    `id` is opaque — never parse it, never derive routing or authority
    from its shape. `label` is a human-readable hint set at spawn time
    (e.g. ``"researcher-1"``); it is informational only and never used
    for delivery.

    Frozen so addresses can live in sets and dict keys.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    label: str = ""

    def __str__(self) -> str:
        return self.id if not self.label else f"{self.id}({self.label})"


# Reserved sentinel addresses for non-agent participants. The ``@`` prefix
# distinguishes them from minted agent IDs (which start with ``ag-``).
USER = Address(id="@user", label="user")
SYSTEM = Address(id="@system", label="system")
