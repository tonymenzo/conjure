"""Gateway-friendly LLM calls: a concurrency cap and retries.

Gateways limit callers: a model access gateway caps parallel requests
per user (``429 max_parallel_requests``), and proxies cut long streams
(``peer closed connection without sending complete message body``). A
conjure fan-out hits both. Without help, either error ends the agent's
whole turn.

``make_resilient(llm, ...)`` wraps one orchestral LLM client in place:

- **Concurrency cap.** Calls share a semaphore keyed by the ``llms:``
  entry (``max_concurrency``), so every agent on that gateway together
  stays under its limit. A slot is held only while a request or stream
  is in flight, never while tools run or an agent waits on its inbox,
  so a parent waiting for its children can't starve them.
- **Retries.** Transient failures (connection drops, timeouts, 429,
  5xx) are retried with exponential backoff and jitter, up to
  ``max_retries`` times. A 429 waits until the gateway's stated reset
  time (``Retry-After``, or "resets at" in the message) when it gives
  one. A broken stream is re-requested from the start: orchestral only
  records a reply once its stream completes, so the conversation is
  untouched. The engine's ``on_retry`` callback is told each time, so
  the UI can show it and so a terminated agent stops retrying.
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger("conjure.llm")

_SEMAPHORES: dict[str, threading.BoundedSemaphore] = {}
_sleep = time.sleep  # indirection so tests can skip the waits
_SEM_LOCK = threading.Lock()

_TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529}
_TRANSIENT_NAMES = (
    "APIConnectionError", "APITimeoutError", "RemoteProtocolError",
    "ReadTimeout", "ConnectTimeout", "ConnectError", "ReadError",
    "InternalServerError", "OverloadedError", "ServiceUnavailableError",
    "RateLimitError",
)

class LLMCallStopped(Exception):
    """No further model calls this turn: the agent was terminated, or
    its engine's guard saw it looping. ``str(exc)`` says why (empty for
    termination)."""


# ``on_retry(attempt, max_retries, delay_s, exc) -> bool``; False stops retrying.
OnRetry = Callable[[int, int, float, BaseException], bool]


def semaphore_for(key: str, limit: int | None) -> threading.BoundedSemaphore | None:
    if not limit or limit <= 0:
        return None
    with _SEM_LOCK:
        sem = _SEMAPHORES.get(key)
        if sem is None:
            sem = _SEMAPHORES[key] = threading.BoundedSemaphore(limit)
        return sem


def is_transient(exc: BaseException) -> bool:
    seen = 0
    cur: BaseException | None = exc
    while cur is not None and seen < 6:
        status = getattr(cur, "status_code", None)
        if isinstance(status, int) and status in _TRANSIENT_STATUS:
            return True
        if type(cur).__name__ in _TRANSIENT_NAMES:
            return True
        if isinstance(cur, (ConnectionError, TimeoutError)):
            return True
        cur = cur.__cause__ or cur.__context__
        seen += 1
    return False


def retry_delay(exc: BaseException, attempt: int, *, base: float = 2.0, cap: float = 60.0) -> float:
    """Seconds to wait before retry number ``attempt`` (1-based)."""
    stated = _stated_wait(exc)
    if stated is not None:
        return min(max(stated, 1.0), 300.0)
    return min(cap, base * (2 ** (attempt - 1))) * (0.5 + random.random() / 2)


def _stated_wait(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            value = headers.get("retry-after")
        except Exception:
            value = None
        if value:
            try:
                return float(value) + 0.5
            except ValueError:
                pass
    m = re.search(r"resets at:?\s*(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})\s*UTC", str(exc), re.I)
    if m:
        try:
            reset = datetime.fromisoformat(m.group(1).replace(" ", "T")).replace(tzinfo=timezone.utc)
            return (reset - datetime.now(timezone.utc)).total_seconds() + 0.5
        except ValueError:
            return None
    return None


class _Slot:
    def __init__(self, sem: threading.BoundedSemaphore | None) -> None:
        self._sem = sem

    def __enter__(self) -> None:
        if self._sem is not None:
            self._sem.acquire()

    def __exit__(self, *exc: Any) -> None:
        if self._sem is not None:
            self._sem.release()


def make_resilient(
    llm: Any,
    *,
    key: str,
    max_concurrency: int | None = None,
    max_retries: int = 5,
) -> Any:
    """Wrap ``llm.get_response`` / ``llm.stream_response`` in place (see
    the module docstring) and return ``llm``. An engine may set
    ``llm.conjure_on_retry`` to hear about retries. Idempotent."""
    if getattr(llm, "_conjure_resilient", False):
        return llm
    sem = semaphore_for(key, max_concurrency)
    get_response = llm.get_response
    stream_response = llm.stream_response

    def should_retry(attempt: int, exc: BaseException) -> float | None:
        if attempt > max_retries or not is_transient(exc):
            return None
        delay = retry_delay(exc, attempt)
        logger.warning(
            "%s call failed (%s: %s); retry %d/%d in %.0fs",
            key, type(exc).__name__, _short(exc), attempt, max_retries, delay,
        )
        callback = getattr(llm, "conjure_on_retry", None)
        if callback is not None:
            try:
                if callback(attempt, max_retries, delay, exc) is False:
                    return None
            except Exception:
                pass
        return delay

    def check_stopped(context: Any) -> None:
        # An engine sets ``conjure_should_stop`` (terminated) and
        # ``conjure_guard`` (looping); either ends the turn before another
        # model call, whichever loop (ours or orchestral's) asks.
        stop = getattr(llm, "conjure_should_stop", None)
        if stop is not None and stop():
            raise LLMCallStopped()
        guard = getattr(llm, "conjure_guard", None)
        reason = guard(context) if guard is not None else None
        if reason:
            raise LLMCallStopped(reason)

    def resilient_get(context: Any, **kwargs: Any) -> Any:
        attempt = 0
        while True:
            check_stopped(context)
            try:
                with _Slot(sem):
                    return get_response(context, **kwargs)
            except Exception as exc:
                attempt += 1
                delay = should_retry(attempt, exc)
                if delay is None:
                    raise
                _sleep(delay)

    def resilient_stream(context: Any, **kwargs: Any):
        attempt = 0
        while True:
            check_stopped(context)
            try:
                with _Slot(sem):
                    gen = stream_response(context, **kwargs)
                    while True:
                        try:
                            chunk = next(gen)
                        except StopIteration as stop:
                            return stop.value
                        yield chunk
            except Exception as exc:
                attempt += 1
                delay = should_retry(attempt, exc)
                if delay is None:
                    raise
                _sleep(delay)

    llm.get_response = resilient_get
    llm.stream_response = resilient_stream
    llm._conjure_resilient = True
    return llm


def _short(exc: BaseException, limit: int = 200) -> str:
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
