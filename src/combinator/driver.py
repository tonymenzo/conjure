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
            record.status = "running"
            prompt = self._build_prompt(envelopes)
            try:
                self.agent.step(prompt)
            except Exception:
                logger.exception(
                    "engine raised in driver for %s", record.addr
                )
            finally:
                if record.status != "terminated":
                    record.status = "idle"

    def _build_prompt(self, envelopes: list[Envelope]) -> str:
        lines = [f"You have {len(envelopes)} new message(s):"]
        for env in envelopes:
            lines.append(
                f"  [seq={env.seq} thread={env.thread_id} from={env.from_}]: "
                f"{env.body!r}"
            )
        lines.append(
            "\nProcess them using your tools (send, recv, spawn, terminate, "
            "introduce) as appropriate."
        )
        return "\n".join(lines)
