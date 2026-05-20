"""Opaque ID generation for agents, messages, and runtime tokens.

IDs are short, lowercase base32 tokens with a type prefix (`ag-`, `msg-`).
They are random — never derive routing or authority from their shape.

Tokens (`new_runtime_token`) are longer and intended for runtime-internal
authentication of tool calls; they should not be exposed in plaintext to
LLM contexts.
"""

from __future__ import annotations

import base64
import secrets

_ID_ENTROPY_BYTES = 8        # 64 bits — comfortably unique for a single runtime
_TOKEN_ENTROPY_BYTES = 24    # 192 bits — for runtime-internal auth


def _short_token(nbytes: int = _ID_ENTROPY_BYTES) -> str:
    return base64.b32encode(secrets.token_bytes(nbytes)).decode("ascii").rstrip("=").lower()


def new_agent_id() -> str:
    """Mint an opaque agent identifier, e.g. ``ag-abc23def45ghi``."""
    return f"ag-{_short_token()}"


def new_message_id() -> str:
    """Mint an opaque message identifier, e.g. ``msg-abc23def45ghi``."""
    return f"msg-{_short_token()}"


def new_runtime_token() -> str:
    """Mint a long opaque token used to authenticate tool calls against
    the runtime registry. Never returned to LLM-facing tool outputs."""
    return secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)
