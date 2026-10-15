"""Toolkit tools: ``tool_catalog``, ``grant_toolkits``, ``request_toolkits``.

The curation surface for toolbase toolkits (see ``conjure.toolbase``).
A parent reads the catalog of what it holds, hands slices of it to
children at spawn (``Spawn(toolkits=...)``) or later
(``GrantToolkits``); a child that finds itself short asks its parent
(``RequestToolkits``), which grants, escalates to its own parent, or
declines. Toolkits flow down the tree the way addresses do — nobody
can grant what they don't hold.

Same two faces as the primitives: ``*_impl`` functions returning
``{"ok": ...}`` dicts, and orchestral tool classes delegating to them.
"""

from __future__ import annotations

import time
from typing import Any

from conjure.envelope import Envelope
from conjure.errors import NotPermitted, Terminated, ToolkitDenied
from conjure.ids import new_message_id
from conjure.toolbase import describe_catalog, parse_toolkits
from conjure.tools._base import (
    RuntimeField,
    StateField,
    StatelessRuntimeTool,
    resolve_token,
)
from conjure.tools.primitives import _err, _resolve_addr


_TOOLKITS_FIELD_HELP = (
    "Toolbase toolkits, in `tb activate` grammar: a list like "
    "[\"heptapod\", \"heptapod/llp\", \"calculator__add\"] (whole toolkit, "
    "one bundle, one tool), or a mapping {toolkit: null | [tool, \"/bundle\", "
    "...] | {bundles, tools, skills}}."
)


def _hub_or_err(runtime: Any) -> Any:
    hub = getattr(runtime, "toolbase", None)
    if hub is None:
        return _err(
            "toolbase_unavailable",
            "toolbase is not configured for this runtime",
        )
    return hub


def catalog_impl(
    *, token: str, toolkit: str = "", refresh: bool = False
) -> dict[str, Any]:
    resolved = resolve_token(token)
    if resolved is None:
        return _err("no_runtime", "tool is not bound to a runtime")
    runtime, caller = resolved
    hub = _hub_or_err(runtime)
    if isinstance(hub, dict):
        return hub
    try:
        catalog = hub.catalog(refresh=bool(refresh))
        authority = hub.authority_of(runtime, caller)
        return describe_catalog(catalog, authority, toolkit=toolkit or None)
    except ToolkitDenied as e:
        return _err("toolkit_denied", str(e))


def grant_toolkits_impl(
    *, token: str, child: str, toolkits: Any
) -> dict[str, Any]:
    resolved = resolve_token(token)
    if resolved is None:
        return _err("no_runtime", "tool is not bound to a runtime")
    runtime, caller = resolved
    if isinstance(_hub_or_err(runtime), dict):
        return _hub_or_err(runtime)
    target = _resolve_addr(child, runtime, caller)
    if target is None:
        return _err("no_such_address", f"unknown address: {child}")
    try:
        grant = parse_toolkits(toolkits)
        merged = runtime.grant_toolkits(
            granter=caller, target=target, toolkits=grant
        )
    except ToolkitDenied as e:
        return _err("toolkit_denied", str(e))
    except NotPermitted as e:
        return _err("not_permitted", str(e))
    except Terminated as e:
        return _err("terminated", str(e))
    return {
        "ok": True,
        "child": target.id,
        "toolkits": {k: v.model_dump(exclude_none=True) for k, v in merged.items()},
        "note": "takes effect at the child's next turn",
    }


def request_toolkits_impl(
    *, token: str, toolkits: Any, reason: str = ""
) -> dict[str, Any]:
    resolved = resolve_token(token)
    if resolved is None:
        return _err("no_runtime", "tool is not bound to a runtime")
    runtime, caller = resolved
    hub = _hub_or_err(runtime)
    if isinstance(hub, dict):
        return hub
    record = runtime.record_for(caller)
    if record.parent is None:
        return _err(
            "no_parent",
            "you are the root: every installed toolkit is already in your "
            "catalog. Anything else must be installed (ask the user, or "
            "author one with `tb init` + `tb install -e`, then "
            "ToolCatalog(refresh=true)).",
        )
    try:
        wanted = parse_toolkits(toolkits)
        if not wanted:
            return _err("bad_args", "toolkits is empty")
    except ToolkitDenied as e:
        return _err("toolkit_denied", str(e))
    parent_record = runtime.record_for(record.parent)
    if parent_record.status == "terminated":
        return _err("terminated", "your parent is terminated")
    body = {
        "request": "toolkits",
        "toolkits": {k: v.model_dump(exclude_none=True) for k, v in wanted.items()},
        "reason": reason,
        "how_to_grant": (
            f"GrantToolkits(child=\"{caller.id}\", toolkits=...) — or, if you "
            "don't hold them, RequestToolkits from your own parent first."
        ),
    }
    msg_id = new_message_id()
    env = Envelope(
        seq=0,
        msg_id=msg_id,
        from_=caller,
        to=record.parent,
        thread_id=msg_id,
        body=body,
        headers={"kind": "toolkit_request"},
        ts=time.time(),
    )
    stored = parent_record.inbox.put(env)
    runtime._journal_send(stored)  # noqa: SLF001
    if parent_record.wakeup is not None:
        parent_record.wakeup.set()
    return {
        "ok": True,
        "msg_id": stored.msg_id,
        "note": (
            "request sent to your parent; granted toolkits appear at your "
            "next turn"
        ),
    }


# ---------- Tool classes ----------


class ToolCatalogTool(StatelessRuntimeTool):
    """List the toolbase toolkits you hold — and may therefore grant to
    the agents you spawn. Without ``toolkit``: one row per toolkit
    (description, bundles, how many tools/skills you hold). With
    ``toolkit``: every tool and skill of it you hold, with descriptions.
    Give each child only what its task needs — a short tool list is
    easier to use well."""

    toolkit: str = RuntimeField(
        default="",
        description="Toolkit to detail; empty for the summary.",
    )
    refresh: bool = RuntimeField(
        default=False,
        description=(
            "Re-read installed toolkits (after something was installed "
            "or authored this session)."
        ),
    )
    runtime_token: str = StateField(description="(internal) caller token.")

    def _run(self) -> dict[str, Any]:
        return catalog_impl(
            token=self.runtime_token,
            toolkit=self.toolkit or "",
            refresh=bool(self.refresh),
        )


class GrantToolkitsTool(StatelessRuntimeTool):
    """Give a live descendant more toolbase toolkits. Additive, and
    bounded by your own: you can grant only what you hold. The child
    sees the new tools from its next turn, with its conversation
    intact. Use this to answer a child's ``toolkit_request``.

    Error codes: ``toolkit_denied`` (you don't hold it / not installed),
    ``not_permitted`` (target isn't your descendant)."""

    child: str = RuntimeField(description="Address id or label of the descendant.")
    toolkits: str | list[str] | dict[str, Any] = RuntimeField(description=_TOOLKITS_FIELD_HELP)
    runtime_token: str = StateField(description="(internal) caller token.")

    def _run(self) -> dict[str, Any]:
        return grant_toolkits_impl(
            token=self.runtime_token,
            child=self.child,
            toolkits=self.toolkits,
        )


class RequestToolkitsTool(StatelessRuntimeTool):
    """Ask your parent for toolbase toolkits you need but weren't given.
    Sends a ``toolkit_request`` message; if granted, the tools appear at
    your next turn. Say why in ``reason`` — the parent decides."""

    toolkits: str | list[str] | dict[str, Any] = RuntimeField(description=_TOOLKITS_FIELD_HELP)
    reason: str = RuntimeField(
        default="",
        description="What you need the tools for.",
    )
    runtime_token: str = StateField(description="(internal) caller token.")

    def _run(self) -> dict[str, Any]:
        return request_toolkits_impl(
            token=self.runtime_token,
            toolkits=self.toolkits,
            reason=self.reason or "",
        )


TOOLKIT_TOOL_CLASSES = (
    ToolCatalogTool,
    GrantToolkitsTool,
    RequestToolkitsTool,
)
