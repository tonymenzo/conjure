"""Runtime — the central coordinator of a conjure session.

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

import asyncio
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

import uuid
from dataclasses import dataclass, field

from conjure.address import SYSTEM, USER, Address
from conjure.capability import CapabilitySet
from conjure.envelope import Envelope
from conjure.errors import (
    BudgetExceeded,
    ConjureError,
    MaxDepthExceeded,
    NoSuchAddress,
    NotPermitted,
    SandboxViolation,
    Terminated,
    ToolkitDenied,
)
from conjure.ids import new_agent_id, new_message_id, new_runtime_token
from conjure.mailbox import Mailbox
from conjure.persistence import Journal
from conjure.record import AgentRecord, AgentSpec
from conjure.tools._base import register_token, unregister_token

if TYPE_CHECKING:
    from conjure.agent import Engine
    from conjure.models import ModelHub
    from conjure.toolbase import Toolset, ToolbaseHub

EngineFactory = Callable[[AgentRecord, "Runtime"], "Engine"]


# Shared default for how long an ``ask``-mode tool call blocks waiting
# for a UI decision. Used by the filesystem tool group and the
# claude_agent engine's ``can_use_tool`` callback so both surfaces
# behave the same.
PERMISSION_WAIT_S: float = 300.0

# Longest a single blocking wait (``WaitFor``, ``Recv``) may block before
# control returns to the agent, whatever ``timeout_s`` it asked for. A
# wait that long without news is better spent checking on the worker
# (``Peek``) than blocking on. ``runtime.max_wait_s`` overrides it.
DEFAULT_MAX_WAIT_S: float = 1800.0

# Longest tool result handed back to a model, in characters
# (``runtime.tool_output_max_chars``); see ``conjure.tool_output``.
DEFAULT_TOOL_OUTPUT_MAX_CHARS: int = 40_000


@dataclass
class PermissionRequest:
    """One pending tool-permission decision from an ``ask``-mode tool.

    The tool worker blocks on ``wait()``; the UI (via the control
    server) calls ``resolve()`` which sets the underlying Event and
    unblocks the tool. ``timeout`` from the wait side returns the
    sentinel ``"timeout"`` so the tool can return a clear error
    instead of hanging forever.
    """

    req_id: str
    addr: Address
    tool_name: str
    args: dict[str, Any]
    ts: float = field(default_factory=time.time)
    _event: threading.Event = field(default_factory=threading.Event)
    _decision: str = ""  # "allow" | "deny" | "timeout"

    def wait(self, timeout_s: float) -> str:
        if self._event.wait(timeout=timeout_s):
            return self._decision or "timeout"
        return "timeout"

    def resolve(self, decision: str) -> None:
        if decision not in ("allow", "deny"):
            raise ValueError(
                f"decision must be 'allow' or 'deny'; got {decision!r}"
            )
        self._decision = decision
        self._event.set()


def _engine_cost(engine: Any) -> float:
    if engine is None:
        return 0.0
    fn = getattr(engine, "cost", None)
    if not callable(fn):
        return 0.0
    try:
        return float(fn())
    except Exception:
        return 0.0


def _new_async_loop() -> asyncio.AbstractEventLoop:
    """Construct the runtime's shared event loop.

    Uses ``uvloop`` (a hard dependency on POSIX) for 2-4× the throughput
    of stock asyncio on the SDK's IO-heavy workload. The
    ``CONJURE_UVLOOP=0`` escape hatch is preserved for diagnostics —
    pinning to stock asyncio is occasionally useful when bisecting
    suspected uvloop-specific behavior — but production runs always use
    uvloop. If the import unexpectedly fails (e.g. running under a
    broken wheel) we fall through to stock asyncio rather than crash
    the runtime."""
    if os.environ.get("CONJURE_UVLOOP", "1") != "0":
        try:
            import uvloop  # type: ignore[import-not-found]

            return uvloop.new_event_loop()
        except ImportError:
            pass
    return asyncio.new_event_loop()


class Runtime:

    def __init__(
        self,
        *,
        store_dir: Path | None = None,
        session_id: str | None = None,
        engine_factory: EngineFactory | None = None,
        max_workers: int = 32,
        max_depth: int = 3,
        spawn_listener: Callable[[AgentRecord], None] | None = None,
        toolbase: "ToolbaseHub | None" = None,
        models: "ModelHub | None" = None,
        deadline: float | None = None,
        max_wait_s: float | None = DEFAULT_MAX_WAIT_S,
        tool_output_max_chars: int | None = DEFAULT_TOOL_OUTPUT_MAX_CHARS,
    ) -> None:
        self._lock = threading.RLock()
        # Run deadline (absolute epoch seconds) set by an embedding
        # harness with a wall clock. Blocking waits are clipped to the
        # time left so an agent never blocks past the end of the run.
        # ``None``: no deadline. Settable after construction.
        self.deadline: float | None = deadline
        # Cap on one blocking wait (see ``DEFAULT_MAX_WAIT_S``); ``None``
        # or 0 means uncapped.
        self.max_wait_s: float | None = max_wait_s
        # Cap on one tool result returned to a model (``conjure.tool_output``);
        # ``None`` or 0 means uncapped.
        self.tool_output_max_chars: int | None = tool_output_max_chars
        # Model catalog + policy (``conjure.models``): resolves each
        # spec's model choice to engine / LLM / provider model and
        # refuses what the session doesn't allow. ``None``: specs pass
        # through as written.
        self.models = models
        # Toolbase hub (``conjure.toolbase``): catalog, root authority,
        # server factory. ``None`` means toolbase isn't configured and
        # any spec asking for toolkits is refused.
        self.toolbase = toolbase
        self._records: dict[Address, AgentRecord] = {}
        self._tokens: dict[str, Address] = {}
        # Reverse index: address id string → Address. ``address_by_id``
        # is called on every ``_resolve_addr`` (every Send, every Spawn
        # capability check, every Terminate, every Introduce). Without
        # this index it's an O(N) scan of ``_records``, which becomes
        # O(N²) work as a fan-out HOF unwinds. Updated wherever
        # ``_records`` is written so the two never drift.
        self._addr_index: dict[str, Address] = {}
        self._root_addr: Address | None = None
        # Per-session scoping: every runtime gets its own subdir under
        # ``store_dir/sessions/{session_id}/`` so journals from
        # different ``conjure run`` invocations don't concatenate
        # into one ambiguous file. When the caller doesn't supply a
        # session_id but does supply a store_dir (tests, REPL), we
        # auto-mint one so the file-per-runtime invariant always holds.
        self._store_dir = store_dir
        if store_dir is not None:
            self._session_id = session_id or f"auto-{uuid.uuid4().hex[:8]}"
            self._session_dir: Path | None = store_dir / "sessions" / self._session_id
        else:
            self._session_id = session_id  # may be None — journal will no-op anyway
            self._session_dir = None
        self._journal = Journal(self._session_dir)
        self._shutdown = False
        self._max_workers = max_workers
        self._max_depth = max_depth
        self._engine_factory = engine_factory
        self._spawn_listener = spawn_listener
        # Pending tool-permission requests (``ask`` decisions). The
        # UI polls ``list_pending_permissions`` and calls
        # ``resolve_permission`` to satisfy them.
        self._permission_requests: dict[str, PermissionRequest] = {}
        self._permission_lock = threading.Lock()
        # Auto-mode: when True, every tool with an ``ask`` decision is
        # auto-allowed (no banner, no wait). Toggle from the UI via the
        # control RPC ``set_auto_mode``. Off by default — explicit
        # opt-in for the "trust me, just go" flow.
        self.auto_mode: bool = False
        # Per-agent session allow-list: when the user picks "Allow
        # always" on a permission prompt, the tool name lands here so
        # the engine's ``can_use_tool`` callback can short-circuit
        # future requests for that tool from that agent without
        # prompting again. Keyed by Address (not just label) so two
        # agents with the same label have independent allow-lists.
        # Lives in memory only — clears on ``conjure quit``.
        self._session_allow: dict[Address, set[str]] = {}
        # Shared asyncio loop + thread for engines that need a sync-
        # from-async bridge (currently ``ClaudeAgentEngine``). Started
        # lazily on first access; the runtime owns its lifetime so we
        # don't pay for N loops + N threads under fan-out. See
        # ``get_shared_async_loop``.
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._async_thread: threading.Thread | None = None
        self._async_lock = threading.Lock()
        # Fast-path flag: stays False until the first spec carrying a
        # ``budget`` is spawned. Drivers consult it before every step;
        # when no budgets exist anywhere (the common case) the check
        # is a single attribute read with no lock.
        self._budgets_in_use = False
        # Combinator collectors (``mark_collector``): a send to one is a
        # worker delivering its result.
        self._collectors: set[Address] = set()
        self._install_sentinels()

    @property
    def store_dir(self) -> Path | None:
        """Root of on-disk runtime state. Sandboxes live directly under
        here (shared across sessions); journals + per-agent event logs
        live under ``session_dir`` (one subdir per runtime)."""
        return self._store_dir

    @property
    def session_id(self) -> str | None:
        """Stable id for this runtime's session — used as the on-disk
        scope for journal + agent logs. ``None`` only when the runtime
        was built without persistence (``store_dir=None``)."""
        return self._session_id

    @property
    def session_dir(self) -> Path | None:
        """``store_dir/sessions/{session_id}/`` — the per-session
        subtree owning journal.jsonl and agents/{agent_id}.jsonl. The
        CLI uses this to place per-agent event logs alongside the
        journal so one session is self-contained on disk."""
        return self._session_dir

    # ----- Permission request queue (for ``ask``-mode tools) -----

    def submit_permission_request(
        self, *, addr: Address, tool_name: str, args: dict[str, Any]
    ) -> PermissionRequest:
        """Register a pending permission request. The tool calls
        ``wait()`` on the returned request; the UI calls
        ``resolve_permission(req_id, decision)`` to unblock it."""
        req = PermissionRequest(
            req_id=f"perm-{uuid.uuid4().hex[:8]}",
            addr=addr,
            tool_name=tool_name,
            args=args,
        )
        with self._permission_lock:
            self._permission_requests[req.req_id] = req
        return req

    def resolve_permission(
        self, req_id: str, decision: str, *, scope: str | None = None
    ) -> bool:
        """Resolve a pending request. Returns False if the request
        was never registered (or was already collected).

        When ``scope="session"`` and ``decision="allow"``, the
        tool name is added to the agent's session allow-list so
        future calls of the same tool from that agent are silently
        allowed. ``scope="once"`` (or ``None``) is the default.
        """
        with self._permission_lock:
            req = self._permission_requests.pop(req_id, None)
            if req is not None and decision == "allow" and scope == "session":
                self._session_allow.setdefault(req.addr, set()).add(
                    req.tool_name
                )
        if req is None:
            return False
        req.resolve(decision)
        return True

    def session_allow_contains(self, addr: Address, tool_name: str) -> bool:
        """True if the user has previously picked "Allow always" for
        ``tool_name`` on this agent. Engines short-circuit the perm
        prompt when this returns True."""
        with self._permission_lock:
            return tool_name in self._session_allow.get(addr, set())

    def list_pending_permissions(
        self, *, addr: Address | None = None
    ) -> list[PermissionRequest]:
        """Snapshot of currently-pending requests (optionally
        filtered to one agent)."""
        with self._permission_lock:
            reqs = list(self._permission_requests.values())
        if addr is not None:
            reqs = [r for r in reqs if r.addr == addr]
        reqs.sort(key=lambda r: r.ts)
        return reqs

    def _discard_permission(self, req_id: str) -> None:
        """Internal: drop a request from the queue without firing
        the event. Called by tools that gave up waiting (timeout)."""
        with self._permission_lock:
            self._permission_requests.pop(req_id, None)

    # ----- Shared asyncio loop -----

    def get_shared_async_loop(self) -> asyncio.AbstractEventLoop:
        """Return the runtime-owned asyncio loop, starting it lazily.

        Engines that need a sync-from-async bridge (e.g.
        ``ClaudeAgentEngine``) should submit coroutines here via
        ``asyncio.run_coroutine_threadsafe``. One loop on one thread
        serves every engine in the runtime — under fan-out, this is the
        difference between N event loops on N OS threads (one per child)
        and a single loop juggling N concurrent SDK clients.

        Uses ``uvloop`` when available unless ``CONJURE_UVLOOP=0``
        in the environment. uvloop's selector and timer paths are 2–4×
        faster than stock asyncio for the SDK's IO-heavy workload.
        """
        with self._async_lock:
            if self._async_loop is not None:
                return self._async_loop
            loop = _new_async_loop()
            self._async_loop = loop
            thread = threading.Thread(
                target=self._run_async_loop,
                name="conjure-async-loop",
                daemon=True,
            )
            self._async_thread = thread
            thread.start()
            return loop

    def _run_async_loop(self) -> None:
        loop = self._async_loop
        if loop is None:
            return
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            try:
                loop.close()
            except Exception:
                pass

    def _stop_shared_async_loop(self, *, join_timeout: float = 1.0) -> None:
        with self._async_lock:
            loop = self._async_loop
            thread = self._async_thread
            self._async_loop = None
            self._async_thread = None
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                # Loop was already closed; nothing to do.
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=join_timeout)

    @property
    def max_depth(self) -> int:
        """Configured ceiling on the spawn-tree depth (root = 0)."""
        return self._max_depth

    def _install_sentinels(self) -> None:
        """Register passive ``@user`` / ``@system`` records so agents can
        send to them via the normal capability machinery. These records
        carry an inbox but no engine or driver — messages just collect
        there until the CLI / runtime owner reads them out.
        """
        for sentinel in (USER, SYSTEM):
            self._records[sentinel] = AgentRecord(
                addr=sentinel,
                spec=AgentSpec(
                    role_prompt=f"(sentinel: {sentinel.label})",
                    label=sentinel.label,
                    lazy=True,
                ),
                inbox=Mailbox(),
                capabilities=CapabilitySet(self_addr=sentinel),
                token="",  # never used; sentinels can't be tool callers
                parent=None,
                status="idle",
            )
            self._addr_index[sentinel.id] = sentinel

    # ----- Public API -----

    def root(self, spec: AgentSpec) -> Address:
        """Spawn the root agent. May be called at most once per runtime.

        The root has no parent. Its capability set is its own address
        plus any addresses listed in ``spec.capabilities``.
        """
        if self.models is not None:
            spec = self.models.resolve_spec(spec, parent_engine=None)
        if spec.toolkits:
            hub = self._require_toolbase()
            hub.check_grant(dict(spec.toolkits), hub.root_authority())
        with self._lock:
            self._require_not_shutdown()
            if self._root_addr is not None:
                raise ConjureError("root agent already spawned")
            addr = self._mint_address(spec.label)
            record = self._build_record(addr=addr, parent=None, spec=spec)
            self._records[addr] = record
            self._addr_index[addr.id] = addr
            self._tokens[record.token] = addr
            self._root_addr = addr
            if spec.budget is not None:
                self._budgets_in_use = True
            register_token(record.token, self, addr)
            self._journal_spawn(record)
        # Notify externally-registered listener (e.g. the tmux
        # orchestrator) so it can set up per-agent state (event log,
        # window) BEFORE the driver starts emitting events.
        try:
            if self._spawn_listener is not None:
                self._spawn_listener(record)
            self._maybe_start_driver(record)
        except BaseException:
            self._cleanup_failed_spawn([record])
            raise
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
            stored = target_record.inbox.put(env, before_publish=self._journal_send)
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
        self._interrupt_engines(terminated)
        return terminated

    def shutdown(self, *, driver_join_timeout: float = 2.0) -> None:
        """Stop the runtime. All non-terminated agents become
        ``terminated``; drivers are signaled and joined; the journal
        is closed.
        """
        # Record each agent's usage while the journal is still open: a run
        # stopped mid-turn (a harness timeout, an agent blocked in WaitFor)
        # never reaches the turn end where the driver records it, and
        # its cost would be lost.
        if not self._shutdown:
            for record in list(self._records.values()):
                note = getattr(record.driver, "note_meta", None)
                if callable(note):
                    try:
                        note()
                    except Exception:
                        pass
        with self._lock:
            if self._shutdown:
                return
            drivers_to_stop: list[Any] = []
            engines_to_shutdown: list[Any] = []
            for record in self._records.values():
                if record.status != "terminated":
                    record.status = "terminated"
                if record.wakeup is not None:
                    record.wakeup.set()
                if record.driver is not None:
                    drivers_to_stop.append(record.driver)
                engine = self._engine_for(record)
                if engine is not None:
                    engines_to_shutdown.append(engine)
                unregister_token(record.token)
            self._journal.close()
            self._shutdown = True
        for d in drivers_to_stop:
            d.stop(timeout=driver_join_timeout)
        # Engines share the runtime-owned async loop; let each one
        # disconnect cleanly before we stop the loop out from under
        # them. Best-effort — a hung engine shouldn't block shutdown.
        for engine in engines_to_shutdown:
            fn = getattr(engine, "shutdown", None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass
        self._stop_shared_async_loop()

    @property
    def root_addr(self) -> Address | None:
        return self._root_addr

    # ----- Run deadline / blocking waits -----

    def remaining_s(self) -> float | None:
        """Seconds left before ``deadline`` (0 once it has passed), or
        ``None`` when the run has no deadline."""
        if self.deadline is None:
            return None
        return max(0.0, float(self.deadline) - time.time())

    def clip_wait(
        self, timeout_s: float, *, single_wait: bool = True
    ) -> tuple[float, str | None]:
        """Clip a blocking wait to what the run allows. Returns the
        timeout to use and, when it was shortened, why (for the agent).

        ``single_wait`` applies ``max_wait_s`` too (message waits:
        ``WaitFor``, ``Recv``); waits whose timeout ends work in flight
        (a combinator's gather, a shell command) take only the deadline."""
        t = max(0.0, float(timeout_s or 0.0))
        reason: str | None = None
        cap = self.max_wait_s
        if single_wait and cap and t > cap:
            t, reason = float(cap), f"the longest single wait is {cap:g}s (runtime.max_wait_s)"
        left = self.remaining_s()
        if left is not None and t > left:
            t = left
            reason = (
                "the run's deadline has passed" if left <= 0
                else f"the run's deadline is {left:.0f}s away"
            )
        return t, reason

    def clip_to_deadline(self, timeout_s: float) -> float:
        """``timeout_s`` clipped to the time left before the deadline."""
        return self.clip_wait(timeout_s, single_wait=False)[0]

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

    def address_by_id(self, id_str: str) -> Address | None:
        """Look up a known Address by its opaque id string. Used by
        tools to resolve LLM-supplied address strings.

        O(1) via the maintained ``_addr_index``. Note that terminated
        agents stay in the index — capability checks and tool error
        codes still need to resolve their ids so an attempted ``Send``
        to a dead address returns ``code=terminated`` rather than
        ``code=no_such_address``."""
        return self._addr_index.get(id_str)

    def total_cost(self) -> float:
        """Sum of every agent's engine cost (USD). Engines that don't
        track cost contribute zero."""
        total = 0.0
        with self._lock:
            for record in self._records.values():
                engine = self._engine_for(record)
                total += _engine_cost(engine)
        return total

    def costs_by_agent(self) -> list[tuple[Address, float]]:
        """Per-agent costs (USD), in spawn order."""
        out: list[tuple[Address, float]] = []
        with self._lock:
            for addr, record in self._records.items():
                engine = self._engine_for(record)
                out.append((addr, _engine_cost(engine)))
        return out

    @staticmethod
    def _engine_for(record: AgentRecord) -> Any:
        agent_wrapper = record.agent
        return getattr(agent_wrapper, "engine", None) if agent_wrapper else None

    # ----- Budget ceilings -----

    def subtree_cost(self, addr: Address) -> float:
        """Total engine cost (USD) of ``addr`` plus every descendant."""
        with self._lock:
            return self._subtree_cost_locked(addr)

    def _subtree_cost_locked(self, addr: Address) -> float:
        record = self._records.get(addr)
        if record is None:
            return 0.0
        total = _engine_cost(self._engine_for(record))
        for child in record.children:
            total += self._subtree_cost_locked(child)
        return total

    def budget_exceeded(self, addr: Address) -> str | None:
        """Return the id of the nearest exhausted budget holder on
        ``addr``'s ancestor chain (including ``addr`` itself), or
        ``None`` when every budget on the chain still has headroom.

        A budget caps its holder's whole subtree, so an agent is
        blocked the moment *any* ancestor's ceiling is spent — a
        child's own (larger) budget can't buy headroom an ancestor
        doesn't have. ``engine.cost()`` is a cached float on every
        engine, so the walk is cheap; drivers additionally skip the
        call entirely while no budgets exist (``_budgets_in_use``).
        """
        if not self._budgets_in_use:
            return None
        with self._lock:
            node: Address | None = addr
            while node is not None:
                record = self._records.get(node)
                if record is None:
                    return None
                ceiling = record.spec.budget
                if ceiling is not None and self._subtree_cost_locked(node) >= ceiling:
                    return node.id
                node = record.parent
        return None

    def wait_for_idle(
        self,
        addr: Address,
        target_seq: int,
        *,
        timeout_s: float = 180.0,
    ) -> bool:
        """Block until ``addr``'s driver has consumed everything up to
        ``target_seq`` and the agent's status is ``idle`` (or it has
        been terminated). Returns True if reached, False on timeout.

        Used by the CLI to know when an agent finishes processing the
        message the user just sent.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self._lock:
                record = self._records.get(addr)
                if record is None or record.status == "terminated":
                    return True
                driver = record.driver
                cursor = getattr(driver, "_cursor", 0) if driver is not None else 0
                if cursor >= target_seq and record.status == "idle":
                    return True
            time.sleep(0.02)
        return False

    def settled(self, addr: Address) -> int | None:
        """``addr``'s driver cursor when it and all its descendants have
        ended their turns with nothing unread (no one in that subtree acts
        again until messaged); ``None`` while any of them may still act.
        A terminated agent counts as settled at cursor ``-1``."""
        with self._lock:
            record = self._records.get(addr)
            if record is None or record.status == "terminated":
                return -1
            driver = record.driver
            if driver is None or getattr(driver, "busy", True) or record.status not in ("idle", "error"):
                return None
            cursor = getattr(driver, "_cursor", 0)
            if cursor < record.inbox.latest_seq():
                return None
            if any(self.settled(child) is None for child in record.children):
                return None
            return cursor

    # ----- Internal spawn (used by the spawn tool) -----

    def _spawn(self, *, parent: Address, spec: AgentSpec) -> Address:
        """Create a child agent under ``parent``. Capability enforcement
        is the caller's responsibility (the spawn tool checks); this
        method is the trusted construction path.

        The parent automatically gains capability for the new child, so
        ``spawn`` followed by ``send(child, ...)`` works without an
        explicit ``introduce`` step.

        Raises ``MaxDepthExceeded`` if the resulting child would sit
        deeper than the runtime's ``max_depth``.
        """
        return self._spawn_batch(parent=parent, specs=[spec])[0]

    def _spawn_batch(
        self, *, parent: Address, specs: list[AgentSpec]
    ) -> list[Address]:
        """Spawn N children under ``parent`` in one critical section.

        Registry mutations (record allocation, token registration,
        parent-side child / capability updates, journal writes) happen
        under a single lock acquisition; the spawn listener and driver
        startup run outside the lock so engine construction for one
        child can't block the registry for the others.

        Order is preserved: the returned address list lines up with
        ``specs``. Validation fails before registry mutation. Startup failure
        terminates the entire batch, retaining its records for inspection.
        """
        if not specs:
            return []
        # A relative sandbox_dir means "inside my sandbox", and a child
        # may not escape its parent's sandbox. Before any registry
        # mutation, so a refused spawn leaves nothing behind.
        specs = [self._resolve_child_sandbox(parent, s) for s in specs]
        if self.models is not None:
            # Model choice → engine / LLM / provider model, checked
            # against the session's policy; before any registry mutation.
            from conjure.engines import effective_engine

            parent_engine = effective_engine(self.record_for(parent), self)
            specs = [self.models.resolve_spec(s, parent_engine=parent_engine) for s in specs]
        # Toolkit attenuation, before any registry mutation so a refused
        # grant leaves no half-spawned batch. Outside the lock: the
        # first check may read toolbase's cache from disk.
        if any(s.toolkits or s.toolbase_loadout for s in specs):
            self._check_toolkit_specs(parent, specs)
        # Engine-specific checks (e.g. orchestral tool-group names), also
        # before any registry mutation: a spec the engine would refuse must
        # not leave a record or a journaled spawn behind (a benchmark that
        # counts spawns from the journal would count the refused one).
        validate = getattr(self._engine_factory, "validate_spec", None)
        if validate is not None:
            for s in specs:
                validate(s, parent)
        records: list[AgentRecord] = []
        addrs: list[Address] = []
        with self._lock:
            self._require_not_shutdown()
            self._require_alive(parent)
            parent_record = self._records[parent]
            child_depth = parent_record.depth + 1
            if child_depth > self._max_depth:
                raise MaxDepthExceeded(
                    f"spawn would create depth {child_depth} but "
                    f"max_depth is {self._max_depth}"
                )
            exhausted_at = self.budget_exceeded(parent)
            if exhausted_at is not None:
                raise BudgetExceeded(
                    f"budget exhausted at {exhausted_at}; spawn refused"
                )
            for spec in specs:
                if spec.budget is not None:
                    self._budgets_in_use = True
                addr = self._mint_address(spec.label)
                record = self._build_record(addr=addr, parent=parent, spec=spec)
                record.depth = child_depth
                self._records[addr] = record
                self._addr_index[addr.id] = addr
                self._tokens[record.token] = addr
                parent_record.children.add(addr)
                parent_record.capabilities.extend(addr)
                register_token(record.token, self, addr)
                self._journal_spawn(record)
                records.append(record)
                addrs.append(addr)
        # Engine construction and listener notification happen outside
        # the lock — one slow child's SDK setup must not block the
        # others' registry visibility (or any other lock contender).
        listener = self._spawn_listener
        try:
            for record in records:
                if listener is not None:
                    listener(record)
                self._maybe_start_driver(record)
        except BaseException:
            # Keep terminated records for inspection/replay, but revoke their
            # tokens and stop every member of the failed batch, including those
            # whose engines started successfully before a sibling failed.
            self._cleanup_failed_spawn(records)
            raise
        return addrs

    def sandbox_of(self, addr: Address) -> Path | None:
        """Where ``addr``'s sandbox is (absolute, not created), the same
        resolution the filesystem tools use: ``spec.sandbox_dir``, else
        ``{store_dir}/sandboxes/{id}``. ``None`` when neither applies."""
        record = self.record_for(addr)
        if record.spec.sandbox_dir:
            return Path(record.spec.sandbox_dir).expanduser().resolve()
        if self._store_dir is None:
            return None
        return (Path(self._store_dir).expanduser() / "sandboxes" / addr.id).resolve()

    def _resolve_child_sandbox(self, parent: Address, spec: AgentSpec) -> AgentSpec:
        """Make a child's ``sandbox_dir`` absolute and contained.

        No ``sandbox_dir`` shares the parent's sandbox, so the parent's
        relative paths work unchanged for the child (an empty sandbox of
        its own left a delegated worker unable to find any input). A
        relative path is resolved against the parent's sandbox, not
        the process's working directory. A path outside the parent's
        sandbox raises ``SandboxViolation``. A parent without a sandbox
        (no store and none set) leaves the spec as written."""
        raw = spec.sandbox_dir
        base = self.sandbox_of(parent)
        if base is None:
            return spec
        if not raw:
            return spec.model_copy(update={"sandbox_dir": str(base)})
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = base / path
        path = path.resolve()
        try:
            path.relative_to(base)
        except ValueError:
            raise SandboxViolation(
                f"sandbox_dir {raw!r} resolves to {path}, outside your sandbox "
                f"{base}; give a path inside it (relative paths are resolved "
                f"against it, e.g. 'work/worker')"
            ) from None
        if str(path) == raw:
            return spec
        return spec.model_copy(update={"sandbox_dir": str(path)})

    def _cleanup_failed_spawn(self, records: list[AgentRecord]) -> None:
        terminated: list[Address] = []
        with self._lock:
            for record in records:
                self._terminate_locked(
                    record.addr, requested_by="spawn-failed", cascade=True,
                    terminated=terminated, emit_events=False,
                )
        self._interrupt_engines(terminated)
        for addr in terminated:
            record = self.record_for(addr)
            driver = record.driver
            if driver is not None:
                driver.stop(timeout=2.0)
                if not driver.join(timeout=0):
                    # Do not close resources under an in-flight turn.
                    continue
            engine = self._engine_for(record)
            shutdown = getattr(engine, "shutdown", None)
            if callable(shutdown):
                try:
                    shutdown()
                except Exception:
                    pass
            if record.event_log is not None:
                record.event_log.close()

    def dispatch_batch(
        self,
        deliveries: list[tuple[Address, Address, Any]],
    ) -> list[Envelope]:
        """Deliver N messages in one critical section.

        ``deliveries`` is a list of ``(sender, recipient, body)``
        tuples. The runtime lock is held just long enough to resolve
        every recipient's record; the actual mailbox puts and wakeups
        happen unlocked so per-mailbox condition locks don't contend
        with the registry. Returns the stored envelopes in the input
        order.

        Privileged: callers bypass the per-tool capability check (this
        is the framework path used by combinators / HOFs).
        """
        if not deliveries:
            return []
        # Resolve all recipient records up front under a single lock
        # acquisition. Looking them up one at a time per dispatch was
        # N runtime-lock grabs even though each lookup is an O(1) dict
        # read — under fan-out, the contention cost dominated the
        # actual work.
        plans: list[tuple[Any, Address, Address, Any]] = []
        with self._lock:
            self._require_not_shutdown()
            for sender, recipient, body in deliveries:
                record = self._records.get(recipient)
                if record is None:
                    continue
                plans.append((record, sender, recipient, body))
        stored: list[Envelope] = []
        for record, sender, recipient, body in plans:
            msg_id = new_message_id()
            env = Envelope(
                seq=0,
                msg_id=msg_id,
                from_=sender,
                to=recipient,
                thread_id=msg_id,
                body=body,
                ts=time.time(),
            )
            placed = record.inbox.put(env, before_publish=self._journal_send)
            wakeup = record.wakeup
            if wakeup is not None:
                wakeup.set()
            stored.append(placed)
        return stored

    # ----- Toolkits -----

    def _require_toolbase(self) -> "ToolbaseHub":
        if self.toolbase is None:
            raise ToolkitDenied(
                "toolbase is not configured for this runtime (install "
                "conjure-agents[toolbase] and add a `toolbase:` section)"
            )
        return self.toolbase

    def _check_toolkit_specs(
        self, parent: Address, specs: list[AgentSpec]
    ) -> None:
        hub = self._require_toolbase()
        authority = hub.authority_of(self, parent)
        for spec in specs:
            if spec.toolbase_loadout:
                # A named loadout is the user's curation; only the root
                # (whose authority the user configured) may hand one out.
                if self.record_for(parent).parent is not None:
                    raise ToolkitDenied(
                        "only the root may grant a named toolbase loadout; "
                        "pass `toolkits` instead"
                    )
            hub.check_grant(hub.toolset_of(spec), authority)

    def grant_toolkits(
        self, *, granter: Address, target: Address, toolkits: "Toolset"
    ) -> "Toolset":
        """Add ``toolkits`` to a live descendant's toolset.

        The granter must be an ancestor of ``target`` and hold every
        grant itself. Grants are additive — a child's authority only
        widens, within its ancestor's — so the subset invariant holds
        down the whole tree without touching grandchildren. The
        target's engine picks the change up at its next step.
        Returns the target's new toolset.
        """
        from conjure.toolbase import merge_toolsets

        hub = self._require_toolbase()
        hub.check_grant(toolkits, hub.authority_of(self, granter))
        with self._lock:
            self._require_alive(target)
            record = self._records[target]
            if granter == target:
                # Only the root serves itself: its authority (from the
                # config) is wider than what it's served (root.toolkits).
                # Anyone else already calls everything it may grant.
                if record.parent is not None:
                    raise NotPermitted("only the root may grant toolkits to itself")
            else:
                cur = record.parent
                while cur is not None and cur != granter:
                    cur = self._records[cur].parent
                if cur is None:
                    raise NotPermitted(
                        f"{granter.id} is not an ancestor of {target.id}"
                    )
            merged = merge_toolsets(record.spec.toolkits, toolkits)
            record.spec = record.spec.model_copy(update={"toolkits": merged})
            self._journal.write(
                "toolkits",
                {
                    "addr": target.model_dump(),
                    "granted_by": granter.model_dump(),
                    "toolkits": {k: v.model_dump() for k, v in merged.items()},
                },
            )
        return merged

    def _replay_toolkits(self, payload: dict[str, Any]) -> None:
        addr = Address.model_validate(payload["addr"])
        record = self._records.get(addr)
        if record is None:
            return
        record.spec = record.spec.model_copy(
            update={
                "toolkits": AgentSpec.model_validate(
                    {"role_prompt": "", "toolkits": payload.get("toolkits")}
                ).toolkits
            }
        )

    # ----- Replay -----

    @classmethod
    def replay(
        cls, session_dir: Path, *, until: float | None = None
    ) -> "Runtime":
        """Reconstruct a runtime from a persisted journal.

        ``session_dir`` is the per-session directory containing
        ``journal.jsonl`` — typically ``store_dir/sessions/{session_id}/``.
        Pass the directory itself, not the store root.

        ``until`` (a journal timestamp) stops the replay at that
        instant, reconstructing the session as it stood then — the web
        UI's time scrubber. Replayed agents carry their journaled spawn
        time in ``spawned_at``.

        The returned runtime is *not* attached to any journal — replay
        is intended for inspection and offline analysis, not for
        seamless resume. Driver threads are not started.
        """
        rt = cls(store_dir=None)
        for entry in Journal.read_all(session_dir):
            kind = entry["kind"]
            payload = entry["payload"]
            ts = entry.get("ts")
            if until is not None and isinstance(ts, (int, float)) and ts > until:
                break
            if kind == "spawn":
                rt._replay_spawn(payload)
                if isinstance(ts, (int, float)):
                    addr = Address.model_validate(payload["addr"])
                    rt._records[addr].spawned_at = float(ts)
            elif kind == "send":
                rt._replay_send(payload)
            elif kind == "terminate":
                rt._replay_terminate(payload)
            elif kind == "toolkits":
                rt._replay_toolkits(payload)
            elif kind == "agent_meta":
                addr = rt._addr_index.get(payload.get("addr", ""))
                if addr is not None:
                    rt._records[addr].meta.update(payload.get("meta") or {})
        return rt

    @staticmethod
    def list_sessions(store_dir: Path) -> list[str]:
        """Enumerate session ids that have a journal under ``store_dir``.

        Returned in lexicographic order (which, for the
        ``conjure-<hex>`` and ``auto-<hex>`` naming used at
        construction time, has no temporal meaning — sort by mtime if
        you need recency).
        """
        sessions_root = store_dir / "sessions"
        if not sessions_root.is_dir():
            return []
        out: list[str] = []
        for child in sorted(sessions_root.iterdir()):
            if child.is_dir() and (child / "journal.jsonl").exists():
                out.append(child.name)
        return out

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
        return AgentRecord(
            addr=addr,
            spec=spec,
            inbox=Mailbox(),
            capabilities=self._initial_caps(addr=addr, parent=parent, spec=spec),
            token=new_runtime_token(),
            parent=parent,
        )

    def _initial_caps(
        self,
        *,
        addr: Address,
        parent: Address | None,
        spec: AgentSpec,
    ) -> CapabilitySet:
        # USER + SYSTEM are universal — every agent can reach them.
        initial: list[Address] = [USER, SYSTEM]
        if parent is not None:
            initial.append(parent)
        initial.extend(spec.capabilities)
        return CapabilitySet(self_addr=addr, initial=initial)

    def _terminate_locked(
        self,
        addr: Address,
        *,
        requested_by: str,
        cascade: bool,
        terminated: list[Address],
        emit_events: bool = True,
    ) -> None:
        if addr not in self._records:
            raise NoSuchAddress(str(addr))
        record = self._records[addr]
        if record.status == "terminated":
            return
        record.status = "terminated"
        terminated.append(addr)
        unregister_token(record.token)
        if record.wakeup is not None:
            record.wakeup.set()
        # Supervision: tell the (live) parent its child just went away.
        # When termination cascades from an ancestor, the parent here
        # is already terminated and the call is a no-op — no spurious
        # spam during teardown. ``oneshot`` terminations are expected
        # (the parent already collected the worker's reply right
        # before the auto-terminate fired) — surfacing them as
        # supervision envelopes floods the inbox at the tail of a
        # fan-out without adding signal. ``emit_events=False`` is the
        # batch path's escape hatch — it coalesces N per-child events
        # into one ``batch_terminated`` envelope per affected parent.
        if emit_events and requested_by != "oneshot":
            self._notify_parent_of_child_event(
                record,
                event="terminated",
                reason=f"requested_by={requested_by}",
            )
        if cascade:
            for child in list(record.children):
                self._terminate_locked(
                    child,
                    requested_by=requested_by,
                    cascade=True,
                    terminated=terminated,
                    emit_events=emit_events,
                )
        self._journal.write(
            "terminate",
            {
                "addr": addr.model_dump(),
                "requested_by": requested_by,
                "cascade": cascade,
            },
        )

    # ----- Batch terminate (coalesced supervision events) -----

    def terminate_batch(
        self,
        addrs: list[Address],
        *,
        requested_by: str = "user",
        cascade: bool = True,
    ) -> list[Address]:
        """Terminate N addresses in one critical section, emitting at
        most one ``batch_terminated`` supervision envelope per affected
        parent rather than N per-child events.

        Use this for HOF cleanup paths (``AgentMap`` finally, race
        losers, expired hedges) where a parent collects results from
        many workers and would otherwise see its inbox flooded by N
        ``child_event terminated`` envelopes at the tail of the
        fan-out. The single coalesced envelope carries every
        affected child id, so a supervisor can still react to the
        mass cleanup if it wants to.

        Returns the flat list of every address actually terminated by
        this call (cascade-aware, in DFS order).
        """
        if not addrs:
            return []
        with self._lock:
            self._require_not_shutdown()
            terminated: list[Address] = []
            for addr in addrs:
                if addr not in self._records:
                    continue
                self._terminate_locked(
                    addr,
                    requested_by=requested_by,
                    cascade=cascade,
                    terminated=terminated,
                    emit_events=False,
                )
            # Group the terminated set by their (still-live) parent and
            # emit one consolidated envelope per parent. Each batch
            # envelope carries the full child list so a watching parent
            # has the same information as N separate events.
            if requested_by != "oneshot":
                self._emit_batch_terminated(
                    terminated, requested_by=requested_by
                )
        self._interrupt_engines(terminated)
        return terminated

    def mark_collector(self, addr: Address) -> None:
        """Register ``addr`` as a combinator collector (see
        ``note_delivery``)."""
        with self._lock:
            self._collectors.add(addr)

    def note_delivery(self, sender: Address, target: Address) -> None:
        """Record that ``sender`` just sent to ``target``; a send to a
        collector marks the sender as having delivered its result."""
        if target in self._collectors:
            record = self._records.get(sender)
            if record is not None:
                record.delivered = True

    def _interrupt_engines(self, addrs: list[Address]) -> None:
        """Best-effort: ask each terminated agent's engine to abort
        its in-flight LLM call so termination takes effect now instead
        of after the current step drains. Engines opt in by exposing
        ``interrupt()`` (``ClaudeAgentEngine`` does; ``interrupt`` is
        fire-and-forget there). Runs outside the registry lock — an
        engine's interrupt path must never block spawns/sends."""
        for addr in addrs:
            with self._lock:
                record = self._records.get(addr)
            if record is None:
                continue
            # A delivered agent's result is already in: let its turn wind
            # down rather than abort a reply mid-acknowledgement.
            if not record.delivered:
                engine = self._engine_for(record)
                fn = getattr(engine, "interrupt", None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:
                        pass
            # Either way its shell commands (and whatever they started)
            # stop; the engine is told first, so its loop can't start
            # another round in between.
            if record.processes:
                from conjure.tools.filesystem import stop_process_group

                for proc in list(record.processes):
                    threading.Thread(
                        target=stop_process_group, args=(proc,), daemon=True,
                        name=f"stop-{addr.id}",
                    ).start()

    def _emit_batch_terminated(
        self,
        terminated: list[Address],
        *,
        requested_by: str,
    ) -> None:
        if not terminated:
            return
        by_parent: dict[Address, list[Address]] = {}
        for addr in terminated:
            record = self._records.get(addr)
            if record is None or record.parent is None:
                continue
            by_parent.setdefault(record.parent, []).append(addr)
        for parent_addr, children_addrs in by_parent.items():
            parent_record = self._records.get(parent_addr)
            if parent_record is None or parent_record.status == "terminated":
                continue
            children_payload = [
                {"addr": ca.id, "label": ca.label or None}
                for ca in children_addrs
            ]
            body: dict[str, Any] = {
                "kind": "child_event",
                "event": "batch_terminated",
                "children": children_payload,
                "count": len(children_payload),
                "reason": f"requested_by={requested_by}",
            }
            msg_id = new_message_id()
            env = Envelope(
                seq=0,
                msg_id=msg_id,
                from_=SYSTEM,
                to=parent_addr,
                thread_id=msg_id,
                body=body,
                ts=time.time(),
            )
            stored = parent_record.inbox.put(env, before_publish=self._journal_send)
            wakeup = parent_record.wakeup
            if wakeup is not None:
                wakeup.set()

    # ----- Supervision (parent gets notified of child lifecycle events) -----

    def notify_child_errored(
        self,
        addr: Address,
        reason: str,
    ) -> None:
        """Public entrypoint for the driver to report a child engine
        failure to the parent. Lock-safe (RLock); no-op if the parent
        is gone or the runtime is shutting down. Also a no-op when the
        child itself is already terminated — an interrupt-induced
        engine error is the expected tail of a terminate, and the
        parent already received the ``terminated`` event."""
        with self._lock:
            record = self._records.get(addr)
            if record is None or record.status == "terminated":
                return
            self._notify_parent_of_child_event(
                record, event="errored", reason=reason
            )

    def notify_budget_exceeded(self, addr: Address, exhausted_at: str) -> None:
        """Driver entrypoint: tell ``addr``'s parent that the agent's
        step was skipped because a budget on its chain is exhausted."""
        with self._lock:
            record = self._records.get(addr)
            if record is None or record.status == "terminated":
                return
            self._notify_parent_of_child_event(
                record,
                event="budget_exceeded",
                reason=f"budget exhausted at {exhausted_at}",
            )

    def _notify_parent_of_child_event(
        self,
        child_record: AgentRecord,
        *,
        event: str,
        reason: str | None = None,
    ) -> None:
        """Inject a ``@system → parent`` envelope describing a child's
        lifecycle transition (``terminated`` or ``errored``). The
        envelope body is structured so the LLM can dispatch on it:

        ``{"kind": "child_event", "event": ..., "child_addr": ...,
        "child_label": ..., "reason": ...}``

        Skipped when the parent doesn't exist (root), is itself
        terminated (cascade), or the runtime is shutting down."""
        parent_addr = child_record.parent
        if parent_addr is None:
            return
        if self._shutdown:
            return
        parent_record = self._records.get(parent_addr)
        if parent_record is None or parent_record.status == "terminated":
            return
        body: dict[str, Any] = {
            "kind": "child_event",
            "event": event,
            "child_addr": child_record.addr.id,
            "child_label": child_record.addr.label or None,
        }
        if reason is not None:
            body["reason"] = str(reason)
        msg_id = new_message_id()
        env = Envelope(
            seq=0,
            msg_id=msg_id,
            from_=SYSTEM,
            to=parent_addr,
            thread_id=msg_id,
            body=body,
            ts=time.time(),
        )
        stored = parent_record.inbox.put(env, before_publish=self._journal_send)
        wakeup = parent_record.wakeup
        if wakeup is not None:
            wakeup.set()

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
            raise ConjureError("runtime has been shut down")

    def _journal_spawn(self, record: AgentRecord) -> None:
        self._journal.write(
            "spawn",
            {
                "addr": record.addr.model_dump(),
                "parent": record.parent.model_dump() if record.parent else None,
                "spec": record.spec.model_dump(),
            },
        )

    def note_agent_meta(self, addr: Address, meta: dict[str, Any]) -> None:
        """Record what ``addr`` runs as (engine, model, system prompt,
        ...). Journals only the keys that changed, so a session's
        journal says which model each agent ran on and when that became
        known."""
        record = self._records.get(addr)
        if record is None:
            return
        changed = {k: v for k, v in meta.items() if v is not None and record.meta.get(k) != v}
        if not changed:
            return
        record.meta.update(changed)
        try:
            self._journal.write("agent_meta", {"addr": addr.id, "meta": changed})
        except Exception:
            pass

    def _journal_send(self, env: Envelope) -> None:
        self._journal.write("send", {"envelope": env.model_dump(by_alias=True)})

    def _maybe_start_driver(self, record: AgentRecord) -> None:
        """Build an engine and start a driver for ``record`` unless its
        spec marks it ``lazy`` or no engine factory is configured."""
        if self._engine_factory is None or record.spec.lazy:
            if record.wakeup is None:
                record.wakeup = threading.Event()
            return
        from conjure.agent import Agent as AgentWrapper
        from conjure.driver import Driver

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
        record = AgentRecord(
            addr=addr,
            spec=spec,
            inbox=Mailbox(),
            capabilities=self._initial_caps(addr=addr, parent=parent, spec=spec),
            token=new_runtime_token(),
            parent=parent,
        )
        self._records[addr] = record
        self._addr_index[addr.id] = addr
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
