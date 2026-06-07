"""CapabilitySet — the set of addresses an agent is permitted to message.

Possession of an address is permission to send to it. Membership is
granted at spawn time (via the spawn spec) or by an explicit introduction
from another holder. An agent's own address is always included so
self-messaging idioms (deferred work, reminders) are available without
ceremony.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from conjure.address import Address


class CapabilitySet:

    def __init__(
        self,
        *,
        self_addr: Address,
        initial: Iterable[Address] | None = None,
    ) -> None:
        self._self = self_addr
        self._addrs: set[Address] = {self_addr}
        if initial:
            self._addrs.update(initial)

    def extend(self, addr: Address) -> None:
        self._addrs.add(addr)

    def contains(self, addr: Address) -> bool:
        return addr in self._addrs

    def snapshot(self) -> list[Address]:
        """Stable, sorted list of addresses, suitable for journaling."""
        return sorted(self._addrs, key=lambda a: a.id)

    def __contains__(self, addr: object) -> bool:
        return addr in self._addrs

    def __len__(self) -> int:
        return len(self._addrs)

    def __iter__(self) -> Iterator[Address]:
        return iter(self._addrs)
