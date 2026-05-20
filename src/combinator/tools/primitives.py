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
from combinator.errors import NoSuchAddress, Terminated
from combinator.ids import new_message_id
from combinator.record import AgentSpec
from combinator.tools._base import (
    RuntimeField,
    StateField,
    StatelessRuntimeTool,
    resolve_token,
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
        cap_addr = _addr_from_str(cap_id, runtime)
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
        tools=list(tools or []),
        llm=llm,
        capabilities=cap_addrs,
        initial_message=initial_message or None,
        lazy=lazy,
    )

    try:
        child_addr = runtime._spawn(parent=caller_addr, spec=spec)
    except Terminated as e:
        return _err("terminated", str(e))
    except NoSuchAddress as e:
        return _err("no_such_address", str(e))

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

    target_addr = _addr_from_str(to, runtime)
    if target_addr is None:
        return _err("no_such_address", f"unknown address: {to}")

    caller_record = runtime.record_for(caller_addr)
    if target_addr not in caller_record.capabilities:
        return _err("not_permitted", f"caller cannot send to {to}")

    target_record = runtime.record_for(target_addr)
    if target_record.status == "terminated":
        return _err("terminated", f"target {to} is terminated")

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
    if target_record.wakeup is not None:
        target_record.wakeup.set()

    return {"ok": True, "msg_id": stored.msg_id, "seq": stored.seq}


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
    if predicate_kind == "thread":
        return recv_impl(
            token=token, thread_id=value, since_seq=since_seq,
            max_n=max_n, timeout_s=timeout_s,
        )
    if predicate_kind == "from":
        return recv_impl(
            token=token, from_=value, since_seq=since_seq,
            max_n=max_n, timeout_s=timeout_s,
        )
    return recv_impl(
        token=token, since_seq=since_seq, max_n=max_n, timeout_s=timeout_s,
    )


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

    target = _addr_from_str(address, runtime)
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

    child_addr = _addr_from_str(child, runtime)
    cap_addr = _addr_from_str(capability, runtime)
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
    """Block until a matching envelope arrives in your inbox, or
    timeout. Returns up to ``max_n`` matches once one is available."""

    predicate_kind: str = RuntimeField(default="any", description="One of 'thread', 'from', 'any'.")
    value: str = RuntimeField(default="", description="Predicate value (thread_id or sender id).")
    since_seq: int = RuntimeField(default=0, description="Return envelopes with seq > since_seq.")
    timeout_s: float = RuntimeField(default=30.0, description="Seconds to block before giving up.")
    max_n: int = RuntimeField(default=1024, description="Maximum envelopes to return once unblocked.")
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


PRIMITIVE_TOOL_CLASSES = (
    SpawnTool,
    SendTool,
    RecvTool,
    WaitForTool,
    TerminateTool,
    IntroduceTool,
    ListInboxTool,
)


def build_primitive_tools(token: str) -> list[StatelessRuntimeTool]:
    """Instantiate the full primitive-tool set bound to ``token``."""
    return [cls(runtime_token=token) for cls in PRIMITIVE_TOOL_CLASSES]
