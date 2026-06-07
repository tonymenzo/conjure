"""Shared fixtures for toy-program tests."""

from __future__ import annotations

import time
from typing import Any

import pytest

from conjure.address import Address
from conjure.runtime import Runtime


@pytest.fixture
def wait_for_result():
    """Polls ``collector``'s inbox until an envelope arrives whose body
    is a dict containing a ``result`` key. Returns the result value, or
    fails the test on timeout."""

    def _wait(rt: Runtime, collector: Address, *, timeout: float = 5.0) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for env in rt.read_inbox(collector):
                if isinstance(env.body, dict) and "result" in env.body:
                    return env.body["result"]
            time.sleep(0.02)
        pytest.fail(f"no result received within {timeout}s")

    return _wait
