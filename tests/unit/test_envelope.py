"""Tests for combinator.envelope."""

from __future__ import annotations

import json

import pytest

from combinator.address import Address
from combinator.envelope import Envelope


def _make(
    *,
    seq: int = 1,
    msg_id: str = "msg-1",
    from_: Address | None = None,
    to: Address | None = None,
    thread_id: str = "msg-1",
    body=None,
) -> Envelope:
    return Envelope(
        seq=seq,
        msg_id=msg_id,
        from_=from_ or Address(id="ag-a"),
        to=to or Address(id="ag-b"),
        thread_id=thread_id,
        body=body,
        ts=0.0,
    )


def test_envelope_roundtrip_json_uses_from_alias():
    env = _make(body={"x": 1})
    data = env.model_dump(by_alias=True)
    assert "from" in data and "from_" not in data
    rehydrated = Envelope.model_validate(data)
    assert rehydrated == env


def test_envelope_is_frozen():
    env = _make()
    with pytest.raises(Exception):
        env.seq = 99  # type: ignore[misc]


def test_envelope_defaults_thread_to_self_in_caller_code():
    env = _make()
    assert env.thread_id == env.msg_id


def test_envelope_ts_default_factory_runs():
    env = Envelope(
        seq=1,
        msg_id="msg-1",
        from_=Address(id="ag-a"),
        to=Address(id="ag-b"),
        thread_id="msg-1",
        body=None,
    )
    assert env.ts > 0


def test_envelope_body_can_be_arbitrary_json():
    env = _make(body=[1, 2, {"k": "v"}])
    serialized = json.dumps(env.model_dump(by_alias=True), default=str)
    assert "k" in serialized
