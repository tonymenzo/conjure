"""Error hierarchy for combinator runtime and tools.

Tools never raise these to LLM-facing code paths — they catch and return
structured ``{"ok": False, "code": ..., "error": ...}`` dicts. The
exceptions are for Python-call sites (the runtime, combinators, tests).
"""

from __future__ import annotations


class CombinatorError(Exception):
    """Base class for all combinator runtime errors."""


class NotPermitted(CombinatorError):
    """Caller lacks permission for the requested operation.

    Capability violations (sending to a non-permitted address), authority
    violations (terminating a non-descendant), and related cases.
    """


class NoSuchAddress(CombinatorError):
    """Target address is not registered with the runtime."""


class Terminated(CombinatorError):
    """Target address belongs to an agent that has been terminated."""


class Timeout(CombinatorError):
    """A ``recv`` or ``wait_for`` with a timeout elapsed without a match."""


class MaxDepthExceeded(CombinatorError):
    """Spawn would exceed the configured ``max_depth`` on the runtime."""
