"""Python combinators: ``agent_map``, ``agent_fold``, ``agent_filter``,
``agent_fixed_point``, ``agent_race``, ``agent_ensemble``, ``agent_critic``.

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

from spawn.address import Address
from spawn.envelope import Envelope
from spawn.errors import Timeout
from spawn.ids import new_message_id
from spawn.record import AgentSpec
from spawn.runtime import Runtime


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
    sender_to_idx = {sender: idx for idx, sender in enumerate(expected_senders)}
    remaining_count = len(expected_senders)
    deadline = time.monotonic() + timeout_s
    cursor = 0
    while remaining_count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            got = len(expected_senders) - remaining_count
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
            idx = sender_to_idx.get(env.from_)
            if idx is None or received[idx]:
                continue
            results[idx] = env.body
            received[idx] = True
            remaining_count -= 1
    return results


def _spawn_collector(runtime: Runtime, parent: Address, label: str) -> Address:
    return runtime._spawn(
        parent=parent,
        spec=AgentSpec(
            role_prompt="(collector)",
            label=label,
            lazy=True,
            internal=True,
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
        # Build every spec, then spawn the whole batch in one lock
        # acquisition. Previously this loop took the runtime lock N
        # times in series — the dominant latency for fan-out before
        # the first worker even saw work.
        specs: list[AgentSpec] = []
        for item in items_list:
            base = spec_factory(item)
            specs.append(
                base.model_copy(
                    update={"capabilities": list(base.capabilities) + [collector]}
                )
            )
        workers = runtime._spawn_batch(parent=parent, specs=specs)
        runtime.dispatch_batch(
            [
                (parent, worker, {"item": item, "reply_to": collector.id})
                for worker, item in zip(workers, items_list)
            ]
        )
        results = _collect(
            runtime=runtime,
            collector=collector,
            expected_senders=workers,
            timeout_s=timeout_s,
        )
        return results
    finally:
        # One coalesced supervision envelope to the parent rather than
        # N per-child events at the tail of the fan-out. Collector goes
        # away separately so the parent doesn't see its plumbing in the
        # event payload.
        if workers:
            runtime.terminate_batch(
                workers, requested_by="map-cleanup", cascade=True
            )
        runtime.terminate(collector, requested_by="oneshot")


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
        if workers:
            runtime.terminate_batch(
                workers, requested_by="fold-cleanup", cascade=True
            )
        runtime.terminate(collector, requested_by="oneshot")


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


def _collect_first(
    *,
    runtime: Runtime,
    collector: Address,
    expected_senders: list[Address],
    timeout_s: float,
) -> tuple[int, Any]:
    """Block until ANY ``expected_senders`` replies; return ``(idx, body)``.

    Companion to ``_collect`` (which waits for all). Used by races and
    other first-wins fan-outs.
    """
    record = runtime.record_for(collector)
    sender_to_idx = {sender: idx for idx, sender in enumerate(expected_senders)}
    deadline = time.monotonic() + timeout_s
    cursor = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Timeout(
                "no reply received before timeout",
                workers=[s.id for s in expected_senders],
                received=0,
                expected=len(expected_senders),
                partial=[],
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
            idx = sender_to_idx.get(env.from_)
            if idx is None:
                continue
            return idx, env.body


def agent_race(
    runtime: Runtime,
    parent: Address,
    specs: Sequence[AgentSpec],
    body: Any,
    *,
    timeout_s: float = 60.0,
) -> tuple[int, Any]:
    """Spawn one worker per spec, dispatch the same ``body`` to each, return
    the **first** reply. Losing workers are terminated.

    Returns ``(winner_idx, winner_body)`` so the caller can attribute the
    answer to the spec that produced it (e.g. "the haiku racer won at
    index 0"). The losers do not get a chance to reply; their work is
    discarded.

    Use this when you don't know which spec will give the best answer
    quickly — race haiku/sonnet/opus on a hard question, race three
    retrieval strategies, race three reasoning approaches.
    """
    specs_list = list(specs)
    if not specs_list:
        raise ValueError("agent_race requires at least one spec")
    collector = _spawn_collector(runtime, parent, label="race-collector")
    workers: list[Address] = []
    try:
        augmented = [
            s.model_copy(
                update={"capabilities": list(s.capabilities) + [collector]}
            )
            for s in specs_list
        ]
        workers = runtime._spawn_batch(parent=parent, specs=augmented)
        runtime.dispatch_batch(
            [
                (parent, worker, {"item": body, "reply_to": collector.id})
                for worker in workers
            ]
        )
        return _collect_first(
            runtime=runtime,
            collector=collector,
            expected_senders=workers,
            timeout_s=timeout_s,
        )
    finally:
        if workers:
            runtime.terminate_batch(
                workers, requested_by="race-cleanup", cascade=True
            )
        runtime.terminate(collector, requested_by="oneshot")


def agent_ensemble(
    runtime: Runtime,
    parent: Address,
    specs: Sequence[AgentSpec],
    body: Any,
    aggregator_spec: AgentSpec,
    *,
    timeout_s: float = 120.0,
) -> Any:
    """Best-of-N synthesis: fan out N workers on the same ``body``, gather
    all replies, hand them to ``aggregator_spec``, return its synthesis.

    Differs from a bare ``agent_map`` because the gather phase IS an
    agent — the aggregator can vote, synthesize, or pick. The aggregator
    receives ``{"item": [worker1_reply, ..., workerN_reply], "reply_to":
    collector_id}`` so it can read its inbox directly and decide.
    """
    specs_list = list(specs)
    if not specs_list:
        raise ValueError("agent_ensemble requires at least one worker spec")
    collector = _spawn_collector(runtime, parent, label="ensemble-collector")
    workers: list[Address] = []
    aggregator: Address | None = None
    try:
        augmented = [
            s.model_copy(
                update={"capabilities": list(s.capabilities) + [collector]}
            )
            for s in specs_list
        ]
        workers = runtime._spawn_batch(parent=parent, specs=augmented)
        runtime.dispatch_batch(
            [
                (parent, worker, {"item": body, "reply_to": collector.id})
                for worker in workers
            ]
        )
        worker_replies = _collect(
            runtime=runtime,
            collector=collector,
            expected_senders=workers,
            timeout_s=timeout_s,
        )
        # Aggregator runs after the fan-in: spawning it earlier would
        # just sit idle. Same collector so we don't allocate two.
        aggregator_with_cap = aggregator_spec.model_copy(
            update={
                "capabilities": list(aggregator_spec.capabilities) + [collector]
            }
        )
        aggregator = runtime._spawn(parent=parent, spec=aggregator_with_cap)
        _dispatch(
            runtime=runtime,
            sender=parent,
            recipient=aggregator,
            body={"item": worker_replies, "reply_to": collector.id},
        )
        [aggregated] = _collect(
            runtime=runtime,
            collector=collector,
            expected_senders=[aggregator],
            timeout_s=timeout_s,
        )
        return aggregated
    finally:
        if workers:
            runtime.terminate_batch(
                workers, requested_by="ensemble-cleanup", cascade=True
            )
        if aggregator is not None:
            runtime.terminate(aggregator, requested_by="oneshot")
        runtime.terminate(collector, requested_by="oneshot")


def _parse_critic_verdict(verdict: Any) -> tuple[bool, str]:
    """Tolerantly extract ``(ok, notes)`` from a critic's reply.

    Accepts a dict (``{"ok": bool, "notes": str}``), a JSON string of
    the same shape, or plain text starting with ``ok`` / ``approved`` /
    ``lgtm`` (case-insensitive). Anything else is treated as a
    not-yet-approved verdict whose notes are the raw text.
    """
    if isinstance(verdict, dict):
        return bool(verdict.get("ok", False)), str(verdict.get("notes", ""))
    if isinstance(verdict, str):
        import json as _json
        try:
            parsed = _json.loads(verdict)
            if isinstance(parsed, dict):
                return (
                    bool(parsed.get("ok", False)),
                    str(parsed.get("notes", "")),
                )
        except (ValueError, TypeError):
            pass
        stripped = verdict.strip().lower()
        if stripped.startswith(("ok", "approved", "lgtm")):
            return True, verdict
        return False, verdict
    return False, str(verdict)


def agent_critic(
    runtime: Runtime,
    parent: Address,
    generator_spec: AgentSpec,
    critic_spec: AgentSpec,
    body: Any,
    *,
    max_iters: int = 5,
    timeout_s: float = 600.0,
) -> tuple[Any, bool, int]:
    """Generator + critic refinement loop. Each iteration spawns a fresh
    generator (it sees the accumulated critique as ``feedback``) and a
    fresh critic. Stops when the critic returns ``{"ok": true, ...}`` or
    after ``max_iters`` iterations.

    Returns ``(last_output, converged, iters_used)``. ``converged`` is
    True only if the critic actually approved before the iteration cap.

    Generator receives ``{"item": <body>, "feedback": [notes...],
    "reply_to": collector_id}``. Critic receives ``{"item":
    <generator_output>, "reply_to": collector_id}`` and is expected to
    reply with a dict ``{"ok": bool, "notes": "<text>"}`` or a JSON
    string of the same shape. ``_parse_critic_verdict`` accepts plain
    text starting with ``ok``/``approved``/``lgtm`` as an approval.

    The proper shape ``agent_fixed_point`` was reaching for — strict
    equality almost never fires on agent output; a critic that says
    "this is good enough" does.
    """
    if max_iters < 1:
        raise ValueError("agent_critic requires max_iters >= 1")
    collector = _spawn_collector(runtime, parent, label="critic-collector")
    feedback: list[str] = []
    last_output: Any = None
    converged = False
    iters_used = 0
    try:
        for i in range(max_iters):
            iters_used = i + 1

            gen_with_cap = generator_spec.model_copy(
                update={
                    "capabilities": list(generator_spec.capabilities) + [collector]
                }
            )
            gen = runtime._spawn(parent=parent, spec=gen_with_cap)
            _dispatch(
                runtime=runtime,
                sender=parent,
                recipient=gen,
                body={
                    "item": body,
                    "feedback": list(feedback),
                    "reply_to": collector.id,
                },
            )
            [output] = _collect(
                runtime=runtime,
                collector=collector,
                expected_senders=[gen],
                timeout_s=timeout_s,
            )
            runtime.terminate(gen, requested_by="oneshot", cascade=True)
            last_output = output

            crit_with_cap = critic_spec.model_copy(
                update={
                    "capabilities": list(critic_spec.capabilities) + [collector]
                }
            )
            crit = runtime._spawn(parent=parent, spec=crit_with_cap)
            _dispatch(
                runtime=runtime,
                sender=parent,
                recipient=crit,
                body={"item": output, "reply_to": collector.id},
            )
            [verdict] = _collect(
                runtime=runtime,
                collector=collector,
                expected_senders=[crit],
                timeout_s=timeout_s,
            )
            runtime.terminate(crit, requested_by="oneshot", cascade=True)

            ok, notes = _parse_critic_verdict(verdict)
            if ok:
                converged = True
                break
            feedback.append(notes)
        return last_output, converged, iters_used
    finally:
        runtime.terminate(collector, requested_by="oneshot")


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
            # ``requested_by="oneshot"`` suppresses the per-iteration
            # ``child_event terminated`` envelope; the parent already
            # consumed the reply via the collector, so the supervision
            # event would just flood the inbox once per loop turn.
            runtime.terminate(
                worker, requested_by="oneshot", cascade=True
            )
            if eq(reply, current):
                current = reply
                converged = True
                break
            current = reply
        return current, converged
    finally:
        runtime.terminate(collector, requested_by="oneshot")
