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
from typing import TYPE_CHECKING, ClassVar

from orchestral.tools.base.field_utils import (
    RuntimeField,
    StateField,
    is_state_field,
)
from orchestral.tools.base.tool import BaseTool
from pydantic import Field
from pydantic_core import PydanticUndefined

from conjure.address import Address

if TYPE_CHECKING:
    from conjure.runtime import Runtime


__all__ = [
    "BaseTool",
    "OptionalRuntimeField",
    "RuntimeField",
    "StateField",
    "StatelessRuntimeTool",
    "register_token",
    "unregister_token",
    "resolve_token",
]


def OptionalRuntimeField(**kwargs):
    """``RuntimeField`` variant for optional parameters that default to
    ``None``.

    orchestral's schema generator treats a plain ``default=None`` as "no
    meaningful default" and marks the field required, so this uses
    ``default_factory`` instead (which the generator counts as a real
    default). ``RuntimeField`` itself can't express this — it injects
    ``default=None`` whenever ``default`` is absent, which conflicts
    with ``default_factory``.
    """
    return Field(
        json_schema_extra={"runtime": True},
        default_factory=lambda: None,
        **kwargs,
    )


_TOKEN_REGISTRY: dict[str, "tuple[Runtime, Address]"] = {}
_TOKEN_LOCK = threading.Lock()


def register_token(token: str, runtime: "Runtime", addr: Address) -> None:
    with _TOKEN_LOCK:
        _TOKEN_REGISTRY[token] = (runtime, addr)


def unregister_token(token: str) -> None:
    with _TOKEN_LOCK:
        entry = _TOKEN_REGISTRY.pop(token, None)
        if entry is not None:
            _REVOKED[token] = entry[1].id


# Tokens of terminated agents → their address id. A tool call made with
# one comes from an agent whose turn outlived its termination; it gets
# ``code=terminated`` (stop working) rather than a puzzling ``no_runtime``.
_REVOKED: dict[str, str] = {}


def revoked(token: str) -> str | None:
    with _TOKEN_LOCK:
        return _REVOKED.get(token)


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

    Every subclass's ``_run`` result also carries an ``inbox`` notice
    when messages are waiting that the caller hasn't seen (see
    ``inbox_notice``), whichever surface ran it: orchestral, the
    in-process or HTTP MCP servers, or the daemon behind the stdio
    bridge.
    """

    # Tools whose result already is the inbox.
    _shows_inbox: ClassVar[bool] = False

    @classmethod
    def get_name(cls) -> str:
        """The tool's name as models see it: the class name without its
        ``Tool`` suffix (``WaitFor``, ``GrantToolkits``, ``ToolCatalog``),
        the same as over MCP, so one prompt names tools right for every
        engine. orchestral's default lowercased the name and stripped
        every "Tool" in it (``grantkits``, ``catalog``)."""
        name = cls.__name__
        return name[: -len("Tool")] if name.endswith("Tool") and name != "Tool" else name

    @classmethod
    def get_tool_spec(cls):
        """The spec sent to the model, under the same name the router
        dispatches on. orchestral's schema generator names specs itself
        (lowercased, every "Tool" stripped) instead of asking
        ``get_name``, so without this the model was offered ``catalog``
        while the router only knew ``ToolCatalog``."""
        spec = super().get_tool_spec()
        spec.name = cls.get_name()
        return spec

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        run = cls.__dict__.get("_run")
        if run is None or getattr(run, "_adds_inbox", False):
            return

        def _run(self, *args, **kw):
            gone = revoked(getattr(self, "runtime_token", "") or "")
            if gone is not None:
                return {
                    "ok": False,
                    "code": "terminated",
                    "error": (
                        f"you ({gone}) were terminated: nothing you do now takes "
                        "effect. Stop and end your turn."
                    ),
                }
            out = run(self, *args, **kw)
            if isinstance(out, dict) and "inbox" not in out and not type(self)._shows_inbox:
                notice = inbox_notice(getattr(self, "runtime_token", "") or "")
                if notice:
                    out["inbox"] = notice
            return out

        _run._adds_inbox = True  # type: ignore[attr-defined]
        _run.__doc__ = run.__doc__
        cls._run = _run  # type: ignore[method-assign]

    def execute(self, stream_callback=None, **kwargs):
        for field_name, field_info in type(self).model_fields.items():
            if is_state_field(field_info):
                continue
            if field_name in kwargs:
                continue
            default = field_info.default
            if default is PydanticUndefined:
                if field_info.default_factory is not None:
                    setattr(self, field_name, field_info.default_factory())
                continue
            setattr(self, field_name, default)
        return super().execute(stream_callback=stream_callback, **kwargs)


def inbox_notice(token: str, *, limit: int = 3) -> dict | None:
    """Messages waiting for the agent behind ``token`` that it hasn't
    seen: arrived after its current turn started (the driver's cursor)
    and not read with ``recv`` / ``wait_for``. ``None`` when there are
    none. An agent mid-turn doesn't get pushed messages until the turn
    ends, so without this a parent busy in a long turn can miss its
    workers' results entirely."""
    resolved = resolve_token(token) if token else None
    if resolved is None:
        return None
    runtime, caller = resolved
    try:
        record = runtime.record_for(caller)
        cursor = getattr(record.driver, "_cursor", 0) or 0
        waiting = [
            e for e in record.inbox.read(since_seq=cursor, max_n=200, timeout_s=0.0)
            if e.seq not in record.pulled
        ]
    except Exception:
        return None
    if not waiting:
        return None

    def preview(env) -> dict:
        body = env.body
        if not isinstance(body, str):
            import json

            try:
                body = json.dumps(body, default=str)
            except Exception:
                body = str(body)
        sender = env.from_.label or env.from_.id
        item = {"from": sender, "seq": env.seq, "preview": body[:200]}
        kind = (env.headers or {}).get("kind")
        if kind:
            item["kind"] = kind
        return item

    return {
        "unread": len(waiting),
        "latest": [preview(e) for e in waiting[-limit:]],
        "hint": (
            "Messages are waiting: Recv() returns them. Otherwise they "
            "reach you only after this turn ends."
        ),
    }
