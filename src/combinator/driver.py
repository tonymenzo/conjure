"""Per-agent driver thread.

The driver waits on its agent's ``wakeup`` event. When triggered — by a
new message landing in the inbox or by termination — it drains unread
messages, formats them into a prompt, and calls ``agent.step(prompt)``.

The driver maintains its own cursor (``_cursor``) over the inbox so it
delivers each message to the engine exactly once. The agent's own
``recv`` tool (Step 6) uses a separate per-tool-call cursor; the two are
independent on purpose — the driver is the *push* path, ``recv`` is the
*pull* path.

Termination semantics: the runtime sets the record's status to
``terminated`` and fires the wakeup. The driver wakes, sees the status,
and exits. Any in-flight ``agent.step`` finishes first — v0.1 does not
interrupt mid-LLM-call work.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from combinator.envelope import Envelope

logger = logging.getLogger(__name__)


class Driver:

    def __init__(self, *, agent: Any, runtime: Any) -> None:
        # ``agent`` is combinator.agent.Agent; ``runtime`` is
        # combinator.runtime.Runtime. Typed as Any to avoid a circular
        # import — Driver is constructed from Runtime which imports
        # this module, so neither side can import the other at module
        # load.
        self.agent = agent
        self.runtime = runtime
        self._cursor = 0
        self._stop_requested = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        record = self.agent.record
        if record.wakeup is None:
            record.wakeup = threading.Event()
        self._thread = threading.Thread(
            target=self._loop,
            name=f"driver-{record.addr.id}",
            daemon=True,
        )
        record.status = "idle"
        self._thread.start()

    def stop(self, *, timeout: float | None = 2.0) -> None:
        self._stop_requested = True
        record = self.agent.record
        if record.wakeup is not None:
            record.wakeup.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def join(self, timeout: float | None = None) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    # ----- internals -----

    def _loop(self) -> None:
        record = self.agent.record
        while not self._stop_requested:
            record.wakeup.wait()
            record.wakeup.clear()
            if self._stop_requested or record.status == "terminated":
                return
            envelopes = record.inbox.read(
                since_seq=self._cursor,
                max_n=1024,
                timeout_s=0.0,
            )
            if not envelopes:
                continue
            self._cursor = envelopes[-1].seq
            # Power the ``"caller"`` address shortcut — tools that
            # resolve ``to="caller"`` look up the agent's
            # ``last_received_from``. Most-recent envelope wins when
            # several arrive in one tick.
            record.last_received_from = envelopes[-1].from_
            record.status = "running"
            prompt = self._build_prompt(envelopes)
            errored = False
            engine_exc: Exception | None = None
            try:
                self.agent.step(prompt)
            except Exception as exc:
                # Surface to the agent's event log so the chat pane
                # shows the failure. Without this the user just sees
                # the agent stop replying.
                errored = True
                engine_exc = exc
                self._emit_engine_error(exc)
                logger.exception(
                    "engine raised in driver for %s", record.addr
                )
            finally:
                if record.status != "terminated":
                    # Sticky ``error`` between turns so the tree's red
                    # dot persists until the agent successfully
                    # processes its next message.
                    record.status = "error" if errored else "idle"
            # Supervision: notify the parent (if any) that we errored
            # so it can react without polling. Outside the ``finally``
            # because we want it ordered after the status flip — the
            # parent's handler may inspect the child's status.
            if engine_exc is not None:
                try:
                    self.runtime.notify_child_errored(
                        record.addr,
                        f"{type(engine_exc).__name__}: {engine_exc}",
                    )
                except Exception:
                    pass
            # Oneshot lifecycle: after a successful step, auto-
            # terminate so fire-and-forget workers clean themselves
            # up. The runtime cascade kills any descendants the
            # worker spawned. An errored turn does NOT auto-terminate
            # — leaves the agent in ``status="error"`` for parental
            # inspection / retry.
            if (
                record.spec.oneshot
                and not errored
                and record.status != "terminated"
            ):
                try:
                    self.runtime.terminate(
                        record.addr,
                        requested_by="oneshot",
                        cascade=True,
                    )
                except Exception:
                    pass

    def _emit_engine_error(self, exc: Exception) -> None:
        event_log = getattr(self.agent.record, "event_log", None)
        if event_log is None:
            return
        try:
            event_log.emit(
                {"kind": "error", "text": f"{type(exc).__name__}: {exc}"}
            )
        except Exception:
            pass

    def _build_prompt(self, envelopes: list[Envelope]) -> str:
        lines = [f"You have {len(envelopes)} new message(s):"]
        for env in envelopes:
            lines.append(
                f"  [seq={env.seq} thread={env.thread_id} from={env.from_.id}]: "
                f"{env.body!r}"
            )
        from_agents = [e for e in envelopes if not e.from_.id.startswith("@")]
        from_user = [e for e in envelopes if e.from_.id == "@user"]
        # Tailored reminder so the agent doesn't mis-route the reply.
        # The system frame has the full protocol; this is the kick-in-
        # the-moment reminder right before its tool decision.
        if from_agents:
            sender_ids = ", ".join(sorted({e.from_.id for e in from_agents}))
            lines.append(
                f"\nREPLY REMINDER — {len(from_agents)} of these message(s) "
                f"are from other agents ({sender_ids}). Inter-agent replies "
                f"are NOT delivered by your final assistant text. If this "
                f"message asks a question or hands you a task, ``send(to="
                f"\"<sender-addr>\", body=...)`` with the answer. If this "
                f"message is itself a REPLY to something you already asked "
                f"for (a result, a status, an acknowledgement), DO NOT send "
                f"another message back — that starts a politeness loop. "
                f"Just produce your final assistant text (which only the "
                f"@user sees) or end the turn with no tool calls at all. "
                f"If you spawn a child to help, remember to ``send`` the "
                f"child its task too — spawn alone is a no-op."
            )
        elif from_user:
            lines.append(
                "\nReply with your final assistant text — the UI delivers it "
                "to the human. If you ``spawn`` a child to help, you MUST "
                "also ``send(to=\"<child-addr>\", body=...)`` so the child "
                "knows what to do; spawn alone is a no-op."
            )
        else:
            lines.append("\nProcess the message(s) as appropriate.")
        return "\n".join(lines)
