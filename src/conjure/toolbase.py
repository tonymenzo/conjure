"""Toolbase integration — agents curate toolkits for the agents they spawn.

A **toolset** maps toolkit names to :class:`ToolkitGrant`\\ s: the whole
toolkit, some of its bundles, some of its tools, and (optionally) an
exact list of its skills. ``AgentSpec.toolkits`` carries the toolset an
agent is *served*; the same toolset is its *authority* — the most it
may hand to a child.

Grants attenuate exactly like budgets and addresses: a child's toolset
must be a subset of its parent's, checked once per spawn at the runtime
choke point (``Runtime._spawn_batch``) and once per live grant. The root
draws on the hub's configured authority — by default every installed
toolkit, so a toolkit authored and installed mid-session becomes
grantable after a catalog refresh.

Three pieces:

- :class:`Catalog` — what is installed: toolkits, their bundles, tools
  (by served name) and skills. Loaded from toolbase's cache; tests and
  embedders inject their own via ``catalog_loader``.
- :class:`ToolbaseHub` — one per runtime. Holds the anchor project
  (whose loadout supplies version pins and ``serve.yaml`` blocklists),
  the root's authority, the catalog, and the server factory.
- :class:`ToolbaseMount` — one per agent. Starts the agent's toolkit
  servers lazily on its first step (so a fan-out spawn doesn't start
  N × K subprocesses serially inside the spawn call), restarts them
  when a live grant changes the toolset, and tears them down when the
  agent terminates.

Toolbase itself is optional (``pip install "conjure-agents[toolbase]"``,
Python ≥ 3.12). Without it no hub is built and any spawn that asks for
toolkits is refused with ``code=toolkit_denied``.
"""

from __future__ import annotations

import io
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping

from pydantic import BaseModel, ConfigDict

from conjure.errors import ToolkitDenied

if TYPE_CHECKING:
    from conjure.address import Address
    from conjure.record import AgentRecord, AgentSpec
    from conjure.runtime import Runtime


# ---------- grants ----------


class ToolkitGrant(BaseModel):
    """One toolkit's slice of a toolset.

    ``bundles`` and ``tools`` both ``None`` grants the whole toolkit;
    either set switches to allowlist mode and the grant is their union
    (toolbase's own loadout semantics). ``tools`` holds served tool
    names without the ``<toolkit>__`` prefix. ``skills=None`` surfaces
    every skill belonging to the granted part; a list pins the set.
    """

    model_config = ConfigDict(extra="forbid")

    bundles: list[str] | None = None
    tools: list[str] | None = None
    skills: list[str] | None = None

    @property
    def is_whole(self) -> bool:
        return self.bundles is None and self.tools is None


Toolset = dict[str, ToolkitGrant]


def parse_toolkits(raw: Any) -> Toolset:
    """Normalize a caller-supplied toolset.

    Accepts the ``tb activate`` grammar as a list — ``"heptapod"`` (whole
    toolkit), ``"heptapod/llp"`` (a bundle), ``"heptapod__MadGraph"`` (a
    tool) — or a mapping ``{toolkit: None | "*" | [items] | {bundles,
    tools, skills}}``, where list ``items`` are tool names or ``"/bundle"``.
    Items for the same toolkit merge; a whole-toolkit item wins over any
    partial one.
    """
    if raw is None:
        return {}
    if isinstance(raw, str):
        raw = [raw]
    out: Toolset = {}
    if isinstance(raw, Mapping):
        for kit, value in raw.items():
            if not isinstance(kit, str) or not kit:
                raise ToolkitDenied(f"toolkit names must be strings: {kit!r}")
            if value is None or value == "*" or value is True:
                _merge(out, kit, ToolkitGrant())
            elif isinstance(value, ToolkitGrant):
                _merge(out, kit, value)
            elif isinstance(value, Mapping):
                try:
                    _merge(out, kit, ToolkitGrant.model_validate(dict(value)))
                except Exception as exc:
                    raise ToolkitDenied(f"bad grant for {kit!r}: {exc}") from exc
            elif isinstance(value, (list, tuple)):
                for item in value:
                    _merge_item(out, kit, str(item))
            else:
                raise ToolkitDenied(
                    f"grant for {kit!r} must be null, '*', a list or a mapping"
                )
        return out
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if not isinstance(item, str) or not item:
                raise ToolkitDenied(f"toolkit items must be strings: {item!r}")
            if "/" in item:
                kit, bundle = item.split("/", 1)
                _merge(out, kit, ToolkitGrant(bundles=[bundle]))
            elif "__" in item:
                kit, tool = item.split("__", 1)
                _merge(out, kit, ToolkitGrant(tools=[tool]))
            else:
                _merge(out, item, ToolkitGrant())
        return out
    raise ToolkitDenied(
        f"toolkits must be a list or a mapping, got {type(raw).__name__}"
    )


def _merge_item(out: Toolset, kit: str, item: str) -> None:
    if item in ("*", ""):
        _merge(out, kit, ToolkitGrant())
    elif item.startswith("/"):
        _merge(out, kit, ToolkitGrant(bundles=[item[1:]]))
    else:
        _merge(out, kit, ToolkitGrant(tools=[item.split("__", 1)[-1]]))


def _merge(out: Toolset, kit: str, grant: ToolkitGrant) -> None:
    prev = out.get(kit)
    if prev is None:
        out[kit] = grant
        return
    skills = _union(prev.skills, grant.skills, none_wins=True)
    if prev.is_whole or grant.is_whole:
        out[kit] = ToolkitGrant(skills=skills)
        return
    out[kit] = ToolkitGrant(
        bundles=_union(prev.bundles, grant.bundles),
        tools=_union(prev.tools, grant.tools),
        skills=skills,
    )


def _union(
    a: list[str] | None, b: list[str] | None, *, none_wins: bool = False
) -> list[str] | None:
    if a is None and b is None:
        return None
    if none_wins and (a is None or b is None):
        return None
    return sorted(set(a or []) | set(b or []))


def toolset_key(toolset: Toolset | None) -> str:
    """Stable identity for a toolset — equal toolsets, equal keys."""
    if not toolset:
        return ""
    return json.dumps(
        {k: v.model_dump() for k, v in sorted(toolset.items())},
        sort_keys=True,
    )


def merge_toolsets(*toolsets: Toolset | None) -> Toolset:
    out: Toolset = {}
    for ts in toolsets:
        for kit, grant in (ts or {}).items():
            _merge(out, kit, grant)
    return out


# ---------- catalog ----------


@dataclass(frozen=True)
class ToolInfo:
    name: str  # served name, without the ``<toolkit>__`` prefix
    description: str = ""
    bundles: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillInfo:
    slug: str
    description: str = ""
    bundle: str | None = None


@dataclass
class ToolkitInfo:
    name: str
    version: str = ""
    description: str = ""
    tools: dict[str, ToolInfo] = field(default_factory=dict)
    skills: dict[str, SkillInfo] = field(default_factory=dict)
    # Bundles the toolkit declares but cannot serve, with the config
    # keys each one needs. Granting them is allowed (the tools will
    # simply not appear); the catalog flags them so a curator can tell.
    unavailable_bundles: dict[str, list[str]] = field(default_factory=dict)

    @property
    def bundles(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {b: [] for b in self.unavailable_bundles}
        for tool in self.tools.values():
            for b in tool.bundles:
                out.setdefault(b, []).append(tool.name)
        for skill in self.skills.values():
            if skill.bundle:
                out.setdefault(skill.bundle, [])
        return {b: sorted(ts) for b, ts in sorted(out.items())}


@dataclass
class Catalog:
    toolkits: dict[str, ToolkitInfo] = field(default_factory=dict)

    def get(self, name: str) -> ToolkitInfo | None:
        return self.toolkits.get(name)


@dataclass(frozen=True)
class _Expanded:
    """A grant resolved against the catalog to concrete name sets."""

    tools: frozenset[str]
    skills: frozenset[str]


def expand(kit: str, grant: ToolkitGrant, catalog: Catalog) -> _Expanded:
    """Resolve ``grant`` to the concrete tools and skills it covers.

    Raises ``ToolkitDenied`` for a toolkit, bundle, tool or skill the
    catalog doesn't know — a typo should fail at spawn, not surface as a
    child that silently has nothing.
    """
    info = catalog.get(kit)
    if info is None:
        raise ToolkitDenied(f"toolkit {kit!r} is not installed")
    known_bundles = set(info.bundles)
    for b in grant.bundles or []:
        if b not in known_bundles:
            raise ToolkitDenied(
                f"toolkit {kit!r} has no bundle {b!r} "
                f"(bundles: {sorted(known_bundles) or 'none'})"
            )
    for t in grant.tools or []:
        if t not in info.tools:
            raise ToolkitDenied(f"toolkit {kit!r} has no tool {t!r}")
    for s in grant.skills or []:
        if s not in info.skills:
            raise ToolkitDenied(f"toolkit {kit!r} has no skill {s!r}")

    if grant.is_whole:
        tools = frozenset(info.tools)
        bundles: set[str] | None = None
    else:
        bundles = set(grant.bundles or [])
        tools = frozenset(
            name
            for name, tool in info.tools.items()
            if name in (grant.tools or []) or bundles.intersection(tool.bundles)
        )
    if grant.skills is not None:
        skills = frozenset(grant.skills)
    else:
        skills = frozenset(
            slug
            for slug, skill in info.skills.items()
            if bundles is None or skill.bundle is None or skill.bundle in bundles
        )
    return _Expanded(tools=tools, skills=skills)


def check_subset(child: Toolset, parent: Toolset, catalog: Catalog) -> None:
    """Raise ``ToolkitDenied`` unless every grant in ``child`` is covered
    by ``parent``."""
    for kit, grant in child.items():
        if catalog.get(kit) is None:
            raise ToolkitDenied(f"toolkit {kit!r} is not installed")
        if kit not in parent:
            raise ToolkitDenied(
                f"cannot grant {kit!r}: you do not hold it "
                f"(held: {sorted(parent) or 'none'})"
            )
        want = expand(kit, grant, catalog)
        have = expand(kit, parent[kit], catalog)
        extra_tools = want.tools - have.tools
        if extra_tools:
            raise ToolkitDenied(
                f"cannot grant {kit!r} tools {sorted(extra_tools)}: "
                "not in your own grant"
            )
        extra_skills = want.skills - have.skills
        if extra_skills:
            raise ToolkitDenied(
                f"cannot grant {kit!r} skills {sorted(extra_skills)}: "
                "not in your own grant"
            )


# ---------- serving ----------


class ToolbaseServing:
    """Live toolkit servers for one toolset. ``tools`` are orchestral
    ``BaseTool`` proxies named ``<toolkit>__<tool>``; ``close`` stops the
    per-toolkit subprocesses. Idempotent."""

    def __init__(self, tools: list[Any], close: Callable[[], None] | None = None):
        self.tools = list(tools)
        self._close = close
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._close is not None:
            try:
                self._close()
            except Exception:
                pass


ServerFactory = Callable[..., ToolbaseServing]
CatalogLoader = Callable[[], Catalog]


class ToolbaseHub:
    """Per-runtime toolbase state: anchor project, root authority,
    catalog cache, and how to start servers.

    ``authority`` is the root's grantable set: ``"all"`` (every
    installed toolkit, re-read on each catalog refresh) or a toolset.
    ``catalog_loader`` / ``server_factory`` default to the real toolbase
    implementations and exist so tests (and future remote deployments)
    can substitute their own.
    """

    def __init__(
        self,
        *,
        project_root: str | Path | None = None,
        loadout: str | None = None,
        authority: Any = "all",
        call_timeout_s: float = 60.0,
        catalog_loader: CatalogLoader | None = None,
        server_factory: ServerFactory | None = None,
        loadout_resolver: Callable[[str], Toolset] | None = None,
    ) -> None:
        self.project_root = (
            Path(project_root).expanduser() if project_root else _discover_project_root()
        )
        self.loadout = loadout
        self.call_timeout_s = call_timeout_s
        self._authority_all = authority in (None, "all", "*")
        self._authority: Toolset = (
            {} if self._authority_all else parse_toolkits(authority)
        )
        self._catalog_loader = catalog_loader or (
            lambda: load_toolbase_catalog(self.project_root)
        )
        self._server_factory = server_factory or _default_server_factory
        self._loadout_resolver = loadout_resolver or self._resolve_named_loadout
        self._catalog: Catalog | None = None
        self._lock = threading.Lock()

    # ----- catalog -----

    def catalog(self, *, refresh: bool = False) -> Catalog:
        with self._lock:
            if self._catalog is None or refresh:
                self._catalog = self._catalog_loader()
            return self._catalog

    # ----- authority -----

    def root_authority(self) -> Toolset:
        if self._authority_all:
            return {name: ToolkitGrant() for name in self.catalog().toolkits}
        return dict(self._authority)

    def toolset_of(self, spec: "AgentSpec") -> Toolset:
        """What ``spec`` is served: its inline toolkits plus, if named,
        the toolbase loadout's selection."""
        inline = dict(spec.toolkits or {})
        if spec.toolbase_loadout:
            return merge_toolsets(self._loadout_resolver(spec.toolbase_loadout), inline)
        return inline

    def authority_of(self, runtime: "Runtime", addr: "Address") -> Toolset:
        record = runtime.record_for(addr)
        if record.parent is None:
            return self.root_authority()
        return self.toolset_of(record.spec)

    def check_grant(self, toolset: Toolset, authority: Toolset) -> None:
        if toolset:
            check_subset(toolset, authority, self.catalog())

    # ----- serving -----

    def serve(self, toolset: Toolset, *, sandbox: Path | None) -> ToolbaseServing:
        return self._server_factory(
            toolset=toolset,
            sandbox=sandbox,
            project_root=self.project_root,
            loadout=self.loadout,
            call_timeout_s=self.call_timeout_s,
        )

    def _resolve_named_loadout(self, name: str) -> Toolset:
        from toolbase.serve.loadouts import resolve_loadout

        resolved = resolve_loadout(self.project_root, cli_loadout=name)
        out: Toolset = {}
        catalog = self.catalog()
        for kit, sel in resolved.toolkits.items():
            if kit in resolved.disabled_toolkits:
                continue
            grant = ToolkitGrant(
                bundles=sel.bundles,
                tools=sel.enabled_tools,
                skills=getattr(sel, "enabled_skills", None),
            )
            disabled = set(sel.disabled_tools) | {
                t.split("__", 1)[1]
                for t in resolved.disabled_tools
                if t.startswith(f"{kit}__")
            }
            if disabled and catalog.get(kit) is not None:
                # Fold blocklists into an explicit allowlist so authority
                # checks can't hand a child a tool the loadout hides.
                served = expand(kit, grant, catalog).tools - disabled
                grant = ToolkitGrant(tools=sorted(served), skills=grant.skills)
            out[kit] = grant
        return out


class ToolbaseMount:
    """One agent's view of its toolkits. Engines call :meth:`sync` at
    the top of every step; it returns the current tool list when the
    toolset changed since the last call (first call included) and
    ``None`` otherwise."""

    def __init__(
        self,
        hub: ToolbaseHub,
        record: "AgentRecord",
        sandbox: Path | Callable[[], Path | None] | None,
    ):
        # ``sandbox`` may be a thunk so agents that never get toolkits
        # never have a sandbox directory created on their behalf.
        self._hub = hub
        self._record = record
        self._sandbox = sandbox
        # ``""`` is the empty toolset's key: an agent with no toolkits
        # reports no change on its first sync instead of forcing its
        # engine to rebuild around an empty tool list.
        self._applied: str | None = ""
        self._serving: ToolbaseServing | None = None
        self._lock = threading.Lock()

    def wanted(self) -> Toolset:
        return self._hub.toolset_of(self._record.spec)

    def sync(self) -> list[Any] | None:
        with self._lock:
            toolset = self.wanted()
            key = toolset_key(toolset)
            if key == self._applied:
                return None
            old, self._serving = self._serving, None
            if old is not None:
                old.close()
            if toolset:
                sandbox = self._sandbox() if callable(self._sandbox) else self._sandbox
                self._serving = self._hub.serve(toolset, sandbox=sandbox)
            self._applied = key
            return list(self._serving.tools) if self._serving else []

    @property
    def tools(self) -> list[Any]:
        return list(self._serving.tools) if self._serving else []

    def close(self) -> None:
        with self._lock:
            if self._serving is not None:
                self._serving.close()
                self._serving = None
            self._applied = ""


# ---------- system-prompt guidance ----------


TOOLKIT_GUIDANCE = """
## Toolbase toolkits

Domain tools come from toolbase toolkits, served to you as
`<toolkit>__<tool>` (in Claude Code: `mcp__toolbase__<toolkit>__<tool>`).
You can hand toolkits to the agents you spawn — but only ones you hold
yourself. Grants attenuate down the tree, like addresses and budgets.

- `ToolCatalog()` — the toolkits you hold: descriptions, bundles,
  tool and skill counts. `ToolCatalog(toolkit="x")` lists each tool.
  `refresh=true` re-reads what's installed.
- `Spawn(..., toolkits=[...])` (also `Call` / combinator `spec`s) —
  serve the child a slice, in `tb activate` grammar: `"heptapod"`
  (whole toolkit), `"heptapod/llp"` (one bundle), `"heptapod__Pythia"`
  (one tool). **Curate**: give each child only what its task needs. A
  short, relevant tool list is used far better than a long one, and a
  child can always ask for more.
- `GrantToolkits(child, toolkits)` — add toolkits to a live descendant;
  it sees them from its next turn, conversation intact.
- `RequestToolkits(toolkits, reason)` — ask your parent for tools you
  lack. You'll get them (from your next turn) or a reply saying why not.

When a child's message has `"request": "toolkits"`, decide: grant it
(`GrantToolkits`) if it serves the task and you hold it; if you don't
hold it, `RequestToolkits` from your own parent first; otherwise reply
explaining why not.
"""


# ---------- catalog rendering (LLM-facing) ----------


def describe_catalog(
    catalog: Catalog,
    authority: Toolset,
    *,
    toolkit: str | None = None,
) -> dict[str, Any]:
    """Summarize what the caller may grant. Without ``toolkit``, one
    line per toolkit (description, bundles, counts) — cheap enough to
    read every time. With ``toolkit``, every tool and skill of that
    toolkit the caller holds, with descriptions."""
    if toolkit is not None:
        if toolkit not in authority:
            return {
                "ok": False,
                "code": "toolkit_denied",
                "error": f"you do not hold toolkit {toolkit!r}",
            }
        info = catalog.get(toolkit)
        if info is None:
            return {
                "ok": False,
                "code": "toolkit_denied",
                "error": f"toolkit {toolkit!r} is not installed",
            }
        held = expand(toolkit, authority[toolkit], catalog)
        return {
            "ok": True,
            "toolkit": toolkit,
            "version": info.version,
            "description": info.description,
            "bundles": {
                b: [t for t in ts if t in held.tools]
                for b, ts in info.bundles.items()
            },
            "unavailable_bundles": info.unavailable_bundles,
            "tools": [
                {
                    "name": t.name,
                    "bundles": list(t.bundles),
                    "description": _first_line(t.description),
                }
                for t in sorted(info.tools.values(), key=lambda t: t.name)
                if t.name in held.tools
            ],
            "skills": [
                {
                    "slug": s.slug,
                    "bundle": s.bundle,
                    "description": _first_line(s.description),
                }
                for s in sorted(info.skills.values(), key=lambda s: s.slug)
                if s.slug in held.skills
            ],
        }
    rows = []
    for name in sorted(authority):
        info = catalog.get(name)
        if info is None:
            continue
        held = expand(name, authority[name], catalog)
        rows.append(
            {
                "toolkit": name,
                "description": _first_line(info.description),
                "bundles": sorted(info.bundles),
                "tools_held": len(held.tools),
                "skills_held": len(held.skills),
                "whole": authority[name].is_whole,
            }
        )
    return {"ok": True, "toolkits": rows}


def _first_line(text: str, limit: int = 160) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"


# ---------- default toolbase-backed implementations ----------


def _discover_project_root() -> Path | None:
    # ``tb serve`` walks up from the cwd for a ``.toolbase/``; toolbase's
    # resolvers given ``None`` read only the user layer, so resolve here.
    try:
        from toolbase.envs import find_project_root
    except Exception:
        return None
    try:
        return find_project_root()
    except Exception:
        return None


def toolbase_available() -> bool:
    try:
        import toolbase.serve.orchestrator  # noqa: F401
        import toolbase.serve.loadouts  # noqa: F401
    except Exception:
        return False
    return True


def load_toolbase_catalog(project_root: Path | None = None) -> Catalog:
    """Read every installed toolkit from toolbase's cache.

    Tool names are the served names (``display_name`` or the class name
    minus ``Tool``) — the same names ``tb activate`` and loadouts use.
    """
    from toolbase.naming import mcp_tool_name
    from toolbase.serve.orchestrator import discover_toolkits
    from toolbase.skills import discover_skills, parse_frontmatter

    catalog = Catalog()
    for disc in discover_toolkits():
        if disc.skip_reason is not None:
            continue
        # ``disc.meta`` is the cache's install record (env, counts); the
        # tools, bundles and description live in the toolkit's manifest.
        meta = disc.meta or {}
        manifest = _read_manifest(Path(disc.path))
        info = ToolkitInfo(
            name=disc.name,
            version=str(
                meta.get("version")
                or ("editable" if meta.get("editable") else "")
                or manifest.get("version")
                or ""
            ),
            description=str(manifest.get("description") or ""),
        )
        for entry in manifest.get("tools") or []:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                continue
            display = entry.get("display_name")
            name = mcp_tool_name(entry["name"], display if isinstance(display, str) else None)
            raw_bundle = entry.get("bundle")
            if isinstance(raw_bundle, str) and raw_bundle:
                bundles: tuple[str, ...] = (raw_bundle,)
            elif isinstance(raw_bundle, list):
                bundles = tuple(b for b in raw_bundle if isinstance(b, str) and b)
            else:
                bundles = ()
            info.tools[name] = ToolInfo(
                name=name,
                description=str(entry.get("description") or ""),
                bundles=bundles,
            )
        for src in discover_skills(Path(disc.path)):
            try:
                fm, _ = parse_frontmatter(src.doc.read_text(encoding="utf-8"))
            except OSError:
                fm = None
            info.skills[src.slug] = SkillInfo(
                slug=src.slug,
                description=(fm.description if fm else "") or "",
                bundle=fm.bundle if fm else None,
            )
        info.unavailable_bundles = _unavailable_bundles(disc)
        catalog.toolkits[info.name] = info
    return catalog


def _read_manifest(path: Path) -> dict[str, Any]:
    import yaml

    try:
        data = yaml.safe_load((path / "toolkit.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _unavailable_bundles(disc: Any) -> dict[str, list[str]]:
    # Best-effort: toolbase keeps bundle gating private. A failure here
    # only costs the catalog its "needs config" annotations.
    try:
        from toolbase.serve.orchestrator import _resolve_bundle_availability

        availability, _ = _resolve_bundle_availability(disc)
        return {b: list(miss) for b, miss in availability.dropped_bundles.items()}
    except Exception:
        return {}


def _default_server_factory(
    *,
    toolset: Toolset,
    sandbox: Path | None,
    project_root: Path | None,
    loadout: str | None,
    call_timeout_s: float,
) -> ToolbaseServing:
    """Start toolbase's per-toolkit servers for ``toolset``, in-process.

    The anchor project's loadout supplies version pins and the
    ``serve.yaml`` blocklists; the toolkit selection is replaced by
    ``toolset``. Nothing is written to disk — two children with
    different toolsets never contend over a loadout file. File-aware
    tools are scoped to the agent's sandbox via ``base_directory``.
    """
    import inspect

    from rich.console import Console
    from toolbase.serve.loadouts import (
        NoActiveLoadoutError,
        ResolvedLoadout,
        ToolkitSelection,
        resolve_loadout,
    )
    from toolbase.serve.orchestrator import Orchestrator

    try:
        base = resolve_loadout(project_root, cli_loadout=loadout)
        versions = dict(base.versions)
        disabled_toolkits = list(base.disabled_toolkits)
        disabled_tools = list(base.disabled_tools)
    except NoActiveLoadoutError:
        versions, disabled_toolkits, disabled_tools = {}, [], []

    sel_params = inspect.signature(ToolkitSelection).parameters
    selections = {}
    for kit, grant in toolset.items():
        kwargs: dict[str, Any] = {
            "bundles": grant.bundles,
            "enabled_tools": grant.tools,
        }
        if "enabled_skills" in sel_params:
            kwargs["enabled_skills"] = grant.skills
        selections[kit] = ToolkitSelection(**kwargs)
    resolved = ResolvedLoadout(
        name="conjure",
        source="conjure grant",
        toolkits=selections,
        versions={k: v for k, v in versions.items() if k in selections},
        disabled_toolkits=disabled_toolkits,
        disabled_tools=disabled_tools,
    )
    orch_kwargs: dict[str, Any] = {
        "console": Console(file=io.StringIO()),
        "loadout": resolved,
        "call_timeout_s": call_timeout_s,
    }
    if sandbox is not None and "config_overrides" in inspect.signature(
        Orchestrator
    ).parameters:
        orch_kwargs["config_overrides"] = {"base_directory": str(sandbox)}
    orch = Orchestrator(**orch_kwargs)
    try:
        tools = orch.start()
    except Exception:
        orch.shutdown()
        raise
    return ToolbaseServing(tools, close=orch.shutdown)


def build_hub_from_config(cfg: Any) -> ToolbaseHub | None:
    """``Config.toolbase`` → hub, or ``None`` when toolbase is disabled
    or not importable."""
    if cfg is None or not getattr(cfg, "enabled", True):
        return None
    if not toolbase_available():
        return None
    return ToolbaseHub(
        project_root=cfg.project_root,
        loadout=cfg.loadout,
        authority=cfg.authority,
        call_timeout_s=cfg.call_timeout_s,
    )


def tool_names(tools: Iterable[Any]) -> list[str]:
    out = []
    for t in tools:
        fn = getattr(t, "get_name", None)
        out.append(fn() if callable(fn) else getattr(t, "name", "?"))
    return out
