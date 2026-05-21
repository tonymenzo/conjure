"""Python combinators: ``agent_map``, ``agent_fold``, ``agent_filter``,
``agent_fixed_point``.

Each combinator builds on the primitive operations (spawn, send via
mailbox, recv via mailbox, terminate) without re-implementing them.
They are framework-privileged: they use ``runtime._spawn`` directly
rather than going through the spawn tool's capability checks (the
combinator itself is trusted code; the parent agent is the principal).

The common pattern is the **collector**: a lazy agent spawned solely
to receive replies. Workers are spawned with the collector in their
capability set and instructed (via the message they receive) to reply
to the collector. The combinator reads from the collector's inbox,
filtering by sender to assemble results in input order.
"""

from __future__ import annotations

import operator
import time
from typing import Any, Callable, Iterable, Sequence

from combinator.address import Address
from combinator.envelope import Envelope
from combinator.errors import Timeout
from combinator.ids import new_message_id
from combinator.record import AgentSpec
from combinator.runtime import Runtime


def _dispatch(
    *,
    runtime: Runtime,
    sender: Address,
    recipient: Address,
    body: Any,
    thread_id: str = "",
) -> Envelope:
    """Privileged: place a message into ``recipient``'s inbox 'from'
    ``sender``. Mirrors ``send_impl`` without capability checks since
    the combinator is framework code."""
    msg_id = new_message_id()
    env = Envelope(
        seq=0,
        msg_id=msg_id,
        from_=sender,
        to=recipient,
        thread_id=thread_id or msg_id,
        body=body,
        ts=time.time(),
    )
    record = runtime.record_for(recipient)
    stored = record.inbox.put(env)
    runtime._journal_send(stored)
    if record.wakeup is not None:
        record.wakeup.set()
    return stored


def _collect(
    *,
    runtime: Runtime,
    collector: Address,
    expected_senders: list[Address],
    timeout_s: float,
) -> list[Any]:
    """Wait for one reply from each ``expected_senders``, return their
    bodies in the same order. Raises ``Timeout`` (with ``workers``,
    ``received``, ``expected``, ``partial`` attached for the tool
    wrapper to surface) if not all replies are in by the deadline."""
    record = runtime.record_for(collector)
    results: list[Any] = [None] * len(expected_senders)
    received: list[bool] = [False] * len(expected_senders)
    deadline = time.monotonic() + timeout_s
    cursor = 0
    while not all(received):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            got = sum(1 for r in received if r)
            raise Timeout(
                f"only {got}/{len(received)} replies received before timeout",
                workers=[s.id for s in expected_senders],
                received=got,
                expected=len(expected_senders),
                partial=list(results),
            )
        envelopes = record.inbox.read(
            since_seq=cursor,
            max_n=len(expected_senders),
            timeout_s=min(remaining, 5.0),
        )
        if not envelopes:
            continue
        for env in envelopes:
            cursor = max(cursor, env.seq)
            for idx, sender in enumerate(expected_senders):
                if env.from_ == sender and not received[idx]:
                    results[idx] = env.body
                    received[idx] = True
                    break
    return results


def _spawn_collector(runtime: Runtime, parent: Address, label: str) -> Address:
    return runtime._spawn(
        parent=parent,
        spec=AgentSpec(
            role_prompt="(collector)",
            label=label,
            lazy=True,
        ),
    )


def agent_map(
    runtime: Runtime,
    parent: Address,
    spec_factory: Callable[[Any], AgentSpec],
    items: Sequence[Any],
    *,
    timeout_s: float = 120.0,
) -> list[Any]:
    """Spawn one worker per item; dispatch each item; gather replies.

    ``spec_factory(item)`` is called for each item to build the worker's
    spec. The worker receives a message ``{"item": item, "reply_to":
    <collector-id>}``. The combinator collects one reply from each
    worker and returns the result bodies in input order.

    All workers and the collector are terminated before this function
    returns.
    """
    items_list = list(items)
    if not items_list:
        return []

    collector = _spawn_collector(runtime, parent, label="map-collector")
    workers: list[Address] = []
    try:
        for item in items_list:
            spec = spec_factory(item)
            spec_with_cap = spec.model_copy(
                update={"capabilities": list(spec.capabilities) + [collector]}
            )
            worker = runtime._spawn(parent=parent, spec=spec_with_cap)
            workers.append(worker)
            _dispatch(
                runtime=runtime,
                sender=parent,
                recipient=worker,
                body={"item": item, "reply_to": collector.id},
            )
        results = _collect(
            runtime=runtime,
            collector=collector,
            expected_senders=workers,
            timeout_s=timeout_s,
        )
        return results
    finally:
        for worker in workers:
            runtime.terminate(worker, cascade=True)
        runtime.terminate(collector)


def agent_fold(
    runtime: Runtime,
    parent: Address,
    spec_factory: Callable[[Any], AgentSpec],
    items: Sequence[Any],
    init: Any,
    *,
    timeout_s: float = 120.0,
    trace: bool = False,
) -> Any:
    """Sequential threading: spawn one worker per item in order; pass
    the accumulator along with the item; the worker returns the new
    accumulator.

    Worker receives ``{"acc": acc, "item": item, "reply_to":
    <collector-id>}`` and replies with the new accumulator value.

    When ``trace=True`` returns ``{"result": final_acc, "trace":
    [init, acc_after_step_0, ..., final]}`` so callers can inspect
    intermediate states — essential when the per-step output IS the
    value (drift detection, narration of a chain, progress UIs).
    Default ``trace=False`` preserves the original return shape.
    """
    items_list = list(items)
    if not items_list:
        return {"result": init, "trace": [init]} if trace else init

    collector = _spawn_collector(runtime, parent, label="fold-collector")
    acc = init
    history: list[Any] = [init] if trace else []
    workers: list[Address] = []
    try:
        for item in items_list:
            spec = spec_factory(item)
            spec_with_cap = spec.model_copy(
                update={"capabilities": list(spec.capabilities) + [collector]}
            )
            worker = runtime._spawn(parent=parent, spec=spec_with_cap)
            workers.append(worker)
            _dispatch(
                runtime=runtime,
                sender=parent,
                recipient=worker,
                body={"acc": acc, "item": item, "reply_to": collector.id},
            )
            [reply] = _collect(
                runtime=runtime,
                collector=collector,
                expected_senders=[worker],
                timeout_s=timeout_s,
            )
            acc = reply
            if trace:
                history.append(acc)
        return {"result": acc, "trace": history} if trace else acc
    finally:
        for worker in workers:
            runtime.terminate(worker, cascade=True)
        runtime.terminate(collector)


def agent_filter(
    runtime: Runtime,
    parent: Address,
    spec_factory: Callable[[Any], AgentSpec],
    items: Sequence[Any],
    *,
    timeout_s: float = 120.0,
) -> list[Any]:
    """Keep each item whose worker returns a truthy value."""
    items_list = list(items)
    if not items_list:
        return []
    verdicts = agent_map(
        runtime, parent, spec_factory, items_list, timeout_s=timeout_s
    )
    return [item for item, keep in zip(items_list, verdicts) if keep]


def agent_fixed_point(
    runtime: Runtime,
    parent: Address,
    spec_factory: Callable[[Any], AgentSpec],
    seed: Any,
    *,
    eq: Callable[[Any, Any], bool] = operator.eq,
    max_iters: int = 16,
    timeout_s: float = 600.0,
) -> tuple[Any, bool]:
    """Iterate: spawn worker, feed it the current value, get a new
    value, terminate. Stop when ``eq(new, current)`` or after
    ``max_iters`` iterations.

    Returns ``(value, converged)`` where ``converged`` is True if the
    loop reached a fixed point before ``max_iters``.
    """
    collector = _spawn_collector(runtime, parent, label="fix-collector")
    current = seed
    converged = False
    try:
        for _ in range(max_iters):
            spec = spec_factory(current)
            spec_with_cap = spec.model_copy(
                update={"capabilities": list(spec.capabilities) + [collector]}
            )
            worker = runtime._spawn(parent=parent, spec=spec_with_cap)
            _dispatch(
                runtime=runtime,
                sender=parent,
                recipient=worker,
                body={"value": current, "reply_to": collector.id},
            )
            [reply] = _collect(
                runtime=runtime,
                collector=collector,
                expected_senders=[worker],
                timeout_s=timeout_s,
            )
            runtime.terminate(worker, cascade=True)
            if eq(reply, current):
                current = reply
                converged = True
                break
            current = reply
        return current, converged
    finally:
        runtime.terminate(collector)
