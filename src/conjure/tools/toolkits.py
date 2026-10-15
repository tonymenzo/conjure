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
        from conjure.engines import effective_engine
        from conjure.toolbase import CALL_PREFIX

        catalog = hub.catalog(refresh=bool(refresh))
        authority = hub.authority_of(runtime, caller)
        record = runtime.record_for(caller)
        served = hub.toolset_of(record.spec)
        prefix = CALL_PREFIX.get(effective_engine(record, runtime), "")
        out = describe_catalog(
            catalog, authority, toolkit=toolkit or None, served=served, prefix=prefix
        )
    except ToolkitDenied as e:
        return _err("toolkit_denied", str(e))
    if out.get("ok"):
        # Grantable and callable differ for the root: its authority comes
        # from the config, its own served tools from root.toolkits.
        out["note"] = (
            "`tools_held` is what you may grant; `tools_you_can_call` (and "
            "each tool's `call_as`) is what you can call yourself."
        )
        if record.parent is None and not served and authority:
            out["note"] += (
                " You hold these to grant but can't call any yet: to use one "
                "yourself, GrantToolkits(child=\"self\", toolkits=[...]); it's "
                "served to you from your next turn."
            )
    return out


def grant_toolkits_impl(
    *, token: str, child: str, toolkits: Any
) -> dict[str, Any]:
    resolved = resolve_token(token)
    if resolved is None:
        return _err("no_runtime", "tool is not bound to a runtime")
    runtime, caller = resolved
    if isinstance(_hub_or_err(runtime), dict):
        return _hub_or_err(runtime)
    if child in ("self", caller.id):
        target = caller
    else:
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


def load_skill_impl(*, token: str, name: str, file: str = "") -> dict[str, Any]:
    resolved = resolve_token(token)
    if resolved is None:
        return _err("no_runtime", "tool is not bound to a runtime")
    runtime, caller = resolved
    hub = _hub_or_err(runtime)
    if isinstance(hub, dict):
        return hub
    toolset = hub.toolset_of(runtime.record_for(caller).spec)
    try:
        found = hub.find_skill(toolset, name.strip())
    except ToolkitDenied as e:
        return _err("toolkit_denied", str(e))
    if found is None:
        held = [f"{k}__{s.slug}" for k, s in hub.skills_for(toolset)]
        return _err(
            "no_such_skill",
            f"you hold no skill {name!r}; yours: {', '.join(held) or '(none)'}",
        )
    kit, skill = found
    files: list[str] = []
    if skill.root is not None:
        files = sorted(
            str(p.relative_to(skill.root))
            for p in skill.root.rglob("*")
            if p.is_file() and p != skill.doc and not p.name.startswith(".")
        )
    if file:
        if skill.root is None:
            return _err("bad_args", f"{kit}__{skill.slug} has no supporting files")
        target = (skill.root / file).resolve()
        try:
            target.relative_to(skill.root.resolve())
        except ValueError:
            return _err("bad_args", "file must be inside the skill's directory")
        if not target.is_file():
            return _err("bad_args", f"no file {file!r}; files: {', '.join(files) or '(none)'}")
        path = target
    else:
        path = skill.doc
    if path is None:
        return _err("no_such_skill", f"{kit}__{skill.slug} has no guide on disk")
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return _err("unreadable", f"{path.name}: {e}")
    return {
        "ok": True,
        "name": f"{kit}__{skill.slug}",
        "file": file or "SKILL.md",
        "content": content,
        "files": files,
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
    stored = parent_record.inbox.put(env, before_publish=runtime._journal_send)
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
    intact. Use this to answer a child's ``toolkit_request``. The root
    may also grant itself (``child="self"``) from what it may grant, to
    call those tools directly.

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


class LoadSkillTool(StatelessRuntimeTool):
    """Read one of your toolkits' skills: an author's how-to guide for
    using its tools well. ``ToolCatalog(toolkit=...)`` lists the skills
    you hold; load one when its description matches your task, before
    using the tools it covers. ``file`` reads one of the skill's
    supporting files (listed in ``files``)."""

    name: str = RuntimeField(description="Skill name, `<toolkit>__<skill>` (or the bare skill name if unique).")
    file: str = RuntimeField(default="", description="A supporting file of the skill; empty for its guide.")
    runtime_token: str = StateField(description="(internal) caller token.")

    def _run(self) -> dict[str, Any]:
        return load_skill_impl(token=self.runtime_token, name=self.name, file=self.file or "")


TOOLKIT_TOOL_CLASSES = (
    ToolCatalogTool,
    GrantToolkitsTool,
    RequestToolkitsTool,
    LoadSkillTool,
)
