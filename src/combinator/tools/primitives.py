"""Primitive tools: ``spawn``, ``send``, ``recv``, ``wait_for``,
``terminate``, ``introduce``, ``list_inbox``.

Each tool has two faces:

- A pure-Python ``*_impl`` function — takes the calling agent's token
  plus arguments, returns a structured ``{"ok": True | False, ...}``
  dict. These are unit-tested directly and are what
  ``ScriptedEngine`` calls in offline tests.
- An orchestral tool class — declares its runtime fields, holds its
  ``runtime_token`` as a state field, delegates to the ``*_impl``
  function in ``_run``.

All return shapes use ``{"ok": True, ...}`` on success and
``{"ok": False, "code": "<machine-readable>", "error": "<message>"}``
on failure — never raise into orchestral's call path.

Capability enforcement happens here, not in the runtime: tools verify
the caller's capability set before invoking the runtime's trusted
``_spawn`` / mailbox operations.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from combinator.address import Address
from combinator.envelope import Envelope
from combinator.errors import MaxDepthExceeded, NoSuchAddress, Terminated, Timeout
from combinator.ids import new_message_id
from combinator.record import AgentSpec
from combinator.tools._base import (
    RuntimeField,
    StateField,
    StatelessRuntimeTool,
    resolve_token,
)


_CALL_WORKER_SUFFIX = (
    "\n\nIMPORTANT INSTRUCTIONS (Call worker):\n"
    "1. Your task body is in the new message shown in this prompt — "
    "read it directly. Do NOT call Recv or ListInbox.\n"
    "2. Compute the answer. Reply with ONE `Send(to=\"caller\", "
    "body=<result>)` call.\n"
    "3. After the send returns ok, you are DONE. The runtime will "
    "auto-terminate you. End your turn with a single short "
    "sentence confirming completion — no narration."
)

if TYPE_CHECKING:
    from combinator.runtime import Runtime


# ---------- Implementation functions ----------

def _err(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "code": code, "error": message}


def _resolve(token: str) -> "tuple[Runtime, Address] | dict[str, Any]":
    resolved = resolve_token(token)
    if resolved is None:
        return _err("no_runtime", "tool is not bound to a runtime")
    return resolved


def _addr_from_str(addr_str: str, runtime: "Runtime") -> Address | None:
    """Look up an Address by its id string. Returns None if not known."""
    return runtime.address_by_id(addr_str)


def _resolve_addr(
    addr_str: str,
    runtime: "Runtime",
    caller_addr: Address,
) -> Address | None:
    """Resolve a caller-supplied address string. Accepts:

    - ``"self"`` → the caller's own address.
    - ``"parent"`` → the caller's parent (``None`` for the root).
    - ``"caller"`` → the sender of the most-recent envelope the
      agent received. Lets workers reply to "whoever just messaged
      me" without the agent tracking the address itself.
    - ``"@user"`` / ``"@system"`` → the sentinels (id lookup).
    - ``"ag-..."`` → exact id lookup.
    - ``"<label>"`` → matched against the caller's own children; only
      resolves when exactly one child carries that label.

    Returns ``None`` if no unambiguous match exists; callers convert
    that into ``code=no_such_address``."""
    if not addr_str:
        return None
    if addr_str == "self":
        return caller_addr
    if addr_str == "parent":
        return runtime.record_for(caller_addr).parent
    if addr_str == "caller":
        return runtime.record_for(caller_addr).last_received_from
    by_id = runtime.address_by_id(addr_str)
    if by_id is not None:
        return by_id
    caller_record = runtime.record_for(caller_addr)
    matches = [c for c in caller_record.children if c.label == addr_str]
    if len(matches) == 1:
        return matches[0]
    return None


def spawn_impl(
    *,
    token: str,
    role_prompt: str,
    label: str = "",
    tools: list[str] | None = None,
    llm: str = "default",
    capabilities: list[str] | None = None,
    initial_message: str = "",
    lazy: bool = False,
    engine: str = "auto",
    sandbox_dir: str | None = None,
    permissions: dict[str, str] | None = None,
    model: str | None = None,
    oneshot: bool = False,
) -> dict[str, Any]:
    resolved = _resolve(token)
    if isinstance(resolved, dict):
        return resolved
    runtime, caller_addr = resolved

    caller_record = runtime.record_for(caller_addr)
    if caller_record.status == "terminated":
        return _err("terminated", "caller is terminated")

    # Resolve handed-in capabilities to Address objects. Each requested
    # capability must already be in the caller's set.
    cap_addrs: list[Address] = []
    for cap_id in capabilities or []:
        cap_addr = _resolve_addr(cap_id, runtime, caller_addr)
        if cap_addr is None:
            return _err("no_such_address", f"unknown address: {cap_id}")
        if cap_addr not in caller_record.capabilities:
            return _err(
                "cap_violation",
                f"caller does not hold capability for {cap_id}",
            )
        cap_addrs.append(cap_addr)

    spec = AgentSpec(
        role_prompt=role_prompt,
        label=label,
        engine=engine,
        tools=list(tools or []),
        llm=llm,
        model=model,
        capabilities=cap_addrs,
        initial_message=initial_message or None,
        lazy=lazy,
        oneshot=oneshot,
        sandbox_dir=sandbox_dir,
        permissions=permissions or {},
    )

    try:
        child_addr = runtime._spawn(parent=caller_addr, spec=spec)
    except Terminated as e:
        return _err("terminated", str(e))
    except NoSuchAddress as e:
        return _err("no_such_address", str(e))
    except MaxDepthExceeded as e:
        return _err("depth_exceeded", str(e))

    # Optionally send the initial message right away.
    if initial_message:
        runtime.send_external(to=child_addr, body=initial_message, sender="system")

    return {"ok": True, "address": child_addr.id, "label": child_addr.label}


def send_impl(
    *,
    token: str,
    to: str,
    body: Any,
    thread_id: str = "",
    in_reply_to: str = "",
    kind: str = "msg",
) -> dict[str, Any]:
    resolved = _resolve(token)
    if isinstance(resolved, dict):
        return resolved
    runtime, caller_addr = resolved

    target_addr = _resolve_addr(to, runtime, caller_addr)
    if target_addr is None:
        return _err("no_such_address", f"unknown address: {to}")

    caller_record = runtime.record_for(caller_addr)
    if target_addr not in caller_record.capabilities:
        return _err("not_permitted", f"caller cannot send to {to}")

    target_record = runtime.record_for(target_addr)
    if target_record.status == "terminated":
        return _err("terminated", f"target {to} is terminated")

    # Suppress near-duplicate sends: if the same caller pushed the
    # exact same body to this target within the last few seconds, the
    # LLM is most likely stuttering (emitting a duplicate ``send`` tool
    # call within one turn). Returning ok=True with the existing
    # msg_id + a ``deduplicated`` flag keeps the engine happy while
    # preventing the target from seeing the same message twice.
    duplicate = _find_recent_duplicate(
        target_record.inbox, sender=caller_addr, body=body
    )
    if duplicate is not None:
        return {
            "ok": True,
            "msg_id": duplicate.msg_id,
            "seq": duplicate.seq,
            "deduplicated": True,
        }

    msg_id = new_message_id()
    env = Envelope(
        seq=0,
        msg_id=msg_id,
        from_=caller_addr,
        to=target_addr,
        thread_id=thread_id or msg_id,
        in_reply_to=in_reply_to or None,
        body=body,
        headers={"kind": kind} if kind != "msg" else {},
        ts=time.time(),
    )
    stored = target_record.inbox.put(env)
    runtime._journal_send(stored)
    # Reciprocal capability: the recipient may now reply to the sender.
    # Without this, common request/response patterns require explicit
    # introductions for every reply path, which doesn't scale across a
    # recursive spawn tree.
    target_record.capabilities.extend(caller_addr)
    if target_record.wakeup is not None:
        target_record.wakeup.set()

    return {"ok": True, "msg_id": stored.msg_id, "seq": stored.seq}


# Dedup window: how recently must a same-(from, body) message have
# landed for a new send to be treated as a stutter. 5s comfortably
# covers an LLM emitting two send tool calls in one turn but is short
# enough that legitimate retries (rare, and usually with different
# bodies) get through.
_DEDUP_WINDOW_S = 5.0


def _find_recent_duplicate(
    inbox: Any,
    *,
    sender: Address,
    body: Any,
) -> Any:
    """Return the most recent envelope in ``inbox`` from ``sender``
    with the same body and within ``_DEDUP_WINDOW_S`` seconds. Returns
    None if there's no such duplicate."""
    now = time.time()
    # Last 10 envelopes is plenty — the duplicate, if any, is among
    # the most recent few.
    recent = inbox.read_recent(max_n=10)
    for env in reversed(recent):
        if (now - env.ts) > _DEDUP_WINDOW_S:
            return None  # older than the window — done scanning
        if env.from_ != sender:
            continue
        if env.body != body:
            continue
        return env
    return None


def recv_impl(
    *,
    token: str,
    thread_id: str = "",
    from_: str = "",
    since_seq: int = 0,
    max_n: int = 1,
    timeout_s: float = 0.0,
) -> dict[str, Any]:
    resolved = _resolve(token)
    if isinstance(resolved, dict):
        return resolved
    runtime, caller_addr = resolved

    record = runtime.record_for(caller_addr)
    envelopes = record.inbox.read(
        since_seq=since_seq,
        max_n=max_n,
        thread_id=thread_id,
        from_id=from_,
        timeout_s=timeout_s,
    )
    return {
        "ok": True,
        "envelopes": [e.model_dump(by_alias=True) for e in envelopes],
        "next_seq": envelopes[-1].seq if envelopes else since_seq,
    }


def wait_for_impl(
    *,
    token: str,
    predicate_kind: str = "any",
    value: str = "",
    since_seq: int = 0,
    timeout_s: float = 30.0,
    max_n: int = 1024,
) -> dict[str, Any]:
    """Block until ``max_n`` matching envelopes are in the inbox OR
    the deadline elapses, then return whatever's been collected.

    ``Mailbox.read`` returns as soon as ≥1 match exists, so a single
    call doesn't actually fan-in N replies. We loop here: each pass
    waits for more matches, advancing ``since_seq`` past the seq we
    already saw, until ``max_n`` is reached or time runs out. Returns
    a partial collection on timeout — callers can check the result
    length against the cap to detect a short read."""
    if max_n <= 0:
        return {"ok": True, "envelopes": [], "next_seq": since_seq}

    deadline = time.monotonic() + max(0.0, timeout_s)
    collected: list[Envelope] = []
    cursor = since_seq

    resolved = _resolve(token)
    if isinstance(resolved, dict):
        return resolved
    runtime, caller_addr = resolved
    record = runtime.record_for(caller_addr)

    while len(collected) < max_n:
        remaining = max(0.0, deadline - time.monotonic())
        envelopes = record.inbox.read(
            since_seq=cursor,
            max_n=max_n - len(collected),
            thread_id=value if predicate_kind == "thread" else "",
            from_id=value if predicate_kind == "from" else "",
            timeout_s=remaining,
        )
        if not envelopes:
            # Deadline elapsed without further matches.
            break
        collected.extend(envelopes)
        cursor = envelopes[-1].seq
        if remaining <= 0:
            break

    return {
        "ok": True,
        "envelopes": [e.model_dump(by_alias=True) for e in collected],
        "next_seq": collected[-1].seq if collected else since_seq,
    }


def terminate_impl(
    *,
    token: str,
    address: str,
    cascade: bool = True,
) -> dict[str, Any]:
    resolved = _resolve(token)
    if isinstance(resolved, dict):
        return resolved
    runtime, caller_addr = resolved

    target = _resolve_addr(address, runtime, caller_addr)
    if target is None:
        return _err("no_such_address", f"unknown address: {address}")

    # Permission: caller must be an ancestor (or the target itself).
    if not _is_descendant_or_self(runtime, ancestor=caller_addr, descendant=target):
        return _err(
            "not_permitted",
            f"caller cannot terminate non-descendant {address}",
        )

    terminated = runtime.terminate(target, requested_by=caller_addr.id, cascade=cascade)
    return {"ok": True, "terminated": [a.id for a in terminated]}


def introduce_impl(
    *,
    token: str,
    child: str,
    capability: str,
) -> dict[str, Any]:
    resolved = _resolve(token)
    if isinstance(resolved, dict):
        return resolved
    runtime, caller_addr = resolved

    child_addr = _resolve_addr(child, runtime, caller_addr)
    cap_addr = _resolve_addr(capability, runtime, caller_addr)
    if child_addr is None:
        return _err("no_such_address", f"unknown address: {child}")
    if cap_addr is None:
        return _err("no_such_address", f"unknown address: {capability}")

    if not _is_descendant_or_self(runtime, ancestor=caller_addr, descendant=child_addr):
        return _err("not_descendant", f"{child} is not a descendant of caller")

    caller_record = runtime.record_for(caller_addr)
    if cap_addr not in caller_record.capabilities:
        return _err("cap_missing", f"caller does not hold {capability}")

    runtime.record_for(child_addr).capabilities.extend(cap_addr)
    return {"ok": True}


def peek_impl(
    *,
    token: str,
    address: str,
    max_envelopes: int = 5,
) -> dict[str, Any]:
    """Snapshot a descendant agent's status + recent inbox. Returns
    structured data the LLM can dispatch on; the caller must be an
    ancestor of the target (or the target itself)."""
    resolved = _resolve(token)
    if isinstance(resolved, dict):
        return resolved
    runtime, caller_addr = resolved

    target = _resolve_addr(address, runtime, caller_addr)
    if target is None:
        return _err("no_such_address", f"unknown address: {address}")
    if not _is_descendant_or_self(runtime, ancestor=caller_addr, descendant=target):
        return _err(
            "not_permitted",
            f"caller cannot peek non-descendant {address}",
        )

    record = runtime.record_for(target)
    cap = max(1, int(max_envelopes))
    recent = record.inbox.read_recent(max_n=cap)
    return {
        "ok": True,
        "address": target.id,
        "label": target.label or None,
        "status": record.status,
        "depth": record.depth,
        "parent": record.parent.id if record.parent else None,
        "children": sorted(c.id for c in record.children),
        "inbox_size": len(record.inbox),
        "recent_envelopes": [
            {
                "seq": e.seq,
                "from": e.from_.id,
                "from_label": e.from_.label or None,
                "thread_id": e.thread_id,
                "body": e.body,
            }
            for e in recent
        ],
    }


def call_impl(
    *,
    token: str,
    spec: dict[str, Any] | None,
    body: Any,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """Synchronous request/reply: spawn a oneshot worker, hand it
    ``body``, wait for its single reply, return it. Atomic ``Spawn``
    + ``Send`` + ``WaitFor`` + cleanup as one tool call — the natural
    "evaluate worker(input)" shape that most one-off agent invocations
    reduce to.

    Mechanics: a private lazy collector is spawned alongside the
    worker; the worker receives the envelope from the collector (so
    its ``"caller"`` shortcut resolves to the collector); a Call-
    specific role-prompt suffix tells the worker to reply with
    ``Send(to="caller", body=...)``; the reply lands in the
    collector's inbox; we drain and return it. Both worker and
    collector are torn down before we return."""
    from combinator.combinators import _collect, _dispatch, _spawn_collector

    resolved = _resolve(token)
    if isinstance(resolved, dict):
        return resolved
    runtime, caller_addr = resolved

    template = spec or {}
    role_prompt = (template.get("role_prompt") or "") + _CALL_WORKER_SUFFIX
    base_spec = AgentSpec(
        role_prompt=role_prompt,
        label=template.get("label") or "call-worker",
        engine=template.get("engine", "auto"),
        tools=list(template.get("tools") or []),
        llm=template.get("llm", "default"),
        model=template.get("model"),
        oneshot=True,
    )

    try:
        collector = _spawn_collector(
            runtime, caller_addr, label="call-collector"
        )
    except MaxDepthExceeded as e:
        return _err("depth_exceeded", str(e))

    worker_spec = base_spec.model_copy(
        update={"capabilities": list(base_spec.capabilities) + [collector]}
    )

    worker: Address | None = None
    try:
        try:
            worker = runtime._spawn(parent=caller_addr, spec=worker_spec)
        except MaxDepthExceeded as e:
            return _err("depth_exceeded", str(e))
        # Dispatch with ``sender=collector`` so the worker's ``"caller"``
        # shortcut routes its reply back into the collector's inbox
        # (clean private channel) rather than the caller's inbox.
        _dispatch(
            runtime=runtime,
            sender=collector,
            recipient=worker,
            body=body,
        )
        try:
            [reply] = _collect(
                runtime=runtime,
                collector=collector,
                expected_senders=[worker],
                timeout_s=float(timeout_s),
            )
        except Timeout as e:
            return {
                "ok": False,
                "code": "timeout",
                "error": str(e),
                "worker": worker.id,
            }
        return {"ok": True, "result": reply, "worker": worker.id}
    finally:
        # ``oneshot=True`` auto-terminates the worker once it replies,
        # but on timeout (or a worker that errored before replying)
        # we may need to clean up explicitly. ``requested_by="oneshot"``
        # suppresses the supervision envelope — the caller already has
        # the reply (or the timeout result), so a tail ``child_event
        # terminated`` adds no signal and just wakes the caller again.
        if worker is not None:
            try:
                runtime.terminate(
                    worker, requested_by="oneshot", cascade=True
                )
            except Exception:
                pass
        try:
            runtime.terminate(collector, requested_by="oneshot")
        except Exception:
            pass


def list_inbox_impl(
    *,
    token: str,
    since_seq: int = 0,
    max_n: int = 50,
) -> dict[str, Any]:
    resolved = _resolve(token)
    if isinstance(resolved, dict):
        return resolved
    runtime, caller_addr = resolved

    record = runtime.record_for(caller_addr)
    envelopes = record.inbox.read(since_seq=since_seq, max_n=max_n, timeout_s=0.0)
    return {
        "ok": True,
        "envelopes": [e.model_dump(by_alias=True) for e in envelopes],
        "total": len(record.inbox),
    }


def _is_descendant_or_self(
    runtime: "Runtime",
    *,
    ancestor: Address,
    descendant: Address,
) -> bool:
    if ancestor == descendant:
        return True
    cur = runtime.record_for(descendant).parent
    while cur is not None:
        if cur == ancestor:
            return True
        cur = runtime.record_for(cur).parent
    return False


# ---------- Tool classes ----------

class SpawnTool(StatelessRuntimeTool):
    """Spawn a child agent.

    Returns ``{"ok": True, "address": str, "label": str}`` on success.
    Error codes: ``cap_violation`` (caller lacks a requested
    capability), ``no_such_address`` (capability id unknown),
    ``terminated`` (caller is terminated).
    """

    role_prompt: str = RuntimeField(
        description="Role prompt for the new child agent. Required.",
    )
    label: str = RuntimeField(
        default="",
        description="Human-readable hint for the child's address.",
    )
    tools: list[str] | None = RuntimeField(
        description="Names of tools the child should be granted.",
    )
    llm: str = RuntimeField(
        default="default",
        description="Named LLM client for the child (from runtime config).",
    )
    capabilities: list[str] | None = RuntimeField(
        description="Address ids to hand the child as capabilities.",
    )
    initial_message: str = RuntimeField(
        default="",
        description="Optional first message sent to the child after spawn.",
    )
    lazy: bool = RuntimeField(
        default=False,
        description="If true, do not start the child's driver until first inbox arrival.",
    )
    engine: str = RuntimeField(
        default="auto",
        description=(
            "Engine for the child agent: ``auto`` (default — picks "
            "``claude_agent`` if the SDK + ``claude`` CLI are present, "
            "else ``orchestral``), or pin to ``orchestral`` / "
            "``claude_agent`` explicitly."
        ),
    )
    sandbox_dir: str | None = RuntimeField(
        default=None,
        description=(
            "Filesystem sandbox path for the child. None auto-allocates "
            "under the runtime's store_dir. Required for any agent that "
            "should use the filesystem tool group."
        ),
    )
    permissions: dict[str, str] | None = RuntimeField(
        default=None,
        description=(
            "Per-tool permission decisions for the child, e.g. "
            "``{'Bash': 'ask', 'Write': 'allow'}``."
        ),
    )
    model: str | None = RuntimeField(
        default=None,
        description=(
            "Model for the child's claude_agent session — alias "
            "(``haiku``, ``sonnet``, ``opus``) or full name "
            "(``claude-sonnet-4-6``). Omit to default to a cheap "
            "model (``haiku``) for the child; explicitly set "
            "``sonnet`` or ``opus`` only when the task genuinely "
            "needs more capability. Ignored by the orchestral engine."
        ),
    )
    oneshot: bool = RuntimeField(
        default=False,
        description=(
            "If true, the child auto-terminates after its first "
            "successful step. Use for fire-and-forget fan-out workers "
            "so you don't have to chase cleanup; the runtime tears "
            "the child down (and cascades to its descendants) as soon "
            "as its turn returns cleanly. An errored turn leaves the "
            "child in ``status=\"error\"`` for inspection / retry."
        ),
    )
    runtime_token: str = StateField(
        description="(internal) runtime token identifying the calling agent.",
    )

    def _run(self) -> dict[str, Any]:
        return spawn_impl(
            token=self.runtime_token,
            role_prompt=self.role_prompt,
            label=self.label or "",
            tools=self.tools or [],
            llm=self.llm or "default",
            capabilities=self.capabilities or [],
            initial_message=self.initial_message or "",
            lazy=bool(self.lazy),
            engine=self.engine or "auto",
            sandbox_dir=self.sandbox_dir,
            permissions=self.permissions,
            model=self.model,
            oneshot=bool(self.oneshot),
        )


class SendTool(StatelessRuntimeTool):
    """Send a message to an address you hold a capability for.

    Error codes: ``not_permitted`` (no capability), ``no_such_address``,
    ``terminated``.
    """

    to: str = RuntimeField(description="Address id of the recipient.")
    body: Any = RuntimeField(description="Message body (any JSON-serializable value).")
    thread_id: str = RuntimeField(default="", description="Conversation thread id; defaults to msg id.")
    in_reply_to: str = RuntimeField(default="", description="Message id this is a reply to.")
    kind: str = RuntimeField(default="msg", description="Free-form message kind tag (e.g. 'result').")
    runtime_token: str = StateField(description="(internal) caller token.")

    def _run(self) -> dict[str, Any]:
        return send_impl(
            token=self.runtime_token,
            to=self.to,
            body=self.body,
            thread_id=self.thread_id or "",
            in_reply_to=self.in_reply_to or "",
            kind=self.kind or "msg",
        )


class RecvTool(StatelessRuntimeTool):
    """Read messages from your own inbox.

    Non-blocking when ``timeout_s`` is 0. Returns
    ``{"ok": True, "envelopes": [...], "next_seq": int}``.
    """

    thread_id: str = RuntimeField(default="", description="Filter by thread id.")
    from_: str = RuntimeField(default="", description="Filter by sender address id.")
    since_seq: int = RuntimeField(default=0, description="Return envelopes with seq > since_seq.")
    max_n: int = RuntimeField(default=1, description="Maximum number of envelopes to return.")
    timeout_s: float = RuntimeField(default=0.0, description="Seconds to block if no match (0 = non-blocking).")
    runtime_token: str = StateField(description="(internal) caller token.")

    def _run(self) -> dict[str, Any]:
        return recv_impl(
            token=self.runtime_token,
            thread_id=self.thread_id or "",
            from_=self.from_ or "",
            since_seq=int(self.since_seq or 0),
            max_n=int(self.max_n or 1),
            timeout_s=float(self.timeout_s or 0.0),
        )


class WaitForTool(StatelessRuntimeTool):
    """Block until ``max_n`` matching envelopes have arrived in your
    inbox, or the timeout fires — whichever comes first. Always
    returns whatever was collected (may be fewer than ``max_n`` if
    the deadline elapsed; the agent can detect a short read by
    comparing ``len(envelopes)`` to ``max_n``). Use this for fan-in
    when you're expecting N replies from N workers."""

    predicate_kind: str = RuntimeField(default="any", description="One of 'thread', 'from', 'any'.")
    value: str = RuntimeField(default="", description="Predicate value (thread_id or sender id).")
    since_seq: int = RuntimeField(default=0, description="Return envelopes with seq > since_seq.")
    timeout_s: float = RuntimeField(default=30.0, description="Maximum seconds to block before returning what's accumulated.")
    max_n: int = RuntimeField(default=1024, description="Target number of matches to collect before returning (deadline still wins).")
    runtime_token: str = StateField(description="(internal) caller token.")

    def _run(self) -> dict[str, Any]:
        return wait_for_impl(
            token=self.runtime_token,
            predicate_kind=self.predicate_kind or "any",
            value=self.value or "",
            since_seq=int(self.since_seq or 0),
            timeout_s=float(self.timeout_s or 30.0),
            max_n=int(self.max_n or 1024),
        )


class TerminateTool(StatelessRuntimeTool):
    """Terminate a descendant agent (or yourself).

    Error codes: ``not_permitted`` (target is not a descendant),
    ``no_such_address``.
    """

    address: str = RuntimeField(description="Address id of the agent to terminate.")
    cascade: bool = RuntimeField(default=True, description="If true, also terminate descendants.")
    runtime_token: str = StateField(description="(internal) caller token.")

    def _run(self) -> dict[str, Any]:
        return terminate_impl(
            token=self.runtime_token,
            address=self.address,
            cascade=bool(self.cascade if self.cascade is not None else True),
        )


class IntroduceTool(StatelessRuntimeTool):
    """Hand a capability you hold to a descendant.

    Error codes: ``not_descendant`` (target is not under caller),
    ``cap_missing`` (caller doesn't hold the capability),
    ``no_such_address``.
    """

    child: str = RuntimeField(description="Address id of the descendant receiving the capability.")
    capability: str = RuntimeField(description="Address id being granted.")
    runtime_token: str = StateField(description="(internal) caller token.")

    def _run(self) -> dict[str, Any]:
        return introduce_impl(
            token=self.runtime_token,
            child=self.child,
            capability=self.capability,
        )


class ListInboxTool(StatelessRuntimeTool):
    """Return a summary of envelopes in your own inbox without
    consuming them (non-blocking)."""

    since_seq: int = RuntimeField(default=0, description="Return envelopes with seq > since_seq.")
    max_n: int = RuntimeField(default=50, description="Maximum envelopes to return.")
    runtime_token: str = StateField(description="(internal) caller token.")

    def _run(self) -> dict[str, Any]:
        return list_inbox_impl(
            token=self.runtime_token,
            since_seq=int(self.since_seq or 0),
            max_n=int(self.max_n or 50),
        )


class CallTool(StatelessRuntimeTool):
    """Synchronous request/reply against a one-shot worker. The
    simplest possible shape for "evaluate worker(body) and return the
    reply" — atomic Spawn + Send + WaitFor + cleanup as one tool
    call. Use this for the bulk of fan-out work; reach for the
    primitives (Spawn / Send / WaitFor) only when you need to keep a
    worker alive across multiple turns, dispatch heterogeneous specs,
    or stream replies as they arrive.

    The worker is spawned with ``oneshot=True`` and a Call-specific
    role-prompt suffix telling it to reply with ``Send(to="caller",
    body=...)``. The reply is the value the worker's send call
    carried; it is returned to you under the ``result`` key.

    Error codes: ``timeout`` (worker didn't reply in time;
    ``worker`` field carries the dead worker's address for
    inspection), ``depth_exceeded`` (you're already at max_depth).
    """

    spec: dict = RuntimeField(
        description=(
            "Worker spec template — same shape as the combinator "
            "tools' ``spec``: ``role_prompt`` (required), plus "
            "optional ``label``, ``tools``, ``engine``, ``llm``, "
            "``model``. The runtime adds ``oneshot=True`` and a "
            "reply-with-Send suffix to the role_prompt."
        ),
    )
    body: Any = RuntimeField(
        description=(
            "Message body delivered to the worker as-is. Any JSON-"
            "serializable value — string, dict, list, etc. The "
            "worker sees the exact shape you pass."
        ),
    )
    timeout_s: float = RuntimeField(
        default=60.0,
        description=(
            "Maximum seconds to wait for the worker's reply before "
            "terminating it and returning ``code=timeout``."
        ),
    )
    runtime_token: str = StateField(description="(internal) caller token.")

    def _run(self) -> dict[str, Any]:
        return call_impl(
            token=self.runtime_token,
            spec=self.spec or {},
            body=self.body,
            timeout_s=float(self.timeout_s or 60.0),
        )


class PeekTool(StatelessRuntimeTool):
    """Snapshot a descendant agent's status + recent inbox without
    consuming any messages. Use this to diagnose stalled fan-ins
    (``which worker is stuck?``), check progress on long-running
    children, or confirm a spawn landed before sending it work.

    Authority: caller must be an ancestor of the target (or the
    target itself). ``address`` accepts ids and the label / `self` /
    `parent` shortcuts.
    """

    address: str = RuntimeField(
        description="Address id, label of a direct child, or 'self' / 'parent'.",
    )
    max_envelopes: int = RuntimeField(
        default=5,
        description="How many recent inbox envelopes to include in the snapshot.",
    )
    runtime_token: str = StateField(description="(internal) caller token.")

    def _run(self) -> dict[str, Any]:
        return peek_impl(
            token=self.runtime_token,
            address=self.address,
            max_envelopes=int(self.max_envelopes or 5),
        )


PRIMITIVE_TOOL_CLASSES = (
    SpawnTool,
    SendTool,
    RecvTool,
    WaitForTool,
    TerminateTool,
    IntroduceTool,
    ListInboxTool,
    PeekTool,
    CallTool,
)


def build_primitive_tools(token: str) -> list[StatelessRuntimeTool]:
    """Instantiate the full primitive-tool set bound to ``token``."""
    return [cls(runtime_token=token) for cls in PRIMITIVE_TOOL_CLASSES]
