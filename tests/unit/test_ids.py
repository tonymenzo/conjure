"""Tests for spawn.ids."""

from __future__ import annotations

import re

from conjure.ids import new_agent_id, new_message_id, new_runtime_token


def test_agent_id_format():
    aid = new_agent_id()
    assert aid.startswith("ag-")
    assert re.fullmatch(r"ag-[a-z2-7]+", aid), aid


def test_message_id_format():
    mid = new_message_id()
    assert mid.startswith("msg-")
    assert re.fullmatch(r"msg-[a-z2-7]+", mid), mid


def test_agent_ids_are_unique():
    sample = {new_agent_id() for _ in range(1000)}
    assert len(sample) == 1000


def test_message_ids_are_unique():
    sample = {new_message_id() for _ in range(1000)}
    assert len(sample) == 1000


def test_runtime_token_unique_and_nontrivial():
    a, b = new_runtime_token(), new_runtime_token()
    assert a != b
    assert len(a) > 16
