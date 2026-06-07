"""Error hierarchy for conjure runtime and tools.

Tools never raise these to LLM-facing code paths — they catch and return
structured ``{"ok": False, "code": ..., "error": ...}`` dicts. The
exceptions are for Python-call sites (the runtime, combinators, tests).
"""

from __future__ import annotations


class ConjureError(Exception):
    """Base class for all conjure runtime errors."""


class NotPermitted(ConjureError):
    """Caller lacks permission for the requested operation.

    Capability violations (sending to a non-permitted address), authority
    violations (terminating a non-descendant), and related cases.
    """


class NoSuchAddress(ConjureError):
    """Target address is not registered with the runtime."""


class Terminated(ConjureError):
    """Target address belongs to an agent that has been terminated."""


class Timeout(ConjureError):
    """A ``recv`` or ``wait_for`` with a timeout elapsed without a match.

    Combinator helpers attach diagnostic detail when a fan-in misses
    its deadline: ``workers`` (every dispatched worker address),
    ``received`` (count of replies seen), ``expected`` (target count),
    and ``partial`` (the bodies that did make it back, indexed in
    input order with ``None`` for the missing slots). All optional —
    a plain ``recv`` timeout from a bare mailbox read leaves them
    unset.
    """

    def __init__(
        self,
        message: str = "",
        *,
        workers: list[str] | None = None,
        received: int | None = None,
        expected: int | None = None,
        partial: list | None = None,
    ) -> None:
        super().__init__(message)
        self.workers = workers
        self.received = received
        self.expected = expected
        self.partial = partial


class MaxDepthExceeded(ConjureError):
    """Spawn would exceed the configured ``max_depth`` on the runtime."""
