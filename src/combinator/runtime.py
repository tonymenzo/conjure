"""Runtime — the central coordinator of a combinator session.

Owns the spawn tree, the agent registry, and the persistence journal.
The driver loop and orchestral.Agent wiring land in Step 5; this module
keeps the core lifecycle and capability machinery so it can be unit-
tested in isolation.

Concurrency model: a single ``threading.RLock`` guards all registry
mutations. Inbox writes go through ``Mailbox`` which has its own
condition variable; the runtime lock is *not* held during a mailbox
read's blocking wait.

Capability enforcement lives in the tool layer (Step 6). Internal
methods (``_spawn``) bypass capability checks deliberately — they are
the trusted entrypoint that tool wrappers call after verifying the
caller's capabilities.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from combinator.address import SYSTEM, USER, Address
from combinator.capability import CapabilitySet
from combinator.envelope import Envelope
from combinator.errors import CombinatorError, NoSuchAddress, Terminated
from combinator.ids import new_agent_id, new_message_id, new_runtime_token
from combinator.mailbox import Mailbox
from combinator.persistence import Journal
from combinator.record import AgentRecord, AgentSpec

if TYPE_CHECKING:
    from combinator.agent import Engine

EngineFactory = Callable[[AgentRecord, "Runtime"], "Engine"]


class Runtime:

    def __init__(
        self,
        *,
        store_dir: Path | None = None,
        engine_factory: EngineFactory | None = None,
        max_workers: int = 32,
    ) -> None:
        self._lock = threading.RLock()
        self._records: dict[Address, AgentRecord] = {}
        self._tokens: dict[str, Address] = {}
        self._root_addr: Address | None = None
        self._journal = Journal(store_dir)
        self._shutdown = False
        self._max_workers = max_workers
        self._engine_factory = engine_factory

    # ----- Public API -----

    def root(self, spec: AgentSpec) -> Address:
        """Spawn the root agent. May be called at most once per runtime.

        The root has no parent. Its capability set is its own address
        plus any addresses listed in ``spec.capabilities``.
        """
        with self._lock:
            self._require_not_shutdown()
            if self._root_addr is not None:
                raise CombinatorError("root agent already spawned")
            addr = self._mint_address(spec.label)
            record = self._build_record(addr=addr, parent=None, spec=spec)
            self._records[addr] = record
            self._tokens[record.token] = addr
            self._root_addr = addr
            self._journal_spawn(record)
        self._maybe_start_driver(record)
        return addr

    def send_external(self, to: Address, body: Any, *, sender: str = "user") -> str:
        """Inject a message into ``to``'s inbox from outside the agent
        graph. ``sender`` is ``"user"`` (default) or ``"system"``.
        Returns the new message id.
        """
        with self._lock:
            self._require_not_shutdown()
            self._require_alive(to)
            sender_addr = USER if sender == "user" else SYSTEM
            msg_id = new_message_id()
            env = Envelope(
                seq=0,
                msg_id=msg_id,
                from_=sender_addr,
                to=to,
                thread_id=msg_id,
                body=body,
                ts=time.time(),
            )
            target_record = self._records[to]
            stored = target_record.inbox.put(env)
            self._journal_send(stored)
            wakeup = target_record.wakeup
        if wakeup is not None:
            wakeup.set()
        return stored.msg_id

    def read_inbox(self, addr: Address, *, since_seq: int = 0) -> list[Envelope]:
        with self._lock:
            self._require_known(addr)
            return self._records[addr].inbox.read(
                since_seq=since_seq, max_n=10_000
            )

    def terminate(
        self,
        addr: Address,
        *,
        requested_by: str = "user",
        cascade: bool = True,
    ) -> list[Address]:
        """Terminate ``addr``. If ``cascade`` is true (default), all
        living descendants are terminated as well. Returns the list of
        addresses terminated by this call.
        """
        with self._lock:
            self._require_not_shutdown()
            terminated: list[Address] = []
            self._terminate_locked(
                addr,
                requested_by=requested_by,
                cascade=cascade,
                terminated=terminated,
            )
            return terminated

    def shutdown(self, *, driver_join_timeout: float = 2.0) -> None:
        """Stop the runtime. All non-terminated agents become
        ``terminated``; drivers are signaled and joined; the journal
        is closed.
        """
        with self._lock:
            if self._shutdown:
                return
            drivers_to_stop: list[Any] = []
            for record in self._records.values():
                if record.status != "terminated":
                    record.status = "terminated"
                if record.wakeup is not None:
                    record.wakeup.set()
                if record.driver is not None:
                    drivers_to_stop.append(record.driver)
            self._journal.close()
            self._shutdown = True
        for d in drivers_to_stop:
            d.stop(timeout=driver_join_timeout)

    @property
    def root_addr(self) -> Address | None:
        return self._root_addr

    # ----- Identity / authentication -----

    def resolve_token(self, token: str) -> Address:
        """Resolve a runtime token to its agent address, or raise
        ``NotPermitted``-style behavior via NoSuchAddress."""
        with self._lock:
            if token not in self._tokens:
                raise NoSuchAddress("unknown runtime token")
            return self._tokens[token]

    def record_for(self, addr: Address) -> AgentRecord:
        with self._lock:
            self._require_known(addr)
            return self._records[addr]

    # ----- Internal spawn (used by the spawn tool) -----

    def _spawn(self, *, parent: Address, spec: AgentSpec) -> Address:
        """Create a child agent under ``parent``. Capability enforcement
        is the caller's responsibility (the spawn tool checks); this
        method is the trusted construction path."""
        with self._lock:
            self._require_not_shutdown()
            self._require_alive(parent)
            addr = self._mint_address(spec.label)
            record = self._build_record(addr=addr, parent=parent, spec=spec)
            self._records[addr] = record
            self._tokens[record.token] = addr
            self._records[parent].children.add(addr)
            self._journal_spawn(record)
        self._maybe_start_driver(record)
        return addr

    # ----- Replay -----

    @classmethod
    def replay(cls, store_dir: Path) -> "Runtime":
        """Reconstruct a runtime from a persisted journal.

        The returned runtime is *not* attached to the same journal —
        replay is intended for inspection and offline analysis, not for
        seamless resume. Driver threads are not started.
        """
        rt = cls(store_dir=None)
        for entry in Journal.read_all(store_dir):
            kind = entry["kind"]
            payload = entry["payload"]
            if kind == "spawn":
                rt._replay_spawn(payload)
            elif kind == "send":
                rt._replay_send(payload)
            elif kind == "terminate":
                rt._replay_terminate(payload)
        return rt

    # ----- Internals -----

    def _mint_address(self, label: str) -> Address:
        return Address(id=new_agent_id(), label=label)

    def _build_record(
        self,
        *,
        addr: Address,
        parent: Address | None,
        spec: AgentSpec,
    ) -> AgentRecord:
        initial: list[Address] = []
        if parent is not None:
            initial.append(parent)
        initial.extend(spec.capabilities)
        capabilities = CapabilitySet(self_addr=addr, initial=initial)
        return AgentRecord(
            addr=addr,
            spec=spec,
            inbox=Mailbox(),
            capabilities=capabilities,
            token=new_runtime_token(),
            parent=parent,
        )

    def _terminate_locked(
        self,
        addr: Address,
        *,
        requested_by: str,
        cascade: bool,
        terminated: list[Address],
    ) -> None:
        if addr not in self._records:
            raise NoSuchAddress(str(addr))
        record = self._records[addr]
        if record.status == "terminated":
            return
        record.status = "terminated"
        terminated.append(addr)
        if record.wakeup is not None:
            record.wakeup.set()
        if cascade:
            for child in list(record.children):
                self._terminate_locked(
                    child,
                    requested_by=requested_by,
                    cascade=True,
                    terminated=terminated,
                )
        self._journal.write(
            "terminate",
            {
                "addr": addr.model_dump(),
                "requested_by": requested_by,
                "cascade": cascade,
            },
        )

    def _require_alive(self, addr: Address) -> None:
        if addr not in self._records:
            raise NoSuchAddress(str(addr))
        if self._records[addr].status == "terminated":
            raise Terminated(str(addr))

    def _require_known(self, addr: Address) -> None:
        if addr not in self._records:
            raise NoSuchAddress(str(addr))

    def _require_not_shutdown(self) -> None:
        if self._shutdown:
            raise CombinatorError("runtime has been shut down")

    def _journal_spawn(self, record: AgentRecord) -> None:
        self._journal.write(
            "spawn",
            {
                "addr": record.addr.model_dump(),
                "parent": record.parent.model_dump() if record.parent else None,
                "spec": record.spec.model_dump(),
            },
        )

    def _journal_send(self, env: Envelope) -> None:
        self._journal.write("send", {"envelope": env.model_dump(by_alias=True)})

    def _maybe_start_driver(self, record: AgentRecord) -> None:
        """Build an engine and start a driver for ``record`` unless its
        spec marks it ``lazy`` or no engine factory is configured."""
        if self._engine_factory is None or record.spec.lazy:
            if record.wakeup is None:
                record.wakeup = threading.Event()
            return
        from combinator.agent import Agent as AgentWrapper
        from combinator.driver import Driver

        record.wakeup = threading.Event()
        engine = self._engine_factory(record, self)
        wrapper = AgentWrapper(record=record, engine=engine)
        driver = Driver(agent=wrapper, runtime=self)
        record.agent = wrapper
        record.driver = driver
        driver.start()

    def _replay_spawn(self, payload: dict[str, Any]) -> None:
        addr = Address.model_validate(payload["addr"])
        parent_data = payload.get("parent")
        parent = Address.model_validate(parent_data) if parent_data else None
        spec = AgentSpec.model_validate(payload["spec"])
        initial: list[Address] = []
        if parent is not None:
            initial.append(parent)
        initial.extend(spec.capabilities)
        capabilities = CapabilitySet(self_addr=addr, initial=initial)
        record = AgentRecord(
            addr=addr,
            spec=spec,
            inbox=Mailbox(),
            capabilities=capabilities,
            token=new_runtime_token(),
            parent=parent,
        )
        self._records[addr] = record
        self._tokens[record.token] = addr
        if parent is not None and parent in self._records:
            self._records[parent].children.add(addr)
        else:
            self._root_addr = addr

    def _replay_send(self, payload: dict[str, Any]) -> None:
        env = Envelope.model_validate(payload["envelope"])
        if env.to not in self._records:
            return
        self._records[env.to].inbox.replay_put(env)

    def _replay_terminate(self, payload: dict[str, Any]) -> None:
        addr = Address.model_validate(payload["addr"])
        if addr in self._records:
            self._records[addr].status = "terminated"
