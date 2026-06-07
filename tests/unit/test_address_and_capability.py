"""Tests for spawn.address and spawn.capability."""

from __future__ import annotations

import pytest

from conjure.address import SYSTEM, USER, Address
from conjure.capability import CapabilitySet


def test_address_is_frozen():
    a = Address(id="ag-x")
    with pytest.raises(Exception):
        a.id = "ag-y"  # type: ignore[misc]


def test_address_is_hashable():
    a, b = Address(id="ag-x"), Address(id="ag-x")
    assert {a, b} == {a}
    assert hash(a) == hash(b)


def test_address_str_with_and_without_label():
    assert str(Address(id="ag-1")) == "ag-1"
    assert str(Address(id="ag-1", label="root")) == "ag-1(root)"


def test_sentinel_addresses_distinct():
    assert USER != SYSTEM
    assert USER.id.startswith("@")
    assert SYSTEM.id.startswith("@")


def test_capability_set_includes_self():
    a = Address(id="ag-1")
    caps = CapabilitySet(self_addr=a)
    assert a in caps
    assert len(caps) == 1


def test_capability_extend_and_contains():
    a, b = Address(id="ag-1"), Address(id="ag-2")
    caps = CapabilitySet(self_addr=a)
    assert b not in caps
    caps.extend(b)
    assert b in caps
    assert caps.contains(b)


def test_capability_initial_addresses():
    a, b, c = Address(id="ag-1"), Address(id="ag-2"), Address(id="ag-3")
    caps = CapabilitySet(self_addr=a, initial=[b, c])
    assert {a, b, c} <= set(caps)


def test_capability_snapshot_is_sorted_by_id():
    a, b, c = Address(id="ag-3"), Address(id="ag-1"), Address(id="ag-2")
    caps = CapabilitySet(self_addr=a, initial=[b, c])
    snap = caps.snapshot()
    assert [x.id for x in snap] == ["ag-1", "ag-2", "ag-3"]


def test_capability_extend_is_idempotent():
    a, b = Address(id="ag-1"), Address(id="ag-2")
    caps = CapabilitySet(self_addr=a)
    caps.extend(b)
    caps.extend(b)
    assert len(caps) == 2
