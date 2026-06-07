"""Tool base class and per-tool runtime identity plumbing.

`StatelessRuntimeTool` resets runtime fields to their declared defaults
at the start of every ``execute()`` so values from a prior call do not
silently leak into a later one. This pattern is borrowed from agenTeX
(see ``agenTeX/src/agentex/tools/agentex.py`` lines 53-76) and is
critical for tools that sometimes want field ``X`` and sometimes want
field ``Y`` — without it, an omitted arg picks up the previous call's
value.

The token registry (``register_token`` / ``resolve_token``) lets each
tool instance recover its calling agent's ``Runtime`` and ``Address``
from the opaque token stored as the tool's ``runtime_token`` state
field. The runtime registers a token when minting an agent and
unregisters it when terminating.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from orchestral.tools.base.field_utils import (
    RuntimeField,
    StateField,
    is_state_field,
)
from orchestral.tools.base.tool import BaseTool
from pydantic_core import PydanticUndefined

from spawn.address import Address

if TYPE_CHECKING:
    from spawn.runtime import Runtime


__all__ = [
    "BaseTool",
    "RuntimeField",
    "StateField",
    "StatelessRuntimeTool",
    "register_token",
    "unregister_token",
    "resolve_token",
]


_TOKEN_REGISTRY: dict[str, "tuple[Runtime, Address]"] = {}
_TOKEN_LOCK = threading.Lock()


def register_token(token: str, runtime: "Runtime", addr: Address) -> None:
    with _TOKEN_LOCK:
        _TOKEN_REGISTRY[token] = (runtime, addr)


def unregister_token(token: str) -> None:
    with _TOKEN_LOCK:
        _TOKEN_REGISTRY.pop(token, None)


def resolve_token(token: str) -> "tuple[Runtime, Address] | None":
    with _TOKEN_LOCK:
        return _TOKEN_REGISTRY.get(token)


class StatelessRuntimeTool(BaseTool):
    """Reset runtime fields to their declared defaults at the start of
    every ``execute()`` call so values from a prior invocation don't
    bleed into a later one.

    State fields (token, configuration set at construction) are
    preserved. Required runtime fields with no default are left alone
    so BaseTool's own missing-field check can fire.
    """

    def execute(self, stream_callback=None, **kwargs):
        for field_name, field_info in type(self).model_fields.items():
            if is_state_field(field_info):
                continue
            if field_name in kwargs:
                continue
            default = field_info.default
            if default is PydanticUndefined:
                continue
            setattr(self, field_name, default)
        return super().execute(stream_callback=stream_callback, **kwargs)
